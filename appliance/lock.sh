#!/bin/bash
set -euo pipefail

HERE=$(dirname "$(realpath "$0")")
exec python3 "$HERE/package_payload.py" extend
