#!/usr/bin/env python3
"""PC-side, recoverable internal EFI installation. No device selection overrides."""
import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys

BASE_ID = '6258b2a5368a'
BASE_SHA = '48e1fb2c68aade6b2ae28faa14482ed332f67bcd27a2a3dc27449d3eae13516f'
UUID = '17C0-8631'
RESERVE = 15 * 1024 * 1024
LAYOUT = ('extlinux/extlinux.conf', 'vmlinuz', 'initramfs', 'dtbs/tegra30-microsoft-surface-rt.dtb')


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('invalid FAT path')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) or re.search(r'[\x00-\x1f<>:"|?*]', p) for p in parts):
        raise ValueError('invalid FAT path: ' + value)
    return value


def safe_path(root, name):
    relative(name)
    current = Path(root)
    for component in name.split('/'):
        if current.exists():
            matches = [p.name for p in current.iterdir() if p.name.casefold() == component.casefold()]
            if matches and matches != [component]:
                raise ValueError('FAT case collision: ' + name)
        current = current / component
        if current.is_symlink():
            raise ValueError('symlink refused: ' + str(current))
    return current


def file_state(path):
    if path.is_symlink():
        raise ValueError('symlink refused: ' + str(path))
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('not a regular file: ' + str(path))
    return digest(path)


def check_hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid SHA256')


def validate_manifest(manifest):
    if manifest['schema'] != 1 or manifest['boot_uuid'] != UUID or manifest['filesystem'] != 'vfat':
        raise ValueError('unsupported bundle or target')
    rid = manifest['release_id']
    if not re.fullmatch('[0-9a-f]{24}', rid) or hashlib.sha256(canonical(manifest['identity'])).hexdigest()[:24] != rid:
        raise ValueError('release identity mismatch')
    base = manifest['required_base']
    if base['id'] != BASE_ID or base['path'] != f'appliance/{BASE_ID}/rootfs.squashfs' or base['sha256'] != BASE_SHA:
        raise ValueError('not the frozen base')
    if type(base['size']) is not int or base['size'] <= 0:
        raise ValueError('invalid base size')
    names = set()
    sources = set()
    entries = manifest['files']
    if not entries:
        raise ValueError('empty bundle')
    for entry in entries:
        name = relative(entry['target'])
        source = relative(entry['source'])
        folded = name.casefold()
        if folded in names or source.casefold() in sources:
            raise ValueError('duplicate FAT destination or source')
        names.add(folded)
        sources.add(source.casefold())
        check_hash(entry['sha256'])
        if type(entry['size']) is not int or entry['size'] < 0 or entry['order'] not in (10, 20, 30, 40):
            raise ValueError('invalid file size or activation order')
        namespace = f'appliance/{rid}/'
        allowed_shared = {'startup.nsh', 'surface-appliance-internal.id', 'EFI/BOOT/BOOTARM.EFI', 'EFI/BOOT/bootshim-test.efi', 'EFI/BOOT/shell.efi', 'EFI/Microsoft/Boot/bootmgfw.efi', 'EFI/Microsoft/Boot/BCD', 'EFI/Microsoft/Boot/Fonts/segoe_slboot.ttf', 'EFI/Microsoft/Boot/Fonts/wgl4_boot.ttf'}
        if not name.startswith(namespace) and name not in allowed_shared:
            raise ValueError('destination outside release contract: ' + name)
        expected_order = 40 if name in ('EFI/BOOT/BOOTARM.EFI', 'EFI/Microsoft/Boot/bootmgfw.efi') else 30 if name == 'startup.nsh' else 10 if name.startswith(namespace) else 20
        if entry['order'] != expected_order:
            raise ValueError('invalid activation order')
    for name in names:
        if any(other.startswith(name + '/') for other in names):
            raise ValueError('file/directory collision')
    return manifest


