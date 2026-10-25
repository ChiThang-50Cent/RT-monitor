#!/usr/bin/python3
"""Persistent framed H.264 receiver using Tegra DMA-BUF and blocking Xv commits."""
import collections
import json
import os
from pathlib import Path
import select
import signal
import sys
import threading
import time

if __name__ == '__main__' and len(sys.argv) > 1:
    if len(sys.argv) != 3 or sys.argv[1] != '--cursor-channel':
        sys.exit('usage: display_receiver.py [--cursor-channel NAME]')
    from surface_cursor import cursor_channel
    sys.exit(cursor_channel(sys.argv[2]))

import gi

gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
from gi.repository import Gio, GLib, Gst, GstVideo

from surface_protocol import WIDTH, HEIGHT, FPS, read_frame
from surface_xv import XvPresenter
from surface_power import AppliancePresentation


# 30 FPS on the 61.78 Hz panel: one frame every 2 vblanks from a small jitter queue.
VBLANK_STEP = 2
READY_LIMIT = 3

def main():
    Gst.init(None)
    stop = threading.Event()
    lock = threading.Lock()
    pending = collections.deque()
    slots = threading.BoundedSemaphore(8)
    errors = collections.deque(maxlen=8)
    gaps = []
    counts = {'fed': 0, 'decoded': 0}
    state = {'input_eof': False, 'feed_done': False, 'last_present': None,
             'progress': time.monotonic(), 'signalled': False, 'revoked': False, 'eos': False}
    batch_started = time.monotonic()
    presenter = pipeline = feeder = pacer = session_bus = None
    lease = None
    ready = collections.deque()
    ready_cv = threading.Condition()
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
                buffer = Gst.Buffer.new_allocate(None, len(payload), None)
                buffer.fill(0, payload)
                buffer.pts = sequence * Gst.SECOND // FPS
                buffer.dts = buffer.pts
                buffer.duration = Gst.SECOND // FPS
                with lock:
                    pending.append(sequence)
                    counts['fed'] += 1
                if src.emit('push-buffer', buffer) != Gst.FlowReturn.OK:
                    raise RuntimeError('appsrc refused access unit')
                expected += 1
        except Exception as error:
            if not stop.is_set():
                fail('input', error)
        finally:
            state['feed_done'] = True

    def allocation(sink, query):
        query.add_allocation_meta(GstVideo.video_meta_api_get_type(), None)
        return True

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

    def show(sequence, decoded):
        # Keeps the old sample alive until this replacement commits synchronously.
        presenter.present(decoded)
        vblank = presenter.wait_vblank()
        now = time.monotonic()
        if state['last_present'] is not None:
            gaps.append((now - state['last_present']) * 1000)
        state['last_present'] = now
        state['progress'] = now
        with lock:
            counts['decoded'] += 1
            decoded_count = counts['decoded']
        emit({'ack': sequence, 'visible': True})
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
                    presenter.wait_vblank(target - 1)
                with ready_cv:
                    if target is None:
                        # Start with one frame of slack against decoder jitter.
                        while (len(ready) < (1 if state['eos'] else 2) and
                               not stop.is_set()):
                            ready_cv.wait(0.2)
                    if stop.is_set():
                        return
                    item = ready.popleft() if ready else None
                if item is None:
                    # Underrun: show the next decoded frame one vblank later.
                    target += 1
                    continue
                target = show(*item) + VBLANK_STEP
        except Exception as error:
            fail('presentation', error)

    def sample(sink):
        try:
            decoded = sink.emit('pull-sample')
            if decoded is None:
                raise RuntimeError('appsink returned no sample')
            caps = decoded.get_caps().get_structure(0)
            if (caps.get_value('width'), caps.get_value('height')) != (WIDTH, HEIGHT):
                raise RuntimeError('expected native visible geometry 1366x768')
            with lock:
                if not pending:
                    raise RuntimeError('decoded frame without input sequence')
                sequence = pending.popleft()
            with ready_cv:
                if len(ready) >= READY_LIMIT:
                    raise RuntimeError(f'paced queue exceeded {READY_LIMIT} frames')
                ready.append((sequence, decoded))
                ready_cv.notify_all()
            return Gst.FlowReturn.OK
        except Exception as error:
            fail('decode', error)
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
        if appliance:
            lease = AppliancePresentation.acquire()
        # Keep a partial instance available for cleanup if initialization fails.
        presenter = XvPresenter.__new__(XvPresenter)
        presenter.__init__()
        pipeline = Gst.parse_launch(
            'appsrc name=src is-live=true format=time block=true max-buffers=4 '
            'max-bytes=16777216 max-time=0 ! h264parse ! v4l2slh264dec ! '
            'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 ! '
            'appsink name=out sync=false emit-signals=true max-buffers=2 drop=false')
        src = pipeline.get_by_name('src')
        src.set_property('caps', Gst.Caps.from_string(
            'video/x-h264,stream-format=byte-stream,alignment=au'))
        out = pipeline.get_by_name('out')
        out.connect('propose-allocation', allocation)
        out.connect('new-sample', sample)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('pipeline refused PLAYING')
        emit({'ready': True, 'width': WIDTH, 'height': HEIGHT,
              'fps': FPS, 'codec': 'h264-cavlc', 'cursor_socket': presenter.cursor_socket})
        state['progress'] = time.monotonic()
        batch_started = state['progress']
        feeder = threading.Thread(target=feed, name='framed-input')
        feeder.start()
        pacer = threading.Thread(target=paced, name='paced-present')
        pacer.start()
        bus = pipeline.get_bus()
        while not stop.is_set():
            if lease is not None and lease.revoked():
                state['revoked'] = True
                stop.set()
                break
            message = bus.timed_pop_filtered(
                Gst.SECOND // 5, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise RuntimeError(f'{error}; {debug}')
                eos = True
                # Every decoded frame is queued by now; let the pacer show them all.
                with ready_cv:
                    state['eos'] = True
                    ready_cv.notify_all()
                    deadline = time.monotonic() + 3
                    while ready and not stop.is_set() and time.monotonic() < deadline:
                        ready_cv.wait(0.05)
                break
            if time.monotonic() - state['progress'] > 15:
                raise TimeoutError('no visible decoder progress for 15 seconds')
    except Exception as error:
        fail('stream', error)
    finally:
        stop.set()
        if pacer is not None:
            with ready_cv:
                ready_cv.notify_all()
            pacer.join(timeout=3)
            if pacer.is_alive():
                fail('cleanup', 'presentation thread did not stop')
                pacer.join()
        ready.clear()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        if feeder is not None:
            feeder.join(timeout=3)
            if feeder.is_alive():
                fail('cleanup', 'input thread did not stop')
        if presenter is not None:
            try:
                presenter.close()
            except Exception as error:
                fail('cleanup', error)
            else:
                if lease is not None:
                    lease.close()
        elif lease is not None:
            lease.close()
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
    drained = (eos and state['input_eof'] and state['feed_done'] and not pending and
               counts['fed'] == counts['decoded'] == presenter.visible_frames)
    success = not errors and (state['signalled'] or state['revoked'] or drained)
    print('receiver stopped ' + json.dumps(dict(counts, eos=eos, success=success)),
          file=sys.stderr, flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
