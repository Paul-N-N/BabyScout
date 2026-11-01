#!/usr/bin/env bash
# Read every deployed BabyScout file back off the Pico and byte-compare it with
# the working tree. Changes nothing on the device.
#
#   ./scripts/verify.sh [PORT]        # default: COM6
set -euo pipefail

PORT="${1:-${PORT:-COM6}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -z "${MPREMOTE:-}" ]; then
    if command -v python.exe >/dev/null 2>&1; then
        MPREMOTE="python.exe -m mpremote"
    else
        MPREMOTE="python3 -m mpremote"
    fi
fi

FILES=(
    main.py
    lib/__init__.py
    lib/display.py
    lib/encoder.py
    lib/pin.py
    lib/scout.py
    lib/ssd1306.py
    lib/utils.py
    picoconnection/__init__.py
)

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
failed=0
for file in "${FILES[@]}"; do
    back="$tmpdir/$(basename "$file")"
    if ! $MPREMOTE connect "$PORT" cp ":$file" "$back" >/dev/null 2>&1; then
        echo "MISSING $file"
        failed=1
        continue
    fi
    if cmp -s "$file" "$back"; then
        echo "MATCH   $file"
    else
        echo "DIFF    $file"
        failed=1
    fi
done

if [ "$failed" -ne 0 ]; then
    echo "Device is NOT in sync with the working tree." >&2
    exit 1
fi
echo "Device is in sync."
