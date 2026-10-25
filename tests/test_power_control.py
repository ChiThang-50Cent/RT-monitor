import concurrent.futures
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import types
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


power = load_module('tested_surface_idle', ROOT / 'appliance/overlay/usr/libexec/surface-idle')
client_module = load_module('tested_surface_power', ROOT / 'surface-gud/surface_power.py')


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Panel:
    def __init__(self, journal):
        self.journal = journal
        self.brightness = 47
        self.saved = 47

    def read(self):
        return self.brightness

    def set(self, lit):
        if not lit and self.brightness > 0:
            self.saved = self.brightness
        self.brightness = self.saved if lit else 0
        self.journal.append(('brightness', self.brightness))


class Display:
    def __init__(self, journal):
        self.journal = journal
        self.off = False

    def blanked(self):
        return self.off

    def awake(self):
        self.off = False
        self.journal.append(('scanout', 'on'))

    def idle(self):
        self.journal.append(('idle', 60))


class Surface:
    def __init__(self, journal):
        self.journal = journal
        self.countdowns = []
        self.message = None

    def draw_countdown(self, seconds):
        self.countdowns.append(seconds)
        self.journal.append(('draw', seconds))

    def draw_idle(self, message=None):
        self.message = message
        self.journal.append(('idle-text', message))


class Leases:
    def __init__(self, journal):
        self.journal = journal
        self.granted = set()
        self._allowed = True

    @property
    def allowed(self):
        return self._allowed

    @allowed.setter
    def allowed(self, value):
        self._allowed = value
        self.journal.append(('admission', value))

    def revoke(self):
        self.allowed = False
        self.journal.append(('revoke',))


class Visibility:
    def __init__(self, journal):
        self.journal = journal
        self.port = None
        self.available = True
        self.progress = True
        self.error = None

    def acquire(self):
        if self.error:
            raise RuntimeError(self.error)
        if self.available:
            self.port = 1
            self.journal.append(('overlay', 'grabbed'))
        return self.available

    def begin(self, now):
        self.journal.append(('witness-start', now))

    def visible(self, now):
        if self.error:
            raise RuntimeError(self.error)
        return self.progress

    def close(self):
        self.port = None
        self.journal.append(('overlay', 'released'))


class Process:
    def __init__(self):
        self.result = None

    def poll(self):
        return self.result


class Appliance:
    def __init__(self):
        self.journal = []
        self.clock = Clock()
        self.panel = Panel(self.journal)
        self.display = Display(self.journal)
        self.surface = Surface(self.journal)
        self.leases = Leases(self.journal)
        self.visibility = Visibility(self.journal)
        self.shutdowns = []
        self.controller = power.Controller(self.surface, self.display, self.panel, self.leases,
                                           self.visibility, self.shutdown, self.clock)

    def shutdown(self):
        process = Process()
        self.shutdowns.append(process)
        return process

    def event(self, kind, at, **kwargs):
        self.clock.now = at
        self.controller.apply(power.PowerEvent(kind, at, **kwargs))

    def poll(self, at):
        self.clock.now = at
        self.controller.poll(at)

    def short(self, at=0):
        self.event('down', at)
        self.event('up', at + 0.1, blanked=self.display.blanked())

    def hold(self, at=0):
        self.event('down', at)
        self.poll(at + 3)

    def visible(self, at=3.1):
        self.poll(at)
        return self.controller.state.generation


