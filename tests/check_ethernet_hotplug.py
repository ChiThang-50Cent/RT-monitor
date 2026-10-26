#!/usr/bin/env python3
"""Simulate Ethernet hotplug event dispatch and verify ifupdown-ng state recovery."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'appliance'))
import package_payload as payload


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_rule_actions(rule_path: Path):
    actions = []
    text = rule_path.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if 'SUBSYSTEM=="net"' in line and 'ACTION=="add"' in line and 'KERNEL=="eth*"' in line:
            for match in re.finditer(r'RUN\+?="([^"]+)"', line):
                actions.append(match.group(1))
    require(len(actions) > 0, f'no matching RUN actions found in {rule_path}')
    return actions


def link_exists(dev: str) -> bool:
    res = subprocess.run(['ip', 'link', 'show', 'dev', dev], capture_output=True, text=True)
    return res.returncode == 0


def get_link_info(dev: str):
    res = subprocess.run(['ip', '-j', 'link', 'show', 'dev', dev], capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    require(len(data) > 0, f'device {dev} not found')
    return data[0]


def is_link_up(dev: str) -> bool:
    info = get_link_info(dev)
    flags = info.get('flags', [])
    return 'UP' in flags


def get_ipv4_addresses(dev: str):
    res = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'dev', dev], capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    if not data:
        return []
    addresses = []
    for entry in data[0].get('addr_info', []):
        if entry.get('family') == 'inet':
            addresses.append(f"{entry['local']}/{entry['prefixlen']}")
    return addresses


def get_default_routes():
    res = subprocess.run(['ip', '-j', '-4', 'route', 'show'], capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    defaults = []
    for r in data:
        if r.get('dst') == 'default':
            defaults.append(r)
    return defaults


def get_connected_routes(dev: str):
    res = subprocess.run(['ip', '-j', '-4', 'route', 'show', 'dev', dev], capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    return [r.get('dst') for r in data if r.get('dst') != 'default']


def check_inside():
    rule_path = REPO / 'appliance/overlay/etc/udev/rules.d/90-network.rules'
    require(rule_path.is_file(), f'missing udev rules file at {rule_path}')
    rule_actions = load_rule_actions(rule_path)

    interfaces_source = REPO / 'appliance/overlay/etc/network/interfaces'
    require(interfaces_source.is_file(), f'missing interfaces file at {interfaces_source}')
    Path('/etc/network').mkdir(parents=True, exist_ok=True)
    shutil.copyfile(interfaces_source, '/etc/network/interfaces')
    Path('/run').mkdir(parents=True, exist_ok=True)
    executor_bin = Path('/run/hotplug-bin')
    executor_bin.mkdir()
    (executor_bin / 'ip').symlink_to('/bin/busybox')
    executor_environment = dict(os.environ, PATH=str(executor_bin) + ':' + os.environ['PATH'])

    if link_exists('eth0'):
        subprocess.run(['ip', 'link', 'set', 'dev', 'eth0', 'down'], check=True)
        subprocess.run(['ip', 'link', 'set', 'dev', 'eth0', 'name', 'uplink'], check=True)
        subprocess.run(['ip', 'link', 'set', 'dev', 'uplink', 'up'], check=True)
        subprocess.run(['ip', 'route', 'flush', 'dev', 'uplink'], check=False)

    for dev in ('eth0', 'mlan0'):
        if link_exists(dev):
            subprocess.run(['ip', 'link', 'del', 'dev', dev], check=True)

    subprocess.run(['ip', 'link', 'add', 'dev', 'eth0', 'type', 'dummy'], check=True)
    subprocess.run(['ip', 'link', 'add', 'dev', 'mlan0', 'type', 'dummy'], check=True)
    subprocess.run(['ip', 'addr', 'add', '192.168.77.2/24', 'dev', 'mlan0'], check=True)
    subprocess.run(['ip', 'link', 'set', 'dev', 'mlan0', 'up'], check=True)

    def assert_eth0_configured():
        require(is_link_up('eth0'), 'eth0 is not UP')
        require('10.42.0.2/24' in get_ipv4_addresses('eth0'), 'eth0 missing 10.42.0.2/24')
        defaults = get_default_routes()
        eth0_defaults = [r for r in defaults if r.get('gateway') == '10.42.0.1' and r.get('dev') == 'eth0']
        require(len(defaults) == 1 and len(eth0_defaults) == 1,
                f'expected exactly one default route via 10.42.0.1 on eth0, found {defaults}')

    def assert_management_intact():
        require('192.168.77.2/24' in get_ipv4_addresses('mlan0'), 'mlan0 address 192.168.77.2/24 changed or lost')
        require('192.168.77.0/24' in get_connected_routes('mlan0'), 'mlan0 connected route 192.168.77.0/24 changed or lost')

    def dispatch_actions():
        for action in rule_actions:
            args = shlex.split(action)
            subprocess.run(args, check=True, env=executor_environment)

    subprocess.run(['/sbin/ifup', 'eth0'], check=True, env=executor_environment)
    assert_eth0_configured()
    assert_management_intact()

    subprocess.run(['ip', 'link', 'del', 'dev', 'eth0'], check=True)
    subprocess.run(['ip', 'link', 'add', 'dev', 'eth0', 'type', 'dummy'], check=True)
    subprocess.run(['/sbin/ifup', 'eth0'], check=False, env=executor_environment)

    require(not is_link_up('eth0'), 'ordinary ifup unexpectedly brought eth0 UP despite stale ifstate')
    require('10.42.0.2/24' not in get_ipv4_addresses('eth0'), 'ordinary ifup unexpectedly configured 10.42.0.2/24 despite stale ifstate')
    require(len(get_default_routes()) == 0, 'ordinary ifup unexpectedly added a default route despite stale ifstate')
    assert_management_intact()

    dispatch_actions()
    assert_eth0_configured()
    assert_management_intact()

    dispatch_actions()
    assert_eth0_configured()
    assert_management_intact()

    for _ in range(5):
        subprocess.run(['ip', 'link', 'del', 'dev', 'eth0'], check=True)
        subprocess.run(['ip', 'link', 'add', 'dev', 'eth0', 'type', 'dummy'], check=True)
        dispatch_actions()
        assert_eth0_configured()
        assert_management_intact()

    print('Ethernet hotplug simulation checks passed:')
    print('- Initial eth0 configured via ifup')
    print('- Stale ifstate reproduction confirmed: ordinary ifup left recreated eth0 DOWN with no IP')
    print('- Parsed udev rule actions reset state and restored UP 10.42.0.2/24 and default via 10.42.0.1')
    print('- Repeated add actions and 5 hotplug cycles converged idempotently')
    print('- Management interface mlan0 (192.168.77.2/24) and connected route remained unchanged throughout')
    print()
    print('Limits: This test exercises simulated rule event dispatch against dummy netdevices in an isolated container.')
    print('Physical USB PHY disconnect, kernel cdc_ether driver lifecycle, carrier detect timing, and asynchronous')
    print('udev daemon event dispatch remain unverified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        check_inside()
        return

    command = [
        'docker', 'run', '--rm',
        '--cap-add=NET_ADMIN',
        '-v', f'{REPO}:/repo:ro',
        payload.IMAGE,
        'sh', '-ec',
        'apk add --no-cache python3 ifupdown-ng iproute2; exec python3 /repo/tests/check_ethernet_hotplug.py --inside',
    ]
    subprocess.run(command, check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit('Ethernet hotplug check failed: ' + str(error))
