# Use and maintain RT-monitor

These tasks assume a finished [installation](install.md). Commands run on the PC from the repository root. `<id>` is a release ID such as `6258b2a5368a`.

All SSH commands use the same options. To shorten them, define this in your shell:

```sh
SURFACE="-i $HOME/.ssh/surface_omp -o UserKnownHostsFile=$PWD/surface-known-hosts -o StrictHostKeyChecking=yes"
```

## Power on the Surface

For an internal EFI installation that has passed the cold-boot checks in [Install internal EFI](install.md#install-internal-efi), press and release **Power**. Leave the ethernet adapter connected. No APX cable, boot USB, Volume button, or menu selection should be needed.

Internal EFI installation is a candidate until that physical check succeeds. Building a bundle does not install it or prove that the firmware selects it.

The preserved base release and the APX recovery path still use RAM U-Boot:

1. Hold **Power** and **Volume Up** until the Surface enters APX mode.
2. Connect the Surface's USB port to the PC with the A-to-A cable.
3. Load U-Boot:

	```sh
	appliance/load-uboot.sh
	```

	The script prints `U-Boot started`.

4. In the boot menu, select **boot Linux from internal storage**. Use the volume buttons to move and the power button to select.
5. Unplug the A-to-A cable and plug in the USB ethernet adapter.

The PC service `surface-display.service` connects once the network is up.

## Connect over built-in Wi-Fi

Use an image built with the optional Wi-Fi service and [provision its boot identity configuration](install.md#provision-optional-wi-fi-debugging). The WPA-PSK helper does not retrieve saved host passwords. Missing credentials leave Wi-Fi disabled.

1. Find the DHCP lease for `surface-rt` in the AP or router lease table. If the hostname is absent, identify the device by its WLAN MAC address through a root console. Do not assume a fixed address or trust an SSH key discovered by scanning.
2. Set `WIFI_IP` to that lease and connect with the existing Surface key pin:

	```sh
	ssh -i "$HOME/.ssh/surface_omp" \
		-o UserKnownHostsFile="$PWD/surface-known-hosts" \
		-o StrictHostKeyChecking=yes -o HostKeyAlias=10.42.0.2 \
		user@"$WIFI_IP"
	```

	`HostKeyAlias` reuses the already pinned Ethernet identity. Do not run `ssh-keyscan`, disable host checks, or add an unchecked DHCP-address key.

3. Inspect the addresses and connected routes:

	```sh
	ip -4 addr show dev mlan0
	ip -4 route
	```

	The offered AP subnet must not overlap `10.42.0.0/24`. Change the AP subnet before use if it overlaps. The service delegates DHCP to the stock Alpine hook with `NO_GATEWAY` and `NO_DNS` for `mlan0`. Wi-Fi must not add a default route or change `/etc/resolv.conf`. Leave the display sender pointed at `10.42.0.2`.

The `surface-wifi` OpenRC service starts independently of Ethernet, SSH, and Xorg. One background worker owns foreground supplicant and DHCP children. It waits for `mlan0`, retries failed sessions after five seconds, and lets the supplicant reconnect after AP loss. It does not configure Wi-Fi through `ifup` or the global `networking` service.

For status, credential reload, or private diagnostics, use an existing authorized root console. The ordinary appliance user has no added service-management or log-reading privilege:

```sh
rc-service surface-wifi status
rc-service surface-wifi restart
rc-service surface-wifi stop
```

Restart reloads the boot configuration. Stop terminates and reaps both clients, removes runtime credentials, and clears only `mlan0`. A missing or invalid source file disables Wi-Fi until restart. Parser and DHCP output stays in root-only `/run/surface-wifi/diagnostics.log` and can contain sensitive information. Do not publish that log or dump the configuration. Kernel diagnostics may require the root console too.

Local image checks do not prove physical Wi-Fi. Before relying on it, record successful firmware initialization, `mlan0` enumeration, WPA association, a non-overlapping DHCP lease, and strict-key SSH with USB Ethernet absent. Then check AP loss and recovery, bad credentials without blocked boot, repeated service restart and stop without leftover clients, and a cold boot. Reconnect Ethernet and confirm the existing display stream still works. Until those checks run on the Surface, hardware Wi-Fi remains unverified.


## Read the idle screen

When no PC is connected, the appliance shows **Surface RT display** and one status line:

| Status line | Meaning |
| --- | --- |
| `No network adapter. Plug the USB ethernet adapter into the Surface.` | The Surface has no `eth0`. |
| `No network link. Connect the ethernet cable to the PC.` | The adapter is present but has no link. |
| `Waiting for the PC (10.42.0.1) to connect.` | The link is up and the sender has not connected yet. |

The idle panel blanks after 60 seconds. An admitted receiver wakes automatic idle blanking. A screen that you turned off with a short Power press stays off across frames and receiver reconnects.

## Control the screen and shut down

These controls require the new confirmed-power-control release. The preserved base release `6258b2a5368a` does not implement them.

- Press and release **Power** before 3 seconds to toggle the backlight. Linux and the network stay running. This is not suspend.
- Hold **Power** for about 3 seconds to request shutdown. Release it when the confirmation appears. The original release does not cancel.
- The visible confirmation counts down for 30 seconds. Press **Power** again to cancel. The cancellation press and its release do not toggle the screen or start another hold.
- If you leave the confirmation alone, the appliance requests a clean shutdown when the countdown expires.
- After cancellation, the previous manual screen-off choice returns. The video sender reconnects because confirmation deliberately closes the video session to release its hardware overlay.

The countdown starts only after the controller establishes its rendering checks. If input, backlight, or presentation ownership fails, the appliance must not shut down behind an invisible warning.

Do not keep holding Power throughout the countdown. A hardware forced power cut can bypass software confirmation. The hardware hold duration has not been measured.

Do not use a forced power cut to shut down Nura. Nura writes to its ext4 partition. Shut it down with `sudo poweroff`.

## Switch to Nura

`surface-boot-select` changes the RAM U-Boot `extlinux` default. It does not change the internal EFI startup script.

1. While the appliance runs, make Nura the default boot entry:

	```sh
	ssh -T $SURFACE user@10.42.0.2 doas /usr/sbin/surface-boot-select nura
	```

	The command prints `DEFAULT surface-rt`.

2. Power off the appliance, then use the **APX and RAM U-Boot recovery path** under [Power on the Surface](#power-on-the-surface). Select **boot Linux from internal storage**. Nura starts. To undo the internal EFI installation itself, restore its backup as described in [Install internal EFI](install.md#install-internal-efi).

## Switch from Nura to the appliance

This procedure selects a full release already listed in extlinux. An internal EFI extension ID is not an extlinux label. To install that release, follow [Install internal EFI](install.md#install-internal-efi) instead.

1. Shut down Nura with `sudo poweroff`.
2. Enter APX mode, connect the A-to-A cable, and run `appliance/load-uboot.sh`.
3. In the boot menu, select **mount internal storage**.
4. Make the appliance the default boot entry:

	```sh
	appliance/recovery-pc.sh select <id>
	```

5. Press a button on the Surface to leave the USB disk mode. Then select **boot Linux from internal storage**.

## Recover when the appliance does not boot

`recovery-pc.sh restore` puts back the original Nura `extlinux.conf` that the first install saved. It touches only the boot partition and needs no `sudo`.

This command does not restore overwritten EFI files. Use the internal EFI installer's verified backup to undo that installation.

1. Enter APX mode, connect the A-to-A cable, and run `appliance/load-uboot.sh`.
2. In the boot menu, select **mount internal storage**.
3. Restore the original boot configuration:

	```sh
	appliance/recovery-pc.sh status
	appliance/recovery-pc.sh restore
	```

	`restore` prints `DEFAULT surface-rt`.

4. Press a button on the Surface to leave the USB disk mode. Then select **boot Linux from internal storage**. Nura starts.

The restored file has no appliance entries. The release files stay in `/boot/appliance/`. To boot the appliance again, run `surface-install.sh` from Nura as in [Stage the release on Nura](install.md#8-stage-the-release-on-nura), then select the release.

## Update the appliance

For an internal EFI extension release, use [Install internal EFI](install.md#install-internal-efi) to build and install another verified bundle. Preserve `6258b2a5368a`; extension releases depend on that frozen base.

The following procedure builds a full release for the RAM U-Boot path:

1. Change the code. Files under `appliance/overlay/` change only the root filesystem. `surface-kernel.config` and the kernel patch change the kernel.
2. Build a new release. The command prints the new `<id>`:

	```sh
	appliance/build.sh
	```

3. [Switch to Nura](#switch-to-nura).
4. Stage the new release as in [Stage the release on Nura](install.md#8-stage-the-release-on-nura).
5. [Switch from Nura to the appliance](#switch-from-nura-to-the-appliance) with the new `<id>`.
6. Check the boot:

	```sh
	appliance/verify-boot.sh <id>
	```

`display_receiver.py`, `surface_xv.py`, `surface_protocol.py`, `surface_power.py`, and `surface_cursor.py` under `surface-gud/` have copies in `appliance/overlay/home/user/`. Update both copies before you build. Rebuild the PC helper with `sh surface-gud/build-cursor-capture.sh` after changing `surface_cursor_capture.c`. Update both endpoints together.

Old releases stay on the boot partition. To delete one, run this on Nura:

```sh
sudo rm -r /boot/appliance/<old-id>
```

Delete only releases that are not the current default, not listed in `/boot/extlinux/extlinux.conf`, and not required as a base by an installed extension. In particular, keep `6258b2a5368a`.

## Check the stream

The PC service logs one `receiver batch` line and one `visible ACK` line every 100 frames:

```sh
journalctl --user -u surface-display.service -f
```

These fields matter most:

| Field | Healthy value |
| --- | --- |
| `present_fps` | About 30 |
| `gap_ms.p50` | About 32.4, which is 2 vblanks of the 61.78 Hz panel |
| `gap_ms.p95` | 37 to 48. About 6% of frames wait 3 vblanks. |
| `source-age-upper-ms p50` | About 80 |

Cursor movement uses a separate SSH connection and is not represented by the
video frame-gap or source-age fields. A missing cursor channel fails the
connection rather than silently reverting to an embedded cursor.

To exercise the current cursor sources on the Surface without installing them,
run this command from an active display session:

```sh
/usr/bin/python3 lab/run_cursor.py
```

Add `--hold 180` to keep the RAM-loaded sender running for a three-minute
physical pointer check before recovery. The recovery timer includes this hold.

The runner interrupts the display, loads receiver code in RAM through pinned
SSH, injects pointer motion, and tests leave, reentry, and channel reconnection.
It restores the baseline sender through a temporary PC service override. When
the running sender is already a recovery copy, that copy is preserved. Otherwise
the baseline comes from committed host sources. Logs and recovery sources stay
under `/tmp/surface-latency-results/<time>-cursor/`. The test checks software
presentation and does not photograph the panel. The restored process runs the
baseline sender until the next service restart. Before testing a committed host
update, make sure the baseline sources match the installed receiver.
It does not install the cursor receiver or change the boot release.

## Measure decoder and presentation performance

The `lab/` scripts stop `surface-display.service`, run a test, and start the service again. They write results to `/tmp/surface-latency-results/<time>-<tag>/`. The repository has no analysis script. The results are raw NDJSON.

1. Copy the probes and the test clip to the Surface. The appliance keeps `/home/user` in RAM, so repeat this after every boot:

	```sh
	ssh -T $SURFACE user@10.42.0.2 mkdir -p /home/user/surface-latency-lab
	scp -q $SURFACE lab/checksum_probe.py lab/replay_probe.py lab/receiver_trace.py lab/trace_core.py \
		lab/vblank_probe.py lab/motion_video.py lab/clip.frames user@10.42.0.2:/home/user/surface-latency-lab/
	```

	Git does not track `lab/clip.frames`. To record a new clip, run `lab/run_live.py`, then copy `clip.frames` from its result directory in `/tmp/surface-latency-results/` to `lab/clip.frames`.

2. Run the test you need:

	```sh
	python3 lab/run_checksum.py --tag mytest --runs 2
	python3 lab/run_replay.py --tag mytest --runs 5 --matrix decode:0,xv:0
	python3 lab/run_live.py --credits 3 --vblank-step 2 --pump-fps 30
	```

	- `run_checksum.py` decodes the clip and hashes every frame. With the tested clip, every run prints `"combined": "6ee345a523c1039a936cbdadc3a85bc1f1c21cf6e02582e5b96e9dbb3cb712b1"`.
	- `run_replay.py` records the decoder time per frame in `trace.decoder_ms`. On the appliance, the median is about 18.6 ms.
	- `run_live.py` streams test scenes and records the vblank of every presented frame in `frames.ndjson`.
