"""Throwaway Surface-local timing helpers for the diagnostic receiver/probe.

This module imports the deployed production protocol and Xv presenter but never
modifies them.  All durations use one monotonic clock on the Surface; nothing
here subtracts timestamps taken on another machine.

The presenter wrapper keeps the production blocking V1 path: it subclasses
XvPresenter and only wraps the Xlib/Xv calls so their wall time can be summed
for the frame currently being presented.  Decoded pixels are never mapped.
"""
import ctypes
import collections
import threading
import time

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo

from surface_protocol import FPS
from surface_xv import XvPresenter


# Same pipeline as display_receiver.py except the decoder is named so probes
# can be attached.  Buffering, caps and the appsink allocation query are kept
# identical so VideoMeta/DMA-BUF lifetime and format stay on the V1 path.
PIPELINE_DESC = (
    'appsrc name=src is-live=true format=time block=true max-buffers=4 '
    'max-bytes=16777216 max-time=0 ! h264parse ! v4l2slh264dec name=dec ! '
    'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! '
    'appsink name=out sync=false emit-signals=true max-buffers=2 drop=false')

APPSRC_CAPS = 'video/x-h264,stream-format=byte-stream,alignment=au'
UNSET_PTS = int(getattr(Gst, 'CLOCK_TIME_NONE', (1 << 64) - 1))

# Stable trace schema.  Fields are always present; unavailable data is null and
# named in "missing" with a short "note" where the cause is known.
TRACE_TIMING_KEYS = (
    'receive_to_appsrc_ms',
    'appsrc_push_ms',
    'receive_to_decoder_ms',
    'decoder_ms',
    'receive_to_decoded_ms',
    'decoded_to_pull_ms',
    'pull_to_present_ms',
    'decoded_to_present_ms',
    'receive_to_present_ms',
    'present_ms',
    'x_sync_ms',
    'xv_putimage_ms',
    'xv_attribute_ms',
)
PRESENT_ONLY_KEYS = frozenset((
    'pull_to_present_ms',
    'decoded_to_present_ms',
    'receive_to_present_ms',
    'present_ms',
    'x_sync_ms',
    'xv_putimage_ms',
    'xv_attribute_ms',
))


def _ms(delta_ns):
    if delta_ns is None:
        return None
    return round(float(delta_ns) / 1e6, 3)


def make_pipeline():
    pipeline = Gst.parse_launch(PIPELINE_DESC)
    if pipeline is None:
        raise RuntimeError('could not build trace pipeline')
    src = pipeline.get_by_name('src')
    dec = pipeline.get_by_name('dec')
    out = pipeline.get_by_name('out')
    if src is None or dec is None or out is None:
        raise RuntimeError('trace pipeline is missing a named element')
    src.set_property('caps', Gst.Caps.from_string(APPSRC_CAPS))
    out.connect('propose-allocation', appsink_allocation)
    return pipeline, src, dec, out


def appsink_allocation(sink, query):
    # Identical to display_receiver.py: ask the decoder for GstVideoMeta so the
    # V1 presenter can find DMA-BUF planes without mapping pixels.
    query.add_allocation_meta(GstVideo.video_meta_api_get_type(), None)
    return True


def make_au_buffer(sequence, payload):
    buffer = Gst.Buffer.new_allocate(None, len(payload), None)
    buffer.fill(0, payload)
    buffer.pts = sequence * Gst.SECOND // FPS
    buffer.dts = buffer.pts
    buffer.duration = Gst.SECOND // FPS
    return buffer


def attach_decoder_probes(decoder, recorder):
    sink_pad = decoder.get_static_pad('sink')
    src_pad = decoder.get_static_pad('src')
    if sink_pad is None or src_pad is None:
        raise RuntimeError('decoder sink/src pad unavailable for probes')

    def input_probe(pad, info):
        try:
            buffer = info.get_buffer()
            if buffer is not None:
                recorder.decoder_input(buffer.pts, time.monotonic_ns())
        except Exception as error:
            recorder.probe_error('decoder_sink', error)
        return Gst.PadProbeReturn.OK

    def output_probe(pad, info):
        try:
            buffer = info.get_buffer()
            if buffer is not None:
                recorder.decoder_output(buffer.pts, time.monotonic_ns())
        except Exception as error:
            recorder.probe_error('decoder_src', error)
        return Gst.PadProbeReturn.OK

    sink_id = sink_pad.add_probe(Gst.PadProbeType.BUFFER, input_probe)
    src_id = src_pad.add_probe(Gst.PadProbeType.BUFFER, output_probe)
    if not sink_id or not src_id:
        if sink_id:
            sink_pad.remove_probe(sink_id)
        if src_id:
            src_pad.remove_probe(src_id)
        raise RuntimeError('could not attach decoder pad probes')
    return [(sink_pad, sink_id), (src_pad, src_id)]


