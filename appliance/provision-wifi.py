#!/usr/bin/env python3
"""Write a private standard WPA-PSK configuration outside the checkout."""
import argparse
import getpass
import hashlib
import os
from pathlib import Path
import re
import tempfile
import warnings

REPO = Path(__file__).resolve().parents[1]


def configuration(ssid, secret):
    ssid_bytes = ssid.encode('utf-8')
    if not 1 <= len(ssid_bytes) <= 32:
        raise ValueError('SSID must contain 1 to 32 UTF-8 bytes')
    if re.fullmatch(r'[0-9a-fA-F]{64}', secret):
        psk = secret.lower()
    elif 8 <= len(secret) <= 63 and all(32 <= ord(c) <= 126 for c in secret):
        psk = hashlib.pbkdf2_hmac('sha1', secret.encode('ascii'), ssid_bytes, 4096, 32).hex()
    else:
        raise ValueError('Use an 8 to 63 character ASCII passphrase or a 64-digit hexadecimal PSK')
    return ('ctrl_interface=DIR=/run/surface-wifi/control GROUP=0\n'
            'update_config=0\nnetwork={\n'
            f'    ssid={ssid_bytes.hex()}\n    psk={psk}\n'
            '    key_mgmt=WPA-PSK\n}\n')


def write_configuration(path, content):
    path = Path(path).expanduser().absolute()
    if path.resolve().is_relative_to(REPO):
        raise ValueError('Credential output must be outside the checkout')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssid')
    parser.add_argument('--output', type=Path, default=Path.home() / '.config/surface-appliance/wpa_supplicant.conf')
    args = parser.parse_args()
    try:
        ssid = args.ssid if args.ssid is not None else input('SSID: ')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            secret = getpass.getpass('WPA-PSK passphrase or hexadecimal PSK: ')
        write_configuration(args.output, configuration(ssid, secret))
    except (OSError, ValueError, getpass.GetPassWarning):
        parser.exit(1, 'Wi-Fi configuration not written. Use a terminal and check the SSID, passphrase format, and output directory.\n')
    print('Private Wi-Fi configuration written. Copy it only to the verified boot identity directory.')


if __name__ == '__main__':
    main()
