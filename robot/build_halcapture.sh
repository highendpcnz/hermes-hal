#!/usr/bin/env bash
# Build robot/halcapture.jar from HalCapture.java.
#
# Built on the Mac, not the Pixel: Termux's ecj ships a stub android.jar with
# no AttributionSource and no CONTROL_ZOOM_RATIO, both of which this needs.
# The output is a dex jar that runs under app_process on the phone.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
android_jar="$(ls -d "$sdk"/platforms/android-*/android.jar 2>/dev/null | sort -V | tail -1)"
d8="$(ls -d "$sdk"/build-tools/*/d8 2>/dev/null | sort -V | tail -1)"
[ -n "$android_jar" ] || { echo "no android.jar under $sdk/platforms" >&2; exit 1; }
[ -n "$d8" ] || { echo "no d8 under $sdk/build-tools" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
javac --release 17 -nowarn -classpath "$android_jar" -d "$tmp" "$here/HalCapture.java"
# The jar must end up non-writable: ART refuses to load a dex it could write to.
rm -f "$here/halcapture.jar"
"$d8" --min-api 30 --lib "$android_jar" --output "$here/halcapture.jar" "$tmp"/*.class
chmod 444 "$here/halcapture.jar"
echo "built $here/halcapture.jar"
