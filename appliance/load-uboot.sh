#!/bin/bash
# Usage: load-uboot.sh
# Loads the RAM U-Boot into a Surface RT sitting in APX (0955:7130).
# nvflash runs only after the fusee exploit succeeds: an nvflash call against an
# unexploited bootrom wedges it until APX is re-entered.
set -euo pipefail
REPO=$(dirname "$(dirname "$(realpath "$0")")")
UBOOT=$REPO/u-boot/u-boot-dtb-tegra.bin
UBOOT_SHA256=269262c18d0f674afa53e62086a2fafca50d30e1e8356a2ccbe290091f025f6f
cd "$REPO/tegra30_debrick"

echo "$UBOOT_SHA256  $UBOOT" | sha256sum -c --quiet
lsusb -d 0955:7130 >/dev/null || { echo "no APX device (0955:7130); enter APX with Power + Volume Up" >&2; exit 1; }
"$REPO/.venv/bin/python" ./fusee-launcher/fusee-launcher.py ./payload/uart_payload_n7.bin -V 0955 -P 7130
for _ in $(seq 20); do lsusb -d 0955:7130 >/dev/null && break; sleep 0.5; done
lsusb -d 0955:7130 >/dev/null || { echo "APX did not come back after the exploit" >&2; exit 1; }
sleep 1
# U-Boot does not answer the nvflash protocol, so "bootloader failed NvError 0x0" after a full upload is expected;
# any other failure is fatal.
log=$(./utils/nvflash_v1.13.87205_miniloader_patched --setbct --bct "$REPO/bootloader/surface-rt.bct" \
	--configfile ./utils/flash.cfg --bl "$UBOOT" --go 2>&1) || true
printf '%s\n' "$log"
grep -q "u-boot-dtb-tegra.bin sent successfully" <<< "$log" || { echo "U-Boot upload incomplete" >&2; exit 1; }
sleep 2
if lsusb -d 0955:7130 >/dev/null; then echo "APX still present: U-Boot did not start" >&2; exit 1; fi
echo "U-Boot started"