class GestureBehavior(unittest.TestCase):
    def test_every_release_below_three_seconds_toggles_only_illumination(self):
        for duration in (0.05, 1.5, 2.9, 2.999):
            with self.subTest(duration=duration):
                app = Appliance()
                app.event('down', 10)
                app.event('up', 10 + duration)
                self.assertEqual(app.panel.brightness, 0)
                self.assertEqual(app.shutdowns, [])
                self.assertEqual(app.surface.countdowns, [])
                self.assertNotIn(('scanout', 'on'), app.journal)
                app.short(20)
                self.assertEqual(app.panel.brightness, 47)

    def test_delayed_input_drain_uses_release_timestamp_not_dispatch_time(self):
        app = Appliance()
        device = power.PowerInput(-1)
        records = (power.INPUT_EVENT.pack(10, 0, 1, 116, 1) +
                   power.INPUT_EVENT.pack(12, 900000, 1, 116, 0))
        app.clock.now = 19
        for event in device.decode(records):
            app.controller.apply(event)
        app.poll(19)
        self.assertEqual(app.panel.brightness, 0)
        self.assertEqual(app.surface.countdowns, [])
        self.assertEqual(app.shutdowns, [])

    def test_exact_and_over_threshold_release_prepare_without_short_toggle(self):
        for duration in (3.0, 3.001):
            with self.subTest(duration=duration):
                app = Appliance()
                app.event('down', 0)
                app.event('up', duration)
                self.assertFalse(app.leases.allowed)
                self.assertEqual(app.panel.brightness, 47)
                self.assertEqual(app.surface.countdowns, [])
                app.poll(duration + 0.1)
                self.assertEqual(app.surface.countdowns, [30])

    def test_hold_triggers_without_release_and_original_release_is_consumed(self):
        app = Appliance()
        app.hold()
        self.assertFalse(app.leases.allowed)
        app.visible()
        app.event('up', 4)
        self.assertFalse(app.leases.allowed)
        self.assertEqual(app.panel.brightness, 47)
        app.poll(33.099)
        self.assertEqual(app.shutdowns, [])
        app.poll(33.1)
        self.assertEqual(len(app.shutdowns), 1)
        app.poll(100)
        app.event('down', 101)
        app.event('up', 102)
        self.assertEqual(len(app.shutdowns), 1)

    def test_duplicate_down_and_repeat_do_not_restart_hold(self):
        app = Appliance()
        device = power.PowerInput(-1)
        records = b''.join(power.INPUT_EVENT.pack(s, 0, 1, 116, v)
                           for s, v in ((0, 1), (1, 2), (2, 1)))
        for event in device.decode(records):
            app.controller.apply(event)
        app.poll(3)
        self.assertFalse(app.leases.allowed)

    def test_short_press_wakes_automatically_blanked_panel(self):
        app = Appliance()
        app.display.off = True
        app.short()
        self.assertFalse(app.display.off)
        self.assertEqual(app.panel.brightness, 47)
        self.assertIn(('idle', 60), app.journal)
        app.short(1)
        self.assertEqual(app.panel.brightness, 0)

    def test_initial_held_and_lost_input_require_release(self):
        app = Appliance()
        app.event('lost', 0)
        app.event('down', 1)
        app.poll(10)
        self.assertEqual(app.surface.countdowns, [])
        app.event('up', 11)
        self.assertEqual(app.panel.brightness, 47)
        app.short(12)
        self.assertEqual(app.panel.brightness, 0)

    def test_partial_native_records_are_preserved(self):
        app = Appliance()
        device = power.PowerInput(-1)
        record = power.INPUT_EVENT.pack(10, 125000, 1, 116, 1)
        self.assertEqual(device.decode(record[:5]), [])
        events = device.decode(record[5:])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].at, 10.125)
        app.controller.apply(events[0])
        app.poll(13.124)
        self.assertTrue(app.leases.allowed)
        app.poll(13.125)
        self.assertFalse(app.leases.allowed)

    def test_syn_dropped_discards_unreliable_gesture_and_resynchronizes(self):
        app = Appliance()
        app.hold()
        app.visible()
        device = power.PowerInput(-1)
        device.down = lambda: False
        records = b''.join(power.INPUT_EVENT.pack(s, 0, k, c, v) for s, k, c, v in (
            (4, 0, 3, 0), (4, 1, 116, 1), (4, 0, 0, 0)))
        for event in device.decode(records):
            app.controller.apply(event)
        app.poll(100)
        self.assertEqual(app.shutdowns, [])
        app.short(101)
        self.assertEqual(app.panel.brightness, 0)


