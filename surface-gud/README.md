# Surface RT display and speakers

## Extended desktop over LAN (current path)

`display_sender.py` owns a dedicated Mutter virtual monitor and sends native
1366x768 H.264 at a **30 fps target** over pinned SSH to the Surface. This is
an extended desktop, not a mirror or RDP session.

This is the running Linux/Xorg trial for the eventual dedicated-display OS,
not a completed custom OS or standalone boot image. XFCE lock/screensaver
edge cases are outside the current trial's acceptance: the intended appliance
should own display power and presentation without the general-purpose desktop.

The running path is PipeWire → NVIDIA NVENC → SSH/LAN → Tegra VDE →
DMA-BUF/Xv passthrough. Python controls framing and lifecycle; it does not
encode video or copy decoded Surface pixels.

- Captured and encoded pixels are native 1366x768; encoder caps are 30/1. The
  panel remains at 1366x768; Xorg reports 60 Hz but DRM vblanks measure
  61.78 Hz. Stream target is not a stable-rate guarantee.
- NVENC uses baseline/CAVLC, no B frames or lookahead, ultra-low-latency tuning,
  a 12 Mbit/s CBR target, and repeated sequence headers.
- H.264 is lossy 4:2:0. Native geometry does **not** imply pixel-exact RGB:
  fine colored text can lose chroma detail. This replaces the lossless LZ4/
  copy-region implementation rather than retaining a fallback.
- The PC cursor is embedded in capture. Surface touch/input is not forwarded.
- A latest-only raw capture sink discards stale frames **before** encoding.
  A monotonic host frame clock selects images at 30 fps and repeats the last
  owned capture while idle. It does not depend on PipeWire keepalive timestamps
  or `videorate` to keep the transport alive. Three admission credits bound the
  entire encoder/transport/presentation path; only an ordered visible ACK
  releases a credit. Encoded interframes are never dropped independently.
- `pipewiresrc use-bufferpool=false` avoids retaining borrowed producer buffers.
  This entails a host-side pixel copy in the installed PipeWire implementation;
  the complete path is not end-to-end zero-copy. Encoder input gets its own
  timing metadata while sharing captured NV12 pixel memory and VideoMeta.
- Each wire frame is a network-order uint64 sequence and uint32 length,
  followed by one Annex-B H.264 access unit, limited to 4 MiB.
- `display_receiver.py` explicitly uses `v4l2slh264dec`. `surface_xv.py` imports
  the decoder's DMA-BUF planes through authenticated DRI2/GEM and submits the
  existing Tegra Xv passthrough V1 header. Visible geometry is 1366x768;
  decoder allocation width 1376 is stride padding, not display scaling.
- The presenter retains the previous decoded sample until its replacement's
  blocking commit completes. ACK follows Xv presentation, XSync and the
  driver's overlay-visible flag. This is software evidence, not an optical
  measurement of panel scanout or input-to-photon latency.
- Presentation is vblank-paced: decoded frames enter a queue of at most three,
  playback starts with two queued, and each commit is submitted after the
  vblank before its slot so it latches every second vblank (~30.9 Hz cadence).
  An empty queue delays the next frame by one vblank. Measured on YouTube
  content: 94% of frame intervals exactly 2 vblanks versus 67% when presenting
  immediately; fresh capture→ACK p50/p95 88/101 ms versus 71/87 ms.
- The capture/Mutter monitor stays alive across receiver reconnects. Each
  connection gets a fresh encoder and starts at sequence zero, with fresh
  codec dependencies. Both endpoints run continuously, not for a finite trial.
  The host frame pump also supplies idle reconnects from the last captured image.
- The receiver inhibits XFCE screensaver and automatic power management while
  connected; the dedicated appliance session retains its existing policy.
  Manual locking/suspend recovery is not guaranteed.
- Host service `surface-display.service` remains enabled for the graphical
  session. No host root permissions or kernel module are needed.

