#!/usr/bin/env bash
set -euo pipefail

# website-builder operator startup helper
# Activates expected Node 26.5.0 before launching the runtime, and selects a
# deterministic, dependency-complete Python interpreter for the runtime.
#
# Python interpreter selection (H-9), in priority order:
#   1. "$SCRIPT_DIR/.venv/bin/python"  when present and executable
#   2. a fallback interpreter that can actually import the runtime deps
#      (playwright.sync_api, yaml) — checked in order: $WB_PYTHON,
#      python3, python (via command -v; no shell function/alias)
#   3. otherwise: fail early, non-zero, with a clear actionable message
# It never creates a venv, installs packages, uses sudo, or touches system
# Python. The chosen interpreter launches `python -m app`, so it remains
# sys.executable for downstream Hermes subprocesses.

# Resolve the script directory reliably, even when invoked through a symlink
# or from an arbitrary cwd.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
STARTER_NVMRC="$SCRIPT_DIR/../templates/frontend-starter/.nvmrc"

# Probe: does this interpreter have the runtime dependencies importable?
# Uses only the stdlib, needs no network, and never installs anything.
PY_PROBE='import playwright.sync_api, yaml'

is_usable_python() {
    local candidate="$1"
    [ -n "$candidate" ] || return 1
    # Accept an absolute/relative path, or a bare command name on PATH.
    if [ -x "$candidate" ]; then
        :
    elif command -v -- "$candidate" >/dev/null 2>&1; then
        candidate="$(command -v -- "$candidate")"
    else
        return 1
    fi
    "$candidate" -c "$PY_PROBE" >/dev/null 2>&1
}

SELECTED_PYTHON=""

# 1. Prefer the project virtualenv when it is present and executable.
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    if is_usable_python "$VENV_PYTHON"; then
        SELECTED_PYTHON="$VENV_PYTHON"
    else
        echo "error: found $VENV_PYTHON but it cannot import the runtime dependencies" >&2
        echo "       (playwright.sync_api, yaml). Recreate it, e.g.:" >&2
        echo "         python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
        exit 1
    fi
fi

# 2. Fall back to an interpreter that satisfies the dependency probe.
if [ -z "$SELECTED_PYTHON" ]; then
    for candidate in "${WB_PYTHON:-}" python3 python; do
        if [ -n "$candidate" ] && is_usable_python "$candidate"; then
            SELECTED_PYTHON="$(command -v -- "$candidate" 2>/dev/null || printf '%s' "$candidate")"
            break
        fi
    done
fi

# 3. Fail early with a clear, actionable message.
if [ -z "$SELECTED_PYTHON" ]; then
    {
        echo "error: no usable Python interpreter found for Website Builder."
        echo "       Required imports: playwright.sync_api, yaml."
        echo
        echo "       Create the project virtualenv (do not use system Python):"
        echo "         cd \"$SCRIPT_DIR\""
        echo "         python3 -m venv .venv"
        echo "         .venv/bin/pip install -r requirements.txt"
        echo "         .venv/bin/python -m playwright install chromium"
        echo
        echo "       Or point WB_PYTHON at an existing interpreter that already"
        echo "       has those dependencies installed."
    } >&2
    exit 1
fi

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
exec "$SELECTED_PYTHON" -m app "$@"
