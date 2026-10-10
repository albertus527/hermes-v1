#!/usr/bin/env bash
# D4c.1 pre-flight live smoke (ZERO paid calls).
# Drives the REAL OpenViking service through the production intake seam with the
# FAST *model* boundary replaced by a recording stand-in. Skips the outage probe
# because D4c.1 forbids restarting OpenViking.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="$HOME/.website-builder/openviking/openviking.env"
OUT="$HOME/.website-builder/openviking/d4c1_preflight_smoke.json"
export OPENVIKING_API_KEY="$(grep -oE '^OPENVIKING_USER_KEY=.*' "$ENV_FILE" | cut -d= -f2-)"
exec ./.venv/bin/python tools/d4c_live_smoke.py \
  --base-url http://127.0.0.1:1933 \
  --project-id wb-design \
  --skip-outage \
  --out "$OUT"
