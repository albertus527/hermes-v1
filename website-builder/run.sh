#!/usr/bin/env bash
set -euo pipefail

# website-builder operator startup helper
# Activates expected Node 26.5.0 before launching the runtime

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STARTER_NVMRC="$SCRIPT_DIR/../templates/frontend-starter/.nvmrc"

# Try to load NVM if nvm is not already in PATH
if ! command -v nvm >/dev/null 2>&1; then
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        # shellcheck disable=SC1090
        \. "$NVM_DIR/nvm.sh"
    fi
fi

# Activate Node 26.5.0
if command -v nvm >/dev/null 2>&1; then
    if [ -f "$STARTER_NVMRC" ]; then
        TARGET_NODE="$(tr -d '[:space:]' < "$STARTER_NVMRC")"
        nvm use "$TARGET_NODE" || nvm use 26.5.0 || true
    else
        nvm use 26.5.0 || true
    fi
fi

cd "$SCRIPT_DIR"
exec python -m app "$@"