class ConfirmationBehavior(unittest.TestCase):
    def test_full_thirty_seconds_start_after_visibility_not_hold(self):
        app = Appliance()
        app.leases.granted.add('receiver')
        app.hold()
        app.poll(6.9)
        self.assertEqual(app.surface.countdowns, [])
        self.assertEqual(app.shutdowns, [])
        app.leases.granted.clear()
        app.visible(7)
        self.assertEqual(app.surface.countdowns, [30])
        app.poll(8)
        self.assertEqual(app.surface.countdowns[-1], 29)
        app.poll(36.999)
        self.assertEqual(app.shutdowns, [])
        app.poll(37)
        self.assertEqual(len(app.shutdowns), 1)

    def test_fresh_press_cancels_preparing_and_counting_with_both_releases_consumed(self):
        for counting in (False, True):
            with self.subTest(counting=counting):
                app = Appliance()
                app.hold()
                generation = app.controller.state.generation
                if counting:
                    app.visible()
                app.event('up', 4)
                app.event('down', 4.1)
                self.assertTrue(app.leases.allowed)
                self.assertIsNone(power.next_deadline(app.controller.state))
                app.poll(20)
                app.event('up', 21)
                self.assertEqual(app.panel.brightness, 47)
                app.event('visible', 22, generation=generation)
                app.poll(100)
                self.assertEqual(app.shutdowns, [])
                app.short(101)
                self.assertEqual(app.panel.brightness, 0)

    def test_cancel_queued_at_expiry_beats_timer(self):
        app = Appliance()
        app.hold()
        app.visible()
        app.event('up', 4)
        app.event('down', 33.1)
        app.poll(33.1)
        app.event('up', 34)
        app.poll(100)
        self.assertEqual(app.shutdowns, [])
        self.assertEqual(app.panel.brightness, 47)

    def test_manual_off_survives_reconnect_confirmation_and_cancel(self):
        app = Appliance()
        app.short()
        app.controller.acquire_receiver()
        self.assertEqual(app.panel.brightness, 0)
        app.hold(1)
        app.visible(4.1)
        self.assertEqual(app.panel.brightness, 47)
        app.event('up', 5)
        app.journal.clear()
        app.event('down', 6)
        self.assertEqual(app.panel.brightness, 0)
        self.assertLess(app.journal.index(('brightness', 0)), app.journal.index(('overlay', 'released')))
        self.assertLess(app.journal.index(('overlay', 'released')), app.journal.index(('admission', True)))
        app.controller.acquire_receiver()
        self.assertEqual(app.panel.brightness, 0)
        app.event('up', 6.1)
        app.short(7)
        self.assertEqual(app.panel.brightness, 47)

    def test_preparation_timeout_and_stuck_port_never_shutdown(self):
        for blocked_by in ('lease', 'port', 'no-vblank'):
            with self.subTest(blocked_by=blocked_by):
                app = Appliance()
                if blocked_by == 'lease':
                    app.leases.granted.add('stuck')
                elif blocked_by == 'port':
                    app.visibility.available = False
                else:
                    app.visibility.progress = False
                app.hold()
                app.poll(3.1)
                app.poll(8)
                app.poll(100)
                self.assertEqual(app.shutdowns, [])
                self.assertTrue(app.leases.allowed)
                self.assertIsNone(power.next_deadline(app.controller.state))

    def test_visibility_or_input_loss_removes_shutdown_deadline(self):
        for failure in ('overlay', 'vblank', 'unviewable', 'brightness', 'dpms', 'input'):
            with self.subTest(failure=failure):
                app = Appliance()
                app.hold()
                app.visible()
                if failure in ('overlay', 'vblank', 'unviewable'):
                    app.visibility.error = failure
                elif failure == 'brightness':
                    app.panel.brightness = 0
                elif failure == 'dpms':
                    app.display.off = True
                else:
                    app.event('lost', 4)
                app.poll(4)
                app.poll(100)
                self.assertEqual(app.shutdowns, [])
                self.assertTrue(app.leases.allowed)
                self.assertIsNone(power.next_deadline(app.controller.state))

    def test_no_shutdown_until_current_vblank_progress_at_expiry(self):
        app = Appliance()
        app.hold()
        app.visible()
        app.visibility.progress = False
        app.poll(33.1)
        self.assertEqual(app.shutdowns, [])
        app.visibility.progress = True
        app.poll(33.2)
        self.assertEqual(len(app.shutdowns), 1)

    def test_failed_shutdown_has_no_delayed_retry_and_requires_new_gesture(self):
        for manual_off in (False, True):
            with self.subTest(manual_off=manual_off):
                app = Appliance()
                if manual_off:
                    app.short(-1)
                app.hold()
                app.visible()
                app.event('up', 4)
                app.poll(33.1)
                app.shutdowns[0].result = 1
                app.poll(33.2)
                self.assertEqual(app.panel.brightness, 0 if manual_off else 47)
                self.assertIn('Shutdown failed', app.surface.message)
                app.poll(200)
                self.assertEqual(len(app.shutdowns), 1)
                app.hold(201)
                app.visible(204.1)
                app.poll(234.1)
                self.assertEqual(len(app.shutdowns), 2)


