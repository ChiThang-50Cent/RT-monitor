#!/usr/bin/python3
"""Keep a native Mutter virtual monitor alive and stream accelerated H264 over SSH."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import threading
import time

# A stale per-user registry can hide the installed NVENC element. Process-local only.
os.environ["GST_REGISTRY"] = "/dev/null"
os.environ["GST_REGISTRY_UPDATE"] = "no"
import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst
from surface_protocol import FRAME, MAX_AU, WIDTH, HEIGHT, FPS, encode_cursor
from surface_cursor_capture import CursorCapture

DEST = "org.gnome.Mutter.ScreenCast"
ROOT = "/org/gnome/Mutter/ScreenCast"
TIMEOUT = 10
# The receiver holds one decoded frame of slack before vblank-paced presentation.
CREDITS = 3


def log(text):
    print(text, file=sys.stderr, flush=True)


class CursorSender:
    def __init__(self, connection, capture, name):
        self.connection = connection
        self.capture = capture
        command = connection.command
        self.command = (command[:-2] +
                        ["-o", "ControlMaster=no", "-o", "ControlPath=none",
                         command[-2], command[-1] + " --cursor-channel " + name])
        self.process = None
        self.thread = None

    def start(self):
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.thread = threading.Thread(target=self.run, name="cursor-send")
        self.thread.start()

    def run(self):
        revision = -1
        previous_shape = None
        try:
            while not self.connection.done.is_set() and not self.connection.stopping.is_set():
                self.connection.check()
                if self.process.poll() is not None:
                    raise OSError("cursor SSH exited")
                current, state = self.capture.snapshot(revision)
                if current == revision:
                    continue
                header, pixels = encode_cursor(state, previous_shape)
                deadline = time.monotonic() + TIMEOUT
                for data in (header, pixels):
                    view = memoryview(data)
                    while view:
                        self.connection.check()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("cursor write timed out")
                        if not select.select([], [self.process.stdin], [], min(remaining, 0.2))[1]:
                            continue
                        try:
                            count = os.write(self.process.stdin.fileno(), view)
                        except BlockingIOError:
                            continue
                        if count <= 0:
                            raise OSError("cursor input closed")
                        view = view[count:]
                reply = bytearray()
                while len(reply) < 3:
                    self.connection.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("cursor ACK timed out")
                    if not select.select([self.process.stdout], [], [], min(remaining, 0.2))[0]:
                        continue
                    chunk = os.read(self.process.stdout.fileno(), 4 - len(reply))
                    if not chunk:
                        raise OSError("cursor output closed")
                    reply.extend(chunk)
                    if len(reply) > 3 or not b"ok\n".startswith(reply):
                        raise OSError("invalid cursor ACK")
                if state.shape is not None:
                    previous_shape = state.shape
                revision = current
        except Exception as error:
            self.connection.fail(error)

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            if self.thread is not None:
                self.thread.join()
            self.process.stdin.close()
            self.process.stdout.close()


class Connection:
    """Three admission credits cover raw input, NVENC, transport and visible ACK."""

    def __init__(self, capture, command, stopping, cursor_capture):
        self.capture = capture
        self.command = command
        self.stopping = stopping
        self.cursor_capture = cursor_capture
        self.cursor_sender = None
        self.cursor_socket = None
        self.cv = threading.Condition()
        self.done = threading.Event()
        self.error = None
        self.ready = False
        self.admitted = 0
        self.inputs = deque()
        self.pending = {}
        self.next_ack = 0
        self.encoder = None
        self.process = None
        self.threads = []

    def fail(self, error):
        with self.cv:
            if self.error is None:
                self.error = str(error)
            self.done.set()
            self.cv.notify_all()

    def check(self):
        self.cursor_capture.check()
        if self.error is not None:
            raise OSError(self.error)
        if self.stopping.is_set() or self.done.is_set():
            raise OSError("connection stopping")
        if self.process.poll() is not None:
            raise OSError("receiver exited")

    def acknowledgements(self):
        ages, wires = [], []
        payload_bytes = 0
        batch_started = time.monotonic()
        replies = bytearray()
        deadline = batch_started + TIMEOUT
        try:
            while not self.done.is_set() and not self.stopping.is_set():
                if not select.select([self.process.stdout], [], [], 0.2)[0]:
                    with self.cv:
                        oldest = next(iter(self.pending.values()), None)
                        limit = oldest[0] + TIMEOUT if oldest else deadline
                        if (not self.ready or oldest) and time.monotonic() > limit:
                            raise TimeoutError("receiver ready/visible ACK timed out")
                    continue
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    raise OSError("receiver output closed")
                replies.extend(chunk)
                while b"\n" in replies:
                    line, _, rest = replies.partition(b"\n")
                    replies[:] = rest
                    if len(line) > 4096:
                        raise OSError("oversized receiver response")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise OSError("receiver response is not an object")
                    now = time.monotonic()
                    with self.cv:
                        if message.get("ready") is True:
                            if self.ready or any(message.get(k) != v for k, v in
                                    (("width", WIDTH), ("height", HEIGHT), ("fps", FPS),
                                     ("codec", "h264-cavlc"))):
                                raise OSError("invalid receiver readiness")
                            name = message.get("cursor_socket")
                            if (not isinstance(name, str) or len(name) > 128 or
                                    re.fullmatch(r"surface-cursor-[A-Za-z0-9_-]+", name) is None):
                                raise OSError("invalid receiver cursor socket")
                            self.cursor_socket = name
                            self.ready = True
                            batch_started = now
                        elif "ack" in message:
                            sequence = message["ack"]
                            if (not self.ready or type(sequence) is not int or
                                    sequence != self.next_ack or message.get("visible") is not True or
                                    sequence not in self.pending):
                                raise OSError("invalid or out-of-order visible ACK")
                            submitted, source_time, size = self.pending.pop(sequence)
                            self.next_ack += 1
                            self.admitted -= 1
                            if source_time is not None:
                                ages.append((now - source_time) * 1000)
                            wires.append((now - submitted) * 1000)
                            payload_bytes += size
                            if self.next_ack % 100 == 0:
                                elapsed = now - batch_started
                                def stats(values):
                                    if not values:
                                        return "n=0"
                                    ordered = sorted(values)
                                    return (f"n={len(ordered)} p50={ordered[(len(ordered)-1)//2]:.1f} "
                                            f"p95={ordered[(len(ordered)*95-1)//100]:.1f} "
                                            f"max={ordered[-1]:.1f}")
                                log(f"visible ACK: frames={self.next_ack} fps={100/elapsed:.2f} "
                                    f"payload-mbps={payload_bytes*8/elapsed/1e6:.2f} "
                                    f"source-age-upper-ms {stats(ages)} wire-ACK-ms {stats(wires)}")
                                ages.clear()
                                wires.clear()
                                payload_bytes = 0
                                batch_started = now
                        else:
                            raise OSError("unexpected receiver response")
                        self.cv.notify_all()
                if len(replies) > 4096:
                    raise OSError("oversized receiver response")
        except Exception as error:
            self.fail(error)

    def feed(self):
        latest = self.capture.get_by_name("latest")
        source = self.encoder.get_by_name("raw")
        capture_caps = None
        period = 1 / FPS
        next_frame = time.monotonic()
        try:
            while not self.done.is_set() and not self.stopping.is_set():
                with self.cv:
                    while True:
                        self.check()
                        delay = next_frame - time.monotonic()
                        if self.admitted < CREDITS and delay <= 0:
                            break
                        self.cv.wait(min(0.2, delay) if delay > 0 else 0.2)
                    # Reserve BEFORE pulling/pushing, including all internal NVENC buffers.
                    self.admitted += 1
                sample = latest.emit("try-pull-sample", 0)
                fresh = sample is not None
                if sample is None:
                    sample = latest.get_property("last-sample")
                if sample is None:
                    with self.cv:
                        self.admitted -= 1
                        self.cv.wait(0.02)
                    continue
                original = sample.get_buffer()
                clock = self.capture.get_clock()
                if clock is None:
                    raise OSError("capture has no clock")
                host_now = time.monotonic()
                running_now = clock.get_time() - self.capture.get_base_time()
                source_time = None
                if fresh:
                    running = sample.get_segment().to_running_time(Gst.Format.TIME, original.pts)
                    if running == Gst.CLOCK_TIME_NONE:
                        raise OSError("capture has no correlated source timestamp")
                    age = (running_now - running) / Gst.SECOND
                    if age < -0.05:
                        raise OSError("capture timestamp is ahead of its clock")
                    source_time = host_now - max(0, age)
                actual_caps = sample.get_caps()
                if capture_caps is None:
                    capture_caps = actual_caps.copy()
                    structure = capture_caps.get_structure(0).copy()
                    structure.remove_field("framerate")
                    encoder_caps = Gst.Caps.from_string(
                        structure.to_string().removesuffix(";") + f", framerate=(fraction){FPS}/1")
                    source.set_property("caps", encoder_caps)
                    log(f"raw encoder caps: {encoder_caps.to_string()}")
                elif not capture_caps.is_equal(actual_caps):
                    raise OSError("capture caps changed during connection")
                # Own timing metadata, but share pixel memory and VideoMeta.
                buffer = original.copy()
                buffer.pts = running_now
                buffer.dts = running_now
                buffer.duration = Gst.SECOND // FPS
                buffer.unset_flags(Gst.BufferFlags.DISCONT)
                with self.cv:
                    self.check()
                    self.inputs.append((source_time, host_now))
                result = source.emit("push-buffer", buffer)
                buffer = original = sample = None
                if result != Gst.FlowReturn.OK:
                    raise OSError(f"raw encoder push failed: {result}")
                # Never catch up with a burst after an ACK or capture delay.
                next_frame += period
                now = time.monotonic()
                if next_frame < now:
                    next_frame = now + period
        except Exception as error:
            self.fail(error)

    def send(self, data, deadline):
        view = memoryview(data)
        while view:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("SSH write timed out")
            if not select.select([], [self.process.stdin], [], min(remaining, 0.2))[1]:
                continue
            try:
                count = os.write(self.process.stdin.fileno(), view)
            except BlockingIOError:
                continue
            if count <= 0:
                raise OSError("receiver input closed")
            view = view[count:]

    def run(self):
        try:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, bufsize=0)
            os.set_blocking(self.process.stdin.fileno(), False)
            reader = threading.Thread(target=self.acknowledgements, name="visible-ack")
            self.threads.append(reader)
            reader.start()
            with self.cv:
                while not self.ready:
                    self.check()
                    self.cv.wait(0.2)
            self.cursor_sender = CursorSender(self, self.cursor_capture, self.cursor_socket)
            self.cursor_sender.start()
            self.encoder = Gst.parse_launch(
                "appsrc name=raw format=time is-live=true block=true max-buffers=1 "
                "max-bytes=0 max-time=0 "
                "! nvh264enc cabac=false bframes=0 rc-lookahead=0 weighted-pred=false "
                "zerolatency=true tune=ultra-low-latency preset=p4 rc-mode=cbr bitrate=12000 "
                "vbv-buffer-size=400 repeat-sequence-header=true "
                "! video/x-h264,profile=baseline,stream-format=byte-stream,alignment=au "
                "! appsink name=encoded max-buffers=1 drop=false sync=false wait-on-eos=false")
            self.encoder.use_clock(self.capture.get_clock())
            self.encoder.set_start_time(Gst.CLOCK_TIME_NONE)
            self.encoder.set_base_time(self.capture.get_base_time())
            if self.encoder.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise OSError("encoder failed to enter PLAYING")
            feeder = threading.Thread(target=self.feed, name="raw-admission")
            self.threads.append(feeder)
            feeder.start()
            sink = self.encoder.get_by_name("encoded")
            gst_bus = self.encoder.get_bus()
            sequence = 0
            logged_caps = False
            while True:
                self.check()
                message = gst_bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if message:
                    if message.type == Gst.MessageType.ERROR:
                        error, debug = message.parse_error()
                        raise OSError(f"encoder: {error}: {debug}")
                    raise OSError("encoder reached EOF")
                sample = sink.emit("try-pull-sample", Gst.SECOND // 5)
                if sample is None:
                    with self.cv:
                        if self.inputs and time.monotonic() - self.inputs[0][1] > TIMEOUT:
                            raise TimeoutError("encoder output timed out")
                    continue
                buffer = sample.get_buffer()
                size = buffer.get_size()
                if not 1 <= size <= MAX_AU:
                    raise OSError(f"invalid encoded AU length: {size}")
                if not logged_caps:
                    log(f"encoded caps: {sample.get_caps().to_string()}")
                    logged_caps = True
                with self.cv:
                    if not self.inputs:
                        raise OSError("encoded AU without admitted raw frame")
                    source_time, _ = self.inputs.popleft()
                    submitted = time.monotonic()
                    self.pending[sequence] = (submitted, source_time, size)
                # Only compressed bytes cross Python; decoded/raw pixels are never mapped.
                payload = buffer.extract_dup(0, size)
                self.send(FRAME.pack(sequence, size), submitted + TIMEOUT)
                self.send(payload, submitted + TIMEOUT)
                sequence += 1
        finally:
            self.done.set()
            with self.cv:
                self.cv.notify_all()
            if self.cursor_sender is not None:
                self.cursor_sender.close()
            # Release a blocked push before joining its producer.
            if self.encoder is not None:
                self.encoder.set_state(Gst.State.NULL)
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                for pipe in (self.process.stdin, self.process.stdout):
                    pipe.close()
            for thread in self.threads:
                thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="user@10.42.0.2")
    parser.add_argument("--key", type=Path, default=Path.home() / ".ssh/surface_omp")
    parser.add_argument("--known-hosts", type=Path,
                        default=Path(__file__).resolve().parent.parent / "surface-known-hosts")
    args = parser.parse_args()
    Gst.init(None)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    loop = GLib.MainLoop()
    stopping = threading.Event()
    status = 0
    node = None
    pipeline = None
    worker = None
    subscriptions = []
    session = None
    cursor_capture = None

    def call(path, interface, method, parameters=None, result=None):
        return bus.call_sync(DEST, path, interface, method, parameters,
                             GLib.VariantType.new(result) if result else None,
                             Gio.DBusCallFlags.NONE, 10000, None)

    def stop():
        stopping.set()
        loop.quit()
        return False

    def closed(*unused):
        nonlocal status
        if not stopping.is_set():
            status = 1
        stop()

    def added(connection, sender, path, interface, name, parameters):
        nonlocal node
        node, = parameters.unpack()
        loop.quit()

    def message(gst_bus, msg):
        nonlocal status
        if msg.type == Gst.MessageType.ERROR:
            error, debug = msg.parse_error()
            log(f"capture error: {error}: {debug}")
            status = 1
            stop()
        elif msg.type == Gst.MessageType.EOS:
            log("capture reached EOF")
            status = 1
            stop()

    ssh = ["ssh", "-T", "-i", str(args.key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
           "-o", f"UserKnownHostsFile={args.known_hosts}", "-o", "StrictHostKeyChecking=yes", args.target,
           "DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/10000 "
           "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10000/bus GST_REGISTRY_UPDATE=no "
           "exec /usr/bin/python3 /home/user/display_receiver.py"]

    def relay():
        while not stopping.is_set():
            try:
                Connection(pipeline, ssh, stopping, cursor_capture).run()
            except Exception as error:
                if not stopping.is_set():
                    log(f"receiver disconnected: {error}")
            stopping.wait(3)

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, stop)
    try:
        session, = call(ROOT, DEST, "CreateSession", GLib.Variant("(a{sv})", ({},)), "(o)").unpack()
        subscriptions.append(bus.signal_subscribe(DEST, DEST + ".Session", "Closed", session,
                                                  None, Gio.DBusSignalFlags.NONE, closed))
        properties = {"cursor-mode": GLib.Variant("u", 2), "is-platform": GLib.Variant("b", True)}
        stream, = call(session, DEST + ".Session", "RecordVirtual",
                       GLib.Variant("(a{sv})", (properties,)), "(o)").unpack()
        subscription = bus.signal_subscribe(DEST, DEST + ".Stream", "PipeWireStreamAdded", stream,
                                            None, Gio.DBusSignalFlags.NONE, added)
        subscriptions.append(subscription)
        timer = GLib.timeout_add_seconds(10, lambda: (loop.quit(), False)[1])
        call(session, DEST + ".Session", "Start")
        loop.run()
        if node is not None:
            GLib.source_remove(timer)
        bus.signal_unsubscribe(subscription)
        subscriptions.remove(subscription)
        if stopping.is_set():
            return status
        if node is None:
            raise RuntimeError("Mutter did not provide a PipeWire node")
        cursor_capture = CursorCapture(node)

        def cursor_health():
            nonlocal status
            try:
                cursor_capture.check()
            except Exception as error:
                log(f"cursor capture: {error}")
                status = 1
                stop()
                return False
            return True

        GLib.timeout_add(200, cursor_health)
        pipeline = Gst.parse_launch(
            f"pipewiresrc path={node} use-bufferpool=false do-timestamp=true "
            f"! video/x-raw,width={WIDTH},height={HEIGHT} "
            "! videoconvert ! video/x-raw,format=NV12 "
            "! appsink name=latest max-buffers=1 drop=true enable-last-sample=true "
            "sync=false wait-on-eos=false")
        pipeline.use_clock(Gst.SystemClock.obtain())
        gst_bus = pipeline.get_bus()
        gst_bus.add_signal_watch()
        gst_bus.connect("message", message)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("capture failed to enter PLAYING")
        worker = threading.Thread(target=relay, name="receiver-reconnect")
        worker.start()
        log(f"virtual display: node={node}, {WIDTH}x{HEIGHT}, {FPS} fps cap, H264 CAVLC, captured cursor")
        loop.run()
    except Exception as error:
        log(f"display sender: {error}")
        status = 1
    finally:
        stopping.set()
        if worker is not None:
            worker.join()
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_bus().remove_signal_watch()
        if cursor_capture is not None:
            cursor_capture.close()
        for subscription in subscriptions:
            bus.signal_unsubscribe(subscription)
        if session is not None:
            try:
                call(session, DEST + ".Session", "Stop")
            except GLib.Error as error:
                log(f"Mutter session stop: {error}")
    return status


if __name__ == "__main__":
    sys.exit(main())