Requirements: GNOME/Mutter ScreenCast `RecordVirtual`, PipeWire, system Python
with PyGObject/Gio/GStreamer, `pipewiresrc`, `videoconvert`,
`appsrc`/`appsink`, and working NVIDIA `nvh264enc` on the PC. On the Surface:
PyGObject/GStreamer, `h264parse`, `v4l2slh264dec`, GstVideoMeta/DMA-BUF support,
libX11, libXv, libdrm, libX11-xcb, libxcb-dri2, and the Tegra Xorg driver's
passthrough V1 implementation. XFCE screensaver and power manager are required
in normal desktop mode. Pillow, Cairo and LZ4 are no longer display dependencies.
The sender uses a process-local fresh GStreamer registry because the cached
registry on this host hides the installed NVENC element.

Mutter's ScreenCast interface is private; version compatibility is not
guaranteed. Deploy both endpoints and their shared protocol together; old
lossless framing is not supported.

The deployed setup uses `user@10.42.0.2`, X11 `:0`, UID 10000, SSH key
`~/.ssh/surface_omp`, and the repository's `surface-known-hosts` pin.
`display_sender.py --help` exposes target/key/known-hosts overrides.

```sh
# Deploy receiver with the same pinned SSH options used for the connection.
scp -i ~/.ssh/surface_omp -o UserKnownHostsFile=surface-known-hosts \
  -o StrictHostKeyChecking=yes surface-gud/display_receiver.py \
  surface-gud/surface_xv.py surface-gud/surface_protocol.py surface-gud/surface_power.py \
  user@10.42.0.2:/home/user/

# Foreground mode (do not run alongside the service).
/usr/bin/python3 surface-gud/display_sender.py

# First installation; unit assumes this checkout is ~/code/sureface.
install -Dm644 surface-gud/surface-display.service \
  ~/.config/systemd/user/surface-display.service
systemctl --user daemon-reload
systemctl --user enable --now surface-display.service

# Apply endpoint updates when the service is already running.
systemctl --user restart surface-display.service
systemctl --user status surface-display.service
journalctl --user -u surface-display.service
```

