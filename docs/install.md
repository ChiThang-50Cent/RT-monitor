# Install RT-monitor

This guide takes a Surface RT that already runs postmarketOS (Nura) on its internal eMMC and ends with the appliance OS showing your PC's second monitor. Nura stays installed as the fallback.

Plan about an hour. The first kernel build takes about 11 minutes of full CPU on the tested PC.

> [!NOTE]
> The scripts hard-code the tested device: boot partition UUID `17C0-8631`, IP addresses `10.42.0.1` and `10.42.0.2`, and the Surface user `user` with UID 10000. On another device, change these values in `appliance/` and `surface-gud/` first.

## Install internal EFI

This procedure applies to the tested Surface with the preserved appliance release `6258b2a5368a` and the EFI chain already proven on its rescue USB. Keep that USB unchanged. This is not a firmware update or a general Surface RT installer.

The new release contains a small read-only extension over the checked base image. It does not replace or delete the base. The tested boot partition has only 110144 KiB free, so a second full image does not fit.

The build also requires retained local inputs that Git does not contain. Preserve `nura/coldboot/nv-efi-kernel-probe/manifest.json` and its complete `package/`, the frozen `~/surface-releases/6258b2a5368a/rootfs.squashfs`, and the matching `~/.cache/surface-appliance/out/kernel/` build output. The builder verifies the retained EFI package and base hashes. A fresh clone alone cannot reconstruct this device-specific release. Do not commit device backups or private recovery material.

1. From the PC checkout, build the bundle:

	```sh
	python3 appliance/build-internal-efi.py --out "$HOME/surface-releases/internal-efi"
	```

	The builder prints the release ID and bundle path. It reuses the proven EFI kernel and device tree, builds the extension and its initramfs, and does not install onto the Surface.

2. Set `BUNDLE` to the printed path and choose a new backup directory on the PC:

	```sh
	BUNDLE="$HOME/surface-releases/internal-efi/<id>"
	BACKUP="$PWD/nura/coldboot/internal-efi-backup-$(date +%Y%m%d-%H%M%S)"
	```

3. Enter APX on the Surface and connect the A-to-A cable.
4. Run `appliance/load-uboot.sh`.
5. Select **mount internal storage** in the U-Boot menu.
6. Inspect the target and bundle:

	```sh
	python3 appliance/install/internal-efi-pc.py inspect --bundle "$BUNDLE"
	```

	The tool must identify the USB-exposed internal FAT partition with UUID `17C0-8631` and verify the preserved base. Do not substitute a guessed `/dev/sdX` path.

7. Install the candidate:

	```sh
	python3 appliance/install/internal-efi-pc.py install --bundle "$BUNDLE" --backup "$BACKUP"
	```

	The backup stays on the PC. Keep it for recovery. The installer changes only its declared files on the boot partition. It does not format, repartition, change firmware variables, or write partition 2.

8. Wait for successful verification and unmount before leaving USB disk mode.
9. Power off the Surface, disconnect the A-to-A cable, and reconnect the ethernet adapter.
10. Press and release **Power** without a Volume button or boot USB.
11. Verify that Linux starts without menu input and that pinned SSH reports the new `appliance.release` in `/proc/cmdline`.
12. Confirm that the display stream resumes with advancing real visible ACKs.
13. Check a short Power press while streaming. The backlight must toggle without losing the network.
14. Hold Power for about 3 seconds, then release when the confirmation appears. Verify that the release does not cancel.
15. Press Power again before the visible 30-second countdown expires. Verify cancellation, restored screen intent, and stream reconnection.
16. After boot and recovery checks succeed, let a new visible countdown expire. Verify clean shutdown and another Power-only cold boot.

Building and locally testing the bundle does not prove the physical checks. If firmware selection, panel visibility, Power events, or clean shutdown has not been observed on the Surface, record that check as unverified.

To restore the prior EFI files, re-enter APX, load RAM U-Boot, select **mount internal storage**, and run:

```sh
python3 appliance/install/internal-efi-pc.py restore --backup "$BACKUP"
```

Wait for verification and unmount before leaving USB disk mode. The restore uses the saved hashes and original absences. It must refuse unknown changes rather than delete unrelated files.

The existing `recovery-pc.sh restore` restores `extlinux.conf`, not the EFI chain. Changing its `DEFAULT` affects RAM U-Boot boots only.

The remaining sections describe the original full-image installation and RAM U-Boot recovery path.

## Check the requirements

You need this hardware:

- A Microsoft Surface RT with Nura on the eMMC. Partition 1 is the vfat boot partition (label `pmOS_boot`) and partition 2 is the ext4 root. Nura must have the user `user` with `sudo`, and `sshd` running.
- A USB ethernet adapter for the Surface. The tested adapter is `35b5:3500`.
- A USB A-to-A cable for APX recovery mode.
- A PC with an NVIDIA GPU, a free wired ethernet port, and an ethernet cable to the adapter.