def load_bundle(root):
    root = Path(root).resolve()
    manifest = validate_manifest(json.loads(safe_path(root, 'manifest.json').read_text()))
    for entry in manifest['files']:
        path = safe_path(root, entry['source'])
        if file_state(path) != entry['sha256'] or path.stat().st_size != entry['size']:
            raise ValueError('bundle payload mismatch: ' + entry['source'])
    rid = manifest['release_id']
    namespace = f'appliance/{rid}/'
    identity = manifest['identity']
    if set(identity['payload_sha256']) != {'extension.squashfs', 'release.conf', 'initramfs', 'zImage', 'tegra30-microsoft-surface-rt-efi.dtb'}:
        raise ValueError('incomplete namespaced boot payload')
    shared_names = {'EFI/BOOT/BOOTARM.EFI', 'EFI/BOOT/bootshim-test.efi', 'EFI/BOOT/shell.efi', 'EFI/Microsoft/Boot/bootmgfw.efi', 'EFI/Microsoft/Boot/BCD', 'EFI/Microsoft/Boot/Fonts/segoe_slboot.ttf', 'EFI/Microsoft/Boot/Fonts/wgl4_boot.ttf'}
    if set(identity['shared_sha256']) != shared_names:
        raise ValueError('incomplete proven EFI chain')
    actual = {entry['target']: entry['sha256'] for entry in manifest['files']}
    expected = {namespace + name: sha for name, sha in identity['payload_sha256'].items()}
    expected.update(identity['shared_sha256'])
    expected_names = set(expected) | {namespace + 'SHA256SUMS', 'startup.nsh', 'surface-appliance-internal.id'}
    if set(actual) != expected_names or any(actual[name] != sha for name, sha in expected.items()):
        raise ValueError('payload does not match release identity')
    by_name = {entry['target']: safe_path(root, entry['source']) for entry in manifest['files']}
    descriptor = (f'format=surface-extension-v1\nbase_id={BASE_ID}\nbase_sha256={BASE_SHA}\n'
                  f"extension_sha256={actual[namespace + 'extension.squashfs']}\n")
    if by_name[namespace + 'release.conf'].read_text() != descriptor:
        raise ValueError('descriptor does not bind frozen base and extension')
    if by_name['surface-appliance-internal.id'].read_text() != f'surface-internal-efi-v1 {rid}\n':
        raise ValueError('internal marker identity mismatch')
    checksums = ''.join(f"{identity['payload_sha256'][name]}  {name}\n" for name in sorted(identity['payload_sha256']))
    if by_name[namespace + 'SHA256SUMS'].read_text() != checksums:
        raise ValueError('release checksums mismatch')
    if actual['EFI/BOOT/BOOTARM.EFI'] != actual['EFI/Microsoft/Boot/bootmgfw.efi']:
        raise ValueError('WBM entry points differ')
    startup = by_name['startup.nsh'].read_text()
    if manifest['cmdline'] not in startup.splitlines() or 'surface-appliance-usb.id' in startup:
        raise ValueError('startup does not select internal release')
    return manifest


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def durable_json(path, value):
    temporary = path.with_name(path.name + '.new')
    with open(temporary, 'wb') as stream:
        stream.write(canonical(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_dir(path.parent)


class FilesystemTransaction:
    def __init__(self, target, backup, identity, checkpoint=None):
        self.root = Path(target).resolve()
        self.backup = Path(backup).resolve()
        if self.backup == self.root or self.root in self.backup.parents:
            raise ValueError('backup must be outside target')
        self.identity = identity
        self.checkpoint = checkpoint or (lambda event, name: None)
        self.receipt_path = self.backup / 'receipt.json'

    def protected(self, entries):
        affected = {e['target'] for e in entries}
        result = {}
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for item in dirs + files:
                path = Path(directory) / item
                if path.is_symlink():
                    raise ValueError('target symlink refused')
            for item in files:
                path = Path(directory) / item
                name = path.relative_to(self.root).as_posix()
                if name not in affected:
                    result[name] = file_state(path)
        return result

    def verify_protected(self, receipt):
        for name, expected in receipt['protected'].items():
            if file_state(safe_path(self.root, name)) != expected:
                raise ValueError('protected file changed: ' + name)

    def capacity(self, manifest):
        info = os.statvfs(self.root)
        cluster = info.f_frsize or info.f_bsize
        rounded = lambda size: ((size + cluster - 1) // cluster) * cluster
        demand = sum(rounded(e['size']) for e in manifest['files'])
        demand += max(rounded(e['size']) for e in manifest['files'])
        directories = {str(PurePosixPath(e['target']).parent) for e in manifest['files']}
        demand += (len(directories) + len(manifest['files']) + 8) * cluster
        demand += RESERVE
        if info.f_bavail * cluster < demand:
            raise ValueError(f'insufficient capacity: need {demand}, available {info.f_bavail * cluster}')
        return demand

    def preflight(self, manifest):
        for name in LAYOUT:
            if file_state(safe_path(self.root, name)) is None:
                raise ValueError('missing Nura layout: ' + name)
        base = manifest['required_base']
        path = safe_path(self.root, base['path'])
        if file_state(path) != base['sha256'] or path.stat().st_size != base['size']:
            raise ValueError('frozen base mismatch')
        for entry in manifest['files']:
            path = safe_path(self.root, entry['target'])
            prior = file_state(path)
            if entry['target'].startswith(f"appliance/{manifest['release_id']}/") and prior is not None:
                raise ValueError('unreceipted release already exists')
            temp = path.with_name(path.name + '.surface-txn')
            if temp.exists() or temp.is_symlink():
                raise ValueError('unreceipted staging file')
        return self.capacity(manifest)

    def save(self, receipt):
        self.checkpoint('before-journal', receipt.get('pending'))
        receipt['receipt_sha256'] = hashlib.sha256(canonical({k: v for k, v in receipt.items() if k != 'receipt_sha256'})).hexdigest()
        durable_json(self.receipt_path, receipt)
        self.checkpoint('after-journal', receipt.get('pending'))

    def read_receipt(self):
        receipt = json.loads(self.receipt_path.read_text())
        receipt_digest = receipt.pop('receipt_sha256', None)
        if receipt_digest != hashlib.sha256(canonical(receipt)).hexdigest():
            raise ValueError('backup receipt checksum mismatch')
        validate_manifest(receipt['manifest'])
        if receipt['identity'] != self.identity:
            raise ValueError('backup belongs to a different target')
        expected_names = {e['target'] for e in receipt['manifest']['files']}
        if set(receipt['before']) != expected_names:
            raise ValueError('invalid backup path map')
        for name, before in receipt['before'].items():
            relative(name)
            if before is not None:
                check_hash(before)
                if file_state(safe_path(self.backup / 'files', name)) != before:
                    raise ValueError('backup corruption: ' + name)
        for name in receipt['created_dirs']:
            relative(name)
        self.verify_protected(receipt)
        self.reconcile(receipt)
        return receipt

    def reconcile(self, receipt):
        for entry in receipt['manifest']['files']:
            name = entry['target']
            path = safe_path(self.root, name)
            state = file_state(path)
            if state not in (receipt['before'][name], entry['sha256']):
                raise ValueError('unexpected target modification: ' + name)
            temp = path.with_name(path.name + '.surface-txn')
            if temp.exists() or temp.is_symlink():
                if receipt.get('pending') != name or temp.is_symlink() or not temp.is_file():
                    raise ValueError('unowned staging file: ' + name)

    def prepare(self, bundle, manifest):
        self.preflight(manifest)
        if self.backup.exists():
            raise ValueError('backup directory already exists without a receipt')
        existing_parent = self.backup.parent
        while not existing_parent.exists():
            existing_parent = existing_parent.parent
        self.backup.mkdir(parents=True, mode=0o700)
        directory = self.backup.parent
        while directory != existing_parent:
            sync_dir(directory)
            directory = directory.parent
        sync_dir(existing_parent)
        before = {}
        directories = set()
        for entry in manifest['files']:
            name = entry['target']
            path = safe_path(self.root, name)
            before[name] = file_state(path)
            parent = path.parent
            while parent != self.root:
                if not parent.exists():
                    directories.add(parent.relative_to(self.root).as_posix())
                parent = parent.parent
            if before[name] is not None:
                saved = self.backup / 'files' / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'rb') as src, open(saved, 'xb') as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                if digest(saved) != before[name] or file_state(path) != before[name]:
                    raise ValueError('target changed during backup')
                sync_dir(saved.parent)
        receipt = {'schema': 1, 'identity': self.identity, 'manifest': manifest, 'before': before, 'protected': self.protected(manifest['files']), 'created_dirs': sorted(directories), 'phase': 'installing', 'pending': None}
        for directory, _, _ in os.walk(self.backup, topdown=False):
            sync_dir(directory)
        self.save(receipt)
        sync_dir(self.backup.parent)
        return receipt

    def replace(self, receipt, name, source, expected):
        path = safe_path(self.root, name)
        if file_state(path) == expected:
            return
        receipt['pending'] = name
        self.save(receipt)
        self.reconcile(receipt)
        path.parent.mkdir(parents=True, exist_ok=True)
        sync_dir(path.parent.parent)
        temporary = path.with_name(path.name + '.surface-txn')
        if source is None:
            self.checkpoint('before-unlink', name)
            path.unlink(missing_ok=True)
            self.checkpoint('after-unlink', name)
        else:
            self.checkpoint('before-copy', name)
            with open(source, 'rb') as src, open(temporary, 'wb') as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            self.checkpoint('after-sync', name)
            if digest(temporary) != expected:
                raise ValueError('staged checksum mismatch: ' + name)
            self.checkpoint('before-rename', name)
            os.replace(temporary, path)
            self.checkpoint('after-rename', name)
        sync_dir(path.parent)
        receipt['pending'] = None
        self.save(receipt)

    def clear_pending(self, receipt):
        if receipt.get('pending'):
            path = safe_path(self.root, receipt['pending'])
            temp = path.with_name(path.name + '.surface-txn')
            if temp.exists():
                temp.unlink()
                sync_dir(temp.parent)
            receipt['pending'] = None
            self.save(receipt)

    def install(self, bundle):
        bundle = Path(bundle).resolve()
        manifest = load_bundle(bundle)
        receipt = self.read_receipt() if self.receipt_path.exists() else self.prepare(bundle, manifest)
        if receipt['manifest'] != manifest:
            raise ValueError('backup is for a different bundle')
        if receipt['phase'] in ('restoring', 'restored'):
            raise ValueError('restored transaction cannot be reactivated; use a new backup')
        self.clear_pending(receipt)
        for entry in sorted(manifest['files'], key=lambda e: (e['order'], e['target'])):
            self.replace(receipt, entry['target'], safe_path(bundle, entry['source']), entry['sha256'])
        self.verify_protected(receipt)
        for entry in manifest['files']:
            if file_state(safe_path(self.root, entry['target'])) != entry['sha256']:
                raise ValueError('installed verification failed')
        receipt['phase'] = 'installed'
        self.save(receipt)
        return manifest['release_id']

    def restore(self):
        receipt = self.read_receipt()
        self.clear_pending(receipt)
        receipt['phase'] = 'restoring'
        self.save(receipt)
        for entry in sorted(receipt['manifest']['files'], key=lambda e: (-e['order'], e['target'])):
            name = entry['target']
            before = receipt['before'][name]
            source = self.backup / 'files' / name if before is not None else None
            self.replace(receipt, name, source, before)
        for name in sorted(receipt['created_dirs'], key=lambda n: (-n.count('/'), n)):
            path = safe_path(self.root, name)
            if path.exists():
                try:
                    path.rmdir()
                except OSError as exc:
                    if exc.errno != errno.ENOTEMPTY:
                        raise
                sync_dir(path.parent)
        self.verify_protected(receipt)
        for name, before in receipt['before'].items():
            if file_state(safe_path(self.root, name)) != before:
                raise ValueError('restore verification failed: ' + name)
        receipt['phase'] = 'restored'
        self.save(receipt)


def run_json(command):
    return json.loads(subprocess.check_output(command, text=True))


def partition_number(device):
    return (Path('/sys/class/block') / Path(device).name / 'partition').read_text().strip()


def discover_target():
    data = run_json(['lsblk', '--json', '--tree', '--bytes', '--paths', '--output', 'PATH,UUID,FSTYPE,TYPE,TRAN,SIZE,SERIAL,WWN,MOUNTPOINTS'])
    matches = []
    def visit(node, disk=None):
        if node.get('type') == 'disk':
            disk = node
        if node.get('uuid') == UUID:
            matches.append((node, disk))
        for child in node.get('children', []):
            visit(child, disk)
    for node in data['blockdevices']:
        visit(node)
    if len(matches) != 1:
        raise ValueError('expected exactly one internal UUID on USB UMS')
    part, disk = matches[0]
    if disk is None or disk.get('tran') != 'usb' or part.get('fstype') != 'vfat' or part.get('type') != 'part' or partition_number(part['path']) != '1':
        raise ValueError('target is not USB UMS vfat partition 1')
    identity = {'uuid': UUID, 'disk_size': disk['size'], 'partition_size': part['size'], 'serial': disk.get('serial'), 'wwn': disk.get('wwn')}
    return part, identity


@contextlib.contextmanager
def mounted_target(writable):
    part, identity = discover_target()
    device = part['path']
    existing = [p for p in part.get('mountpoints', []) if p]
    if len(existing) > 1:
        raise ValueError('target has multiple mounts')
    if existing:
        subprocess.run(['udisksctl', 'unmount', '-b', device, '--no-user-interaction'], check=True)
        part, current_id = discover_target()
        if part['path'] != device or current_id != identity or any(part.get('mountpoints', [])):
            raise ValueError('target identity or mount changed during unmount')
    subprocess.run(['udisksctl', 'mount', '-b', device, '--options', 'rw' if writable else 'ro', '--no-user-interaction'], check=True)
    try:
        current, current_id = discover_target()
        if current['path'] != device or current_id != identity:
            raise ValueError('target identity changed during mount')
        mounts = run_json(['findmnt', '--json', '--source', device, '--output', 'SOURCE,TARGET,FSTYPE,OPTIONS'])['filesystems']
        if len(mounts) != 1 or mounts[0]['fstype'] != 'vfat' or os.path.realpath(mounts[0]['source']) != os.path.realpath(device):
            raise ValueError('mount source association mismatch')
        mode = 'rw' if writable else 'ro'
        if mode not in mounts[0]['options'].split(','):
            raise ValueError('wrong mount access mode')
        root = Path(mounts[0]['target']).resolve()
        if [p for p in current.get('mountpoints', []) if p] != [str(root)]:
            raise ValueError('ambiguous target mounts')
        yield root, identity
        if writable:
            subprocess.run(['sync', '-f', str(root)], check=True)
    finally:
        subprocess.run(['udisksctl', 'unmount', '-b', device, '--no-user-interaction'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--bundle', required=True, type=Path)
    install = commands.add_parser('install')
    install.add_argument('--bundle', required=True, type=Path)
    install.add_argument('--backup', required=True, type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('--backup', required=True, type=Path)
    args = parser.parse_args()
    if args.command != 'restore':
        manifest = load_bundle(args.bundle)
    lockdir = Path.home() / '.cache/surface-appliance'
    lockdir.mkdir(parents=True, exist_ok=True)
    with open(lockdir / 'internal-efi.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mounted_target(args.command != 'inspect') as (root, identity):
            backup = args.backup if args.command != 'inspect' else lockdir / 'unused-inspect-backup'
            if args.command != 'inspect':
                ancestor = backup.resolve()
                while not ancestor.exists():
                    ancestor = ancestor.parent
                if ancestor.stat().st_dev == root.stat().st_dev:
                    raise ValueError('backup must be on the host, not target filesystem')
            transaction = FilesystemTransaction(root, backup, identity)
            if args.command == 'inspect':
                demand = transaction.preflight(manifest)
                print(json.dumps({'target': str(root), 'identity': identity, 'release_id': manifest['release_id'], 'required_peak_bytes_with_reserve': demand}, indent=2))
            elif args.command == 'install':
                transaction.install(args.bundle)
            else:
                transaction.restore()
        print('Verified and unmounted. Safe to exit UMS.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print('internal-efi-pc: ' + str(exc), file=sys.stderr)
        sys.exit(1)
