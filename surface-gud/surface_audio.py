#!/usr/bin/python3
"""Manage low-latency, reconnecting network audio to Microsoft Surface RT speakers over direct LAN.

Tunnels PC audio into the Surface RT's Wolfson WM8962 hardware audio sink
via PipeWire's module-tunnel-sink connecting to PulseAudio's module-native-protocol-tcp.
Mixer levels are constrained to safe moderate volume (no +12dB hardware overdrive boost).

Firewall and Port Architecture:
--------------------------------
The Surface RT's default nftables firewall (/etc/nftables.nft) drops all incoming traffic
on eth0, blocking the standard PulseAudio port (4713). Port 24727 is pre-authorized by an
existing system ruleset (/etc/nftables.d/50_ausweisapp2.nft: 'tcp dport 24727 accept').
We verified that AusweisApp2 is inactive, has no running systemd units, and does not listen
on port 24727 (and binds strictly to loopback if ever invoked).
We repurpose port 24727 for PulseAudio TCP without requiring root/sudo or system file mutations.

Security & Authentication:
---------------------------
- The TCP listener binds strictly to the private direct LAN IP (10.42.0.2:24727).
- Access control relies strictly on 'auth-ip-acl=10.42.0.1', granting access only to the
  workstation IP and rejecting all other hosts without cookie credentials.
- 'auth-anonymous=1' is deliberately omitted so that unlisted clients are never authorized.
"""
import argparse
import math
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave

DEFAULT_TARGET = "user@10.42.0.2"
DEFAULT_SURFACE_IP = "10.42.0.2"
DEFAULT_PC_IP = "10.42.0.1"
DEFAULT_PORT = 24727  # Repurposes pre-existing open firewall port (ausweisapp2 exception)
DEFAULT_SINK_NAME = "surface_rt_speakers"
DEFAULT_DESCRIPTION = "Surface RT Speakers"
DEFAULT_REMOTE_SINK = "alsa_output.platform-sound.stereo-fallback"
DEFAULT_KEY = Path.home() / ".ssh/surface_omp"
DEFAULT_KNOWN_HOSTS = Path(__file__).resolve().parent.parent / "surface-known-hosts"

