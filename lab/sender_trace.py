#!/usr/bin/python3
"""Throwaway host-side timing sender for the native Mutter virtual monitor.

This is a diagnostic copy of surface-gud/display_sender.py.  It keeps the
production capture/encode/transport flow intact and adds per-ACK NDJSON timing
records.  It never edits or imports production code other than the wire structs.

The receiver may append an optional ``trace`` object to each visible ACK.  Host
code logs that object verbatim; it never compares Surface timestamps with host
timestamps.  All host durations below are monotonic same-host milliseconds.
"""
import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import select
import shlex
import signal
import subprocess
import sys
import threading
import time

# A stale per-user registry can hide the installed NVENC element. Process-local only.
os.environ["GST_REGISTRY"] = "/dev/null"
os.environ["GST_REGISTRY_UPDATE"] = "no"

PRODUCTION_MODULE_DIR = Path(__file__).resolve().parent.parent / "surface-gud"
try:
    from surface_protocol import FRAME, MAX_AU, WIDTH, HEIGHT, FPS
except ModuleNotFoundError as error:
    if error.name != "surface_protocol":
        raise
    # The throwaway script lives outside the production tree; --help and normal
    # runs work without PYTHONPATH by falling back to the installed module.
    sys.path.append(str(PRODUCTION_MODULE_DIR))
    from surface_protocol import FRAME, MAX_AU, WIDTH, HEIGHT, FPS

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst

DEST = "org.gnome.Mutter.ScreenCast"
ROOT = "/org/gnome/Mutter/ScreenCast"
TIMEOUT = 10

# Bounded diagnostic output.  duration*fps+credits ACK records fit comfortably.
MAX_LINE = 8192
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_TRACE_BYTES = 4096
MAX_RECORD_BYTES = 64 * 1024 * 1024
RECORD_QUEUE_DEPTH = 8

PROCESS_START = time.monotonic()
PUMP_FPS = float(FPS)


def log(text):
    print(text, file=sys.stderr, flush=True)


def milliseconds(start, end):
    return (end - start) * 1000.0


class TraceWriter:
    """One compact JSON object per visible ACK; bounded records and bytes."""

    def __init__(self, path, max_records):
        self.path = path
        self.max_records = max_records
        self.records = 0
        self.bytes = 0
        self.failure = None
        self.lock = threading.Lock()
        self.file = open(path, "w", encoding="utf-8", newline="\n", buffering=1)

    def write(self, record):
        try:
            line = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
        except (TypeError, ValueError) as error:
            self.failure = f"NDJSON encoding failed: {error}"
            raise OSError(self.failure)
        if len(line) > MAX_LINE:
            self.failure = f"NDJSON record too large: {len(line)} bytes"
            raise OSError(self.failure)
        with self.lock:
            if self.failure is not None:
                raise OSError(self.failure)
            if self.records >= self.max_records:
                self.failure = (f"ACK record budget exhausted: {self.records} records")
                raise OSError(self.failure)
            if self.bytes + len(line) > MAX_OUTPUT_BYTES:
                self.failure = f"NDJSON byte budget exhausted: {self.bytes} bytes"
                raise OSError(self.failure)
            try:
                self.file.write(line)
                self.file.flush()
            except OSError as error:
                self.failure = f"NDJSON write failed: {error}"
                raise OSError(self.failure)
            self.records += 1
            self.bytes += len(line)

    def close(self):
        with self.lock:
            try:
                self.file.close()
            except Exception as error:
                log(f"sender_trace: closing {self.path}: {error}")


