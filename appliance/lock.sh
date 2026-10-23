#!/bin/bash
set -euo pipefail

HERE=$(dirname "$(realpath "$0")")
CACHE_DIR="$HOME/.cache/surface-appliance/apks"
PACKAGES_TXT="$HERE/packages.txt"
PACKAGES_LOCK="$HERE/packages.lock"

mkdir -p "$CACHE_DIR"

if [ ! -f "$PACKAGES_TXT" ]; then
    echo "packages.txt missing at $PACKAGES_TXT" >&2
    exit 1
fi

docker run --rm \
    -e HOST_UID="$(id -u)" \
    -e HOST_GID="$(id -g)" \
    -v "$CACHE_DIR":/cache/apks \
    -v "$HERE":/appliance \
    alpine:edge sh -e -c '
apk add --no-cache alpine-keys
mkdir -p /keys
cp -L /usr/share/apk/keys/armv7/* /keys/
cat << "EOF" > /repos
https://dl-cdn.alpinelinux.org/alpine/edge/main
https://dl-cdn.alpinelinux.org/alpine/edge/community
EOF

PACKAGES=$(grep -vE "^\s*#|^\s*$" /appliance/packages.txt | tr "\n" " ")
echo "Resolving packages: $PACKAGES"

apk --keys-dir /keys --repositories-file /repos --arch armv7 update

echo "Calculating dependency closure..."
URLS=$(apk --keys-dir /keys --repositories-file /repos --arch armv7 fetch --url --recursive $PACKAGES)

echo "Fetching .apk files into cache..."
apk --keys-dir /keys --repositories-file /repos --arch armv7 fetch --recursive -o /cache/apks $PACKAGES

echo "Generating packages.lock..."
LOCK_TMP="/appliance/packages.lock.tmp"
rm -f "$LOCK_TMP"

for url in $URLS; do
    fn=$(basename "$url")
    apk_file="/cache/apks/$fn"
    if [ ! -f "$apk_file" ]; then
        echo "Missing fetched file: $apk_file" >&2
        exit 1
    fi
    pkgname=$(tar -xf "$apk_file" .PKGINFO -O | grep "^pkgname =" | cut -d= -f2 | tr -d " ")
    pkgver=$(tar -xf "$apk_file" .PKGINFO -O | grep "^pkgver =" | cut -d= -f2 | tr -d " ")
    sha256=$(sha256sum "$apk_file" | cut -d" " -f1)
    echo "$pkgname $pkgver $sha256" >> "$LOCK_TMP"
done

sort -k1,1 -u "$LOCK_TMP" > /appliance/packages.lock
rm -f "$LOCK_TMP"
chmod 644 /appliance/packages.lock
chown "$HOST_UID:$HOST_GID" /appliance/packages.lock
echo "Successfully locked $(wc -l < /appliance/packages.lock) packages in /appliance/packages.lock"
'

echo "lock.sh completed successfully."
