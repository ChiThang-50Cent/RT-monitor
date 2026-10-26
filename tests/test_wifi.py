import hashlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


provision = load('provision_wifi', REPO / 'appliance/provision-wifi.py')
payload = load('package_payload', REPO / 'appliance/package_payload.py')
worker = load('surface_wifi', REPO / 'appliance/overlay/usr/sbin/surface-wifi')
checker = load('check_wifi_image', REPO / 'tests/check_wifi_image.py')


class WifiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_standard_configuration_derives_known_wpa_vector_without_plaintext(self):
        text = provision.configuration('IEEE', 'password')
        self.assertIn('ssid=49454545\n', text)
        self.assertIn('psk=f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e\n', text)
        self.assertNotIn('password', text)

    def test_hex_ssid_prevents_configuration_injection(self):
        text = provision.configuration('x"\n}\nnetwork={', 'a' * 64)
        self.assertEqual(text.count('network={'), 1)
        self.assertIn('psk=' + 'a' * 64, text)
        for ssid, secret in [('', 'password'), ('x' * 33, 'password'), ('x', 'short'), ('x', 'é' * 8)]:
            with self.assertRaises(ValueError):
                provision.configuration(ssid, secret)

    def test_atomic_output_is_private_and_does_not_follow_existing_symlink(self):
        victim = self.root / 'victim'
        victim.write_text('unchanged')
        output = self.root / 'private/config'
        output.parent.mkdir()
        output.symlink_to(victim)
        provision.write_configuration(output, provision.configuration('IEEE', 'password'))
        self.assertFalse(output.is_symlink())
        self.assertEqual(victim.read_text(), 'unchanged')
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with self.assertRaises(ValueError):
            provision.write_configuration(REPO / '.wifi-secret-test', 'must not be written')

    def test_runtime_copy_rejects_links_special_files_and_oversize_config(self):
        runtime = self.root / 'run'
        runtime.mkdir()
        source = self.root / 'source'
        source.write_text('standard config')
        worker.copy_config(source, runtime)
        self.assertEqual(stat.S_IMODE((runtime / 'wpa_supplicant.conf').stat().st_mode), 0o600)
        link = self.root / 'link'
        link.symlink_to(source)
        with self.assertRaises(OSError):
            worker.copy_config(link, runtime)
        source.write_bytes(b'x' * 65537)
        with self.assertRaises(ValueError):
            worker.copy_config(source, runtime)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            worker.copy_config(fifo, runtime)

    def test_cli_refuses_password_echo_fallback_without_a_terminal(self):
        output = self.root / 'private.conf'
        secret = 'not-for-terminal-output'
        result = subprocess.run([sys.executable, str(REPO / 'appliance/provision-wifi.py'), '--ssid', 'IEEE', '--output', str(output)], input=secret + '\n', capture_output=True, text=True, start_new_session=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_missing_secret_or_device_waits_only_in_worker_and_stops_cleanly(self):
        for configured in (False, True):
            runtime = self.root / ('configured' if configured else 'disabled')
            source = self.root / 'config'
            if configured:
                source.write_text(provision.configuration('IEEE', 'password'))
            code = ('import runpy, pathlib; m=runpy.run_path(' + repr(str(REPO / 'appliance/overlay/usr/sbin/surface-wifi')) + '); '
                    'g=m["run"].__globals__; g["RUNTIME"]=pathlib.Path(' + repr(str(runtime)) + '); '
                    'g["SOURCE"]=pathlib.Path(' + repr(str(source)) + '); '
                    'g["DEVICE"]=pathlib.Path(' + repr(str(self.root / 'absent-device')) + '); m["run"]()')
            process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not (runtime / 'lock').exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIsNone(process.poll())
                self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
                process.send_signal(signal.SIGTERM)
                out, err = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, err.decode())
                self.assertNotIn(b'psk=', out + err)
                self.assertFalse((runtime / 'wpa_supplicant.conf').exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_owned_children_are_reaped(self):
        children = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) for _ in range(2)]
        self.addCleanup(lambda: [p.kill() for p in children if p.poll() is None])
        worker.stop_children(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_exact_lock_ignores_poisoned_extra_cache_and_rejects_tampering(self):
        cache = self.root / 'cache'
        cache.mkdir()
        apk = cache / 'example-1-r0.apk'
        apk.write_bytes(b'locked bytes')
        lock = self.root / 'lock'
        lock.write_text('example 1-r0 ' + hashlib.sha256(apk.read_bytes()).hexdigest() + '\n')
        (cache / 'unrelated-99.apk').write_bytes(b'not a package')
        self.assertEqual(payload.verified_apk_paths(lock, cache), [apk])
        apk.write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            payload.verified_apk_paths(lock, cache)
        apk.unlink()
        with self.assertRaises(ValueError):
            payload.verified_apk_paths(lock, cache)

    def test_payload_rejects_traversal_modules_whiteouts_and_escaping_links(self):
        for name in ('/absolute', '../escape', 'usr/../escape', 'usr/.wh.secret', 'lib/modules/new.ko'):
            with self.assertRaises(ValueError):
                payload.safe_relative(name)
        source = self.root / 'source'
        source.mkdir()
        (source / 'escape').symlink_to('../../outside')
        with self.assertRaises(ValueError):
            payload.copy_payload_file(source, self.root / 'destination', 'escape')
        (source / 'loop').symlink_to('loop')
        with self.assertRaises(ValueError):
            payload.rooted_target(source, 'loop')

    def test_merged_extension_resolves_absolute_links_only_in_image(self):
        base = self.root / 'base'
        extension = self.root / 'extension'
        (base / 'etc/init.d').mkdir(parents=True)
        (base / 'etc/init.d/sshd').write_text('base service')
        (extension / 'etc/runlevels/default').mkdir(parents=True)
        (extension / 'etc/runlevels/default/sshd').symlink_to('/etc/init.d/sshd')
        checker.merge(extension, base)
        self.assertEqual(payload.rooted_target(base, 'etc/runlevels/default/sshd'), base / 'etc/init.d/sshd')


if __name__ == '__main__':
    unittest.main()