class Recorder:
    """Keep the first contiguous FRAME+AU run (seq 0, IDR) for replay.

    Only payload references are queued; a writer thread performs file I/O so the
    capture/send critical path never waits on the filesystem.  Memory is bounded
    by RECORD_QUEUE_DEPTH * MAX_AU.  Records are never mixed across reconnects.
    """

    def __init__(self, path, max_frames, max_bytes=MAX_RECORD_BYTES):
        self.path = path
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self.queue = queue.Queue(maxsize=RECORD_QUEUE_DEPTH)
        self.claimed_conn = None
        self.accepted = 0
        self.bytes = 0
        self.written = 0
        self.written_bytes = 0
        self.capped = False
        self.failure = None
        # Open now so a bad path fails before the virtual monitor is created;
        # all writes still happen on the recorder thread.
        self.file = open(path, "wb")
        self.thread = threading.Thread(target=self._write, name="trace-record", daemon=True)
        self.thread.start()

    @staticmethod
    def nal_types(payload):
        types = []
        index = 0
        length = len(payload)
        while index + 3 <= length:
            if payload[index] == 0 and payload[index + 1] == 0:
                if payload[index + 2] == 1:
                    header = index + 3
                elif (index + 3 < length and payload[index + 2] == 0 and
                      payload[index + 3] == 1):
                    header = index + 4
                else:
                    index += 1
                    continue
                if header < length:
                    types.append(payload[header] & 0x1F)
                index = header + 1
            else:
                index += 1
        return types

    def check(self):
        if self.failure is not None:
            raise OSError(self.failure)

    def offer(self, connection, sequence, payload):
        if self.failure is not None:
            raise OSError(self.failure)
        if self.claimed_conn is None:
            if sequence != 0:
                return
            types = self.nal_types(payload)
            if 5 not in types:
                raise OSError("record start AU is not an IDR keyframe "
                              f"(NAL types: {types})")
            self.claimed_conn = connection
            log(f"record: claiming connection #{connection} at seq 0 "
                f"(NAL types: {types})")
        if connection != self.claimed_conn:
            return
        if self.accepted >= self.max_frames:
            return
        if sequence != self.accepted:
            self.failure = (f"record sequence gap: expected {self.accepted}, "
                            f"got {sequence}")
            raise OSError(self.failure)
        record_bytes = FRAME.size + len(payload)
        if self.bytes + record_bytes > self.max_bytes:
            if not self.capped:
                log(f"record: byte cap {self.max_bytes} reached after "
                    f"{self.accepted} frames")
                self.capped = True
            return
        try:
            # Do not copy payload; the writer packs FRAME+AU off the critical path.
            self.queue.put_nowait((sequence, payload))
        except queue.Full:
            self.failure = "record writer queue full"
            raise OSError(self.failure)
        self.accepted += 1
        self.bytes += record_bytes
        if self.accepted == self.max_frames:
            log(f"record: captured {self.accepted} frames / {self.bytes} bytes")

    def _write(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                sequence, payload = item
                self.file.write(FRAME.pack(sequence, len(payload)))
                self.file.write(payload)
                self.written += 1
                self.written_bytes += FRAME.size + len(payload)
        except Exception as error:
            self.failure = f"record writer: {error}"
            log(f"sender_trace: {self.failure}")
        finally:
            try:
                self.file.close()
            except OSError as error:
                self.failure = f"record close: {error}"
                log(f"sender_trace: {self.failure}")

    def close(self):
        deadline = time.monotonic() + 10
        while self.thread.is_alive() and time.monotonic() < deadline:
            try:
                self.queue.put(None, timeout=0.2)
                break
            except queue.Full:
                continue
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            if self.failure is None:
                self.failure = "record writer did not stop"
            log(f"sender_trace: {self.failure}")


def _validate_trace_value(value, depth=0):
    if depth > 4:
        raise OSError("receiver trace nesting is too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise OSError("receiver trace has a non-string key")
            _validate_trace_value(item, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _validate_trace_value(item, depth + 1)
        return
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OSError("receiver trace contains a non-finite duration")
        return
    if isinstance(value, int):
        return
    raise OSError(f"receiver trace contains unsupported value {type(value).__name__}")


def receiver_trace(message, require):
    """Return (trace-or-None, missing-reason-or-None); validate instrumented data."""
    if "trace" not in message:
        if require:
            raise OSError("visible ACK has no receiver trace "
                          "(is receiver_trace.py staged?)")
        return None, "ack has no trace key"
    trace = message["trace"]
    if trace is None:
        if require:
            raise OSError("visible ACK has a null receiver trace")
        return None, "ack trace is null"
    if not isinstance(trace, dict):
        raise OSError("receiver trace is not an object")
    if not trace:
        raise OSError("receiver trace is empty")
    # Nulls are valid probe data when the receiver explains them in "missing";
    # the host passes the object through verbatim and never subtracts clocks.
    _validate_trace_value(trace)
    encoded = json.dumps(trace, separators=(",", ":"), allow_nan=False)
    if len(encoded) > MAX_TRACE_BYTES:
        raise OSError(f"receiver trace is {len(encoded)} bytes "
                      f"(limit {MAX_TRACE_BYTES})")
    return trace, None


class Connection:
    """Production connection flow plus same-host timing instrumentation."""

    def __init__(self, capture, command, stopping, conn_id, credits, writer,
                 recorder, stats, stats_lock, require_trace):
        self.capture = capture
        self.command = command
        self.stopping = stopping
        self.conn_id = conn_id
        self.credits = credits
        self.writer = writer
        self.recorder = recorder
        self.stats = stats
        self.stats_lock = stats_lock
        self.require_trace = require_trace
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
        self.empty_pulls = 0

    def fail(self, error):
        with self.cv:
            if self.error is None:
                self.error = str(error)
            self.done.set()
            self.cv.notify_all()

    def check(self):
        if self.error is not None:
            raise OSError(self.error)
        if self.stopping.is_set() or self.done.is_set():
            raise OSError("connection stopping")
        if self.process.poll() is not None:
            raise OSError("receiver exited")
        if self.recorder is not None:
            self.recorder.check()

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
                    ack_record = None
                    ack_trace = None
                    ack_missing = None
                    ack_fresh = False
                    do_batch = False
                    if "ack" in message:
                        with self.cv:
                            completing = self.pending.get(message["ack"])
                        if completing and not completing[3]["send_ready"].wait(0.5):
                            raise OSError("send completion timing unavailable")
                    with self.cv:
                        if message.get("ready") is True:
                            if self.ready or any(message.get(k) != v for k, v in
                                    (("width", WIDTH), ("height", HEIGHT), ("fps", FPS),
                                     ("codec", "h264-cavlc"))):
                                raise OSError("invalid receiver readiness")
                            self.ready = True
                            batch_started = now
                        elif "ack" in message:
                            sequence = message["ack"]
                            if (not self.ready or type(sequence) is not int or
                                    sequence != self.next_ack or message.get("visible") is not True or
                                    sequence not in self.pending):
                                raise OSError("invalid or out-of-order visible ACK")
                            try:
                                ack_trace, ack_missing = receiver_trace(message,
                                                                       self.require_trace)
                            except Exception:
                                with self.stats_lock:
                                    self.stats["trace_errors"] += 1
                                raise
                            submitted, source_time, size, timing = self.pending.pop(sequence)
                            self.next_ack += 1
                            self.admitted -= 1
                            if source_time is not None:
                                ages.append((now - source_time) * 1000)
                            wires.append((now - submitted) * 1000)
                            payload_bytes += size
                            ack_fresh = bool(timing["fresh"])
                            ack_record = {
                                "host_ms": milliseconds(PROCESS_START, now),
                                "conn": self.conn_id,
                                "seq": sequence,
                                "credits": self.credits,
                                "fresh": ack_fresh,
                                "size": size,
                                "capture_age_ms": timing["source_age_ms"],
                                "admission_wait_ms": timing["admission_wait_ms"],
                                "credit_wait_ms": timing["credit_wait_ms"],
                                "late_admission_ms": timing["late_admission_ms"],
                                "push_ms": timing["push_ms"],
                                "encode_wait_ms": timing["encode_wait_ms"],
                                "pull_ms": timing["pull_ms"],
                                "copy_ms": timing["copy_ms"],
                                "header_send_ms": timing["header_send_ms"],
                                "payload_send_ms": timing["payload_send_ms"],
                                "send_ms": timing["send_ms"],
                                "submit_to_ack_ms": milliseconds(submitted, now),
                                "send_to_ack_ms": milliseconds(timing["send_end"], now),
                                "push_to_ack_ms": milliseconds(timing["push_end"], now),
                                "source_to_ack_ms": (milliseconds(source_time, now)
                                                     if source_time is not None else None),
                                "pending": len(self.pending),
                                "trace": ack_trace,
                            }
                            if ack_missing is not None:
                                ack_record["trace_missing"] = ack_missing
                            do_batch = self.next_ack % 100 == 0
                        else:
                            raise OSError("unexpected receiver response")
                        # Release feed's credit wait before doing any file I/O.
                        self.cv.notify_all()
                    if ack_record is not None:
                        self.writer.write(ack_record)
                        with self.stats_lock:
                            self.stats["acks"] += 1
                            self.stats["fresh" if ack_fresh else "stale"] += 1
                            if ack_trace is not None:
                                self.stats["trace_acks"] += 1
                            elif ack_missing is not None:
                                self.stats["trace_missing"] += 1
                            trace_acks = self.stats["trace_acks"]
                            acks = self.stats["acks"]
                        if do_batch:
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
                                f"source-age-upper-ms {stats(ages)} wire-ACK-ms {stats(wires)} "
                                f"trace={trace_acks}/{acks}")
                            ages.clear()
                            wires.clear()
                            payload_bytes = 0
                            batch_started = now
                if len(replies) > 4096:
                    raise OSError("oversized receiver response")
        except Exception as error:
            self.fail(error)

    def feed(self):
        latest = self.capture.get_by_name("latest")
        source = self.encoder.get_by_name("raw")
        capture_caps = None
        period = 1 / PUMP_FPS
        next_frame = time.monotonic()
        try:
            while not self.done.is_set() and not self.stopping.is_set():
                wait_started = time.monotonic()
                credit_wait = 0.0
                with self.cv:
                    while True:
                        self.check()
                        delay = next_frame - time.monotonic()
                        if self.admitted < self.credits and delay <= 0:
                            break
                        credits_full = self.admitted >= self.credits
                        wait_block = time.monotonic()
                        self.cv.wait(min(0.2, delay) if delay > 0 else 0.2)
                        if credits_full:
                            credit_wait += time.monotonic() - wait_block
                    # Reserve BEFORE pulling/pushing, including all internal NVENC buffers.
                    self.admitted += 1
                admitted_at = time.monotonic()
                admission_wait = milliseconds(wait_started, admitted_at)
                # Positive only when credits/allocation delayed the frame past its slot.
                late_admission = max(0.0, milliseconds(next_frame, admitted_at))
                sample = latest.emit("try-pull-sample", 0)
                fresh = sample is not None
                if sample is None:
                    sample = latest.get_property("last-sample")
                if sample is None:
                    self.empty_pulls += 1
                    with self.stats_lock:
                        self.stats["empty_pulls"] += 1
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
                source_age = None
                if fresh:
                    running = sample.get_segment().to_running_time(Gst.Format.TIME, original.pts)
                    if running == Gst.CLOCK_TIME_NONE:
                        raise OSError("capture has no correlated source timestamp")
                    age = (running_now - running) / Gst.SECOND
                    if age < -0.05:
                        raise OSError("capture timestamp is ahead of its clock")
                    source_age = max(0, age) * 1000
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
                timing = {
                    "fresh": fresh,
                    "host_now": host_now,
                    "source_time": source_time,
                    "source_age_ms": source_age,
                    "admission_wait_ms": admission_wait,
                    "credit_wait_ms": credit_wait,
                    "late_admission_ms": late_admission,
                    "push_ms": None,
                    "push_end": None,
                    "push_ready": threading.Event(),
                    "send_ready": threading.Event(),
                    "encode_wait_ms": None,
                    "pull_ms": None,
                    "copy_ms": None,
                    "header_send_ms": None,
                    "payload_send_ms": None,
                    "send_ms": None,
                    "send_end": None,
                }
                with self.cv:
                    self.check()
                    self.inputs.append(timing)
                push_started = time.monotonic()
                try:
                    result = source.emit("push-buffer", buffer)
                finally:
                    push_finished = time.monotonic()
                    timing["push_ms"] = milliseconds(push_started, push_finished)
                    timing["push_end"] = push_finished
                    timing["push_ready"].set()
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
                pull_started = time.monotonic()
                sample = sink.emit("try-pull-sample", Gst.SECOND // 5)
                pull_finished = time.monotonic()
                if sample is None:
                    with self.cv:
                        if (self.inputs and
                                time.monotonic() - self.inputs[0]["host_now"] > TIMEOUT):
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
                    timing = self.inputs.popleft()
                    submitted = time.monotonic()
                    self.pending[sequence] = (submitted, timing["source_time"], size, timing)
                if not timing["push_ready"].wait(0.5):
                    raise OSError("raw push completion timing unavailable")
                timing["pull_ms"] = milliseconds(pull_started, pull_finished)
                # May be microscopically negative if the AU became available
                # before push-buffer returned; kept raw rather than faked.
                timing["encode_wait_ms"] = milliseconds(timing["push_end"], pull_finished)
                copy_started = time.monotonic()
                # Only compressed bytes cross Python; decoded/raw pixels are never mapped.
                payload = buffer.extract_dup(0, size)
                copy_finished = time.monotonic()
                timing["copy_ms"] = milliseconds(copy_started, copy_finished)
                header_started = time.monotonic()
                self.send(FRAME.pack(sequence, size), submitted + TIMEOUT)
                header_finished = time.monotonic()
                timing["header_send_ms"] = milliseconds(header_started, header_finished)
                payload_started = time.monotonic()
                self.send(payload, submitted + TIMEOUT)
                payload_finished = time.monotonic()
                timing["payload_send_ms"] = milliseconds(payload_started, payload_finished)
                timing["send_ms"] = milliseconds(header_started, payload_finished)
                timing["send_end"] = payload_finished
                timing["send_ready"].set()
                if self.recorder is not None:
                    self.recorder.offer(self.conn_id, sequence, payload)
                sequence += 1
        finally:
            self.done.set()
            with self.cv:
                self.cv.notify_all()
            log(f"connection #{self.conn_id}: closed after {self.next_ack} visible ACKs "
                f"({self.empty_pulls} empty capture pulls)")
            # Release a blocked push before joining its producer.
            if self.encoder is not None:
                try:
                    self.encoder.set_state(Gst.State.NULL)
                except Exception as error:
                    log(f"connection #{self.conn_id}: encoder stop: {error}")
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                for pipe in (self.process.stdin, self.process.stdout):
                    try:
                        pipe.close()
                    except OSError:
                        pass
            for thread in self.threads:
                try:
                    thread.join()
                except RuntimeError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="user@10.42.0.2")
    parser.add_argument("--key", type=Path, default=Path.home() / ".ssh/surface_omp")
    parser.add_argument("--known-hosts", type=Path,
                        default=Path(__file__).resolve().parent.parent / "surface-known-hosts")
    parser.add_argument("--receiver-path",
                        default="/home/user/surface-latency-lab/receiver_trace.py")
    parser.add_argument("--duration", type=float, default=120.0,
                        help="streaming seconds before a clean stop (default 120)")
    parser.add_argument("--output", type=Path, required=True,
                        help="per-ACK NDJSON output path")
    parser.add_argument("--record", type=Path, default=None,
                        help="optional first-N FRAME+AU replay file (binary)")
    parser.add_argument("--record-frames", type=int, default=300,
                        help="maximum replay frames (default 300)")
    parser.add_argument("--credits", type=int, default=2,
                        help="admission credits; production value is 2")
    parser.add_argument("--pump-fps", type=float, default=float(FPS),
                        help="raw admission rate; encoder caps stay at the protocol FPS")
    parser.add_argument("--receiver-args", default="",
                        help="extra arguments for the diagnostic receiver")
    parser.add_argument("--require-trace", action="store_true",
                        help="abort a connection if an ACK lacks a receiver trace")
    parser.add_argument("--schedule", default="",
                        help="comma list credits:vblank_step:pump_fps:seconds, one "
                             "receiver connection each, same virtual monitor")
    args = parser.parse_args()
    global PUMP_FPS
    if not 10 <= args.pump_fps <= 31:
        parser.error("--pump-fps must be in [10, 31]")
    PUMP_FPS = args.pump_fps

    if not 0 < args.duration <= 3600:
        parser.error("--duration must be in (0, 3600] seconds")
    if not 1 <= args.credits <= 8:
        parser.error("--credits must be in [1, 8]")
    if not 1 <= args.record_frames <= 1000:
        parser.error("--record-frames must be in [1, 1000]")
    key = args.key.expanduser()
    known_hosts = args.known_hosts.expanduser()
    args.output = args.output.expanduser()
    if args.record is not None:
        args.record = args.record.expanduser()
    if not key.is_file():
        log(f"sender_trace: SSH key not found: {key}")
        return 2
    if not known_hosts.is_file():
        log(f"sender_trace: known-hosts not found: {known_hosts}")
        return 2
    if args.record is not None and args.record.resolve() == args.output.resolve():
        log("sender_trace: --record and --output must differ")
        return 2
    for label, path in (("--output", args.output), ("--record", args.record)):
        if path is not None and not path.parent.is_dir():
            log(f"sender_trace: {label} parent directory is missing: {path.parent}")
            return 2

    max_records = int(math.ceil(args.duration * FPS)) + args.credits + 8
    log(f"sender_trace: target={args.target} receiver={args.receiver_path} "
        f"credits={args.credits} duration={args.duration:g}s "
        f"output={args.output} record={args.record} max_ack_records={max_records}")
    try:
        writer = TraceWriter(args.output, max_records)
    except OSError as error:
        log(f"sender_trace: cannot open --output: {error}")
        return 1
    recorder = None
    if args.record is not None:
        try:
            recorder = Recorder(args.record, args.record_frames)
        except OSError as error:
            log(f"sender_trace: cannot open --record: {error}")
            writer.close()
            return 1
    stats = {"connections": 0, "acks": 0, "fresh": 0, "stale": 0,
             "trace_acks": 0, "trace_missing": 0, "trace_errors": 0,
             "empty_pulls": 0, "errors": 0}
    stats_lock = threading.Lock()

    stopping = threading.Event()
    loop = None
    bus = None
    call = None
    status = 0
    expired = False
    signalled = False
    connection_counter = 0
    connection_errors = 0
    node = None
    pipeline = None
    worker = None
    subscriptions = []
    session = None
    node_timer = None
    duration_timer = None

    def stop():
        stopping.set()
        if loop is not None:
            loop.quit()
        return False

    def stop_signal(signum=None):
        nonlocal signalled
        signalled = True
        log(f"sender_trace: signal {signum}")
        return stop()

    def expire():
        nonlocal expired, duration_timer
        duration_timer = None
        expired = True
        log(f"sender_trace: duration {args.duration:g}s reached")
        stop()
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

    def node_timeout():
        nonlocal node_timer
        node_timer = None
        loop.quit()
        return False

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

    ssh = ["ssh", "-T", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
           "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes",
           args.target,
           "DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/10000 "
           "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/10000/bus GST_REGISTRY_UPDATE=no "
           f"exec /usr/bin/python3 {shlex.quote(args.receiver_path)}"]

    def segments():
        # Each schedule entry reconnects the receiver; the capture monitor persists.
        if not args.schedule:
            while True:
                yield args.credits, args.pump_fps, args.receiver_args, None
        for entry in args.schedule.split(","):
            credits, step, fps, seconds = entry.split(":")
            yield int(credits), float(fps), f"--vblank-step {int(step)}", float(seconds)
        stop()

    def relay():
        global PUMP_FPS
        nonlocal connection_counter, connection_errors
        for credits, fps, receiver_args, seconds in segments():
            if stopping.is_set():
                break
            PUMP_FPS = fps
            connection_counter += 1
            with stats_lock:
                stats["connections"] += 1
            command = ssh[:-1] + [f"{ssh[-1]} {receiver_args}"]
            log(f"segment #{connection_counter}: credits={credits} pump={fps:g} "
                f"receiver='{receiver_args}' seconds={seconds}")
            connection = Connection(pipeline, command, stopping, connection_counter, credits,
                                    writer, recorder, stats, stats_lock, args.require_trace)
            timer = threading.Timer(seconds, connection.done.set) if seconds else None
            if timer:
                timer.start()
            try:
                connection.run()
            except Exception as error:
                if not stopping.is_set() and not (timer and timer.finished.is_set()):
                    connection_errors += 1
                    with stats_lock:
                        stats["errors"] += 1
                    log(f"receiver disconnected (#{connection_counter}): {error}")
            finally:
                if timer:
                    timer.cancel()
            stopping.wait(3)

    try:
        Gst.init(None)
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        loop = GLib.MainLoop()

        def call(path, interface, method, parameters=None, result=None):
            return bus.call_sync(DEST, path, interface, method, parameters,
                                 GLib.VariantType.new(result) if result else None,
                                 Gio.DBusCallFlags.NONE, 10000, None)

        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, stop_signal)
        session, = call(ROOT, DEST, "CreateSession",
                        GLib.Variant("(a{sv})", ({},)), "(o)").unpack()
        subscriptions.append(bus.signal_subscribe(DEST, DEST + ".Session", "Closed", session,
                                                  None, Gio.DBusSignalFlags.NONE, closed))
        properties = {"cursor-mode": GLib.Variant("u", 1),
                      "is-platform": GLib.Variant("b", True)}
        stream, = call(session, DEST + ".Session", "RecordVirtual",
                       GLib.Variant("(a{sv})", (properties,)), "(o)").unpack()
        subscription = bus.signal_subscribe(DEST, DEST + ".Stream", "PipeWireStreamAdded", stream,
                                            None, Gio.DBusSignalFlags.NONE, added)
        subscriptions.append(subscription)
        node_timer = GLib.timeout_add_seconds(10, node_timeout)
        call(session, DEST + ".Session", "Start")
        loop.run()
        if node_timer is not None:
            GLib.source_remove(node_timer)
            node_timer = None
        bus.signal_unsubscribe(subscription)
        subscriptions.remove(subscription)
        if node is None and not stopping.is_set():
            raise RuntimeError("Mutter did not provide a PipeWire node")
        if node is not None and not stopping.is_set():
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
            log(f"virtual display: node={node}, {WIDTH}x{HEIGHT}, {FPS} fps cap, "
                "H264 CAVLC, embedded cursor")
            duration_timer = GLib.timeout_add(max(1, int(args.duration * 1000)), expire)
            if not stopping.is_set():
                loop.run()
    except Exception as error:
        log(f"display sender: {error}")
        status = 1
    finally:
        stopping.set()
        if duration_timer is not None:
            try:
                GLib.source_remove(duration_timer)
            except Exception:
                pass
            duration_timer = None
        if node_timer is not None:
            try:
                GLib.source_remove(node_timer)
            except Exception:
                pass
            node_timer = None
        if worker is not None:
            try:
                worker.join()
            except RuntimeError:
                pass
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception as error:
                log(f"capture stop: {error}")
            try:
                pipeline.get_bus().remove_signal_watch()
            except Exception:
                pass
        if bus is not None:
            for subscription in subscriptions:
                try:
                    bus.signal_unsubscribe(subscription)
                except Exception:
                    pass
        if session is not None and call is not None:
            try:
                call(session, DEST + ".Session", "Stop")
            except Exception as error:
                log(f"Mutter session stop: {error}")
        writer.close()
        if recorder is not None:
            recorder.close()

    record_summary = None
    if recorder is not None:
        record_summary = {
            "path": str(args.record),
            "accepted_frames": recorder.accepted,
            "written_frames": recorder.written,
            "bytes": recorder.written_bytes,
            "max_frames": recorder.max_frames,
            "capped": recorder.capped,
            "complete": (recorder.written > 0 and recorder.written == recorder.accepted and
                         recorder.failure is None),
            "failure": recorder.failure,
        }
    with stats_lock:
        snapshot = dict(stats)

    exit_status = status
    if exit_status == 0:
        if writer.failure is not None or (recorder is not None and recorder.failure is not None):
            exit_status = 1
        elif writer.records == 0:
            exit_status = 3
        elif snapshot["trace_acks"] == 0:
            exit_status = 4
        elif args.require_trace and snapshot["trace_errors"]:
            exit_status = 4

    summary = {
        "target": args.target,
        "receiver_path": args.receiver_path,
        "credits": args.credits,
        "duration_s": args.duration,
        "expired": expired,
        "signalled": signalled,
        "connections": snapshot["connections"],
        "connection_errors": connection_errors,
        "acks": snapshot["acks"],
        "fresh_acks": snapshot["fresh"],
        "stale_acks": snapshot["stale"],
        "empty_pulls": snapshot["empty_pulls"],
        "trace_acks": snapshot["trace_acks"],
        "trace_missing": snapshot["trace_missing"],
        "trace_errors": snapshot["trace_errors"],
        "connection_error_count": snapshot["errors"],
        "output": str(args.output),
        "output_records": writer.records,
        "output_bytes": writer.bytes,
        "output_failure": writer.failure,
        "record": record_summary,
        "status": exit_status,
    }
    log("sender_trace summary " + json.dumps(summary, separators=(",", ":")))
    return exit_status


if __name__ == "__main__":
    sys.exit(main())