def remove_probes(probes):
    for pad, probe_id in probes:
        try:
            pad.remove_probe(probe_id)
        except Exception:
            pass
    probes.clear()


class _Sheet:
    __slots__ = ('seq', 'pts', 'recv_ns', 'push_start_ns', 'push_done_ns',
                 'dec_in_ns', 'dec_out_ns')

    def __init__(self, seq, pts, recv_ns):
        self.seq = seq
        self.pts = pts
        self.recv_ns = recv_ns
        self.push_start_ns = None
        self.push_done_ns = None
        self.dec_in_ns = None
        self.dec_out_ns = None


class TraceRecorder:
    """Bounded per-sequence timeline keyed by appsrc PTS.

    The receiver already has an ordered pending deque.  That deque supplies the
    authoritative sequence for each appsink sample; the PTS map supplies the
    decoder probes' timing, and a mismatch withholds decoder timings instead of
    guessing.
    """

    def __init__(self, max_tracked=64):
        self._max_tracked = max(1, int(max_tracked))
        self._lock = threading.Lock()
        self._sheets = collections.OrderedDict()
        self._pts_to_seq = {}
        self._evicted = 0
        self._duplicate_pts = 0
        self._unknown_decoder_input = 0
        self._unknown_decoder_output = 0
        self._probe_errors = collections.deque(maxlen=8)

    def received(self, sequence, pts, recv_ns):
        with self._lock:
            previous = self._sheets.pop(sequence, None)
            if previous is not None and self._pts_to_seq.get(previous.pts) == sequence:
                self._pts_to_seq.pop(previous.pts, None)
            while len(self._sheets) >= self._max_tracked:
                old_seq, old_sheet = self._sheets.popitem(last=False)
                if self._pts_to_seq.get(old_sheet.pts) == old_seq:
                    self._pts_to_seq.pop(old_sheet.pts, None)
                self._evicted += 1
            if pts != UNSET_PTS:
                other = self._pts_to_seq.get(pts)
                if other is not None and other != sequence:
                    self._duplicate_pts += 1
                self._pts_to_seq[pts] = sequence
            self._sheets[sequence] = _Sheet(sequence, pts, recv_ns)

    def push_started(self, sequence, now_ns):
        with self._lock:
            sheet = self._sheets.get(sequence)
            if sheet is not None and sheet.push_start_ns is None:
                sheet.push_start_ns = now_ns

    def push_finished(self, sequence, now_ns):
        with self._lock:
            sheet = self._sheets.get(sequence)
            if sheet is not None and sheet.push_done_ns is None:
                sheet.push_done_ns = now_ns

    def decoder_input(self, pts, now_ns):
        with self._lock:
            sequence = self._pts_to_seq.get(pts)
            if sequence is None:
                self._unknown_decoder_input += 1
                return
            sheet = self._sheets.get(sequence)
            if sheet is None:
                self._unknown_decoder_input += 1
                return
            if sheet.dec_in_ns is None:
                sheet.dec_in_ns = now_ns

    def decoder_output(self, pts, now_ns):
        with self._lock:
            sequence = self._pts_to_seq.get(pts)
            if sequence is None:
                self._unknown_decoder_output += 1
                return
            sheet = self._sheets.get(sequence)
            if sheet is None:
                self._unknown_decoder_output += 1
                return
            if sheet.dec_out_ns is None:
                sheet.dec_out_ns = now_ns

    def probe_error(self, where, error):
        with self._lock:
            self._probe_errors.append('%s: %s' % (where, error))

    def finish(self, sequence, sample_pts, pull_ns, present_start_ns,
               present_end_ns, lib_timings, controlled_delay_ms, first_frame,
               presented):
        with self._lock:
            sheet = self._sheets.pop(sequence, None)
            if sheet is not None and self._pts_to_seq.get(sheet.pts) == sequence:
                self._pts_to_seq.pop(sheet.pts, None)

        lib_timings = lib_timings or {}
        trace = {
            'seq': int(sequence),
            'pts_ns': (None if sample_pts is None or sample_pts == UNSET_PTS
                       else int(sample_pts)),
            'pts_match': False,
            'presentation': 'xv_visible' if presented else 'decode_only',
            'controlled_delay_ms': round(float(controlled_delay_ms or 0.0), 3),
            'first_frame': bool(first_frame) if presented else None,
            'x_sync_calls': 0,
            'xv_putimage_calls': 0,
            'xv_attribute_calls': 0,
        }
        for key in TRACE_TIMING_KEYS:
            trace[key] = None

        notes = []
        if sheet is None:
            notes.append('no receive record for sequence')
        else:
            if (trace['pts_ns'] is not None and sheet.pts != UNSET_PTS and
                    sample_pts == sheet.pts):
                trace['pts_match'] = True
            else:
                notes.append('sample PTS differs from queued sequence; decoder timings withheld')

            recv_ns = sheet.recv_ns
            if recv_ns is not None:
                if sheet.push_done_ns is not None:
                    trace['receive_to_appsrc_ms'] = _ms(sheet.push_done_ns - recv_ns)
                if (sheet.push_start_ns is not None and
                        sheet.push_done_ns is not None):
                    trace['appsrc_push_ms'] = _ms(
                        sheet.push_done_ns - sheet.push_start_ns)
                if presented and present_end_ns is not None:
                    trace['receive_to_present_ms'] = _ms(
                        present_end_ns - recv_ns)

            if trace['pts_match']:
                if recv_ns is not None and sheet.dec_in_ns is not None:
                    trace['receive_to_decoder_ms'] = _ms(
                        sheet.dec_in_ns - recv_ns)
                if sheet.dec_in_ns is not None and sheet.dec_out_ns is not None:
                    trace['decoder_ms'] = _ms(
                        sheet.dec_out_ns - sheet.dec_in_ns)
                if sheet.dec_out_ns is not None:
                    if recv_ns is not None:
                        trace['receive_to_decoded_ms'] = _ms(
                            sheet.dec_out_ns - recv_ns)
                    if pull_ns is not None:
                        trace['decoded_to_pull_ms'] = _ms(
                            pull_ns - sheet.dec_out_ns)
                    if presented and present_end_ns is not None:
                        trace['decoded_to_present_ms'] = _ms(
                            present_end_ns - sheet.dec_out_ns)
                else:
                    notes.append('decoder output probe saw no buffer at this PTS')
                if sheet.dec_in_ns is None:
                    notes.append('decoder input probe saw no buffer at this PTS')

        if presented:
            if pull_ns is not None and present_end_ns is not None:
                trace['pull_to_present_ms'] = _ms(present_end_ns - pull_ns)
            if present_start_ns is not None and present_end_ns is not None:
                trace['present_ms'] = _ms(present_end_ns - present_start_ns)
            trace['x_sync_ms'] = _ms(lib_timings.get('x_sync_ns'))
            trace['xv_putimage_ms'] = _ms(lib_timings.get('xv_putimage_ns'))
            trace['xv_attribute_ms'] = _ms(lib_timings.get('xv_attribute_ns'))
            trace['x_sync_calls'] = int(lib_timings.get('x_sync_calls', 0))
            trace['xv_putimage_calls'] = int(
                lib_timings.get('xv_putimage_calls', 0))
            trace['xv_attribute_calls'] = int(
                lib_timings.get('xv_attribute_calls', 0))

        applicable = TRACE_TIMING_KEYS
        if not presented:
            applicable = tuple(k for k in TRACE_TIMING_KEYS
                               if k not in PRESENT_ONLY_KEYS)
        trace['missing'] = [key for key in applicable
                            if trace.get(key) is None]
        if notes:
            trace['note'] = '; '.join(notes)
        return trace

    def snapshot(self):
        with self._lock:
            return {
                'tracked': len(self._sheets),
                'max_tracked': self._max_tracked,
                'evicted': self._evicted,
                'duplicate_pts': self._duplicate_pts,
                'unknown_decoder_input': self._unknown_decoder_input,
                'unknown_decoder_output': self._unknown_decoder_output,
                'probe_errors': list(self._probe_errors),
            }


