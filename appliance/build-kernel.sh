#!/bin/bash
# Usage: build-kernel.sh OUT
# Builds the pinned libre-tegra kernel with the tegra_vde cache-sync patch and the
# running Surface config. Writes OUT/kernel/{zImage,tegra30-microsoft-surface-rt.dtb,
# kernel.release,config,modules/lib/modules/<release>}.
set -euo pipefail
OUT=$(realpath -m "$1")
HERE=$(dirname "$(realpath "$0")")
REPO=$(dirname "$HERE")
K=$HOME/.cache/surface-kbuild
B=$REPO/build-deps/root
TARBALL=$K/linux-grate.tar.gz
TARBALL_SHA512=fa6a723db93d0c4c799ca8fb69a37ce13cc98a80ff688bc45dea7bd6d36df05b8f0deaa15a416e271a4ea0c5bb2097e63fc2d0a02903f11e6c404fbec82718a2
PATCH=$REPO/tegra-vde-cache-sync/tegra-vde-cache-sync.patch
CONFIG=$REPO/surface-kernel.config

export M4=$B/usr/bin/m4 BISON_PKGDATADIR=$B/usr/share/bison
export LD_LIBRARY_PATH=$K/shimlib:$K/pahole-root/usr/lib/x86_64-linux-gnu
export PATH=$K/LLVM-22.1.8-Linux-X64/bin:$K/pahole-root/usr/bin:$B/usr/bin:$PATH
# pmaports builds with KBUILD_BUILD_VERSION=$((pkgrel + 1))-postmarketOS for pkgrel 4.
MAKE=(make -s ARCH=arm LLVM=1 KBUILD_BUILD_VERSION=5-postmarketOS
      HOSTCFLAGS=-I$B/usr/include HOSTLDFLAGS=-L$B/usr/lib/x86_64-linux-gnu -j"$(nproc)")

echo "$TARBALL_SHA512  $TARBALL" | sha512sum -c --quiet
INPUTS=$(cat "$PATCH" "$CONFIG" "$0" | sha256sum | cut -d' ' -f1)
if [ "$(cat "$OUT/kernel/inputs" 2>/dev/null)" = "$INPUTS" ]; then
	echo "kernel up to date ($INPUTS)"
	exit 0
fi
SRC=$OUT/kernel-src
rm -rf "$SRC" "$OUT/kernel" "$OUT/kernel-build"
mkdir -p "$SRC" "$OUT/kernel"
tar -xzf "$TARBALL" -C "$SRC" --strip-components=1
patch -d "$SRC" -p1 --forward --fuzz=0 --no-backup-if-mismatch < "$PATCH"

BUILD=$OUT/kernel-build
mkdir -p "$BUILD"
cp "$CONFIG" "$BUILD/.config"
quiet() { grep -vE 'no version information|resolve_btfids: unresolved' || [ $? = 1 ]; }
"${MAKE[@]}" -C "$SRC" O="$BUILD" olddefconfig 2>&1 | quiet
if ! diff <(grep -v '^#' "$CONFIG" | grep -v CC_VERSION_TEXT | grep . | sort) \
          <(grep -v '^#' "$BUILD/.config" | grep -v CC_VERSION_TEXT | grep . | sort); then
	echo "effective config differs from $CONFIG" >&2
	exit 1
fi
"${MAKE[@]}" -C "$SRC" O="$BUILD" zImage modules dtbs 2>&1 | quiet
"${MAKE[@]}" -C "$SRC" O="$BUILD" modules_install INSTALL_MOD_PATH="$OUT/kernel/modules" INSTALL_MOD_STRIP=1 2>&1 | quiet
[ "$(wc -l < "$BUILD/modules.order")" = "$(find "$OUT/kernel/modules" -name '*.ko*' | wc -l)" ]
rm -f "$OUT"/kernel/modules/lib/modules/*/build "$OUT"/kernel/modules/lib/modules/*/source

cp "$BUILD/arch/arm/boot/zImage" "$OUT/kernel/zImage"
cp "$BUILD/arch/arm/boot/dts/nvidia/tegra30-microsoft-surface-rt.dtb" "$OUT/kernel/"
cp "$BUILD/include/config/kernel.release" "$OUT/kernel/kernel.release"
cp "$BUILD/.config" "$OUT/kernel/config"
REL=$(cat "$OUT/kernel/kernel.release")
VDE=$(find "$OUT/kernel/modules/lib/modules/$REL" -name 'tegra-vde.ko*')
/usr/sbin/modinfo -F intree "$VDE" | grep -qx Y
/usr/sbin/modinfo -p "$VDE" | grep -q '^skip_capture_cache_sync:'
echo "kernel $REL: $(sha256sum < "$OUT/kernel/zImage" | cut -c1-16) zImage, $(find "$OUT/kernel/modules" -name '*.ko*' | wc -l) modules"
echo "$INPUTS" > "$OUT/kernel/inputs"
