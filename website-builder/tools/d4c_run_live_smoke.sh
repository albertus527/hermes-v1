#!/usr/bin/env bash
# D4c live-smoke runner: loads the OpenViking user key from the service env file
# (value never echoed) and drives tools/d4c_live_smoke.py against the REAL
# OpenViking service. Not committed (prefixed with _).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="$HOME/.website-builder/openviking/openviking.env"
OUT="$HOME/.website-builder/openviking/d4c_live_smoke.json"
export OPENVIKING_API_KEY="$(grep -oE '^OPENVIKING_USER_KEY=.*' "$ENV_FILE" | cut -d= -f2-)"
exec ./.venv/bin/python tools/d4c_live_smoke.py \
  --base-url http://127.0.0.1:1933 \
  --project-id wb-design \
  --out "$OUT"
