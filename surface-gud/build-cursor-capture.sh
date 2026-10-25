#!/bin/sh
set -eu
if [ "$#" -gt 1 ]; then
    printf 'Usage: %s [OUTPUT]\n' "$0" >&2
    exit 1
fi
output=${1:-"$HOME/.local/libexec/surface-cursor-capture"}
mkdir -p "$(dirname -- "$output")"
${CC:-cc} ${CPPFLAGS:-} ${CFLAGS:--O2 -Wall -Wextra} \
    $(pkg-config --cflags libpipewire-0.3) \
    "$(dirname -- "$0")/surface_cursor_capture.c" \
    ${LDFLAGS:-} $(pkg-config --libs libpipewire-0.3) -o "$output"
