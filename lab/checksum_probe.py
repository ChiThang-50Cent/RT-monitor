#!/usr/bin/python3
"""Decode a recorded FRAME clip with v4l2slh264dec and hash every decoded frame.

CPU reads go through GStreamer's DMA-BUF map, which brackets them with
DMA_BUF_IOCTL_SYNC, so the hashes reflect what the decoder wrote to memory.
"""
import argparse
import hashlib
import json
import struct
import sys

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo

FRAME = struct.Struct('!QI')

parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()

Gst.init(None)
pipeline = Gst.parse_launch(
    'appsrc name=src format=time ! h264parse ! v4l2slh264dec ! '
    'appsink name=out sync=false max-buffers=4 drop=false')
src = pipeline.get_by_name('src')
src.set_property('caps', Gst.Caps.from_string(
    'video/x-h264,stream-format=byte-stream,alignment=au'))
sink = pipeline.get_by_name('out')
pipeline.set_state(Gst.State.PLAYING)

with open(args.input, 'rb') as clip:
    data = clip.read()
offset = count = 0
while offset < len(data):
    _, size = FRAME.unpack_from(data, offset)
    offset += FRAME.size
    buffer = Gst.Buffer.new_wrapped(data[offset:offset + size])
    buffer.pts = count * Gst.SECOND // 30
    offset += size
    count += 1
    if src.emit('push-buffer', buffer) != Gst.FlowReturn.OK:
        sys.exit('push failed')
src.emit('end-of-stream')

hashes = []
while True:
    sample = sink.emit('try-pull-sample', 10 * Gst.SECOND)
    if sample is None:
        break
    info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    if info.finfo.n_planes != 3:
        sys.exit(f'expected 3-plane 4:2:0 output, got {info.finfo.name}')
    buffer = sample.get_buffer()
    meta = GstVideo.buffer_get_video_meta(buffer)
    layout = meta if meta is not None else info
    ok, mapped = buffer.map(Gst.MapFlags.READ)
    if not ok:
        sys.exit('cannot map decoded frame')
    pixels = mapped.data
    digest = hashlib.sha256()
    for plane in range(3):
        width = info.width if plane == 0 else (info.width + 1) // 2
        height = info.height if plane == 0 else (info.height + 1) // 2
        base, stride = layout.offset[plane], layout.stride[plane]
        for row in range(height):
            start = base + row * stride
            digest.update(pixels[start:start + width])
    buffer.unmap(mapped)
    hashes.append(digest.hexdigest())
pipeline.set_state(Gst.State.NULL)

with open(args.output, 'w') as out:
    json.dump({'input_frames': count, 'decoded_frames': len(hashes), 'sha256': hashes}, out)
print(json.dumps({'input_frames': count, 'decoded_frames': len(hashes),
                  'combined': hashlib.sha256(''.join(hashes).encode()).hexdigest()}))
