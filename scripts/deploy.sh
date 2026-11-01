#!/usr/bin/env bash
# Deploy BabyScout to a MicroPython Pico W over USB, then byte-verify every
# file and reset the board.
#
#   ./scripts/deploy.sh [PORT]        # default: COM6
#   PORT=/dev/ttyACM0 ./scripts/deploy.sh
#
# Never touches secrets.json or pump_volume.json (device-only state).
set -euo pipefail

PORT="${1:-${PORT:-COM6}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# WSL: mpremote lives in the Windows Python (no /dev/ttyACM* in WSL2).
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

echo "Deploying to $PORT with: $MPREMOTE"

# Directories first (mkdir fails harmlessly when they already exist).
$MPREMOTE connect "$PORT" fs mkdir :lib 2>/dev/null || true
$MPREMOTE connect "$PORT" fs mkdir :picoconnection 2>/dev/null || true

for file in "${FILES[@]}"; do
    echo "  cp $file"
    $MPREMOTE connect "$PORT" cp "$file" ":$file"
done

echo "Verifying (device -> host, byte compare)..."
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
failed=0
for file in "${FILES[@]}"; do
    back="$tmpdir/$(basename "$file")"
    $MPREMOTE connect "$PORT" cp ":$file" "$back" >/dev/null
    if cmp -s "$file" "$back"; then
        echo "  MATCH $file"
    else
        echo "  MISMATCH $file"
        failed=1
    fi
done
if [ "$failed" -ne 0 ]; then
    echo "Deploy FAILED: at least one file differs on the device." >&2
    exit 1
fi

echo "Resetting the Pico..."
$MPREMOTE connect "$PORT" reset
echo "Deploy OK. Open the serial console with: make logs PORT=$PORT"