class BacklightBehavior(unittest.TestCase):
    def make_panel(self, root):
        driver = root / 'drivers/pwm-backlight'
        driver.mkdir(parents=True)
        device = root / 'devices/panel'
        device.mkdir(parents=True)
        (device / 'driver').symlink_to(driver)
        panel = root / 'class/backlight'
        panel.mkdir(parents=True)
        (panel / 'device').symlink_to(device)
        (panel / 'brightness').write_text('73')
        (panel / 'max_brightness').write_text('255')
        return panel

    def test_exact_brightness_is_saved_and_unknown_zero_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            panel = self.make_panel(root)
            light = power.Backlight(root / 'class')
            light.set(False)
            self.assertEqual((panel / 'brightness').read_text(), '0')
            light.set(True)
            self.assertEqual((panel / 'brightness').read_text(), '73')
            (panel / 'brightness').write_text('0')
            light = power.Backlight(root / 'class')
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                light.set(True)
            self.assertEqual((panel / 'brightness').read_text(), '0')
            (panel / 'actual_brightness').write_text('61')
            light = power.Backlight(root / 'class')
            light.set(True)
            self.assertEqual((panel / 'brightness').read_text(), '61')

    def test_unknown_driver_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            panel = self.make_panel(root)
            (panel / 'device/driver').unlink()
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                power.Backlight(root / 'class')
            self.assertEqual((panel / 'brightness').read_text(), '73')


class LeaseBehavior(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = power.LeaseServer(Path(self.directory.name) / 'surface-power.sock')
        self.addCleanup(self.server.close)
        self.environment = patch.dict(os.environ, XDG_RUNTIME_DIR=self.directory.name)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown, wait=True)
        self.acquired = []

    def poll(self):
        self.server.poll(lambda: self.acquired.append(True))

    def acquire(self):
        future = self.executor.submit(client_module.AppliancePresentation.acquire)
        deadline = time.monotonic() + 3
        while not future.done() and time.monotonic() < deadline:
            self.poll()
            time.sleep(.001)
        return future.result(timeout=1)

    def test_real_acquire_revoke_release_and_readmission(self):
        client = self.acquire()
        self.addCleanup(client.close)
        self.assertEqual(len(self.acquired), 1)
        self.assertFalse(client.revoked())
        with self.assertRaises(RuntimeError):
            self.acquire()
        self.server.revoke()
        self.assertTrue(client.revoked())
        with self.assertRaises(RuntimeError):
            self.acquire()
        self.assertEqual(len(self.acquired), 1)
        client.close()
        self.poll()
        self.assertEqual(len(self.server.granted), 0)
        self.server.allowed = True
        replacement = self.acquire()
        self.addCleanup(replacement.close)
        self.assertFalse(replacement.revoked())
        self.assertEqual(len(self.acquired), 2)

    def test_controller_eof_revokes_client_and_close_is_idempotent(self):
        client = self.acquire()
        self.addCleanup(client.close)
        for connection in tuple(self.server.clients):
            self.server.drop(connection)
        self.assertTrue(client.revoked())
        client.close()
        client.close()
        self.assertTrue(client.revoked())

    def test_fragmented_acquire_and_invalid_protocol_have_no_early_admission(self):
        connection = socket.socket(socket.AF_UNIX)
        self.addCleanup(connection.close)
        connection.settimeout(1)
        connection.connect(str(self.server.path))
        connection.sendall(b'surface-power-v1 acq')
        self.poll()
        self.assertEqual(self.acquired, [])
        connection.sendall(b'uire\n')
        self.poll()
        self.assertEqual(connection.recv(128), b'surface-power-v1 granted\n')
        self.assertEqual(len(self.acquired), 1)
        connection.close()
        self.poll()
        bad = socket.socket(socket.AF_UNIX)
        self.addCleanup(bad.close)
        bad.settimeout(1)
        bad.connect(str(self.server.path))
        bad.sendall(b'surface-power-v2 acquire\n')
        self.poll()
        self.assertEqual(bad.recv(128), b'')
        self.assertEqual(len(self.acquired), 1)

    def test_wrong_peer_uid_is_rejected(self):
        self.server.uid = os.getuid() + 1
        with self.assertRaises((OSError, RuntimeError)):
            self.acquire()
        self.assertEqual(self.acquired, [])


