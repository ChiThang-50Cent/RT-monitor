#!/bin/sh
# Usage (as root on Nura): surface-install.sh RELEASE_DIR
# Copies a verified release to p1:/appliance/<id>, captures the SSH identity once,
# saves the original extlinux.conf once, and adds LABEL appliance-<id>.
# DEFAULT is left alone; use surface-boot-select for that.
set -eu
BOOT_UUID=17C0-8631
die() { echo "surface-install: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "must run as root"
[ $# = 1 ] || die "usage: surface-install.sh RELEASE_DIR"
src=$(cd "$1" && pwd)

dev=$(blkid | sed -n "s/^\([^:]*\):.*UUID=\"$BOOT_UUID\".*/\1/p")
[ "$(awk '$2 == "/boot" { print $1 }' /proc/mounts)" = "$dev" ] || die "/boot is not the p1 partition $BOOT_UUID"
conf=/boot/extlinux/extlinux.conf
grep -qx 'LABEL surface-rt' "$conf" || die "Nura label surface-rt missing from $conf"

id=$(sed -n 's/.*"id": *"\([0-9a-f]*\)".*/\1/p' "$src/manifest.json")
[ -n "$id" ] || die "no id in $src/manifest.json"
(cd "$src" && sha256sum -c SHA256SUMS) || die "release checksums do not match"

nura_sums() { (cd /boot && sha256sum vmlinuz initramfs dtbs/tegra30-microsoft-surface-rt.dtb); }
before=$(nura_sums)

mkdir -p /boot/appliance/recovery
[ -f /boot/appliance/recovery/extlinux.conf.orig ] || cp "$conf" /boot/appliance/recovery/extlinux.conf.orig

dst=/boot/appliance/$id
if [ -d "$dst" ]; then
	(cd "$dst" && sha256sum -c -s SHA256SUMS) || die "$dst exists with different contents"
	echo "release $id already staged"
else
	rm -rf "$dst.tmp"
	mkdir -p "$dst.tmp"
	(cd "$src" && cp $(awk '{print $2}' SHA256SUMS) SHA256SUMS "$dst.tmp/")
	sync
	(cd "$dst.tmp" && sha256sum -c -s SHA256SUMS) || die "copy to $dst.tmp is corrupt"
	mv "$dst.tmp" "$dst"
	sync
fi

ident=/boot/appliance/identity
if [ ! -d "$ident" ]; then
	[ -f "$src/known_hosts" ] || die "release has no known_hosts to check the host key against"
	mkdir -p "$ident.tmp"
	for k in /etc/ssh/ssh_host_*_key; do
		grep -qF "$(cut -d' ' -f2 "$k.pub")" "$src/known_hosts" || continue
		cp "$k" "$k.pub" "$ident.tmp/"
	done
	ls "$ident.tmp"/ssh_host_*_key >/dev/null 2>&1 || die "no host key here matches the pinned known_hosts"
	cp /home/user/.ssh/authorized_keys "$ident.tmp/authorized_keys"
	sync
	mv "$ident.tmp" "$ident"
	sync
fi

if ! grep -qx "LABEL appliance-$id" "$conf"; then
	cmdline=$(sed -n 's/.*"cmdline": *"\([^"]*\)".*/\1/p' "$src/manifest.json")
	[ -n "$cmdline" ] || die "no cmdline in manifest"
	cp "$conf" "$conf.new"
	cat >> "$conf.new" <<EOF

LABEL appliance-$id
    MENU LABEL Surface display appliance $id
    LINUX /appliance/$id/zImage
    INITRD /appliance/$id/initramfs
    FDT /appliance/$id/tegra30-microsoft-surface-rt.dtb
    APPEND $cmdline
EOF
	sync
	mv "$conf.new" "$conf"
	sync
fi

[ "$before" = "$(nura_sums)" ] || die "Nura boot files changed during install"
echo "staged appliance-$id; Nura boot files unchanged"
grep -E '^(DEFAULT|LABEL) ' "$conf"
