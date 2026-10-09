#!/usr/bin/env bash
# D4b live qualification launcher. Reads the OpenViking USER key from the
# application-owned env file and runs the qualification runner.
set -euo pipefail
cd "$(dirname "$0")/.."
export OPENVIKING_API_KEY="$(sed -n 's/^OPENVIKING_USER_KEY=//p' "$HOME/.website-builder/openviking/openviking.env")"
echo "user key length: ${#OPENVIKING_API_KEY}"
exec ./.venv/bin/python tools/laya_qualify.py \
    --base-url http://127.0.0.1:1933 \
    --project-id wb-design \
    --out "$HOME/.website-builder/openviking/laya_qualification.json" \
    "$@"