The PC needs this software. The tested PC runs Ubuntu 26.04.

- GNOME on Wayland. The sender uses the Mutter ScreenCast `RecordVirtual` API.
- PipeWire, GStreamer with `nvh264enc`, and system Python 3 with PyGObject.
- NetworkManager, Docker (your user in the `docker` group), `git`, `curl`, and `udisksctl`.
- Read and write access to the APX USB device (`0955:7130`) without `sudo`. On the tested PC, the user is in the `plugdev` group.

## 1. Clone the repository

The user service expects the checkout at `~/code/sureface`.

```sh
git clone https://github.com/ChiThang-50Cent/RT-monitor.git ~/code/sureface
cd ~/code/sureface
```

## 2. Connect the PC and the Surface

1. Plug the USB ethernet adapter into the Surface and connect it to the PC's wired port.
2. On the PC, share the wired connection. NetworkManager then gives the PC the address `10.42.0.1/24`:

	```sh
	nmcli connection modify "Wired connection 1" ipv4.method shared
	nmcli connection up "Wired connection 1"
	```

3. On Nura, give the adapter the static address `10.42.0.2/24`.
4. On the PC, check the link:

	```sh
	ping -c 3 10.42.0.2
	```

## 3. Set up SSH

The sender and every script log in with the key `~/.ssh/surface_omp` and trust only the host key in `surface-known-hosts`.

```sh
ssh-keygen -t ed25519 -f ~/.ssh/surface_omp -N ''
ssh-copy-id -i ~/.ssh/surface_omp.pub user@10.42.0.2
ssh-keyscan -t ed25519 10.42.0.2 > surface-known-hosts
ssh -i ~/.ssh/surface_omp -o UserKnownHostsFile=surface-known-hosts -o StrictHostKeyChecking=yes user@10.42.0.2 true
```

The last command must exit without a prompt. The appliance OS later reuses this same host key, so `surface-known-hosts` stays valid.

## 4. Start the display sender

Copy the receiver to Nura and start the PC service:

```sh
scp -i ~/.ssh/surface_omp -o UserKnownHostsFile=surface-known-hosts \
	surface-gud/display_receiver.py surface-gud/surface_xv.py surface-gud/surface_protocol.py surface-gud/surface_power.py \
	user@10.42.0.2:/home/user/
install -Dm644 surface-gud/surface-display.service ~/.config/systemd/user/surface-display.service
systemctl --user daemon-reload
systemctl --user enable --now surface-display.service
```

GNOME now shows a 1366x768 monitor, and the Surface shows it. The service log prints a `receiver batch` line every 100 frames:

```sh
journalctl --user -u surface-display.service -f
```

## 5. Build the RAM U-Boot

For the original recovery path, the PC loads U-Boot into the Surface's RAM through APX after each power loss. An installed and physically accepted internal EFI release uses its separate Power-only startup path.

1. Install the ARM toolchain and the USB library for the exploit script:

	```sh
	sudo apt install gcc-arm-none-eabi
	python3 -m venv .venv
	.venv/bin/pip install pyusb
	```

2. Clone the loader tools at the tested commit:

	```sh
	git clone https://github.com/tofurky/tegra30_debrick.git
	git -C tegra30_debrick checkout 7b43567
	git -C tegra30_debrick submodule update --init
	```

3. Build U-Boot with the boot menu patch:

	```sh
	git clone https://github.com/u-boot/u-boot.git
	cd u-boot
	git checkout 55084065
	git apply ../bootloader/u-boot-bootmenu.patch
	CROSS_COMPILE=arm-none-eabi- make surface-rt_defconfig
	CROSS_COMPILE=arm-none-eabi- make
	cd ..
	```

4. Compare the hash with the one that `appliance/load-uboot.sh` expects:

	```sh
	sha256sum u-boot/u-boot-dtb-tegra.bin
	grep UBOOT_SHA256= appliance/load-uboot.sh
	```

	The tested build is `269262c1…`. If your hash differs, put your hash in `UBOOT_SHA256` in `appliance/load-uboot.sh`.

5. Test the loader. Shut down Nura, then hold **Power** and **Volume Up** until the Surface enters APX mode. Connect the Surface's USB port to the PC with the A-to-A cable. Then run:

	```sh
	lsusb -d 0955:7130
	appliance/load-uboot.sh
	```

	The script prints `U-Boot started`, and the Surface shows the U-Boot boot menu. Use the volume buttons to move and the power button to select. Select **boot Linux from internal storage** to start Nura again.

> [!WARNING]
> Run `load-uboot.sh` only while the Surface is in APX mode. If an `nvflash` call reaches the boot ROM before the exploit runs, the boot ROM stops answering until you enter APX mode again.

## 6. Prepare the kernel build inputs

`appliance/build-kernel.sh` builds without `sudo`. It reads its tools from `~/.cache/surface-kbuild` and `build-deps/root`. These commands reconstruct how the tested PC was set up. Nobody has re-run them from scratch.

