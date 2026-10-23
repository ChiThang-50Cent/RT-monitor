#!/bin/bash
# Usage: verify-boot.sh RELEASE_ID
# Read-only checks of a booted appliance over the pinned SSH identity. Exits nonzero on any failure.
set -uo pipefail
ID=$1
REPO=$(dirname "$(dirname "$(realpath "$0")")")
SSH=(ssh -T -i "$HOME/.ssh/surface_omp" -o UserKnownHostsFile="$REPO/surface-known-hosts"
     -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=5 user@10.42.0.2)
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1: $2"; else echo "FAIL $1: got '$2', want '$3'"; fail=1; fi; }

out=$("${SSH[@]}" 'cat /proc/cmdline; echo; cat /sys/module/tegra_vde/parameters/skip_capture_cache_sync
cat /sys/module/tegra_vde/parameters/coded_buffer_min_kb; cat /proc/sys/kernel/tainted; uname -r
awk "\$2 == \"/\" {print \$3}" /proc/mounts; awk "\$2 == \"/media/boot\" {split(\$4,o,\",\"); print o[1]}" /proc/mounts
grep -c mmcblk0p2 /proc/mounts; test -e /run/user/10000/surface-appliance-active && echo marker
ps -o comm= -A 2>/dev/null || ps -eo comm=') || { echo "FAIL ssh: host key or login rejected"; exit 1; }
mapfile -t L <<< "$out"
cmdline=${L[0]}
check release "$(grep -o 'appliance.release=[0-9a-f]*' <<< "$cmdline")" "appliance.release=$ID"
check skip_capture_cache_sync "${L[2]}" Y
check coded_buffer_min_kb "${L[3]}" 512
t=${L[4]}
check taint_out_of_tree_or_unsigned "$(( t & (4096 | 8192) ))" 0
echo "INFO tainted=$t (512 = a kernel WARN happened since boot)"
check kernel "${L[5]}" 7.0.1-postmarketos-grate
check rootfs "${L[6]}" overlay
check boot_mount "${L[7]}" ro
check nura_root_mounted "${L[8]}" 0
check runtime_marker "${L[9]}" marker
procs=$(printf '%s\n' "${L[@]:10}" | sort -u | tr '\n' ' ')
echo "processes: $procs"
for p in Xorg sshd udevd supervise-daemo; do grep -q "$p" <<< "$procs" || { echo "FAIL missing process $p"; fail=1; }; done
for p in xfce4-session lightdm xfwm4 dbus-daemon NetworkManager pulseaudio; do grep -qw "$p" <<< "$procs" && { echo "FAIL desktop process $p"; fail=1; }; done
echo "result: $([ $fail = 0 ] && echo PASS || echo FAIL)"
exit $fail