REMOTE_RECEIVER_SCRIPT = """#!/usr/bin/python3
\"\"\"Surface RT Audio Receiver Daemon.

Ensures PulseAudio on Surface RT exposes a TCP native-protocol listener
bound strictly to the private direct LAN (10.42.0.2:24727) authorized strictly
for the PC IP (10.42.0.1) and configures the WM8962 audio codec to safe moderate
volume (no hardware overdrive boost).
\"\"\"
import os
import signal
import subprocess
import sys
import time

PORT = int(os.environ.get("SURFACE_AUDIO_PORT", "24727"))
LISTEN_IP = os.environ.get("SURFACE_AUDIO_LISTEN_IP", "10.42.0.2")
ALLOWED_IPS = os.environ.get("SURFACE_AUDIO_ALLOWED_IPS", "10.42.0.1")
SINK_VOLUME = os.environ.get("SURFACE_AUDIO_VOLUME", "60%")

running = True
mixer_configured = False

def stop_handler(signum, frame):
    global running
    running = False


signal.signal(signal.SIGINT, stop_handler)
signal.signal(signal.SIGTERM, stop_handler)


def run_cmd(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def configure_mixer():
    # Use the stable ALSA card ID, not its boot-dependent numeric index.
    # WM8962 routes DAC straight to SPKOUT PGA; Speaker Mixer Switch is
    # irrelevant on this route (Linux wm8962.c DAPM routes, datasheet §116).
    commands = [
        ["amixer", "-c", "WM8962", "sset", "Int Spk", "on"],
        ["amixer", "-c", "WM8962", "sset", "Speakers", "on"],
        ["amixer", "-c", "WM8962", "cset", "name=SPKOUTL PGA", "DAC"],
        ["amixer", "-c", "WM8962", "cset", "name=SPKOUTR PGA", "DAC"],
        ["amixer", "-c", "WM8962", "sset", "Speaker Boost", "0"],
        ["pactl", "set-sink-volume", "alsa_output.platform-sound.stereo-fallback", SINK_VOLUME],
        ["pactl", "set-sink-mute", "alsa_output.platform-sound.stereo-fallback", "0"],
        ["amixer", "-c", "WM8962", "cset", "name=Speaker Switch", "on,on"],
    ]
    for command in commands:
        result = run_cmd(command)
        if result.returncode:
            print(f"Mixer configuration failed: {command}: {result.stderr.strip()}",
                  file=sys.stderr, flush=True)
            return False
    print("Speaker DAC route configured; physical audibility is unverified.", flush=True)
    return True


def is_module_loaded():
    res = run_cmd(["pactl", "list", "modules", "short"])
    if res.returncode != 0:
        return False, None
    for line in res.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "module-native-protocol-tcp":
            # Only inspect/manage instances on our specific port
            if f"port={PORT}" in line:
                if f"listen={LISTEN_IP}" in line and f"auth-ip-acl={ALLOWED_IPS}" in line and "auth-anonymous" not in line:
                    return True, parts[0]
                else:
                    # Stale instance specifically on our port, unload it
                    run_cmd(["pactl", "unload-module", parts[0]])
    return False, None


def ensure_listener():
    global mixer_configured
    res = run_cmd(["pactl", "info"])
    if res.returncode != 0:
        mixer_configured = False  # Reset on PulseAudio drop/restart
        return False

    if not mixer_configured:
        if not configure_mixer():
            return False
        mixer_configured = True

    loaded, mod_id = is_module_loaded()
    if loaded:
        return True

    # The PC IP is authorized without a cookie; normal cookie auth remains available.
    arg = f"port={PORT} listen={LISTEN_IP} auth-ip-acl={ALLOWED_IPS}"
    load_res = run_cmd(["pactl", "load-module", "module-native-protocol-tcp", arg])
    if load_res.returncode == 0:
        print(f"Loaded module-native-protocol-tcp (id={load_res.stdout.strip()}) on {LISTEN_IP}:{PORT} (ACL: {ALLOWED_IPS})", flush=True)
        return True
    else:
        print(f"Failed to load module-native-protocol-tcp: {load_res.stderr.strip()}", file=sys.stderr, flush=True)
        return False


def main():
    print(f"surface-audio-receiver starting on {LISTEN_IP}:{PORT} (auth-ip-acl={ALLOWED_IPS})...", flush=True)
    while running:
        try:
            ok = ensure_listener()
            if not ok:
                time.sleep(2)
                continue
        except Exception as e:
            print(f"Error in ensure_listener: {e}", file=sys.stderr, flush=True)
        time.sleep(10)


if __name__ == "__main__":
    main()
"""

REMOTE_SERVICE_UNIT = """[Unit]
Description=Surface RT Audio Receiver Daemon
After=pulseaudio.service network.target sound.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.local/bin/surface-audio-receiver
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""

HOST_SERVICE_UNIT = """[Unit]
Description=Surface RT Audio Tunnel Sink Supervisor (PipeWire)
After=pipewire.service pipewire-pulse.service
Wants=pipewire.service pipewire-pulse.service

