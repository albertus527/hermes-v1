#!/usr/bin/env bash
# STAGING preflight for Website Builder R2 — N6 live E2E (owner: wbe2e).
#
# Runs the application's own --preflight (Node toolchain + Playwright smoke
# support), then EXITS. It never starts the Telegram loop.
# NOTE: --preflight does NOT check model roles/design capabilities; use
# bin/probe-roles-design.py for those.
#
# Usage (as wbe2e):
#   sudo -u wbe2e -H bash /home/wbe2e/wb-e2e-r2/bin/preflight-staging.sh
set -euo pipefail

STAGE=/home/wbe2e/wb-e2e-r2
SRC=/home/wbe2e/src-r2
WB="$SRC/website-builder"
VENV="$STAGE/venv"

if [ "$(id -un)" != "wbe2e" ]; then
  echo "REFUSING: must run as wbe2e (current: $(id -un))." >&2
  exit 1
fi

echo "=== isolation map ==="
echo "config : $STAGE/config/e2e.yaml"
echo "state  : $STAGE/state"
echo "ws     : $STAGE/workspaces"
echo "output : $STAGE/output-repo"
echo "profile: $STAGE/hermes-home"
echo "python : $VENV/bin/python"
echo "node   : $STAGE/node/bin/node"
echo "source : $SRC (read-only copy)"
echo

cd "$WB"
export WEBSITE_BUILDER_CONFIG_PATH="$STAGE/config/e2e.yaml"
export WEBSITE_BUILDER_STATE_ROOT="$STAGE/state"
export WEBSITE_BUILDER_WORKSPACE_ROOT="$STAGE/workspaces"
export WEBSITE_BUILDER_OUTPUT_REPO="$STAGE/output-repo"
export HERMES_HOME="$STAGE/hermes-home"
export PYTHONPATH="$SRC"
export PATH="$STAGE/node/bin:$VENV/bin:/usr/bin:/bin"
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"

echo "=== running: python -m app --preflight ==="
exec "$VENV/bin/python" -m app --preflight
