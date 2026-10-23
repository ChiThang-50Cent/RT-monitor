# Use and maintain RT-monitor

These tasks assume a finished [installation](install.md). Commands run on the PC from the repository root. `<id>` is a release ID such as `6258b2a5368a`.

All SSH commands use the same options. To shorten them, define this in your shell:

```sh
SURFACE="-i $HOME/.ssh/surface_omp -o UserKnownHostsFile=$PWD/surface-known-hosts -o StrictHostKeyChecking=yes"
```

## Power on the Surface

The U-Boot that starts Linux lives in RAM, so you repeat these steps after every power-off and every reboot.

1. Hold **Power** and **Volume Up** until the Surface enters APX mode.
2. Connect the Surface's USB port to the PC with the A-to-A cable.
3. Load U-Boot:

	```sh
	appliance/load-uboot.sh
	```

	The script prints `U-Boot started`.

4. In the boot menu, select **boot Linux from internal storage**. The menu waits until you choose. Use the volume buttons to move and the power button to select.
5. Unplug the A-to-A cable and plug in the USB ethernet adapter.

The PC service `surface-display.service` connects on its own once the network is up.

## Read the idle screen

When no PC is connected, the appliance shows **Surface RT display** and one status line:

| Status line | Meaning |
| --- | --- |
| `No network adapter. Plug the USB ethernet adapter into the Surface.` | The Surface has no `eth0`. |
| `No network link. Connect the ethernet cable to the PC.` | The adapter is present but has no link. |
| `Waiting for the PC (10.42.0.1) to connect.` | The link is up and the sender has not connected yet. |

After 60 seconds without input, the panel and its backlight turn off. When the sender connects, the receiver turns the panel on and covers the idle screen. When the sender disconnects, the idle screen returns and the 60-second timer restarts.

## Power off the appliance

Hold **Power** for about 10 seconds. A hard power-off is safe on the appliance. The appliance mounts the boot partition read-only, never mounts the Nura partition, and keeps all its changes in RAM.

Do not hold **Power** to shut down Nura. Nura writes to its ext4 partition. Shut it down with `sudo poweroff`.

## Switch to Nura

1. While the appliance runs, make Nura the default boot entry:

	```sh
	ssh -T $SURFACE user@10.42.0.2 doas /usr/sbin/surface-boot-select nura
	```

	The command prints `DEFAULT surface-rt`.

2. Power off the appliance, then [power on the Surface](#power-on-the-surface). Nura starts.

## Switch from Nura to the appliance

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

`surface-gud/display_receiver.py`, `surface-gud/surface_xv.py`, and `surface-gud/surface_protocol.py` have copies in `appliance/overlay/home/user/`. Copy your changes into the overlay before you build.

Old releases stay on the boot partition. To delete one, run this on Nura:

```sh
sudo rm -r /boot/appliance/<old-id>
```

Delete only releases that are not the current default and not listed in `/boot/extlinux/extlinux.conf`.

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