class ReceiverCleanupBehavior(unittest.TestCase):
    def test_real_lease_is_retained_until_presenter_closes_without_unpresented_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'surface-appliance-active').touch()
            server = power.LeaseServer(root / 'surface-power.sock')
            journal = []
            revoke = threading.Event()
            finished = threading.Event()
            server_errors = []

            def serve():
                try:
                    while not finished.is_set():
                        server.poll(lambda: journal.append('admitted'))
                        if revoke.is_set() and server.allowed:
                            server.revoke()
                        time.sleep(.001)
                except Exception as error:
                    server_errors.append(error)

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()

            class Presenter:
                visible_frames = 0

                def __init__(self):
                    journal.append('presenter-created')
                    if not server.granted:
                        raise AssertionError('presenter constructed without admission')

                def close(self):
                    journal.append('presenter-closed')
                    if not server.granted:
                        raise AssertionError('lease released before presenter cleanup')

            class Element:
                def set_property(self, *args):
                    pass

                def connect(self, *args):
                    pass

                def emit(self, *args):
                    return 0

            class Pipeline:
                def get_by_name(self, name):
                    return Element()

                def set_state(self, state):
                    if state == 0:
                        journal.append('pipeline-stopped')
                    return 1

                def get_bus(self):
                    return self

                def timed_pop_filtered(self, *args):
                    revoke.set()
                    time.sleep(.01)
                    return None

            gst = types.SimpleNamespace(
                init=lambda _: None, parse_launch=lambda _: Pipeline(), SECOND=1000000000,
                State=types.SimpleNamespace(PLAYING=1, NULL=0),
                StateChangeReturn=types.SimpleNamespace(FAILURE=-1),
                FlowReturn=types.SimpleNamespace(OK=0, ERROR=-1),
                MessageType=types.SimpleNamespace(ERROR=1, EOS=2),
                Caps=types.SimpleNamespace(from_string=lambda _: None))
            gi = types.ModuleType('gi')
            gi.require_version = lambda *args: None
            repository = types.ModuleType('gi.repository')
            repository.Gst = gst
            repository.GstVideo = repository.Gio = repository.GLib = types.SimpleNamespace()
            xv = types.ModuleType('surface_xv')
            xv.XvPresenter = Presenter
            protocol = types.ModuleType('surface_protocol')
            protocol.WIDTH, protocol.HEIGHT, protocol.FPS = 1366, 768, 30
            protocol.read_frame = lambda *args: None
            output = io.StringIO()
            try:
                with patch.dict(os.environ, XDG_RUNTIME_DIR=directory), patch.dict(sys.modules, {
                        'gi': gi, 'gi.repository': repository, 'surface_xv': xv,
                        'surface_protocol': protocol, 'surface_power': client_module}):
                    receiver = load_module('tested_display_receiver', ROOT / 'surface-gud/display_receiver.py')
                    with patch('sys.stdout', output):
                        self.assertEqual(receiver.main(), 0)
                deadline = time.monotonic() + 1
                while server.granted and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertEqual(server_errors, [])
                self.assertEqual(len(server.granted), 0)
                self.assertLess(journal.index('admitted'), journal.index('presenter-created'))
                self.assertLess(journal.index('pipeline-stopped'), journal.index('presenter-closed'))
                self.assertNotIn('\"ack\"', output.getvalue())
                self.assertIn('\"ready\":true', output.getvalue())
            finally:
                finished.set()
                worker.join(timeout=2)
                server.close()

    def test_native_input_pipe_preserves_fragment_and_eof_is_a_failure(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        os.set_blocking(read_fd, False)
        device = power.PowerInput(read_fd)
        record = struct.pack('llHHi', 23, 500000, 1, 116, 1)
        try:
            os.write(write_fd, record[:7])
            self.assertEqual(device.drain(), [])
            os.write(write_fd, record[7:])
            events = device.drain()
            self.assertEqual([(event.kind, event.at) for event in events], [('down', 23.5)])
        finally:
            os.close(write_fd)
        with self.assertRaises(EOFError):
            device.drain()


if __name__ == '__main__':
    unittest.main()