1. Download the pinned kernel source and the LLVM toolchain:

	```sh
	mkdir -p ~/.cache/surface-kbuild
	cd ~/.cache/surface-kbuild
	curl -sSfL -o linux-grate.tar.gz \
		https://codeberg.org/libre-tegra/linux/archive/bfffc6d6656e87f3de3aa4c09d852849081ef088.tar.gz
	curl -sSfL https://github.com/llvm/llvm-project/releases/download/llvmorg-22.1.8/LLVM-22.1.8-Linux-X64.tar.xz | tar xJ
	```

	`build-kernel.sh` checks the tarball's SHA-512 before it builds.

2. Extract `pahole` into `pahole-root`:

	```sh
	apt download pahole libdwarves1
	for d in pahole_*.deb libdwarves1_*.deb; do dpkg -x "$d" pahole-root; done
	```

3. If `LLVM-22.1.8-Linux-X64/bin/clang` cannot find `libxml2.so.2`, link it to the system library:

	```sh
	mkdir -p shimlib
	ln -s /usr/lib/x86_64-linux-gnu/libxml2.so.16 shimlib/libxml2.so.2
	```

4. Extract the host build tools into `build-deps/root` in the repository:

	```sh
	cd ~/code/sureface
	mkdir -p build-deps && cd build-deps
	apt download bc bison flex m4 libelf-dev libzstd-dev
	for d in *.deb; do dpkg -x "$d" root; done
	ln -sf /usr/lib/x86_64-linux-gnu/libelf.so.1 root/usr/lib/x86_64-linux-gnu/libelf.so.1
	ln -sf /usr/lib/x86_64-linux-gnu/libzstd.so.1 root/usr/lib/x86_64-linux-gnu/libzstd.so.1
	cd ..
	```

## 7. Build the appliance OS

1. Download the Alpine packages that `appliance/packages.lock` lists:

	```sh
	appliance/lock.sh
	```

	> [!NOTE]
	> `lock.sh` resolves `appliance/packages.txt` against today's Alpine edge and rewrites `packages.lock`. Edge moves, so you can get newer versions than the tested set. The committed `packages.lock` records the tested set. Use `git diff appliance/packages.lock` to see what changed.

2. Build the kernel, the root filesystem, the initramfs, and the release bundle:

	```sh
	appliance/build.sh
	```

	The last line names the release, for example `published /home/you/surface-releases/6258b2a5368a`. The rest of this guide calls that ID `<id>`. Later builds skip the kernel when its inputs have not changed. A rootfs-only rebuild takes about 10 seconds.

## 8. Stage the release on Nura

Staging copies the release to the boot partition and adds a boot entry. Nura stays the default.

```sh
ssh -T -i ~/.ssh/surface_omp -o UserKnownHostsFile=surface-known-hosts user@10.42.0.2 \
	mkdir -p /home/user/appliance-release
scp -r -i ~/.ssh/surface_omp -o UserKnownHostsFile=surface-known-hosts \
	~/surface-releases/<id> user@10.42.0.2:/home/user/appliance-release/
ssh -t -i ~/.ssh/surface_omp -o UserKnownHostsFile=surface-known-hosts user@10.42.0.2 \
	'cd /home/user/appliance-release/<id> && sha256sum -c SHA256SUMS && sudo sh ./surface-install.sh .'
```

Enter the Nura password when `sudo` asks. The output ends like this:

```text
staged appliance-<id>; Nura boot files unchanged
DEFAULT surface-rt
LABEL surface-rt
LABEL appliance-<id>
```

The first install also saves the original `extlinux.conf` and copies the SSH host key and `authorized_keys` to the boot partition. The appliance loads them at boot.

## 9. Boot the appliance for the first time

1. Shut down Nura:

	```sh
	ssh -t -i ~/.ssh/surface_omp -o UserKnownHostsFile=surface-known-hosts user@10.42.0.2 sudo poweroff
	```

2. Enter APX mode, connect the A-to-A cable, and run `appliance/load-uboot.sh`.
3. In the boot menu, select **mount internal storage**. The PC now sees the eMMC as a USB disk.
4. Make the appliance the default boot entry:

	```sh
	appliance/recovery-pc.sh status
	appliance/recovery-pc.sh select <id>
	```

	`status` must show `recovery copy present`. `select` prints `DEFAULT appliance-<id>`.

5. Press a button on the Surface to leave the USB disk mode. Then select **boot Linux from internal storage**.
6. Wait for the screen to show **Surface RT display**. Unplug the A-to-A cable and plug in the ethernet adapter.
7. Check the boot:

	```sh
	appliance/verify-boot.sh <id>
	```

	The last line is `result: PASS`. Within a few seconds, the sender connects and the Surface shows your second monitor.

If the appliance does not boot, follow [Recover when the appliance does not boot](operate.md#recover-when-the-appliance-does-not-boot).
