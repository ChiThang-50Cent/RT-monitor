#!/usr/bin/python3
"""Stop the display service, hash decoded frames of clip.frames N times, always restore."""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--tag', required=True)
p.add_argument('--runs', type=int, default=2)
a = p.parse_args()
out = Path('/tmp/surface-latency-results') / (time.strftime('%Y%m%d-%H%M%S') + '-checksum-' + a.tag)
out.mkdir(parents=True)
unit = 'surface-latency-recover-' + str(os.getpid())
key = ['-i', str(Path.home() / '.ssh/surface_omp'),
       '-o', f'UserKnownHostsFile={Path(__file__).resolve().parent.parent}/surface-known-hosts',
       '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes']
remote = 'cd /home/user/surface-latency-lab && GST_REGISTRY_UPDATE=no '
try:
    subprocess.run(['systemd-run', '--user', '--unit=' + unit, '--on-active=6min',
                    '/usr/bin/systemctl', '--user', 'start', 'surface-display.service'],
                   check=True, timeout=15)
    subprocess.run(['systemctl', '--user', 'stop', 'surface-display.service'], check=True, timeout=30)
    time.sleep(3)
    for n in range(a.runs):
        name = f'hashes-r{n}.json'
        r = subprocess.run(['ssh', '-T', *key, 'user@10.42.0.2',
                            remote + f'/usr/bin/python3 checksum_probe.py --input clip.frames --output {name}'],
                           capture_output=True, text=True, timeout=180)
        (out / f'r{n}.log').write_text(r.stdout + r.stderr)
        subprocess.run(['scp', '-q', *key, f'user@10.42.0.2:/home/user/surface-latency-lab/{name}', str(out)],
                       timeout=60)
        print(json.dumps({'run': n, 'rc': r.returncode, 'stdout': r.stdout.strip()[-300:],
                          'stderr': r.stderr.strip()[-300:]}), flush=True)
finally:
    r = subprocess.run(['systemctl', '--user', 'start', 'surface-display.service'], timeout=30)
    if r.returncode == 0:
        subprocess.run(['systemctl', '--user', 'stop', unit + '.timer'], timeout=15)
    print(json.dumps({'restore_rc': r.returncode, 'out': str(out)}), flush=True)