class _TimedLib:
    """Forward a CDLL while timing selected calls into the owner's frame stats."""

    def __init__(self, lib, owner, timed_names):
        self._lib = lib
        self._owner = owner
        self._timed_names = timed_names

    def __getattr__(self, name):
        value = getattr(self._lib, name)
        key = self._timed_names.get(name)
        if key is None:
            return value
        owner = self._owner

        def timed(*args, **kwargs):
            stats = owner._active_stats
            if stats is None:
                return value(*args, **kwargs)
            started_ns = time.monotonic_ns()
            try:
                return value(*args, **kwargs)
            finally:
                elapsed_ns = time.monotonic_ns() - started_ns
                ns_key = key + '_ns'
                calls_key = key + '_calls'
                stats[ns_key] = stats.get(ns_key, 0) + elapsed_ns
                stats[calls_key] = stats.get(calls_key, 0) + 1

        return timed


class TracedXvPresenter(XvPresenter):
    """Actual production XvPresenter with per-present call timings."""

    def __init__(self):
        super().__init__()
        self._active_stats = None
        self.last_lib_timings = None
        self._timed_x = _TimedLib(self.x, self, {'XSync': 'x_sync'})
        self._timed_xv = _TimedLib(self.xv, self, {
            'XvPutImage': 'xv_putimage',
            'XvGetPortAttribute': 'xv_attribute',
        })
        self.x = self._timed_x
        self.xv = self._timed_xv

    def present(self, sample):
        stats = {}
        self._active_stats = stats
        self.last_lib_timings = None
        started_ns = time.monotonic_ns()
        try:
            return super().present(sample)
        finally:
            stats['present_ns'] = time.monotonic_ns() - started_ns
            self._active_stats = None
            self.last_lib_timings = stats