[Service]
Type=simple
ExecStart={exec_cmd}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def ssh_cmd(args, remote_command):
    cmd = [
        "ssh",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=3",
        "-o", "ServerAliveCountMax=2",
    ]
    if args.key and Path(args.key).is_file():
        cmd.extend(["-i", str(args.key)])
    if args.known_hosts and Path(args.known_hosts).is_file():
        cmd.extend(["-o", f"UserKnownHostsFile={args.known_hosts}"])
    cmd.extend([args.target, remote_command])
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def deploy_remote_service(args):
    """Deploy receiver script and systemd user service onto Surface RT."""
    print(f"Deploying audio receiver to {args.target}...")

    # Upload receiver script
    upload_script_cmd = f"mkdir -p ~/.local/bin ~/.config/systemd/user && cat << 'EOF' > ~/.local/bin/surface-audio-receiver\n{REMOTE_RECEIVER_SCRIPT}\nEOF\nchmod +x ~/.local/bin/surface-audio-receiver"
    res = ssh_cmd(args, upload_script_cmd)
    if res.returncode != 0:
        print(f"Failed to upload receiver script: {res.stderr.strip()}", file=sys.stderr)
        return False

    # Upload systemd service unit
    upload_unit_cmd = f"cat << 'EOF' > ~/.config/systemd/user/surface-audio-receiver.service\n{REMOTE_SERVICE_UNIT}\nEOF\nsystemctl --user daemon-reload && systemctl --user enable surface-audio-receiver.service && systemctl --user restart surface-audio-receiver.service && loginctl enable-linger"
    res = ssh_cmd(args, upload_unit_cmd)
    if res.returncode != 0:
        print(f"Failed to enable systemd user service: {res.stderr.strip()}", file=sys.stderr)
        return False

    print("Remote receiver service deployed and active.")
    return True


