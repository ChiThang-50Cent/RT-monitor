#!/usr/bin/python3
"""Run finite traced workloads, restoring the original user service in finally."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time

p = argparse.ArgumentParser()
p.add_argument('--credits', type=int, choices=(2, 3), default=2)
p.add_argument('--vblank-step', type=int, choices=(0, 2, 3), default=0)
p.add_argument('--pump-fps', type=float, default=30.0)
p.add_argument('--watch', default='',
                help='sender --schedule; user supplies the content, no test scenes')
a = p.parse_args()
lab = Path(__file__).resolve().parent
watch_seconds = sum(float(e.split(':')[3]) + 4 for e in a.watch.split(',')) if a.watch else 0
duration = watch_seconds + 20 if a.watch else 100
out = Path('/tmp/surface-latency-results') / (time.strftime('%Y%m%d-%H%M%S') + (
       '-watch' if a.watch else f'-c{a.credits}-v{a.vblank_step}-f{a.pump_fps:g}'))
out.mkdir(parents=True)
service = ['systemctl', '--user']
unit = 'surface-latency-recover-' + str(os.getpid())
processes = []
reader = None
ready = threading.Event()
state = {'ready': False}

def terminate(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=25)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

def scene(pattern):
    with (out / (pattern + '.log')).open('w') as output:
        child = subprocess.Popen(['/usr/bin/python3', str(lab/'motion_video.py'),
                                  '--seconds', '35', '--pattern', pattern],
                                 stdout=output, stderr=subprocess.STDOUT)
        processes.append(child)
        code = child.wait(timeout=50)
        if code:
            raise RuntimeError(f'{pattern} workload exited {code}')

try:
    subprocess.run(['systemd-run', '--user', '--unit='+unit,
                    f'--on-active={int(duration + 300)}s',
                    '/usr/bin/systemctl', '--user', 'start', 'surface-display.service'],
                   check=True, timeout=15)
    subprocess.run(service+['stop', 'surface-display.service'], check=True, timeout=30)
    with (out/'sender.log').open('w') as log:
        env = dict(os.environ, PYTHONPATH=str(lab.parent / 'surface-gud'))
        sender = subprocess.Popen(['/usr/bin/python3', str(lab/'sender_trace.py'),
                                   '--duration', str(duration), '--credits', str(a.credits),
                                   '--schedule', a.watch,
                                   '--output', str(out/'frames.ndjson'),
                                   *([] if a.watch else ['--record', str(out/'clip.frames')]),
                                   '--pump-fps', str(a.pump_fps),
                                   f'--receiver-args=--vblank-step {a.vblank_step}',
                                   '--key', str(Path.home() / '.ssh/surface_omp'),
                                   '--known-hosts', str(lab.parent / 'surface-known-hosts')],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1)
        processes.append(sender)
        def relay():
            for line in sender.stdout:
                log.write(line)
                log.flush()
                if 'virtual display:' in line:
                    state['ready'] = True
                    ready.set()
            ready.set()
        reader = threading.Thread(target=relay)
        reader.start()
        if not ready.wait(25) or not state['ready']:
            raise RuntimeError('diagnostic virtual monitor did not become ready')
        if a.watch:
            result = sender.wait(timeout=duration + 30)
        else:
            scene('snow')
            time.sleep(5)
            scene('ball')
            result = sender.wait(timeout=65)
        reader.join(timeout=5)
        if result:
            raise RuntimeError(f'diagnostic sender exited {result}')
    print(json.dumps({'completed': True, 'output_directory': str(out)}), flush=True)
finally:
    for child in reversed(processes):
        terminate(child)
    if reader is not None:
        reader.join(timeout=5)
    restored = subprocess.run(service+['start', 'surface-display.service'], timeout=30)
    # Keep the recovery timer if synchronous restoration failed.
    if restored.returncode == 0:
        subprocess.run(service+['stop', unit+'.timer'], timeout=15)
    print(json.dumps({'service_restore_returncode': restored.returncode,
                      'output_directory': str(out)}), flush=True)