def controlled_delay(stop, delay_s):
    """Optional explicit experiment: sleep before the blocking V1 present."""
    if delay_s <= 0:
        return 0.0
    started_ns = time.monotonic_ns()
    deadline_ns = started_ns + int(delay_s * 1e9)
    while True:
        remaining_ns = deadline_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            break
        if stop.wait(min(remaining_ns / 1e9, 0.05)):
            raise InterruptedError('controlled presentation delay cancelled')
    return (time.monotonic_ns() - started_ns) / 1e6


def wait_until_ns(deadline_ns, stop):
    while True:
        remaining_ns = deadline_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            return
        if stop.wait(min(remaining_ns / 1e9, 0.05)):
            raise InterruptedError('replay pacing cancelled')


class _VBlankRequest(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint), ('sequence', ctypes.c_uint),
                ('signal', ctypes.c_ulong)]


class _VBlankReply(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint), ('sequence', ctypes.c_uint),
                ('seconds', ctypes.c_long), ('microseconds', ctypes.c_long)]


class _VBlank(ctypes.Union):
    _fields_ = [('request', _VBlankRequest), ('reply', _VBlankReply)]


# Active LVDS output is CRTC 1 (xrandr); measured 61.78 Hz on this panel.
_VBLANK_SECONDARY = 0x20000000
_libdrm = ctypes.CDLL('libdrm.so.2', use_errno=True)
_libdrm.drmWaitVBlank.argtypes = [ctypes.c_int, ctypes.POINTER(_VBlank)]
_libdrm.drmWaitVBlank.restype = ctypes.c_int


def wait_vblank(fd, target=None):
    """Return current vblank count, or block until absolute count `target`."""
    event = _VBlank()
    if target is None:
        event.request.type = 1 | _VBLANK_SECONDARY  # relative 0: query only
        event.request.sequence = 0
    else:
        event.request.type = 0 | _VBLANK_SECONDARY  # absolute
        event.request.sequence = target & 0xFFFFFFFF
    if _libdrm.drmWaitVBlank(fd, ctypes.byref(event)):
        raise OSError(ctypes.get_errno(), 'drmWaitVBlank')
    return event.reply.sequence
