#!/usr/bin/python3
"""Stop production sender, run decode/xv replay matrix on Surface, always restore."""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--tag', default='replay')
p.add_argument('--runs', type=int, default=1)
p.add_argument('--matrix', default='decode:0,decode:30,xv:30,xv:0')
a = p.parse_args()
matrix = [(m, int(f)) for m, f in (e.split(':') for e in a.matrix.split(','))] * a.runs
out = Path('/tmp/surface-latency-results') / (time.strftime('%Y%m%d-%H%M%S') + '-' + a.tag)
out.mkdir(parents=True)
unit = 'surface-latency-recover-' + str(os.getpid())
ssh = ['ssh', '-T', '-i', str(Path.home() / '.ssh/surface_omp'),
       '-o', f'UserKnownHostsFile={Path(__file__).resolve().parent.parent}/surface-known-hosts',
       '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
       'user@10.42.0.2']
remote = ('cd /home/user/surface-latency-lab && DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/10000 '
          'PYTHONPATH=/home/user GST_REGISTRY_UPDATE=no ')
try:
    subprocess.run(['systemd-run', '--user', '--unit=' + unit,
                    f'--on-active={6 + 2 * len(matrix)}min',
                    '/usr/bin/systemctl', '--user', 'start', 'surface-display.service'],
                   check=True, timeout=15)
    subprocess.run(['systemctl', '--user', 'stop', 'surface-display.service'], check=True, timeout=30)
    time.sleep(3)
    for n, (mode, pace) in enumerate(matrix):
        name = f'{mode}-pace{pace}-r{n}'
        # Sample per-process CPU via /proc/self-like wrapper: time -v unavailable; use getrusage.
        cmd = remote + (
            "/usr/bin/python3 -c 'import resource,subprocess,sys,json,time;"
            "t=time.monotonic();"
            f"r=subprocess.run([\"/usr/bin/python3\",\"replay_probe.py\",\"--mode\",\"{mode}\","
            f"\"--input\",\"clip.frames\",\"--pace\",\"{pace}\",\"--trace-out\",\"{name}.ndjson\"]);"
            "u=resource.getrusage(resource.RUSAGE_CHILDREN);"
            "print(json.dumps({\"rc\":r.returncode,\"wall\":time.monotonic()-t,\"user_s\":u.ru_utime,\"sys_s\":u.ru_stime}))'")
        p = subprocess.run(ssh + [cmd], capture_output=True, text=True, timeout=120)
        (out / f'{name}.stdout').write_text(p.stdout)
        (out / f'{name}.stderr').write_text(p.stderr)
        subprocess.run(['scp', '-i', str(Path.home() / '.ssh/surface_omp'),
                        '-o', f'UserKnownHostsFile={Path(__file__).resolve().parent.parent}/surface-known-hosts',
                        '-o', 'StrictHostKeyChecking=yes', '-o', 'BatchMode=yes',
                        f'user@10.42.0.2:/home/user/surface-latency-lab/{name}.ndjson', str(out)],
                       timeout=60)
        print(json.dumps({'run': name, 'rc': p.returncode, 'tail': p.stdout[-400:]}), flush=True)
finally:
    r = subprocess.run(['systemctl', '--user', 'start', 'surface-display.service'], timeout=30)
    if r.returncode == 0:
        subprocess.run(['systemctl', '--user', 'stop', unit + '.timer'], timeout=15)
    print(json.dumps({'restore_rc': r.returncode, 'out': str(out)}), flush=True)
