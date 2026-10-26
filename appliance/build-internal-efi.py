#!/usr/bin/env python3
"""Build a checked one-level extension and immutable internal EFI bundle locally."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import package_payload

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('internal_efi_pc', HERE / 'install/internal-efi-pc.py')
efi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(efi)


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def build(out):
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    base = Path.home() / f'surface-releases/{efi.BASE_ID}/rootfs.squashfs'
    if efi.file_state(base) != efi.BASE_SHA:
        raise ValueError('frozen base checksum mismatch')
    proven = HERE.parent / 'nura/coldboot/nv-efi-kernel-probe'
    original = json.loads((proven / 'manifest.json').read_text())
    package = proven / 'package'
    for name, expected in original['output_sha256'].items():
        if efi.file_state(efi.safe_path(package, name)) != expected:
            raise ValueError('proven EFI source mismatch: ' + name)
    kernel = Path.home() / '.cache/surface-appliance/out/kernel'
    with tempfile.TemporaryDirectory(prefix='.internal-efi-', dir=out) as temporary:
        work = Path(temporary)
        overlay = work / 'overlay'
        inventory = package_payload.stage_extension_packages(base, overlay)
        for path in (HERE / 'overlay').rglob('*'):
            relative = path.relative_to(HERE / 'overlay')
            target = overlay / relative
            if '__pycache__' in relative.parts or path.suffix == '.pyc':
                continue
            if path.is_dir() and not path.is_symlink():
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    raise ValueError('source overlay directory collision: ' + str(relative))
                target.mkdir(parents=True, exist_ok=True)
            else:
                if target.exists() or target.is_symlink():
                    raise ValueError('source overlay collides with package: ' + str(relative))
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.is_symlink():
                    target.symlink_to(os.readlink(path))
                else:
                    shutil.copy2(path, target)
        if (overlay / 'lib/modules').exists():
            raise ValueError('extension must not contain modules')
        for path in overlay.rglob('*'):
            if not path.is_symlink() and not (path.is_file() or path.is_dir()):
                raise ValueError('extension contains special file: ' + str(path))
            if path.name.startswith('.wh.'):
                raise ValueError('extension whiteouts are not supported')
        modes = {'etc/doas.conf': 0o400, 'etc/shadow': 0o640, 'etc/ssh/sshd_config': 0o600, 'usr/sbin/surface-boot-select': 0o755, 'home/user': 0o755, 'home/user/display_receiver.py': 0o755}
        for name, mode in modes.items():
            path = overlay / name
            if path.exists():
                path.chmod(mode)
        for path in (overlay / 'etc/init.d').glob('surface-*'):
            path.chmod(0o755)
        for path in (overlay / 'home/user').glob('*.py'):
            if path.name != 'display_receiver.py':
                path.chmod(0o644)
        pseudo = work / 'ownership.pseudo'
        lines = []
        for path in sorted(overlay.rglob('*')):
            relative = path.relative_to(overlay).as_posix()
            if any(c.isspace() for c in relative):
                raise ValueError('unsupported pseudo filename')
            uid = 10000 if relative == 'home/user' or relative.startswith('home/user/') else 0
            name = '/' + relative
            lines.append(f'{name} m {stat.S_IMODE(path.lstat().st_mode):o} {uid} {uid}\n')
        pseudo.write_text(''.join(lines))
        extension = work / 'extension.squashfs'
        subprocess.run(['mksquashfs', str(overlay), str(extension), '-comp', 'gzip', '-root-uid', '0', '-root-gid', '0', '-root-mode', '755', '-pf', str(pseudo), '-noappend', '-no-progress', '-processors', '1', '-mkfs-time', '0', '-all-time', '0'], check=True)
        descriptor = work / 'release.conf'
        descriptor.write_text(f'format=surface-extension-v1\nbase_id={efi.BASE_ID}\nbase_sha256={efi.BASE_SHA}\nextension_sha256={efi.digest(extension)}\n')
        initout = work / 'init-build'
        initout.mkdir()
        (initout / 'kernel').symlink_to(kernel, target_is_directory=True)
        subprocess.run(['bash', str(HERE / 'build-initramfs.sh'), str(initout), str(descriptor)], check=True)
        artifact = work / 'bundle'
        artifact.mkdir()
        payloads = {'extension.squashfs': extension, 'release.conf': descriptor, 'initramfs': initout / 'initramfs', 'zImage': package / 'zImage', 'tegra30-microsoft-surface-rt-efi.dtb': package / 'tegra30-microsoft-surface-rt-efi.dtb'}
        shared = {name: package / name for name in original['output_sha256'] if name.startswith('EFI/')}
        shared['EFI/Microsoft/Boot/bootmgfw.efi'] = package / 'EFI/BOOT/BOOTARM.EFI'
        identity = {'format': 'surface-internal-efi-v1', 'base_id': efi.BASE_ID, 'base_sha256': efi.BASE_SHA, 'descriptor_sha256': efi.digest(descriptor), 'payload_sha256': {name: efi.digest(path) for name, path in payloads.items()}, 'shared_sha256': {name: efi.digest(path) for name, path in shared.items()}, 'source_manifest_sha256': efi.digest(proven / 'manifest.json'), 'packages_lock_sha256': efi.digest(HERE / 'packages.lock'), 'installer_sha256': efi.digest(HERE / 'install/internal-efi-pc.py'), 'builder_sha256': efi.digest(Path(__file__)), 'init_source_sha256': efi.digest(HERE / 'initramfs/init'), 'compression': 'gzip'}
        identity['package_payload_sha256'] = efi.digest(HERE / 'package_payload.py')
        identity['package_inventory_sha256'] = efi.digest(inventory)
        rid = hashlib.sha256(efi.canonical(identity)).hexdigest()[:24]
        namespace = f'appliance/{rid}'
        files = []
        def add(source, target, order):
            dest = artifact / 'payload' / target
            copy(source, dest)
            files.append({'source': 'payload/' + target, 'target': target, 'sha256': efi.digest(dest), 'size': dest.stat().st_size, 'order': order})
        for name, path in payloads.items():
            add(path, namespace + '/' + name, 10)
        checksums = work / 'SHA256SUMS'
        checksums.write_text(''.join(f'{efi.digest(path)}  {name}\n' for name, path in sorted(payloads.items())))
        add(checksums, namespace + '/SHA256SUMS', 10)
        for name, path in shared.items():
            add(path, name, 40 if name in ('EFI/BOOT/BOOTARM.EFI', 'EFI/Microsoft/Boot/bootmgfw.efi') else 20)
        marker = work / 'surface-appliance-internal.id'
        marker.write_text(f'surface-internal-efi-v1 {rid}\n')
        add(marker, marker.name, 20)
        proven_line = next(line for line in (package / 'startup.nsh').read_text().splitlines() if line.startswith('\\zImage '))
        args = proven_line.split()[3:]
        args = [f'appliance.release={rid}' if arg.startswith('appliance.release=') else arg for arg in args]
        prefix = '\\' + namespace.replace('/', '\\') + '\\'
        cmdline = prefix + 'zImage initrd=' + prefix + 'initramfs dtb=' + prefix + 'tegra30-microsoft-surface-rt-efi.dtb ' + ' '.join(args)
        startup = work / 'startup.nsh'
        startup.write_text('echo -off\necho SURFACE INTERNAL EFI APPLIANCE\nfor %a run (0 9)\n  if exist fs%a:\\surface-appliance-internal.id then\n    fs%a:\n    goto boot\n  endif\nendfor\necho ERROR: Internal volume not found.\ngoto end\n:boot\n' + cmdline + '\necho ERROR: Kernel returned. Status %lasterror%\n:end\n')
        add(startup, 'startup.nsh', 30)
        copy(package / 'initramfs', artifact / 'recovery/proven-initramfs')
        copy(proven / 'manifest.json', artifact / 'recovery/source-manifest.json')
        copy(HERE / 'install/internal-efi-pc.py', artifact / 'internal-efi-pc.py')
        copy(inventory, artifact / 'package-inventory.json')
        manifest = {'schema': 1, 'release_id': rid, 'identity': identity, 'boot_uuid': efi.UUID, 'filesystem': 'vfat', 'source_manifest_sha256': identity['source_manifest_sha256'], 'cmdline': cmdline, 'required_base': {'id': efi.BASE_ID, 'path': f'appliance/{efi.BASE_ID}/rootfs.squashfs', 'sha256': efi.BASE_SHA, 'size': base.stat().st_size}, 'files': sorted(files, key=lambda e: (e['order'], e['target']))}
        (artifact / 'manifest.json').write_bytes(efi.canonical(manifest) + b'\n')
        efi.load_bundle(artifact)
        final = out / rid
        if final.exists():
            if (final / 'manifest.json').read_bytes() != (artifact / 'manifest.json').read_bytes():
                raise ValueError('existing release ID has different content')
            efi.load_bundle(final)
            if efi.digest(final / 'package-inventory.json') != identity['package_inventory_sha256']:
                raise ValueError('existing package inventory checksum mismatch')
        else:
            for path in artifact.rglob('*'):
                if path.is_file():
                    with open(path, 'rb') as stream:
                        os.fsync(stream.fileno())
                    path.chmod(0o444)
            for directory, _, _ in os.walk(artifact, topdown=False):
                efi.sync_dir(directory)
            os.rename(artifact, final)
            efi.sync_dir(out)
        print(final)
        print('release_id=' + rid)
        return final


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    options = parser.parse_args()
    try:
        build(options.out)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'build-internal-efi: {exc}\n')
