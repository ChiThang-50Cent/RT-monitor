#!/usr/bin/python3
"""Finite replay probe for a surface_protocol.FRAME-recorded H.264 clip.

The same pipeline, caps, appsink allocation query and native geometry check as
the receiver are used for both modes.  "decode" is decoded-only and never
creates a window or claims visibility.  "xv" runs the production blocking
XvPresenter through TracedXvPresenter and labels a frame visible only after the
overlay visibility attribute was validated and visible_frames advanced.

Results are NDJSON frame records plus one final JSON summary.  Stage durations
are Surface-local monotonic milliseconds; no CPU time is measured here.
"""
import argparse
import collections
import json
import os
import signal
import sys
import threading
import time

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gst, GstVideo

try:
    from surface_protocol import FPS, WIDTH, HEIGHT, read_frame
except ModuleNotFoundError as error:
    if error.name != 'surface_protocol':
        raise
    sys.path.append('/home/user')
    from surface_protocol import FPS, WIDTH, HEIGHT, read_frame

from surface_xv import GstAllocators
from trace_core import (TRACE_TIMING_KEYS, TraceRecorder, TracedXvPresenter,
                        attach_decoder_probes, make_au_buffer, make_pipeline,
                        remove_probes, wait_until_ns)

MAX_STAT_SAMPLES = 20000


def _percentile(ordered, fraction):
    if not ordered:
        return None
    index = round((len(ordered) - 1) * fraction)
    index = max(0, min(len(ordered) - 1, int(index)))
    return ordered[index]


def _new_stats():
    return {key: {'n': 0, 'samples': collections.deque(maxlen=MAX_STAT_SAMPLES)}
            for key in TRACE_TIMING_KEYS}


def _add_stats(stats, trace):
    for key in TRACE_TIMING_KEYS:
        value = trace.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            entry = stats[key]
            entry['n'] += 1
            entry['samples'].append(float(value))


def _summarize(stats):
    result = {}
    for key in TRACE_TIMING_KEYS:
        entry = stats[key]
        ordered = sorted(entry['samples'])
        result[key] = {
            'n': entry['n'],
            'retained': len(ordered),
            'p50': _percentile(ordered, 0.50),
            'p95': _percentile(ordered, 0.95),
            'max': ordered[-1] if ordered else None,
        }
    return result


