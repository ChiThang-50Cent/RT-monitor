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
INIT_SRC="$HERE/initramfs/init"

if [ ! -f "$LOCK_FILE" ]; then
    echo "packages.lock missing at $LOCK_FILE" >&2
    exit 1
fi
if [ ! -f "$INIT_SRC" ]; then
    echo "init source missing at $INIT_SRC" >&2
    exit 1
fi
if [ ! -f "$OUT/kernel/kernel.release" ]; then
    echo "kernel.release missing at $OUT/kernel/kernel.release" >&2
    exit 1
fi

REL=$(cat "$OUT/kernel/kernel.release")
MODULES_DEP="$OUT/kernel/modules/lib/modules/$REL/modules.dep"
if [ ! -f "$MODULES_DEP" ]; then
    echo "modules.dep missing at $MODULES_DEP" >&2
    exit 1
fi

BB_VER=$(awk '$1=="busybox-static"{print $2}' "$LOCK_FILE")
BB_SHA=$(awk '$1=="busybox-static"{print $3}' "$LOCK_FILE")
if [ -z "$BB_VER" ] || [ -z "$BB_SHA" ]; then
    echo "busybox-static not found in $LOCK_FILE" >&2
    exit 1
fi

BB_APK="$CACHE_DIR/busybox-static-$BB_VER.apk"
if [ ! -f "$BB_APK" ]; then
    echo "busybox-static apk missing at $BB_APK" >&2
    exit 1
fi

ACTUAL_BB_SHA=$(sha256sum "$BB_APK" | awk '{print $1}')
if [ "$ACTUAL_BB_SHA" != "$BB_SHA" ]; then
    echo "busybox-static checksum mismatch ($ACTUAL_BB_SHA != $BB_SHA)" >&2
    exit 1
fi

WORK=$(mktemp -d /tmp/surface-initfs.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

INITFS="$WORK/initfs"
mkdir -p "$INITFS/bin" "$INITFS/sbin" "$INITFS/dev" "$INITFS/proc" "$INITFS/sys" \
         "$INITFS/media/boot" "$INITFS/ro_squash" "$INITFS/overlay_tmpfs" "$INITFS/sysroot" \
         "$INITFS/lib/modules/$REL"

echo "Extracting busybox-static..."
tar -xf "$BB_APK" -C "$WORK" bin/busybox.static
mv "$WORK/bin/busybox.static" "$INITFS/bin/busybox"
chmod 755 "$INITFS/bin/busybox"

APPLETS=(
    "[" "[[" "ash" "sh" "mount" "umount" "mkdir" "rm" "rmdir" "cp" "mv" "ls" "cat"
    "echo" "grep" "egrep" "awk" "sed" "cut" "tr" "head" "tail" "sleep" "mknod"
    "blkid" "findfs" "switch_root" "modprobe" "insmod" "rmmod" "lsmod"
    "sha256sum" "date" "sync" "test" "true" "false"
)

for applet in "${APPLETS[@]}"; do
    ln -sf busybox "$INITFS/bin/$applet"
done

for sbin_applet in switch_root modprobe insmod blkid findfs; do
    ln -sf ../bin/busybox "$INITFS/sbin/$sbin_applet"
done

echo "Resolving modules for storage and squashfs/overlay..."
# In our kernel, MMC (SDHCI/Tegra), FAT, loop and tmpfs are built-in (=y).
# squashfs and overlay are modules (=m).
resolve_module() {
    local mod="$1"
    local line
    line=$(grep -E "(^|/)${mod}\.ko" "$MODULES_DEP") || { echo "module $mod not in modules.dep" >&2; return 1; }
    echo "${line%%:*}"
    for d in ${line#*:}; do
        echo "$d"
    done
}

RESOLVED_MODULES=$(
    for m in squashfs overlay; do
        resolve_module "$m" || exit 1
    done | sort -u
) || exit 1

INITFS_DEP="$INITFS/lib/modules/$REL/modules.dep"
: > "$INITFS_DEP"

for mod_rel in $RESOLVED_MODULES; do
    src="$OUT/kernel/modules/lib/modules/$REL/$mod_rel"
    dst="$INITFS/lib/modules/$REL/$mod_rel"
    if [ ! -f "$src" ]; then
        echo "Required kernel module $src missing!" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$dst")"
    cp -p "$src" "$dst"
    grep "^${mod_rel}:" "$MODULES_DEP" >> "$INITFS_DEP" || true
    echo "  + $mod_rel"
done

cp -p "$INIT_SRC" "$INITFS/init"
chmod 755 "$INITFS/init"

mkdir -p "$OUT"
INITRAMFS_OUT="$OUT/initramfs"
echo "Creating $INITRAMFS_OUT..."
(cd "$INITFS" && find . -mindepth 1 | sort | cpio -H newc -R 0:0 -o 2>/dev/null) | gzip -9 > "$INITRAMFS_OUT"
chmod 644 "$INITRAMFS_OUT"

echo "build-initramfs.sh complete: $INITRAMFS_OUT ($(du -h "$INITRAMFS_OUT" | awk '{print $1}'))"
