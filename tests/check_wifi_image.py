#!/usr/bin/env python3
"""Check Wi-Fi payload bytes and execute ARM utilities in the actual image."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'appliance'))
import package_payload as payload


def require(condition, message):
    if not condition:
        raise ValueError(message)


def extract(image, root):
    subprocess.run(['unsquashfs', '-no-progress', '-d', str(root), str(image)], check=True)


def merge(extension, root):
    for path in sorted(extension.rglob('*')):
        name = path.relative_to(extension).as_posix()
        payload.safe_relative(name)
        dest = root / name
        for parent in Path(name).parents:
            if str(parent) != '.':
                require(not (root / parent).is_symlink(), 'extension writes through a symlink')
        if path.is_dir() and not path.is_symlink():
            require(not dest.is_symlink() and (not dest.exists() or dest.is_dir()), 'extension directory collision')
            dest.mkdir(parents=True, exist_ok=True)
        else:
            if dest.exists() or dest.is_symlink():
                require(not dest.is_dir() or dest.is_symlink(), 'extension file collision')
                dest.unlink()
            if path.is_symlink():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(os.readlink(path))
            else:
                payload.copy_payload_file(extension, root, name)
    for path in extension.rglob('*'):
        if path.is_symlink():
            payload.rooted_target(root, path.relative_to(extension).as_posix())


def elf_closure(root, binary):
    pending = [binary]
    visited = set()
    while pending:
        name = pending.pop()
        target = payload.rooted_target(root, name)
        relative = target.relative_to(root).as_posix()
        if relative in visited:
            continue
        visited.add(relative)
        info = subprocess.run(['readelf', '-hld', str(target)], check=True, capture_output=True, text=True).stdout
        require(re.search(r'Machine:\s+ARM\b', info) is not None, 'non-ARM ELF in Wi-Fi closure')
        for interpreter in re.findall(r'Requesting program interpreter: ([^\]]+)', info):
            pending.append(interpreter.lstrip('/'))
        for library in re.findall(r'\(NEEDED\).*\[([^\]]+)\]', info):
            candidates = ['lib/' + library, 'usr/lib/' + library]
            selected = next((p for p in candidates if (root / p).exists() or (root / p).is_symlink()), None)
            require(selected is not None, 'missing ELF dependency: ' + library)
            pending.append(selected)
    return visited


def check_inside(args):
    with tempfile.TemporaryDirectory(prefix='surface-wifi-check-') as temporary:
        work = Path(temporary)
        root = work / 'root'
        if args.bundle:
            spec = importlib.util.spec_from_file_location('efi', REPO / 'appliance/install/internal-efi-pc.py')
            efi = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(efi)
            manifest = efi.load_bundle(args.bundle)
            require(payload.digest(args.base) == manifest['required_base']['sha256'], 'frozen base checksum mismatch')
            extract(args.base, root)
            ns = 'appliance/' + manifest['release_id'] + '/extension.squashfs'
            entry = next(e for e in manifest['files'] if e['target'] == ns)
            extension = work / 'extension'
            extract(args.bundle / entry['source'], extension)
            inventory_path = args.bundle / 'package-inventory.json'
            require(payload.digest(inventory_path) == manifest['identity']['package_inventory_sha256'], 'package inventory checksum mismatch')
            inventory = json.loads(inventory_path.read_text())
            for record in inventory['files']:
                name = record['path']
                payload.safe_relative(name)
                path = extension / name
                require(stat.S_IMODE(path.lstat().st_mode) == record['mode'], 'payload mode mismatch')
                if 'symlink' in record:
                    require(path.is_symlink() and os.readlink(path) == record['symlink'], 'payload symlink mismatch')
                else:
                    require(not path.is_symlink() and payload.digest(path) == record['sha256'], 'payload hash mismatch')
            require(not (extension / 'lib/modules').exists(), 'extension replaces frozen modules')
            merge(extension, root)
            print('extension_bytes=' + str((args.bundle / entry['source']).stat().st_size))
            print('bundle_bytes=' + str(sum(p.stat().st_size for p in args.bundle.rglob('*') if p.is_file())))
        else:
            extract(args.rootfs, root)
        for name in ('usr/sbin/surface-wifi', 'sbin/wpa_supplicant', 'sbin/wpa_cli', 'etc/init.d/surface-wifi'):
            path = root / name
            require(path.is_file() and path.stat().st_mode & 0o111, 'missing Wi-Fi executable: ' + name)
            require(path.stat().st_uid == 0, 'Wi-Fi executable is not root-owned')
        require(payload.rooted_target(root, 'etc/runlevels/default/surface-wifi') == root / 'etc/init.d/surface-wifi', 'Wi-Fi service not enabled')
        for name in ('media/boot/appliance/identity/wpa_supplicant.conf', 'run/surface-wifi/wpa_supplicant.conf'):
            require(not (root / name).exists(), 'image contains runtime credentials')
        for path in (root / 'etc').rglob('*.conf'):
            if path.is_file() and not path.is_symlink() and path.stat().st_size < 65536:
                require(re.search(rb'^\s*psk\s*=', path.read_bytes(), re.M) is None, 'image contains a configured PSK')
        notice = root / 'usr/share/licenses/surface-wifi/LICENCE.NXP'
        require(payload.digest(notice) == payload.LICENSE_SHA, 'firmware license absent or changed')
        packages = payload.read_lock(REPO / 'appliance/packages.lock')
        actual_packages = {name: record[0] for name, record in payload.installed(root).items()}
        if args.bundle:
            require(manifest['identity']['packages_lock_sha256'] == payload.digest(REPO / 'appliance/packages.lock'), 'bundle uses a different package lock')
            for package in inventory['packages']:
                locked = packages[package['name']]
                require(package == vars(locked), 'package inventory differs from reviewed lock')
                require(package['name'] not in actual_packages, 'extension replaces a base package')
                actual_packages[package['name']] = package['version']
        require(actual_packages == {name: package.version for name, package in packages.items()}, 'image package closure differs from lock')
        firmware_apk = Path('/cache/apks') / packages['linux-firmware-mrvl'].filename
        require(payload.digest(firmware_apk) == packages['linux-firmware-mrvl'].sha256, 'firmware APK checksum mismatch')
        original = work / 'firmware-apk'
        original.mkdir()
        subprocess.run(['apk', '--keys-dir', str(root / 'etc/apk/keys'), 'extract', '--destination', str(original), str(firmware_apk)], check=True)
        require(payload.digest(root / payload.FIRMWARE) == payload.digest(original / payload.FIRMWARE), 'firmware differs from signed APK')
        subprocess.run(['zstd', '-t', str(root / payload.FIRMWARE)], check=True)
        for module in ('mwifiex_sdio', 'mwifiex', 'cfg80211'):
            require(any((root / 'lib/modules').rglob(module + '.ko*')), 'missing kernel module: ' + module)
        for binary, arguments in (('sbin/wpa_supplicant', ['-v']), ('sbin/wpa_cli', ['-v']), ('bin/busybox', ['udhcpc', '--help'])):
            closure = elf_closure(root, binary)
            result = subprocess.run(['proot', '-0', '-q', 'qemu-arm', '-r', str(root), '/' + binary, *arguments], capture_output=True, text=True, timeout=30)
            require(result.returncode == 0, 'ARM utility failed: ' + binary)
            print(binary + ': ARM execution OK; ELF closure=' + str(len(closure)))
        print('Wi-Fi image checks passed. Hardware association, DHCP, SSH, and supervision remain unverified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    images = parser.add_mutually_exclusive_group(required=True)
    images.add_argument('--rootfs', type=Path)
    images.add_argument('--bundle', type=Path)
    parser.add_argument('--base', type=Path)
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.bundle and not args.base:
        parser.error('--bundle requires --base')
    if args.inside:
        check_inside(args)
        return
    command = ['docker', 'run', '--rm', '-v', f'{REPO}:/repo:ro', '-v', f'{payload.CACHE}:/cache:ro']
    options = ['--inside']
    for name in ('rootfs', 'bundle', 'base'):
        value = getattr(args, name)
        if value:
            command += ['-v', f'{value.expanduser().resolve()}:/input-{name}:ro']
            options += ['--' + name, '/input-' + name]
    command += [payload.IMAGE, 'sh', '-ec', 'apk add --no-cache python3 proot qemu-arm squashfs-tools zstd binutils; exec python3 /repo/tests/check_wifi_image.py "$@"', 'sh', *options]
    subprocess.run(command, check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit('Wi-Fi image check failed: ' + str(error))
