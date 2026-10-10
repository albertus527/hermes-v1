#!/usr/bin/env bash
# Generic D4c.1 runner: loads the OpenViking USER key from the service env file
# (value never echoed) and runs the given website-builder python tool.
#   usage: bash tools/d4c1_run.sh <tool.py> [args...]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="$HOME/.website-builder/openviking/openviking.env"
if [ -f "$ENV_FILE" ]; then
  export OPENVIKING_API_KEY="$(grep -oE '^OPENVIKING_USER_KEY=.*' "$ENV_FILE" | cut -d= -f2-)"
fi
exec ./.venv/bin/python "$@"
