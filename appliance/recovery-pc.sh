#!/bin/bash
# Usage: recovery-pc.sh status | select nura|<release-id> | restore
# Run on the PC while the Surface is in U-Boot "mount internal storage" (UMS).
# Touches only p1 (vfat 17C0-8631), mounted through udisks; never p2.
set -euo pipefail
BOOT_UUID=17C0-8631
die() { echo "recovery-pc: $*" >&2; exit 1; }

dev=$(lsblk -rno PATH,UUID | awk -v u="$BOOT_UUID" '$2 == u { print $1 }')
[ -n "$dev" ] || die "no partition with UUID $BOOT_UUID; is the Surface in UMS mode?"
[ "$(wc -l <<< "$dev")" = 1 ] || die "more than one partition has UUID $BOOT_UUID: $dev"
[ "$(lsblk -dno TRAN "/dev/$(lsblk -no PKNAME "$dev")")" = usb ] || die "$dev is not on a USB disk"

mnt=$(lsblk -no MOUNTPOINT "$dev")
if [ -z "$mnt" ]; then
	udisksctl mount -b "$dev" --no-user-interaction >/dev/null
	mnt=$(lsblk -no MOUNTPOINT "$dev")
fi
conf=$mnt/extlinux/extlinux.conf
orig=$mnt/appliance/recovery/extlinux.conf.orig
[ -f "$conf" ] || die "$conf missing"

case ${1:-} in
status)
	echo "device $dev mounted at $mnt"
	grep -E '^(DEFAULT|LABEL) ' "$conf"
	if [ -f "$orig" ]; then echo "recovery copy present: $(sha256sum < "$orig" | cut -c1-16)"; else echo "recovery copy MISSING"; fi
	;;
select)
	[ $# = 2 ] || die "usage: recovery-pc.sh select nura|<release-id>"
	label=appliance-$2
	[ "$2" = nura ] && label=surface-rt
	grep -qx "LABEL $label" "$conf" || die "no LABEL $label"
	sed "s/^DEFAULT .*/DEFAULT $label/" "$conf" > "$conf.new"
	sync "$conf.new"
	mv "$conf.new" "$conf"
	;;
restore)
	[ -f "$orig" ] || die "$orig missing"
	cp "$orig" "$conf.new"
	sync "$conf.new"
	mv "$conf.new" "$conf"
	;;
*) die "usage: recovery-pc.sh status | select nura|<release-id> | restore" ;;
esac
sync
[ "${1}" = status ] || grep -E '^DEFAULT ' "$conf"
udisksctl unmount -b "$dev" --no-user-interaction >/dev/null
echo "p1 unmounted; exit UMS on the Surface"
