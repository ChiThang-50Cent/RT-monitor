import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('internal_efi', Path(__file__).resolve().parents[1] / 'appliance/install/internal-efi-pc.py')
efi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(efi)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


class InternalEfiTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.target = self.home / 'target'
        self.target.mkdir()
        self.bundle = self.home / 'bundle'
        self.bundle.mkdir()
        self.backup = self.home / 'backup'
        self.base = b'fixture frozen squashfs dependency'
        self.base_patch = patch.object(efi, 'BASE_SHA', sha(self.base))
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.identity = {'uuid': efi.UUID, 'serial': 'fixture-device', 'disk_size': 1000000000}
        for name in efi.LAYOUT:
            put(self.target, name, ('original Nura ' + name).encode())
        put(self.target, f'appliance/{efi.BASE_ID}/rootfs.squashfs', self.base)
        put(self.target, 'appliance/older/SHA256SUMS', b'old release remains')
        put(self.target, 'EFI/BOOT/BOOTARM.EFI', b'unknown but operator backed up firmware entry')
        put(self.target, 'startup.nsh', b'original startup')
        payload = {'extension.squashfs': b'extension', 'initramfs': b'checked initramfs', 'zImage': b'proven kernel', 'tegra30-microsoft-surface-rt-efi.dtb': b'proven dtb'}
        payload['release.conf'] = f'format=surface-extension-v1\nbase_id={efi.BASE_ID}\nbase_sha256={efi.BASE_SHA}\nextension_sha256={sha(payload["extension.squashfs"])}\n'.encode()
        shared = {name: ('proven ' + name).encode() for name in ('EFI/BOOT/BOOTARM.EFI', 'EFI/BOOT/bootshim-test.efi', 'EFI/BOOT/shell.efi', 'EFI/Microsoft/Boot/bootmgfw.efi', 'EFI/Microsoft/Boot/BCD', 'EFI/Microsoft/Boot/Fonts/segoe_slboot.ttf', 'EFI/Microsoft/Boot/Fonts/wgl4_boot.ttf')}
        shared['EFI/Microsoft/Boot/bootmgfw.efi'] = shared['EFI/BOOT/BOOTARM.EFI']
        identity = {'payload_sha256': {n: sha(b) for n, b in payload.items()}, 'shared_sha256': {n: sha(b) for n, b in shared.items()}}
        self.rid = sha(efi.canonical(identity))[:24]
        ns = f'appliance/{self.rid}/'
        contents = {ns + n: b for n, b in payload.items()}
        contents.update(shared)
        contents[ns + 'SHA256SUMS'] = ''.join(f'{sha(payload[name])}  {name}\n' for name in sorted(payload)).encode()
        contents['surface-appliance-internal.id'] = f'surface-internal-efi-v1 {self.rid}\n'.encode()
        cmdline = f'\\appliance\\{self.rid}\\zImage appliance.release={self.rid} efi=noruntime'
        contents['startup.nsh'] = ('if exist surface-appliance-internal.id then\n' + cmdline + '\n').encode()
        entries = []
        for name, data in contents.items():
            put(self.bundle, 'payload/' + name, data)
            order = 40 if name in ('EFI/BOOT/BOOTARM.EFI', 'EFI/Microsoft/Boot/bootmgfw.efi') else 30 if name == 'startup.nsh' else 10 if name.startswith(ns) else 20
            entries.append({'target': name, 'source': 'payload/' + name, 'sha256': sha(data), 'size': len(data), 'order': order})
        self.manifest = {'schema': 1, 'release_id': self.rid, 'identity': identity, 'boot_uuid': efi.UUID, 'filesystem': 'vfat', 'cmdline': cmdline, 'required_base': {'id': efi.BASE_ID, 'path': f'appliance/{efi.BASE_ID}/rootfs.squashfs', 'sha256': sha(self.base), 'size': len(self.base)}, 'files': entries}
        self.save_manifest()
        self.original = snapshot(self.target)

    def save_manifest(self):
        (self.bundle / 'manifest.json').write_bytes(efi.canonical(self.manifest))

    def transaction(self, checkpoint=None):
        return efi.FilesystemTransaction(self.target, self.backup, self.identity, checkpoint)

    def test_install_repeat_restore_repeat_preserves_original_files_and_absences(self):
        tx = self.transaction()
        tx.install(self.bundle)
        installed = snapshot(self.target)
        tx.install(self.bundle)
        self.assertEqual(snapshot(self.target), installed)
        for entry in self.manifest['files']:
            self.assertEqual(efi.digest(self.target / entry['target']), entry['sha256'])
        tx.restore()
        self.assertEqual(snapshot(self.target), self.original)
        tx.restore()
        self.assertEqual(snapshot(self.target), self.original)
        self.assertFalse((self.target / f'appliance/{self.rid}').exists())

    def test_corrupt_bundle_is_rejected_before_backup_or_target_write(self):
        entry = self.manifest['files'][0]
        (self.bundle / entry['source']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.transaction().install(self.bundle)
        self.assertEqual(snapshot(self.target), self.original)
        self.assertFalse(self.backup.exists())

    def test_missing_or_changed_base_refuses_install(self):
        base = self.target / self.manifest['required_base']['path']
        base.write_bytes(b'wrong base')
        before = snapshot(self.target)
        with self.assertRaises(ValueError):
            self.transaction().install(self.bundle)
        self.assertEqual(snapshot(self.target), before)
        self.assertFalse(self.backup.exists())

    def test_invalid_manifest_paths_case_collisions_and_activation_order_refuse(self):
        for mutation in ('traversal', 'absolute', 'duplicate', 'order', 'case'):
            with self.subTest(mutation=mutation):
                manifest = copy.deepcopy(self.manifest)
                if mutation == 'traversal':
                    manifest['files'][0]['target'] = '../outside'
                elif mutation == 'absolute':
                    manifest['files'][0]['source'] = '/etc/passwd'
                elif mutation == 'duplicate':
                    manifest['files'].append(copy.deepcopy(manifest['files'][0]))
                elif mutation == 'case':
                    entry = copy.deepcopy(manifest['files'][0])
                    entry['target'] = entry['target'].upper()
                    manifest['files'].append(entry)
                else:
                    manifest['files'][0]['order'] = 40
                (self.bundle / 'manifest.json').write_bytes(efi.canonical(manifest))
                with self.assertRaises(ValueError):
                    self.transaction().install(self.bundle)
                self.assertEqual(snapshot(self.target), self.original)

    def test_target_symlink_and_case_alias_refuse_without_writes(self):
        (self.target / 'EFI').rename(self.target / 'efi')
        before = snapshot(self.target)
        with self.assertRaises(ValueError):
            self.transaction().install(self.bundle)
        self.assertEqual(snapshot(self.target), before)
        (self.target / 'efi').rename(self.target / 'EFI')
        (self.target / 'EFI/BOOT/shell.efi').symlink_to(self.home / 'outside')
        with self.assertRaises(ValueError):
            self.transaction().install(self.bundle)
        self.assertFalse(self.backup.exists())

    def test_insufficient_cluster_capacity_refuses_before_backup(self):
        fs = types.SimpleNamespace(f_frsize=4096, f_bsize=4096, f_bavail=1)
        with patch.object(efi.os, 'statvfs', return_value=fs):
            with self.assertRaisesRegex(ValueError, 'insufficient capacity'):
                self.transaction().install(self.bundle)
        self.assertEqual(snapshot(self.target), self.original)
        self.assertFalse(self.backup.exists())

    def test_backup_on_target_refused(self):
        with self.assertRaises(ValueError):
            efi.FilesystemTransaction(self.target, self.target / 'backup', self.identity)

    def test_unknown_target_change_refuses_restore_before_any_change(self):
        tx = self.transaction()
        tx.install(self.bundle)
        (self.target / 'startup.nsh').write_bytes(b'operator changed startup')
        before = snapshot(self.target)
        with self.assertRaisesRegex(ValueError, 'unexpected target modification'):
            tx.restore()
        self.assertEqual(snapshot(self.target), before)

    def test_corrupt_backup_refuses_restore_before_any_change(self):
        tx = self.transaction()
        tx.install(self.bundle)
        (self.backup / 'files/startup.nsh').write_bytes(b'bad backup')
        before = snapshot(self.target)
        with self.assertRaisesRegex(ValueError, 'backup corruption'):
            tx.restore()
        self.assertEqual(snapshot(self.target), before)

    def test_corrupt_receipt_and_wrong_target_identity_refuse(self):
        tx = self.transaction()
        tx.install(self.bundle)
        before = snapshot(self.target)
        other = efi.FilesystemTransaction(self.target, self.backup, {'uuid': 'wrong'})
        with self.assertRaisesRegex(ValueError, 'different target'):
            other.restore()
        receipt = json.loads((self.backup / 'receipt.json').read_text())
        receipt['phase'] = 'restored'
        (self.backup / 'receipt.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'receipt checksum'):
            tx.restore()
        self.assertEqual(snapshot(self.target), before)

    def test_journaled_copy_and_rename_failures_resume_or_restore(self):
        for event in ('before-copy', 'after-sync', 'before-rename', 'after-rename', 'after-journal'):
            for recovery in ('resume', 'restore'):
                with self.subTest(event=event, recovery=recovery):
                    with tempfile.TemporaryDirectory(dir=self.home) as case:
                        root = Path(case)
                        target = root / 'target'
                        import shutil
                        shutil.copytree(self.target, target)
                        backup = root / 'backup'
                        fired = []
                        def interrupt(actual_event, name):
                            if actual_event == event and name is not None and not fired:
                                fired.append(name)
                                raise OSError('injected real transaction boundary failure')
                        tx = efi.FilesystemTransaction(target, backup, self.identity, interrupt)
                        with self.assertRaises(OSError):
                            tx.install(self.bundle)
                        self.assertTrue(fired)
                        recovered = efi.FilesystemTransaction(target, backup, self.identity)
                        if recovery == 'resume':
                            recovered.install(self.bundle)
                        recovered.restore()
                        self.assertEqual(snapshot(target), self.original)

    def test_interrupted_restore_converges(self):
        self.transaction().install(self.bundle)
        fired = []
        def interrupt(event, name):
            if event == 'after-rename' and not fired:
                fired.append(name)
                raise OSError('interrupted restore')
        with self.assertRaises(OSError):
            self.transaction(interrupt).restore()
        self.transaction().restore()
        self.assertEqual(snapshot(self.target), self.original)

    def test_partial_copy_error_is_journaled_and_restore_removes_staging(self):
        tx = self.transaction()
        tx.prepare(self.bundle, efi.load_bundle(self.bundle))
        def fail_copy(source, destination):
            destination.write(source.read(3))
            destination.flush()
            raise OSError('injected partial filesystem copy')
        with patch.object(efi.shutil, 'copyfileobj', side_effect=fail_copy):
            with self.assertRaises(OSError):
                tx.install(self.bundle)
        self.assertTrue(list(self.target.rglob('*.surface-txn')))
        self.transaction().restore()
        self.assertEqual(snapshot(self.target), self.original)
        self.assertFalse(list(self.target.rglob('*.surface-txn')))

    def test_protected_nura_change_refuses_before_restore(self):
        self.transaction().install(self.bundle)
        (self.target / 'vmlinuz').write_bytes(b'unknown replacement')
        before = snapshot(self.target)
        with self.assertRaisesRegex(ValueError, 'protected file changed'):
            self.transaction().restore()
        self.assertEqual(snapshot(self.target), before)


class ProductionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        partition = patch.object(efi, 'partition_number', return_value='1')
        self.partition_number = partition.start()
        self.addCleanup(partition.stop)

    def disk(self, **changes):
        partition = {'path': '/dev/fixture1', 'uuid': efi.UUID, 'fstype': 'vfat', 'type': 'part', 'partn': 1, 'size': 500000000, 'mountpoints': [None]}
        partition.update(changes)
        return {'path': '/dev/fixture', 'type': 'disk', 'tran': 'usb', 'size': 32000000000, 'serial': 'fixture-serial', 'children': [partition]}

    def test_discovery_rejects_missing_duplicate_wrong_partition_and_non_usb(self):
        good = self.disk()
        non_usb = self.disk()
        non_usb['tran'] = 'sata'
        for disks in ([], [good, copy.deepcopy(good)], [non_usb], [self.disk(partn=2)], [self.disk(fstype='ext4')], [self.disk(uuid='wrong')]):
            self.partition_number.return_value = str(disks[0]['children'][0]['partn']) if disks else '1'
            with self.subTest(disks=disks), patch.object(efi, 'run_json', return_value={'blockdevices': disks}):
                with self.assertRaises(ValueError):
                    efi.discover_target()

    def test_wrong_mount_association_never_yields(self):
        partition = self.disk()['children'][0]
        identity = {'serial': 'fixture'}
        with patch.object(efi, 'discover_target', return_value=(partition, identity)), patch.object(efi.subprocess, 'run'), patch.object(efi, 'run_json', return_value={'filesystems': [{'source': '/dev/wrong', 'target': '/wrong', 'fstype': 'vfat', 'options': 'rw'}]}):
            with self.assertRaisesRegex(ValueError, 'mount source'):
                with efi.mounted_target(True):
                    self.fail('must not yield wrong mount')


if __name__ == '__main__':
    unittest.main()