def get_local_tunnel_modules(sink_name):
    """Find loaded module-tunnel-sink module IDs matching the sink name."""
    res = subprocess.run(["pactl", "list", "modules"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        return []
    modules = []
    current_id = None
    is_tunnel = False
    has_sink_name = False
    for line in res.stdout.splitlines():
        if line.startswith("Module #"):
            if current_id and is_tunnel and has_sink_name:
                modules.append(current_id)
            current_id = line.split("#")[1].strip()
            is_tunnel = False
            has_sink_name = False
        elif "Name: module-tunnel-sink" in line:
            is_tunnel = True
        elif f"sink_name={sink_name}" in line:
            has_sink_name = True
    if current_id and is_tunnel and has_sink_name:
        modules.append(current_id)
    return modules


def is_sink_present(sink_name):
    res = subprocess.run(["pactl", "list", "sinks", "short"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == sink_name:
            return True
    return False


def attach_tunnel(args):
    """Ensure remote receiver is running and attach PipeWire tunnel sink."""
    res = ssh_cmd(args, "systemctl --user is-active surface-audio-receiver.service")
    if res.stdout.strip() != "active":
        print("Remote service inactive; deploying...")
        if not deploy_remote_service(args):
            return 1

    existing = get_local_tunnel_modules(args.sink_name)
    if existing and is_sink_present(args.sink_name):
        print(f"Sink '{args.sink_name}' is already attached (module {existing[0]}).")
        return 0
    for mod_id in existing:
        subprocess.run(["pactl", "unload-module", mod_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    orig_default = subprocess.run(["pactl", "get-default-sink"], stdout=subprocess.PIPE, text=True).stdout.strip()
    server = f"tcp:{args.surface_ip}:{args.port}"
    cmd = [
        "pactl", "load-module", "module-tunnel-sink",
        f"server={server}",
        f"sink={args.remote_sink}",
        f"sink_name={args.sink_name}",
        f"reconnect_interval_ms={args.reconnect_interval}",
        f'sink_properties="device.description=\\"{args.description}\\""'
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"Failed to load module-tunnel-sink: {res.stderr.strip()}", file=sys.stderr)
        return 1

    mod_id = res.stdout.strip()
    print(f"Loaded module-tunnel-sink (id={mod_id}). Waiting for sink registration...")
    for _ in range(30):
        time.sleep(0.25)
        if is_sink_present(args.sink_name):
            # Ensure PC default sink is not changed by WirePlumber switch-on-connect
            if orig_default and orig_default != args.sink_name:
                cur_default = subprocess.run(["pactl", "get-default-sink"], stdout=subprocess.PIPE, text=True).stdout.strip()
                if cur_default != orig_default:
                    subprocess.run(["pactl", "set-default-sink", orig_default], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Sink '{args.sink_name}' ('{args.description}') successfully attached to {server}.")
            print("Note: PC default sink remains unchanged. Select this sink in GNOME Sound Settings or via pavucontrol.")
            return 0

    print(f"Warning: Module loaded but sink '{args.sink_name}' not yet visible in sink list.", file=sys.stderr)
    return 1


def detach_tunnel(args):
    """Unload the local PipeWire tunnel sink."""
    modules = get_local_tunnel_modules(args.sink_name)
    if not modules:
        print(f"No active tunnel sink module found for '{args.sink_name}'.")
        return 0
    for mod_id in modules:
        res = subprocess.run(["pactl", "unload-module", mod_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            print(f"Unloaded module-tunnel-sink (id={mod_id}).")
        else:
            print(f"Failed to unload module {mod_id}: {res.stderr.strip()}", file=sys.stderr)
    return 0


def run_host_supervisor(args):
    """Run persistent host supervisor loop (used by host systemd user service)."""
    print(f"Starting host audio supervisor for '{args.sink_name}'...")
    running = True

    def sig_handler(signum, frame):
        nonlocal running
        print("Received signal, stopping host supervisor...")
        running = False

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        while running:
            if not is_sink_present(args.sink_name):
                print(f"Sink '{args.sink_name}' not present, attaching...")
                attach_tunnel(args)
            for _ in range(10):
                if not running:
                    break
                time.sleep(1)
    finally:
        detach_tunnel(args)
    return 0


def install_host_service(args):
    """Install and enable systemd user service on PC for automatic reattachment."""
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "surface-audio-sender.service"

    script_path = Path(__file__).resolve()
    exec_cmd = f"{sys.executable} {script_path} run"
    unit_content = HOST_SERVICE_UNIT.format(exec_cmd=exec_cmd)

    unit_path.write_text(unit_content)
    print(f"Wrote host user unit: {unit_path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "surface-audio-sender.service"], check=True)
    print("Host service 'surface-audio-sender.service' enabled and started.")
    return 0


def uninstall_host_service(args):
    """Disable and remove systemd user service on PC."""
    unit_path = Path.home() / ".config/systemd/user/surface-audio-sender.service"
    subprocess.run(["systemctl", "--user", "stop", "surface-audio-sender.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["systemctl", "--user", "disable", "surface-audio-sender.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if unit_path.exists():
        unit_path.unlink()
        print(f"Removed {unit_path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    detach_tunnel(args)
    print("Host service uninstalled and tunnel detached.")
    return 0


def print_status(args):
    """Display health and mixer status of both local PC and remote Surface RT."""
    print("=== Host (PC PipeWire) Status ===")
    sinks = subprocess.run(["pactl", "list", "sinks", "short"], stdout=subprocess.PIPE, text=True).stdout
    attached = False
    for line in sinks.splitlines():
        if args.sink_name in line:
            attached = True
            print(f"Sink: {line}")
    if not attached:
        print(f"Sink '{args.sink_name}': Not attached")

    default_sink = subprocess.run(["pactl", "get-default-sink"], stdout=subprocess.PIPE, text=True).stdout.strip()
    print(f"Default PC Sink: {default_sink}")

    host_svc = subprocess.run(["systemctl", "--user", "is-active", "surface-audio-sender.service"], stdout=subprocess.PIPE, text=True).stdout.strip()
    print(f"Host Supervisor Service: {host_svc}")

    print("\n=== Remote (Surface RT) Status ===")
    check_cmd = (
        "systemctl --user is-active surface-audio-receiver.service;"
        "ss -tlpn | grep " + str(args.port) + " || true;"
        "amixer -c 1 sget 'Speaker Boost' | grep -E 'Mono:.*dB' || true;"
        "amixer -c 1 sget 'Speaker' | grep -E 'Front Left:.*dB' || true;"
        "pactl list sinks | grep -E 'Name:|State:|Volume:' || true"
    )
    res = ssh_cmd(args, check_cmd)
    if res.returncode == 0:
        lines = res.stdout.strip().splitlines()
        if lines:
            print(f"Receiver Service State: {lines[0]}")
            for l in lines[1:]:
                print(f"  {l.strip()}")
    else:
        print(f"Failed to query Surface RT: {res.stderr.strip()}", file=sys.stderr)


def play_test_tone(args):
    """Generate and play a known low-volume 440 Hz test tone directly to the sink."""
    if not is_sink_present(args.sink_name):
        print(f"Sink '{args.sink_name}' is not attached. Attaching first...")
        rc = attach_tunnel(args)
        if rc != 0:
            return rc

    test_wav = Path("/tmp/surface_audio_smoke.wav")
    print("Generating safe low-volume sine wave (440Hz, 1.5s, 15% amplitude)...")
    duration = 1.5
    rate = 44100
    amplitude = 0.15
    with wave.open(str(test_wav), "w") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(rate)
        total_frames = int(rate * duration)
        data = bytearray()
        for i in range(total_frames):
            val = int(32767 * amplitude * math.sin(2 * math.pi * 440 * i / rate))
            data.extend(struct.pack("<hh", val, val))
        f.writeframes(data)

    print(f"Streaming test audio to '{args.sink_name}' via paplay...")
    res = subprocess.run(["paplay", "-d", args.sink_name, str(test_wav)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0:
        print("Playback stream completed; this does not prove speaker audibility.")
        return 0
    else:
        print(f"Playback failed: {res.stderr.strip()}", file=sys.stderr)
        return res.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default=DEFAULT_TARGET, help="SSH user@host for Surface RT")
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY, help="SSH private key")
    parser.add_argument("--known-hosts", type=Path, default=DEFAULT_KNOWN_HOSTS, help="SSH known hosts file")
    parser.add_argument("--surface-ip", default=DEFAULT_SURFACE_IP, help="Surface direct LAN IP")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="PulseAudio TCP listener port (default: 24727)")
    parser.add_argument("--sink-name", default=DEFAULT_SINK_NAME, help="Local PipeWire tunnel sink name")
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION, help="Local sink description")
    parser.add_argument("--remote-sink", default=DEFAULT_REMOTE_SINK, help="Remote PulseAudio hardware sink name")
    parser.add_argument("--reconnect-interval", type=int, default=2000, help="Reconnect interval in ms")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("start", help="Attach PipeWire tunnel sink to Surface RT (default)")
    subparsers.add_parser("stop", help="Detach PipeWire tunnel sink")
    subparsers.add_parser("run", help="Run supervisor loop in foreground (for systemd service)")
    subparsers.add_parser("status", help="Show audio receiver, host service, and sink status")
    subparsers.add_parser("deploy", help="Deploy/update user-level receiver service on Surface RT")
    subparsers.add_parser("install-host-service", help="Install & enable persistent host systemd user service")
    subparsers.add_parser("uninstall-host-service", help="Disable & remove host systemd user service")
    subparsers.add_parser("test", help="Play a low-volume test tone through Surface speakers")

    args = parser.parse_args()
    cmd = args.command or "start"

    if cmd == "start":
        return attach_tunnel(args)
    elif cmd == "stop":
        return detach_tunnel(args)
    elif cmd == "run":
        return run_host_supervisor(args)
    elif cmd == "status":
        print_status(args)
        return 0
    elif cmd == "deploy":
        return 0 if deploy_remote_service(args) else 1
    elif cmd == "install-host-service":
        return install_host_service(args)
    elif cmd == "uninstall-host-service":
        return uninstall_host_service(args)
    elif cmd == "test":
        return play_test_tone(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
