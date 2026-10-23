#!/usr/bin/python3
"""Wire-compatible diagnostic receiver with Surface-local V1 stage timings.

This is a throwaway lab copy of display_receiver.py.  It keeps the deployed
blocking Xv path, readiness/ACK validation, buffer lifetime, XFCE inhibition and
native 1366x768 geometry.  The only wire addition is an optional "trace" object
inside each existing visible ACK; the host sender ignores unknown ACK keys.

Run with the production directory on PYTHONPATH so surface_protocol.py and
surface_xv.py are imported, not copied.
"""
import argparse
import collections
import json
import os
from pathlib import Path
import select
import signal
import sys
import threading
import time

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gio, GLib, Gst, GstVideo

# Deployed production modules live one level above the staging directory.
try:
    from surface_protocol import WIDTH, HEIGHT, FPS, read_frame
except ModuleNotFoundError as error:
    if error.name != 'surface_protocol':
        raise
    sys.path.append('/home/user')
    from surface_protocol import WIDTH, HEIGHT, FPS, read_frame

from trace_core import (TraceRecorder, TracedXvPresenter, attach_decoder_probes,
                        make_au_buffer, make_pipeline,
                        remove_probes, wait_vblank)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vblank-step', type=int, choices=(0, 2, 3), default=0,
                        help='0 = present immediately (production); N = one frame '
                             'every N vblanks from a one-frame jitter queue')
    args = parser.parse_args(argv)
    ready = collections.deque()
    ready_cv = threading.Condition()
    present_thread = None

    Gst.init(None)
    stop = threading.Event()
    lock = threading.Lock()
    pending = collections.deque()
    slots = threading.BoundedSemaphore(8)
    errors = collections.deque(maxlen=8)
    gaps = []
    counts = {'fed': 0, 'decoded': 0, 'trace_missing': 0}
    state = {'input_eof': False, 'feed_done': False, 'last_present': None,
             'progress': time.monotonic(), 'signalled': False}
    batch_started = time.monotonic()
    presenter = pipeline = feeder = session_bus = None
    recorder = None
    probes = []
    inhibitors = []
    eos = False
    old_signals = {}

    def emit(value):
        print(json.dumps(value, separators=(',', ':')), flush=True)

    def fail(where, error):
        with lock:
            errors.append(f'{where}: {error}')
        print(f'receiver {where}: {error}', file=sys.stderr, flush=True)
        stop.set()

    def interrupt(signum, frame):
        state['signalled'] = True
        stop.set()

    def exact_read(size, clean_eof=False):
        result = bytearray()
        deadline = time.monotonic() + 15
        while len(result) < size:
            if stop.is_set():
                raise InterruptedError('input cancelled')
            if time.monotonic() >= deadline:
                raise TimeoutError('framed input stalled for 15 seconds')
            if not select.select([0], [], [], 0.2)[0]:
                continue
            chunk = os.read(0, size - len(result))
            if not chunk:
                if clean_eof and not result:
                    return None
                raise EOFError(f'truncated framed input ({len(result)}/{size} bytes)')
            result.extend(chunk)
            deadline = time.monotonic() + 15
        return result

    def feed():
        expected = 0
        try:
            while not stop.is_set():
                while not slots.acquire(timeout=0.2):
                    if stop.is_set():
                        return
                item = read_frame(exact_read, expected)
                if item is None:
                    slots.release()
                    state['input_eof'] = True
                    if src.emit('end-of-stream') != Gst.FlowReturn.OK:
                        raise RuntimeError('appsrc refused EOS')
                    break
                sequence, payload = item
                received_ns = time.monotonic_ns()
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
            if not stop.is_set():
                fail('input', error)
        finally:
            state['feed_done'] = True

    def log_batch(now):
        nonlocal batch_started
        ordered = sorted(gaps)
        def percentile(fraction):
            return ordered[round((len(ordered) - 1) * fraction)] if ordered else None
        print('receiver batch ' + json.dumps({
            'frames': 100, 'present_fps': 100 / (now - batch_started),
            'gap_ms': {'p50': percentile(.5), 'p95': percentile(.95),
                       'p99': percentile(.99), 'max': max(gaps) if gaps else None},
            'geometry': presenter.geometry, 'visible_count': presenter.visible_frames,
        }, separators=(',', ':')), file=sys.stderr, flush=True)
        gaps.clear()
        batch_started = now

    def show(sequence, decoded, pull_ns):
        first_frame = presenter.geometry is None
        visible_before = presenter.visible_frames
        present_start_ns = time.monotonic_ns()
        # Keeps the old sample alive until this replacement commits synchronously.
        presenter.present(decoded)
        present_end_ns = time.monotonic_ns()
        vblank = wait_vblank(presenter.gpu)
        now = time.monotonic()
        if presenter.visible_frames != visible_before + 1:
            raise RuntimeError('presenter did not confirm a new visible frame')
        lib_timings = dict(presenter.last_lib_timings or {})
        trace = recorder.finish(
            sequence, decoded.get_buffer().pts, pull_ns, present_start_ns,
            present_end_ns, lib_timings, 0.0, first_frame, True)
        trace['vblank'] = vblank
        trace['queued'] = len(ready)
        if state['last_present'] is not None:
            gaps.append((now - state['last_present']) * 1000)
        state['last_present'] = now
        state['progress'] = now
        with lock:
            counts['decoded'] += 1
            decoded_count = counts['decoded']
            if trace['missing']:
                counts['trace_missing'] += 1
        emit({'ack': sequence, 'visible': True, 'trace': trace})
        slots.release()
        if decoded_count % 100 == 0:
            log_batch(now)
        return vblank

    def paced():
        # Submit after vblank target-1 so the blocking commit latches at target.
        try:
            target = None
            while not stop.is_set():
                if target is not None:
                    wait_vblank(presenter.gpu, target - 1)
                with ready_cv:
                    if target is None:
                        # Prefill one frame of slack before the first slot.
                        while len(ready) < 2 and not stop.is_set():
                            ready_cv.wait(0.2)
                    if stop.is_set():
                        return
                    item = ready.popleft() if ready else None
                if item is None:
                    # Underrun: no decoded frame for this slot; try next vblank.
                    target += 1
                    continue
                target = show(*item) + args.vblank_step
        except Exception as error:
            if not stop.is_set():
                fail('paced presentation', error)

    def sample(sink):
        try:
            decoded = sink.emit('pull-sample')
            pull_ns = time.monotonic_ns()
            if decoded is None:
                raise RuntimeError('appsink returned no sample')
            caps = decoded.get_caps().get_structure(0)
            if (caps.get_value('width'), caps.get_value('height')) != (WIDTH, HEIGHT):
                raise RuntimeError('expected native visible geometry 1366x768')
            with lock:
                if not pending:
                    raise RuntimeError('decoded frame without input sequence')
                sequence = pending.popleft()
            if args.vblank_step:
                with ready_cv:
                    if len(ready) >= 3:
                        raise RuntimeError('paced queue exceeded 3 frames')
                    ready.append((sequence, decoded, pull_ns))
                    ready_cv.notify_all()
            else:
                show(sequence, decoded, pull_ns)
            return Gst.FlowReturn.OK
        except Exception as error:
            fail('presentation', error)
            return Gst.FlowReturn.ERROR

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_signals[sig] = signal.signal(sig, interrupt)
        appliance = (Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/10000')) /
                     'surface-appliance-active').exists()
        if not appliance:
            session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for destination, path, interface in (
                ('org.xfce.ScreenSaver', '/org/xfce/ScreenSaver', 'org.xfce.ScreenSaver'),
                ('org.freedesktop.PowerManagement', '/org/freedesktop/PowerManagement/Inhibit',
                 'org.freedesktop.PowerManagement.Inhibit'),
            ):
                cookie, = session_bus.call_sync(
                    destination, path, interface, 'Inhibit',
                    GLib.Variant('(ss)', ('Surface PC Display', 'External monitor active')),
                    GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 5000, None,
                ).unpack()
                inhibitors.append((destination, path, interface, cookie))
            # Wake an automatic blanker normally; a real lock still requires authentication.
            session_bus.call_sync(
                'org.xfce.ScreenSaver', '/org/xfce/ScreenSaver', 'org.xfce.ScreenSaver',
                'SimulateUserActivity', None, None, Gio.DBusCallFlags.NONE, 5000, None)
        # Keep a partial instance available for cleanup if initialization fails.
        presenter = TracedXvPresenter.__new__(TracedXvPresenter)
        presenter.__init__()
        recorder = TraceRecorder(max_tracked=64)
        pipeline, src, dec, out = make_pipeline()
        probes = attach_decoder_probes(dec, recorder)
        out.connect('new-sample', sample)
        print('receiver trace ' + json.dumps({
            'decoder': 'dec', 'probes': True, 'clock': 'monotonic_ns',
            'vblank_step': args.vblank_step,
        }, separators=(',', ':')), file=sys.stderr, flush=True)
        if args.vblank_step:
            present_thread = threading.Thread(target=paced, name='paced-present')
            present_thread.start()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('pipeline refused PLAYING')
        emit({'ready': True, 'width': WIDTH, 'height': HEIGHT,
              'fps': FPS, 'codec': 'h264-cavlc'})
        state['progress'] = time.monotonic()
        batch_started = state['progress']
        feeder = threading.Thread(target=feed, name='framed-input')
        feeder.start()
        bus = pipeline.get_bus()
        while not stop.is_set():
            message = bus.timed_pop_filtered(
                Gst.SECOND // 5, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise RuntimeError(f'{error}; {debug}')
                eos = True
                break
            if time.monotonic() - state['progress'] > 15:
                raise TimeoutError('no visible decoder progress for 15 seconds')
    except Exception as error:
        fail('stream', error)
    finally:
        stop.set()
        if present_thread is not None:
            with ready_cv:
                ready_cv.notify_all()
            present_thread.join(timeout=3)
            if present_thread.is_alive():
                fail('cleanup', 'paced presenter did not stop')
        ready.clear()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        remove_probes(probes)
        if feeder is not None:
            feeder.join(timeout=3)
            if feeder.is_alive():
                fail('cleanup', 'input thread did not stop')
        if presenter is not None:
            try:
                presenter.close()
            except Exception as error:
                fail('cleanup', error)
        if inhibitors:
            try:
                active, = session_bus.call_sync(
                    'org.xfce.ScreenSaver', '/org/xfce/ScreenSaver', 'org.xfce.ScreenSaver',
                    'GetActive', None, GLib.VariantType.new('(b)'),
                    Gio.DBusCallFlags.NONE, 5000, None).unpack()
                if not active:
                    # Do not release inhibition with an already-expired idle timer.
                    session_bus.call_sync(
                        'org.xfce.ScreenSaver', '/org/xfce/ScreenSaver', 'org.xfce.ScreenSaver',
                        'SimulateUserActivity', None, None, Gio.DBusCallFlags.NONE, 5000, None)
            except Exception as error:
                fail('idle timer reset', error)
        for destination, path, interface, cookie in reversed(inhibitors):
            try:
                session_bus.call_sync(
                    destination, path, interface, 'UnInhibit',
                    GLib.Variant('(u)', (cookie,)), None, Gio.DBusCallFlags.NONE, 5000, None)
            except Exception as error:
                fail('inhibitor cleanup', error)
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
        if recorder is not None:
            try:
                print('receiver trace stats ' + json.dumps(
                    recorder.snapshot(), separators=(',', ':')),
                    file=sys.stderr, flush=True)
            except Exception:
                pass
    visible_frames = getattr(presenter, 'visible_frames', 0)
    drained = (eos and state['input_eof'] and state['feed_done'] and not pending and
               counts['fed'] == counts['decoded'] == visible_frames)
    success = not errors and (state['signalled'] or drained)
    print('receiver stopped ' + json.dumps(dict(counts, eos=eos, success=success)),
          file=sys.stderr, flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
