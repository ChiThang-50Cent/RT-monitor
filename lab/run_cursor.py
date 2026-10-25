#!/usr/bin/python3
"""Exercise current cursor sources in RAM and restore the baseline sender."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'surface-gud'
BOOTSTRAP = r'''
import os, sys, json, types, importlib.abc, importlib.util

def take(size):
    parts = bytearray()
    while len(parts) < size:
        part = os.read(0, size - len(parts))
        if not part:
            raise EOFError('source bundle truncated')
        parts.extend(part)
    return bytes(parts)

sources = json.loads(take(int.from_bytes(take(4), 'big')))
class Sources(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name in sources:
            return importlib.util.spec_from_loader(name, self)
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        module.__file__ = '/home/user/' + module.__name__ + '.py'
        exec(compile(sources[module.__name__], module.__file__, 'exec'), module.__dict__)
sys.meta_path.insert(0, Sources())
if len(sys.argv) == 1:
    import surface_cursor
    original = surface_cursor.CursorRenderer.apply
    def observed(self, state):
        original(self, state)
        shape = state.shape
        print('cursor smoke ' + json.dumps({'x': state.x, 'y': state.y,
              'shape': None if shape is None else [shape.width, shape.height, shape.hotspot_x, shape.hotspot_y]}),
              file=sys.stderr, flush=True)
    surface_cursor.CursorRenderer.apply = observed
exec(compile(sources['display_receiver'], '/home/user/display_receiver.py', 'exec'),
     {'__name__': '__main__', '__file__': '/home/user/display_receiver.py'})
'''


def run_sender():
    sys.path.insert(0, str(SOURCE))
    import display_sender
    names = ('surface_protocol', 'surface_power', 'surface_cursor', 'surface_xv', 'display_receiver')
    body = json.dumps({name: (SOURCE / (name + '.py')).read_text() for name in names}).encode()
    bundle = len(body).to_bytes(4, 'big') + body
    real_popen = subprocess.Popen

    def launch(command, *args, **kwargs):
        if (isinstance(command, list) and command[0] == 'ssh' and
                '/home/user/display_receiver.py' in command[-1]):
            command = command.copy()
            prefix, suffix = command[-1].split('exec /usr/bin/python3 /home/user/display_receiver.py', 1)
            command[-1] = prefix + 'exec /usr/bin/python3 -c ' + shlex.quote(BOOTSTRAP) + suffix
            child = real_popen(command, *args, **kwargs)
            view = memoryview(bundle)
            try:
                while view:
                    count = child.stdin.write(view)
                    if not count:
                        raise BrokenPipeError('receiver source input closed')
                    view = view[count:]
                child.stdin.flush()
            except Exception:
                child.terminate()
                child.wait(timeout=10)
                raise
            return child
        return real_popen(command, *args, **kwargs)

    subprocess.Popen = launch
    sys.argv = [str(SOURCE / 'display_sender.py')]
    return display_sender.main()


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def children(pid):
    result = set()
    for path in Path(f'/proc/{pid}/task').glob('*/children'):
        try:
            result.update(int(value) for value in path.read_text().split())
        except FileNotFoundError:
            pass
    return result


def command(pid):
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode()
    except FileNotFoundError:
        return ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sender', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--hold', type=int, default=0, metavar='SECONDS',
                        help='keep the test display running for manual observation')
    args = parser.parse_args()
    if args.sender:
        return run_sender()
    import gi
    gi.require_version('Gio', '2.0')
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(destination, path, interface, method, signature=None, values=None, result=None):
        return bus.call_sync(destination, path, interface, method,
                             GLib.Variant(signature, values) if signature else None,
                             GLib.VariantType.new(result) if result else None,
                             Gio.DBusCallFlags.NONE, 5000, None)

    def monitors():
        state = call('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                     'org.gnome.Mutter.DisplayConfig', 'GetCurrentState').unpack()
        return {monitor[0][0] for monitor in state[1]}

    service = ['systemctl', '--user']
    pid = int(subprocess.check_output(service + ['show', 'surface-display.service', '-p', 'MainPID', '--value']))
    if not pid:
        raise RuntimeError('surface-display.service must be running before the smoke run')
    command_line = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    running = next(Path(os.fsdecode(arg)).resolve() for arg in command_line
                   if arg.endswith(b'/display_sender.py'))
    names = ('display_sender.py', 'surface_protocol.py', 'surface_cursor_capture.py')
    if running.parent != ROOT / 'surface-gud':
        baseline = {name: (running.parent / name).read_bytes() for name in names
                    if (running.parent / name).is_file()}
    else:
        baseline = {}
        for name in names:
            source = 'HEAD:surface-gud/' + name
            if subprocess.run(['git', 'cat-file', '-e', source], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                baseline[name] = subprocess.check_output(['git', 'show', source], cwd=ROOT)
    out = Path('/tmp/surface-latency-results') / (time.strftime('%Y%m%d-%H%M%S') + '-cursor')
    out.mkdir(parents=True)
    ready = threading.Event()
    lines = []
    child = relay = rd_path = None
    recovery = out / 'baseline'
    recovery.mkdir()
    for name, content in baseline.items():
        (recovery / name).write_bytes(content)
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
    override = runtime / 'systemd/user/surface-display.service.d' / ('90-cursor-smoke-' + str(os.getpid()) + '.conf')
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text('[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' +
                        str(recovery / 'display_sender.py') + ' --known-hosts ' +
                        str(ROOT / 'surface-known-hosts') + '\n')
    stopped = False
    timer = 'surface-cursor-recover-' + str(os.getpid())
    timer_started = False
    result = 1
    try:
        subprocess.run(service + ['daemon-reload'], check=True, timeout=15)
        subprocess.run(['systemd-run', '--user', '--collect', '--unit=' + timer,
                        '--on-active=' + str(120 + max(0, args.hold)) + 's',
                        '/usr/bin/systemctl', '--user', 'start',
                        'surface-display.service'], check=True, timeout=15)
        timer_started = True
        subprocess.run(service + ['stop', 'surface-display.service'], check=True, timeout=30)
        stopped = True
        before = monitors()
        child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).resolve()), '--sender'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)

        def collect():
            with (out / 'sender.log').open('w') as log:
                for line in child.stderr:
                    lines.append(line.rstrip())
                    log.write(line)
                    log.flush()
                    if 'receiver batch ' in line:
                        ready.set()
                ready.set()

        relay = threading.Thread(target=collect)
        relay.start()
        if not ready.wait(25) or not any('receiver batch ' in line for line in lines):
            raise RuntimeError('new receiver did not present 100 frames; see ' + str(out / 'sender.log'))
        added = monitors() - before
        if len(added) != 1:
            raise RuntimeError(f'expected one diagnostic monitor, got {added}')
        connector = added.pop()
        rd = 'org.gnome.Mutter.RemoteDesktop'
        sc = 'org.gnome.Mutter.ScreenCast'
        rd_path, = call(rd, '/org/gnome/Mutter/RemoteDesktop', rd, 'CreateSession', result='(o)').unpack()
        session_id, = call(rd, rd_path, 'org.freedesktop.DBus.Properties', 'Get', '(ss)',
                           (rd + '.Session', 'SessionId'), '(v)').unpack()
        sc_path, = call(sc, '/org/gnome/Mutter/ScreenCast', sc, 'CreateSession', '(a{sv})',
                        ({'remote-desktop-session-id': GLib.Variant('s', session_id)},), '(o)').unpack()
        stream, = call(sc, sc_path, sc + '.Session', 'RecordMonitor', '(sa{sv})',
                       (connector, {'cursor-mode': GLib.Variant('u', 0)}), '(o)').unpack()
        primary = next(name for name in before if not name.startswith('Meta-'))
        primary_stream, = call(sc, sc_path, sc + '.Session', 'RecordMonitor', '(sa{sv})',
                               (primary, {'cursor-mode': GLib.Variant('u', 0)}), '(o)').unpack()
        call(rd, rd_path, rd + '.Session', 'Start')

        def move(target, x, y):
            call(rd, rd_path, rd + '.Session', 'NotifyPointerMotionAbsolute', '(sdd)', (target, x, y))

        move(stream, 100., 100.)
        time.sleep(.3)
        for index in range(90):
            move(stream, 100. + index * 12., 100. + index * 5.)
            time.sleep(1 / 60)
        move(stream, 1200., 700.)
        time.sleep(.5)
        visible = [json.loads(line.removeprefix('cursor smoke ')) for line in lines if line.startswith('cursor smoke ')]
        if not any(row['x'] == 1200 and row['y'] == 700 and row['shape'] is not None for row in visible):
            raise RuntimeError('final injected cursor position was not rendered')
        move(primary_stream, 500., 400.)
        time.sleep(.5)
        states = [json.loads(line.removeprefix('cursor smoke ')) for line in lines if line.startswith('cursor smoke ')]
        if not states or states[-1]['shape'] is not None:
            raise RuntimeError('cursor did not hide after leaving diagnostic monitor')
        move(stream, 200., 300.)
        time.sleep(.5)
        cursor_pid = next((entry for entry in children(child.pid) if '--cursor-channel ' in command(entry)), None)
        if cursor_pid is None:
            raise RuntimeError('dedicated cursor SSH process not found')
        os.kill(cursor_pid, signal.SIGTERM)
        count = sum('receiver batch ' in line for line in lines)
        deadline = time.monotonic() + 15
        while sum('receiver batch ' in line for line in lines) <= count and time.monotonic() < deadline:
            time.sleep(.1)
        move(stream, 600., 400.)
        time.sleep(.5)
        states = [json.loads(line.removeprefix('cursor smoke ')) for line in lines if line.startswith('cursor smoke ')]
        if not states or (states[-1]['x'], states[-1]['y']) != (600, 400) or states[-1]['shape'] is None:
            raise RuntimeError('cursor did not recover after cursor-channel exit')
        if sum('receiver batch ' in line for line in lines) <= count:
            raise RuntimeError('video did not resume after cursor-channel exit')
        move(primary_stream, 500., 400.)
        print(json.dumps({'result': 'PASS', 'checks': ['visible video', 'captured motion', 'leave hides',
              'reentry', 'cursor-channel exit and reconnect'], 'optical_scanout': 'not verified',
              'output': str(out)}), flush=True)
        if args.hold > 0:
            move(stream, 600., 400.)
            print('VISUAL CHECK READY: move your mouse onto the Surface for ' +
                  str(args.hold) + ' seconds.', flush=True)
            time.sleep(args.hold)
            move(primary_stream, 500., 400.)
        result = 0
    finally:
        if rd_path is not None:
            try:
                call('org.gnome.Mutter.RemoteDesktop', rd_path, 'org.gnome.Mutter.RemoteDesktop.Session', 'Stop')
            except GLib.Error as error:
                print('input-session cleanup: ' + str(error), file=sys.stderr)
        if child is not None:
            stop(child)
        if relay is not None:
            relay.join(timeout=5)
        restored = 1
        if stopped:
            restored = subprocess.run(service + ['start', 'surface-display.service'], timeout=30).returncode
        if restored == 0:
            if timer_started:
                subprocess.run(service + ['stop', timer + '.timer'], check=True, timeout=15)
            override.unlink()
            subprocess.run(service + ['daemon-reload'], check=True, timeout=15)
        print(json.dumps({'baseline_service_restore_returncode': restored, 'output': str(out)}), flush=True)
    return result


if __name__ == '__main__':
    sys.exit(main())