def _run(args, signal_stop, emit_frame):
    Gst.init(None)
    run_stop = threading.Event()
    lock = threading.Lock()
    pending = collections.deque()
    slots = threading.BoundedSemaphore(8)
    errors = collections.deque(maxlen=8)
    counts = {'fed': 0, 'decoded': 0, 'trace_missing': 0}
    state = {'input_eof': False, 'feed_done': False,
             'progress': time.monotonic()}
    stats = _new_stats()
    started = time.monotonic()
    input_bytes = 0
    eos = False
    presenter = pipeline = feeder = None
    probes = []
    recorder = TraceRecorder(max_tracked=64)
    fd = None

    def halted():
        return signal_stop.is_set() or run_stop.is_set()

    def fail(where, error):
        with lock:
            errors.append(f'{where}: {error}')
        print(f'replay {where}: {error}', file=sys.stderr, flush=True)
        run_stop.set()

    def file_read(size, clean_eof=False):
        result = bytearray()
        while len(result) < size:
            chunk = os.read(fd, size - len(result))
            if not chunk:
                if clean_eof and not result:
                    return None
                raise EOFError(
                    f'truncated framed input ({len(result)}/{size} bytes)')
            result.extend(chunk)
        return result

    def feed():
        nonlocal input_bytes
        expected = 0
        frame_ns = Gst.SECOND // args.pace if args.pace else 0
        next_ns = None
        try:
            while not halted():
                while not slots.acquire(timeout=0.2):
                    if halted():
                        return
                item = read_frame(file_read, expected)
                if item is None:
                    slots.release()
                    state['input_eof'] = True
                    if src.emit('end-of-stream') != Gst.FlowReturn.OK:
                        raise RuntimeError('appsrc refused EOS')
                    break
                sequence, payload = item
                if args.pace:
                    now_ns = time.monotonic_ns()
                    if next_ns is None or next_ns < now_ns:
                        next_ns = now_ns
                    wait_until_ns(next_ns, run_stop)
                    if signal_stop.is_set():
                        raise InterruptedError('replay stopped')
                    next_ns += frame_ns
                received_ns = time.monotonic_ns()
                input_bytes += len(payload)
                buffer = make_au_buffer(sequence, payload)
                recorder.received(sequence, buffer.pts, received_ns)
                with lock:
                    pending.append(sequence)
                    counts['fed'] += 1
                recorder.push_started(sequence, time.monotonic_ns())
                try:
                    pushed = src.emit('push-buffer', buffer)
                finally:
                    recorder.push_finished(sequence, time.monotonic_ns())
                if pushed != Gst.FlowReturn.OK:
                    raise RuntimeError('appsrc refused access unit')
                expected += 1
        except Exception as error:
            if not halted():
                fail('input', error)
        finally:
            state['feed_done'] = True

    def sample(sink):
        try:
            decoded = sink.emit('pull-sample')
            pull_ns = time.monotonic_ns()
            if decoded is None:
                raise RuntimeError('appsink returned no sample')
            caps = decoded.get_caps().get_structure(0)
            if (caps.get_value('width'), caps.get_value('height')) != (WIDTH, HEIGHT):
                raise RuntimeError('expected native visible geometry 1366x768')
            buffer = decoded.get_buffer()
            meta = GstVideo.buffer_get_video_meta(buffer)
            if meta is None or meta.n_planes != 3 or not buffer.n_memory():
                raise RuntimeError('expected three-plane decoder VideoMeta')
            if not all(GstAllocators.is_dmabuf_memory(buffer.peek_memory(i))
                       for i in range(buffer.n_memory())):
                raise RuntimeError('decoder output is not entirely DMA-BUF')
            with lock:
                if not pending:
                    raise RuntimeError('decoded frame without input sequence')
                sequence = pending.popleft()
            visible = False
            first_frame = None
            present_start_ns = present_end_ns = None
            lib_timings = {}
            if args.mode == 'xv':
                first_frame = presenter.geometry is None
                visible_before = presenter.visible_frames
                present_start_ns = time.monotonic_ns()
                presenter.present(decoded)
                present_end_ns = time.monotonic_ns()
                if presenter.visible_frames != visible_before + 1:
                    raise RuntimeError('presenter did not confirm a new visible frame')
                lib_timings = dict(presenter.last_lib_timings or {})
                visible = True
            trace = recorder.finish(
                sequence, buffer.pts, pull_ns, present_start_ns, present_end_ns,
                lib_timings, 0.0, first_frame, args.mode == 'xv')
            with lock:
                counts['decoded'] += 1
                if trace['missing']:
                    counts['trace_missing'] += 1
            state['progress'] = time.monotonic()
            _add_stats(stats, trace)
            emit_frame({
                'type': 'frame',
                'mode': args.mode,
                'seq': sequence,
                'visible': visible,
                'video_meta_planes': int(meta.n_planes) if meta is not None else None,
                'dmabuf_verified': True,
                'backing_geometry': [int(meta.width), int(meta.height)],
                'strides': [int(meta.stride[i]) for i in range(3)],
                'trace': trace,
            })
            slots.release()
            return Gst.FlowReturn.OK
        except Exception as error:
            fail('presentation' if args.mode == 'xv' else 'decode', error)
            return Gst.FlowReturn.ERROR

    try:
        fd = os.open(args.input, os.O_RDONLY)
        if args.mode == 'xv':
            presenter = TracedXvPresenter.__new__(TracedXvPresenter)
            presenter.__init__()
        pipeline, src, dec, out = make_pipeline()
        probes = attach_decoder_probes(dec, recorder)
        out.connect('new-sample', sample)
        state['progress'] = time.monotonic()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('pipeline refused PLAYING')
        feeder = threading.Thread(target=feed, name='replay-input')
        feeder.start()
        bus = pipeline.get_bus()
        while not halted():
            message = bus.timed_pop_filtered(
                Gst.SECOND // 5, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise RuntimeError(f'{error}; {debug}')
                eos = True
                break
            if time.monotonic() - state['progress'] > 15:
                raise TimeoutError('no decoded progress for 15 seconds')
    except Exception as error:
        fail('replay', error)
    finally:
        run_stop.set()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if feeder is not None:
            feeder.join(timeout=3)
            if feeder.is_alive():
                fail('cleanup', 'input thread did not stop')
        remove_probes(probes)
        if presenter is not None:
            try:
                presenter.close()
            except Exception as error:
                fail('cleanup', error)
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass

    with lock:
        pending_left = len(pending)
        fed = counts['fed']
        decoded = counts['decoded']
        trace_missing = counts['trace_missing']
    visible_frames = getattr(presenter, 'visible_frames', 0)
    drained = (eos and state['input_eof'] and state['feed_done'] and
               pending_left == 0 and fed > 0 and fed == decoded and
               (args.mode != 'xv' or visible_frames == decoded))
    success = not errors and drained
    return {
        'mode': args.mode,
        'classification': 'xv_visible' if args.mode == 'xv' else 'decoded_only',
        'input': args.input,
        'pace': args.pace,
        'success': success,
        'eos': eos,
        'drained': drained,
        'frames_fed': fed,
        'frames_decoded': decoded,
        'frames_visible': visible_frames if args.mode == 'xv' else 0,
        'input_bytes': input_bytes,
        'wall_s': round(time.monotonic() - started, 3),
        'trace_missing_frames': trace_missing,
        'timing_unit': 'ms',
        'timing_sample_cap': MAX_STAT_SAMPLES,
        'timing_ms': _summarize(stats),
        'recorder': recorder.snapshot(),
        'errors': list(errors),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Finite native H.264 framed-AU replay probe.  decode is '
                    'decoded-only; xv uses the production visible presenter.')
    parser.add_argument('--mode', choices=('decode', 'xv'), required=True)
    parser.add_argument('--input', required=True,
                        help='surface_protocol.FRAME file starting at sequence 0')
    parser.add_argument('--pace', type=int, choices=(0, FPS), default=FPS,
                        help='%s = native %s fps pacing; 0 = flat out' % (FPS, FPS))
    parser.add_argument('--trace-out', default=None,
                        help='write per-frame NDJSON here instead of stdout')
    args = parser.parse_args(argv)
    if not os.path.isfile(args.input):
        parser.error('--input must be a regular file')
    if args.mode == 'xv' and not os.environ.get('DISPLAY'):
        parser.error('--mode xv requires DISPLAY (Surface X11 :0)')

    signal_stop = threading.Event()
    old_signals = {}
    trace_file = None

    def interrupt(signum, frame):
        signal_stop.set()

    def emit_frame(record):
        line = json.dumps(record, separators=(',', ':'))
        if trace_file is not None:
            print(line, file=trace_file, flush=True)
        else:
            print(line, flush=True)

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_signals[sig] = signal.signal(sig, interrupt)
        if args.trace_out:
            trace_file = open(args.trace_out, 'w', encoding='utf-8')
        print(f'replay start pid={os.getpid()} mode={args.mode} '
              f'pace={args.pace} input={args.input}', file=sys.stderr, flush=True)
        result = _run(args, signal_stop, emit_frame)
        summary = {
            'type': 'summary',
            'pid': os.getpid(),
            'cpu_measured': False,
            'mode': args.mode,
            'classification': result['classification'],
            'success': result['success'],
            'result': result,
        }
        print(json.dumps(summary, separators=(',', ':')), flush=True)
        return 0 if result['success'] else 1
    except Exception as error:
        print(json.dumps({
            'type': 'summary',
            'pid': os.getpid(),
            'cpu_measured': False,
            'mode': args.mode,
            'success': False,
            'error': str(error),
        }, separators=(',', ':')), flush=True)
        print(f'replay failed: {error}', file=sys.stderr, flush=True)
        return 2
    finally:
        if trace_file is not None:
            trace_file.close()
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    sys.exit(main())
