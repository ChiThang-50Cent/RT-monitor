#!/bin/bash
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 OUT" >&2
    exit 1
fi

OUT=$(realpath -m "$1")
HERE=$(dirname "$(realpath "$0")")
CACHE_DIR="$HOME/.cache/surface-appliance/apks"
LOCK_FILE="$HERE/packages.lock"
OVERLAY_DIR="$HERE/overlay"
MODULES_SRC="$OUT/kernel/modules"

if [ ! -f "$LOCK_FILE" ]; then
    echo "packages.lock missing at $LOCK_FILE" >&2
    exit 1
fi
if [ ! -d "$CACHE_DIR" ]; then
    echo "cache directory missing at $CACHE_DIR" >&2
    exit 1
fi
if [ ! -d "$OVERLAY_DIR" ]; then
    echo "overlay directory missing at $OVERLAY_DIR" >&2
    exit 1
fi
if [ ! -d "$MODULES_SRC" ]; then
    echo "kernel modules directory missing at $MODULES_SRC" >&2
    exit 1
fi

APK_LIST=$(python3 "$HERE/package_payload.py" paths)
mapfile -t APK_PATHS <<< "$APK_LIST"
[ "${#APK_PATHS[@]}" -gt 0 ] || exit 1
LICENSE=$(python3 "$HERE/package_payload.py" license)
APK_NAMES=()
for path in "${APK_PATHS[@]}"; do
    APK_NAMES+=("/cache/apks/${path##*/}")
done

echo "Building rootfs in container..."
docker run --rm \
    -e HOST_UID="$(id -u)" \
    -e HOST_GID="$(id -g)" \
    -v "$CACHE_DIR":/cache/apks:ro \
    -v "$OVERLAY_DIR":/overlay:ro \
    -v "$LICENSE":/LICENCE.NXP:ro \
    -v "$OUT":/out \
    alpine:edge@sha256:020dfcbaaf4cc1078bf2d9c7ba31a8466e334061dcd2f248001d68f79e52c000 sh -e -c '
apk add --no-cache alpine-keys qemu-arm proot squashfs-tools tar

mkdir -p /keys /target/etc/apk/keys
cp -L /usr/share/apk/keys/armv7/* /keys/
cp -L /keys/* /target/etc/apk/keys/

echo "Installing APKs into target rootfs from cache..."
apk --arch armv7 --root /target --initdb --no-network --no-scripts add "$@"

echo "Running busybox setup and package install triggers under proot+qemu-arm..."
proot -q qemu-arm -r /target -0 /bin/busybox sh -c "
    /bin/busybox --install -s
    apk fix --scripts
"

cp /target/lib/firmware/mrvl/sd8797_uapsta.bin.zst /tmp/surface-firmware.zst
rm -rf /target/lib/firmware/mrvl /target/lib/firmware/libertas
mkdir -p /target/lib/firmware/mrvl
cp /tmp/surface-firmware.zst /target/lib/firmware/mrvl/sd8797_uapsta.bin.zst

echo "Installing kernel modules tree..."
cp -a /out/kernel/modules/* /target/
chown 0:0 /target/lib
chown -R 0:0 /target/lib/modules

echo "Applying overlay..."
tar -C /overlay --owner=0 --group=0 -cf - . | tar -C /target -xpf -
mkdir -p /target/usr/share/licenses/surface-wifi
cp /LICENCE.NXP /target/usr/share/licenses/surface-wifi/LICENCE.NXP
chmod 644 /target/usr/share/licenses/surface-wifi/LICENCE.NXP

test -u /target/usr/bin/doas

# Ensure critical permissions
mkdir -p /target/media/boot /target/run /target/var/run /target/home/user
chmod 755 /target/usr/sbin/surface-boot-select 2>/dev/null || true
chmod 400 /target/etc/doas.conf
chmod 640 /target/etc/shadow
chmod 600 /target/etc/ssh/sshd_config
chmod 755 /target/etc/init.d/surface-*
chmod 755 /target/home/user /target/home/user/display_receiver.py
chmod 644 /target/home/user/surface_xv.py /target/home/user/surface_protocol.py

echo "Generating pseudo definitions for /home/user (UID:GID 10000:10000)..."
PSEUDO_FILE="/tmp/pseudo.txt"
rm -f "$PSEUDO_FILE"
(cd /target && find home/user | while read -r f; do
    mode=$(stat -c "%a" "$f")
    echo "/$f m $mode 10000 10000"
done) > "$PSEUDO_FILE"

echo "Building /out/rootfs.squashfs..."
rm -f /out/rootfs.squashfs
mksquashfs /target /out/rootfs.squashfs -comp gzip -pf "$PSEUDO_FILE" -noappend

chown "$HOST_UID:$HOST_GID" /out/rootfs.squashfs
chmod 644 /out/rootfs.squashfs
echo "rootfs.squashfs created successfully: $(du -h /out/rootfs.squashfs | awk "{print \$1}")"
' sh "${APK_NAMES[@]}"

echo "build-rootfs.sh complete: $OUT/rootfs.squashfs"
