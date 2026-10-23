#!/bin/bash
# Usage: build.sh [OUT]   (default ~/.cache/surface-appliance/out)
# Builds kernel, rootfs and initramfs, then publishes ~/surface-releases/<id>/.
set -euo pipefail
HERE=$(dirname "$(realpath "$0")")
REPO=$(dirname "$HERE")
OUT=$(realpath -m "${1:-$HOME/.cache/surface-appliance/out}")
RELEASES=$HOME/surface-releases

"$HERE/build-kernel.sh" "$OUT"
"$HERE/build-rootfs.sh" "$OUT"
"$HERE/build-initramfs.sh" "$OUT"

BASE_CMDLINE="console=ttyS0,115200n8 console=tty0 cpuidle.off=1 tegra_vde.skip_capture_cache_sync=1 tegra_vde.coded_buffer_min_kb=512"
files=(kernel/zImage initramfs kernel/tegra30-microsoft-surface-rt.dtb rootfs.squashfs)
extras=("$HERE/install/surface-install.sh" "$HERE/overlay/usr/sbin/surface-boot-select" "$REPO/surface-known-hosts")
id=$( { (cd "$OUT" && sha256sum "${files[@]}"); cat "${extras[@]}"; echo "$BASE_CMDLINE"; } | sha256sum | cut -c1-12)
dst=$RELEASES/$id
if [ -d "$dst" ]; then
	(cd "$dst" && sha256sum -c --quiet SHA256SUMS)
	echo "release $id already published: $dst"
	exit 0
fi

tmp=$RELEASES/.$id.tmp
rm -rf "$tmp"
mkdir -p "$tmp"
for f in "${files[@]}"; do cp "$OUT/$f" "$tmp/"; done
cp "${extras[@]:0:2}" "$tmp/"
cp "${extras[2]}" "$tmp/known_hosts"
cat > "$tmp/manifest.json" <<EOF
{
  "id": "$id",
  "cmdline": "$BASE_CMDLINE appliance.release=$id",
  "kernel_release": "$(cat "$OUT/kernel/kernel.release")",
  "kernel_patch_sha256": "$(sha256sum < "$REPO/tegra-vde-cache-sync/tegra-vde-cache-sync.patch" | cut -d' ' -f1)",
  "kernel_config_sha256": "$(sha256sum < "$OUT/kernel/config" | cut -d' ' -f1)",
  "packages_lock_sha256": "$(sha256sum < "$HERE/packages.lock" | cut -d' ' -f1)"
}
EOF
(cd "$tmp" && sha256sum zImage initramfs tegra30-microsoft-surface-rt.dtb rootfs.squashfs manifest.json \
	surface-install.sh surface-boot-select known_hosts > SHA256SUMS)
mv "$tmp" "$dst"
echo "published $dst"
