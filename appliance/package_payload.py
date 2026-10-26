#!/usr/bin/env python3
"""Verify locked APK inputs and project additive packages over the frozen base."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
IMAGE = 'alpine:edge@sha256:020dfcbaaf4cc1078bf2d9c7ba31a8466e334061dcd2f248001d68f79e52c000'
CACHE = Path.home() / '.cache/surface-appliance'
LICENSE_URL = 'https://gitlab.com/kernel-firmware/linux-firmware/-/raw/20260916/LICENSES/LICENCE.NXP'
LICENSE_SHA = '043048cbf0e5dd45ed58855949c41aa4b447751a871a36d80a23e398bbf3746d'
FIRMWARE = 'lib/firmware/mrvl/sd8797_uapsta.bin.zst'


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    sha256: str

    @property
    def filename(self):
        return f'{self.name}-{self.version}.apk'


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_lock(path):
    packages = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        name, version, sha = line.split()
        if not re.fullmatch(r'[a-zA-Z0-9+_.-]+', name + version) or not re.fullmatch(r'[0-9a-f]{64}', sha) or name in packages:
            raise ValueError('invalid or duplicate package lock entry')
        packages[name] = LockedPackage(name, version, sha)
    if not packages:
        raise ValueError('empty package lock')
    return packages


def verified_apk_paths(lock, cache):
    paths = []
    for package in read_lock(lock).values():
        path = Path(cache) / package.filename
        if path.is_symlink() or not path.is_file() or digest(path) != package.sha256:
            raise ValueError('missing or corrupt locked APK: ' + package.filename)
        paths.append(path)
    return paths


def license_path(cache=CACHE):
    path = Path(cache) / 'LICENCE.NXP'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(LICENSE_URL) as response:
            data = response.read(65537)
        if hashlib.sha256(data).hexdigest() != LICENSE_SHA:
            raise ValueError('firmware license checksum mismatch')
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.chmod(0o644)
        temporary.replace(path)
    if path.is_symlink() or digest(path) != LICENSE_SHA:
        raise ValueError('firmware license checksum mismatch')
    return path


def installed(root):
    records = {}
    for block in (root / 'lib/apk/db/installed').read_text().split('\n\n'):
        fields = {}
        files = []
        folder = ''
        for line in block.splitlines():
            if line.startswith('F:'):
                folder = line[2:]
            elif line.startswith('R:'):
                files.append(folder + '/' + line[2:])
            elif len(line) > 1 and line[1] == ':' and line[0] not in fields:
                fields[line[0]] = line[2:]
        if 'P' in fields:
            records[fields['P']] = (fields['V'], fields.get('A'), files)
    return records


def safe_relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') or p.startswith('.wh.') for p in path.parts) or str(path) != name:
        raise ValueError('unsafe payload path: ' + name)
    if name == 'lib/modules' or name.startswith('lib/modules/'):
        raise ValueError('package payload must not contain modules')
    return path


def rooted_target(root, name):
    pending = list(PurePosixPath(name).parts)
    resolved = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ('', '/', '.'):
            continue
        if part == '..':
            if not resolved:
                raise ValueError('symlink escapes merged root')
            resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('payload symlink loop')
            target = PurePosixPath(os.readlink(candidate))
            if target.is_absolute():
                resolved = []
            pending = list(target.parts) + pending
        else:
            resolved.append(part)
    result = root.joinpath(*resolved)
    if not result.exists():
        raise ValueError('dangling payload symlink: ' + name)
    return result


def copy_payload_file(source, destination, name):
    safe_relative(name)
    src = source / name
    for parent in PurePosixPath(name).parents:
        if str(parent) != '.' and (source / str(parent)).is_symlink():
            raise ValueError('symlink payload parent: ' + name)
    mode = src.lstat().st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
        raise ValueError('special payload file: ' + name)
    if src.is_symlink():
        rooted_target(source, name)
    dest = destination / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        dest.symlink_to(os.readlink(src))
    else:
        shutil.copy2(src, dest)
    return {'path': name, 'mode': stat.S_IMODE(mode),
            **({'symlink': os.readlink(src)} if src.is_symlink() else {'sha256': digest(src)})}


def same_file(left, right):
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)
    return left.is_file() and right.is_file() and stat.S_IMODE(left.stat().st_mode) == stat.S_IMODE(right.stat().st_mode) and digest(left) == digest(right)


def stage_inside(base_image, lock, cache, destination):
    packages = read_lock(lock)
    paths = verified_apk_paths(lock, cache)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        base = work / 'base'
        subprocess.run(['unsquashfs', '-no-progress', '-d', str(base), str(base_image)], check=True)
        before = installed(base)
        for name, (version, _, _) in before.items():
            if name not in packages or packages[name].version != version:
                raise ValueError('frozen package differs from lock: ' + name)
        stage = work / 'stage'
        shutil.copytree(base, stage, symlinks=True)
        added = [p for p in paths if p.name not in {packages[n].filename for n in before}]
        keys = base / 'etc/apk/keys'
        subprocess.run(['apk', '--keys-dir', str(keys), 'verify', *map(str, added)], check=True)
        world = stage / 'etc/apk/world'
        world.write_text(''.join(f'{name}={record[0]}\n' for name, record in sorted(before.items())))
        subprocess.run(['apk', '--root', str(stage), '--arch', 'armv7', '--keys-dir', str(keys), '--no-network', '--no-scripts', 'add', *map(str, added)], check=True)
        after = installed(stage)
        if any(after.get(name, ())[:2] != record[:2] for name, record in before.items()):
            raise ValueError('APK transaction changed frozen packages')
        if set(after) != set(packages):
            raise ValueError('APK transaction does not match lock closure')
        destination.mkdir(parents=True)
        inventory = []
        for name in sorted(set(after) - set(before)):
            version, arch, files = after[name]
            if version != packages[name].version or arch not in ('armv7', 'noarch'):
                raise ValueError('APK metadata differs from lock: ' + name)
            for relative in files:
                safe_relative(relative)
                old = base / relative
                new = stage / relative
                if old.exists() or old.is_symlink():
                    if not same_file(old, new):
                        raise ValueError('package collides with frozen base: ' + relative)
                    continue
                if relative.startswith('lib/firmware/') and relative != FIRMWARE:
                    continue
                record = copy_payload_file(stage, destination, relative)
                record['package'] = name
                inventory.append(record)
        if not (destination / FIRMWARE).is_file():
            raise ValueError('selected firmware absent from APK payload')
        notice = destination / 'usr/share/licenses/surface-wifi/LICENCE.NXP'
        notice.parent.mkdir(parents=True)
        shutil.copyfile(license_path(Path(cache).parent), notice)
        notice.chmod(0o644)
        inventory.append({'path': notice.relative_to(destination).as_posix(), 'sha256': LICENSE_SHA, 'source': LICENSE_URL, 'mode': 0o644})
        document = {'packages': [vars(packages[n]) for n in sorted(set(after) - set(before))], 'files': inventory}
        (destination.parent / 'package-inventory.json').write_text(json.dumps(document, sort_keys=True, indent=2) + '\n')


def stage_extension_packages(base, destination, cache=CACHE):
    verified_apk_paths(HERE / 'packages.lock', cache / 'apks')
    license_path(cache)
    subprocess.run(['docker', 'run', '--rm', '-e', f'HOST_UID={os.getuid()}', '-e', f'HOST_GID={os.getgid()}', '-v', f'{HERE}:/appliance:ro', '-v', f'{cache}:/cache:ro', '-v', f'{base}:/base.squashfs:ro', '-v', f'{destination.parent}:/work', IMAGE, 'sh', '-ec', 'apk add --no-cache python3 squashfs-tools; python3 /appliance/package_payload.py stage /base.squashfs /work/' + destination.name + '; chown -R "$HOST_UID:$HOST_GID" /work/' + destination.name + ' /work/package-inventory.json'], check=True)
    return destination.parent / 'package-inventory.json'


def extend_lock_inside():
    lock = HERE / 'packages.lock'
    packages = read_lock(lock)
    requested = [line.split('#', 1)[0].strip() for line in (HERE / 'packages.txt').read_text().splitlines()]
    additions = [name for name in requested if name and name not in packages]
    if not additions:
        return
    cache = Path('/cache/apks')
    paths = verified_apk_paths(lock, cache)
    keys = Path('/keys')
    keys.mkdir()
    for key in Path('/usr/share/apk/keys/armv7').iterdir():
        shutil.copyfile(key, keys / key.name)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / 'root'
        subprocess.run(['apk', '--root', str(root), '--arch', 'armv7', '--keys-dir', str(keys), '--initdb', '--no-network', '--no-scripts', 'add', *map(str, paths)], check=True)
        before = installed(root)
        (root / 'etc/apk/world').write_text(''.join(f'{p.name}={p.version}\n' for p in packages.values()))
        repositories = root / 'etc/apk/repositories'
        repositories.write_text('https://dl-cdn.alpinelinux.org/alpine/edge/main\nhttps://dl-cdn.alpinelinux.org/alpine/edge/community\n')
        subprocess.run(['apk', '--root', str(root), '--arch', 'armv7', '--keys-dir', str(keys), '--no-scripts', '--update-cache', 'add', *additions], check=True)
        after = installed(root)
        if any(after.get(name, ())[:2] != record[:2] for name, record in before.items()):
            raise ValueError('dependency update would change an existing pin')
        for name in sorted(set(after) - set(packages)):
            version, arch, _ = after[name]
            if arch not in ('armv7', 'noarch'):
                raise ValueError('unexpected dependency architecture')
            target = cache / f'{name}-{version}.apk'
            if not target.exists():
                subprocess.run(['apk', '--arch', 'armv7', '--keys-dir', str(keys), '--repositories-file', str(repositories), '--update-cache', 'fetch', '--output', str(cache), f'{name}={version}'], check=True)
            subprocess.run(['apk', '--keys-dir', str(keys), 'verify', str(target)], check=True)
            packages[name] = LockedPackage(name, version, digest(target))
        temporary_lock = lock.with_suffix('.lock.tmp')
        temporary_lock.write_text(''.join(f'{p.name} {p.version} {p.sha256}\n' for p in sorted(packages.values(), key=lambda p: p.name)))
        temporary_lock.chmod(0o644)
        temporary_lock.replace(lock)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('paths', 'license', 'stage', 'fetch', 'extend', 'extend-inside'))
    parser.add_argument('arguments', nargs='*')
    args = parser.parse_args()
    if args.command == 'paths':
        for path in verified_apk_paths(HERE / 'packages.lock', CACHE / 'apks'):
            print(path)
    elif args.command == 'license':
        print(license_path())
    elif args.command == 'stage':
        stage_inside(Path(args.arguments[0]), HERE / 'packages.lock', Path('/cache/apks'), Path(args.arguments[1]))
    elif args.command == 'extend-inside':
        extend_lock_inside()
    elif args.command == 'extend':
        subprocess.run([sys.executable, __file__, 'fetch'], check=True)
        subprocess.run(['docker', 'run', '--rm', '-e', f'HOST_UID={os.getuid()}', '-e', f'HOST_GID={os.getgid()}', '-v', f'{HERE}:/appliance', '-v', f'{CACHE}:/cache', IMAGE, 'sh', '-ec', 'apk add --no-cache python3 alpine-keys; python3 /appliance/package_payload.py extend-inside; chown \"$HOST_UID:$HOST_GID\" /appliance/packages.lock'], check=True)
        license_path()
    else:
        cache = CACHE / 'apks'
        cache.mkdir(parents=True, exist_ok=True)
        for package in read_lock(HERE / 'packages.lock').values():
            target = cache / package.filename
            if not target.exists():
                for repository in ('main', 'community'):
                    try:
                        urllib.request.urlretrieve(f'https://dl-cdn.alpinelinux.org/alpine/edge/{repository}/armv7/{package.filename}', target)
                        break
                    except urllib.error.HTTPError as error:
                        target.unlink(missing_ok=True)
                        if error.code != 404:
                            raise
        verified_apk_paths(HERE / 'packages.lock', cache)
        license_path()
        subprocess.run(['docker', 'run', '--rm', '-v', f'{HERE}:/appliance:ro', '-v', f'{cache}:/cache:ro', IMAGE, 'sh', '-ec', 'apk add --no-cache alpine-keys; mkdir /keys; cp -L /usr/share/apk/keys/armv7/* /keys/; while read -r name version sha; do apk --keys-dir /keys verify "/cache/$name-$version.apk"; done < /appliance/packages.lock'], check=True)