The Surface must already be running the appliance OS (`appliance/`) or the
Nura XFCE session, with its SSH server up. Stopping
the host service removes its virtual monitor; a host service restart recreates
it, unlike a receiver-only reconnect. Internal EFI installation and its physical
cold-boot checks are documented in [Install internal EFI](../docs/install.md#install-internal-efi).
Until those checks pass, keep the proven rescue USB and APX recovery assets.

The appliance receiver requires the matching `surface-idle` presentation controller.
Do not copy a new receiver alone onto the preserved base appliance. Install its
complete extension release. Nura desktop operation does not acquire that lease.

Logs report 100-frame batches: acknowledged throughput, compressed payload
bandwidth, fresh-capture-timestamp-to-ACK upper bounds, wire-to-ACK duration,
and receiver presentation intervals. Source time is correlated against the
capture pipeline's system clock/base time, not NVENC's shifted output
timestamp. Source-age `n` counts only new captures; `n=0` is normal when
repeating an unchanged idle image. ACK counts are not unique-content FPS.

Ordinary GTK motion smokes exercise the production capture/network path, not
a preloaded decoder clip. Native caps and visible 1366x768 presentation have
been observed, but average throughput alone does not establish even 30 fps.
Root/window screenshots can omit the hardware overlay; physical sharpness,
cursor feel and tearing still require the user's panel check.

An independent check found that frame input stopped during ordinary desktop
idle while the service remained active. The current sender separates the
monotonic frame pump and its idle image cache from source capture; motion
checks must verify new captures resume, not just that cached-frame ACKs continue.

Final independent runtime check (`VerifyFramePumpFinal`):

| Measurement | Observed |
| --- | --- |
| Continuous observation | 190.313 seconds |
| Aggregate software presentation ACK rate | 29.9985/s |
| Batch median presentation gap | 32.30–32.57 ms |
| Worst batch p95 / maximum individual gap | 49.56 / 65.43 ms |
| Receiver RSS, start → end | 31,160 → 31,356 KiB |
| Sender / receiver FD counts | 91 / 48, unchanged |

Two real 20-second GTK motion phases were separated by 120 seconds idle,
followed by another 30 seconds idle. New captures resumed in both moving
phases; cached-frame ACKs continued through idle. The sender and receiver
PIDs stayed unchanged, three deployed hashes matched, and no disconnect or
stream error occurred. The service was left running; audio processes were
unchanged. This is a short software-visible continuity check, not optical
proof, perfectly even 30 fps or a long-term leak test.

Independent fullscreen video follow-ups on the same deployed artifacts:

| Generated content | Duration | Software presentation ACK rate | Maximum gap | Payload |
| --- | --- | --- | --- | --- |
| Moving ball, native fullscreen surface | 45.237 s | 30.004/s | 68.00 ms | 0.20–3.65 Mbit/s |
| Snow, all-image temporal changes | 30.225 s | 29.481/s | 64.34 ms | 10.11–10.76 Mbit/s |

Both host video surfaces were confirmed at 1366x768, position (1920,0).
The host sinks rendered 1,349 and 899 frames respectively without reporting
drops; every production batch contained fresh captures, with native visible
geometry preserved. There were no stream errors or disconnects, both process
IDs stayed unchanged and the service was left running. These synthetic cases
are not YouTube, decoded-pixel checks or optical/quality proof. **Strict
sustained 30 fps was not achieved in the snow stress case**, and the ball
case still had uneven frame spacing. Do not describe this as guaranteed
smooth 30 fps.

The first independent sustained check exercised motion, idle and fullframe
synthetic video for about 150 seconds, then found that a receiver-only exit
allowed XFCE's blanker to cover the next receiver. The receiver now uses the
normal [XFCE activity API](https://docs.xfce.org/apps/xfce4-screensaver/4.18/dbus#methods)
at startup and resets an inactive idle timer before
releasing inhibition; authentication is not bypassed. Long-idle/reconnect
qualification is not claimed for this desktop trial.

## Surface speakers

`surface_audio.py` exposes the PC sink `surface_rt_speakers`, described as
**Surface RT Speakers**, without selecting it as the PC default. Choose it in
GNOME Sound Settings or direct individual applications to that sink.

```sh
/usr/bin/python3 surface-gud/surface_audio.py status
/usr/bin/python3 surface-gud/surface_audio.py test
/usr/bin/python3 surface-gud/surface_audio.py install-host-service
systemctl --user status surface-audio-sender.service
```

The remote user service `surface-audio-receiver.service` maintains a PulseAudio
native listener on `10.42.0.2:24727`, authorizing the PC IP `10.42.0.1` without
anonymous authentication. Port 24727 reuses the existing AusweisApp firewall
exception on this image; this is not the standard PulseAudio port. Changing
that rule or running another service on that interface/port requires revisiting
this choice. No firewall rule was added for audio.
This is unencrypted PulseAudio TCP on the dedicated LAN. The PC address is
authorized without a cookie; normal PulseAudio cookie authentication is also
available. The IP ACL is not an exclusive client-identity boundary.

The receiver initially enables the WM8962 internal speaker route, sets hardware
boost to 0 dB and PulseAudio volume to 60%. Later user volume changes are
preserved. Host and remote user services recover the tunnel after disconnection.
The user reports no speaker sound. Local and network tone playback completed,
but PCM `RUNNING` is not acoustic proof and the silence is not yet fixed.
`surface_audio_diagnose.py` collects bounded, read-only ALSA, kernel and debugfs
diagnostics. Root access is needed for the latter two on this image; collect
them during playback before changing codec registers, clocks or power rails.

## Integrated cameras

No integrated camera capture device is currently available. `/dev/video0`
identifies as `tegra-vde`, the video decoder, **not a webcam**. The
[postmarketOS Surface RT feature matrix](https://wiki.postmarketos.org/wiki/Microsoft_Surface_RT_(microsoft-surface-rt))
marks cameras broken. Camera export remains blocked on capture/sensor driver
and device-tree support; no replacement kernel has been installed.

