#!/usr/bin/python3
"""Read-only WM8962 power-path diagnostics; run on Surface, optionally as root."""
import os
from pathlib import Path
import subprocess


def command(args):
    print("\n$ " + " ".join(args), flush=True)
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=8)
        print(result.stdout[-24000:], flush=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(error, flush=True)


def show(path):
    print("\n=== " + str(path) + " ===", flush=True)
    try:
        with path.open() as stream:
            print(stream.read(16000), flush=True)
    except OSError as error:
        print(error, flush=True)


print("WM8962 read-only diagnostics; uid=" + str(os.getuid()), flush=True)
command(["uname", "-a"])
command(["dmesg", "--level=err,warn"])
command(["amixer", "-c", "WM8962", "contents"])
for name in ("cards", "card1/pcm0p/sub0/status", "card1/pcm0p/sub0/hw_params"):
    show(Path("/proc/asound") / name)
# Only read relevant codec/card debugfs files; never alter register or power state.
root = Path("/sys/kernel/debug")
count = 0
for directory in (root / "asoc", root / "regmap"):
    print("\n=== " + str(directory) + " ===", flush=True)
    try:
        for current, dirs, files in os.walk(directory, onerror=lambda error: print(error)):
            relative = Path(current).relative_to(directory)
            if len(relative.parts) > 5:
                dirs[:] = []
                continue
            for name in sorted(files):
                path = Path(current) / name
                relevant = directory.name == "asoc" or "wm8962" in str(path).lower() or "001a" in str(path)
                if not relevant or name in ("access", "ranges"):
                    continue
                if name == "registers" or name in ("bias_level", "dapm_pop_time") or "dapm" in path.parts or len(relative.parts) == 0:
                    show(path)
                    count += 1
                    if count >= 120:
                        print("Debugfs file limit reached", flush=True)
                        raise SystemExit(0)
    except OSError as error:
        print(error, flush=True)
