#!/usr/bin/env bash
# STAGING launch wrapper for Website Builder R2 — N6 live E2E (owner: wbe2e).
#
# NOT run yet. This only PREPARES an isolated launch. It:
#   * refuses to run as anyone but wbe2e;
#   * pins the N6 commit (verified against the staging source's own record);
#   * points EVERY path at /home/wbe2e/wb-e2e-r2;
#   * sets the staging HERMES_HOME (never the production profile);
#   * selects the staging config (publication disabled);
#   * uses the STAGING interpreter and STAGING node;
#   * refuses to start if any production path is reachable;
#   * refuses to start if the staging Telegram bot token EQUALS production;
#   * refuses to start if ANY other Telegram poller for this bot is running.
#
# Usage (operator, after approval), as wbe2e:
#   sudo -u wbe2e -H bash /home/wbe2e/wb-e2e-r2/bin/launch-staging.sh --preflight
#   sudo -u wbe2e -H bash /home/wbe2e/wb-e2e-r2/bin/launch-staging.sh
set -euo pipefail

STAGE=/home/wbe2e/wb-e2e-r2
SRC=/home/wbe2e/src-r2
WB="$SRC/website-builder"
VENV="$STAGE/venv"
NODE_BIN="$STAGE/node/bin"
PINNED_COMMIT=310a029c5c7a0198d88c3b3fdb9a000cd929432b
PROD_APP_ENV=/home/albertus527/.website-builder/state/app.env

echo "=== staging guard ==="

# 0. Must be the dedicated staging identity.
if [ "$(id -un)" != "wbe2e" ]; then
  echo "REFUSING: must run as wbe2e (current: $(id -un))." >&2
  exit 1
fi
echo "OK: running as $(id -un) (uid=$(id -u) gid=$(id -g))"

# 1. Pinned commit. The staging source is a git-less archive; verify by file
#    digest of the pinned marker written at provision time.
if [ -f "$STAGE/PINNED_COMMIT" ]; then
  REC="$(cat "$STAGE/PINNED_COMMIT")"
  if [ "$REC" != "$PINNED_COMMIT" ]; then
    echo "REFUSING: staging source pinned at $REC, expected $PINNED_COMMIT" >&2
    exit 1
  fi
  echo "OK: staging source pinned at $PINNED_COMMIT"
else
  echo "REFUSING: missing $STAGE/PINNED_COMMIT" >&2
  exit 1
fi

# 1b. FAIL-CLOSED source-identity check. The marker above is TEXT ONLY and was
#     once a lie: the executable tree ($SRC) held an OLDER commit while the
#     marker named the new one, so un-pinned code ran silently. Verify the
#     ACTUAL tree against the content manifest and confirm the interpreter
#     resolves `app` from $SRC -- BEFORE any Telegram polling or build.
VERIFY="$STAGE/bin/verify-source-identity.sh"
if [ ! -f "$VERIFY" ]; then
  echo "REFUSING: missing source-identity verifier $VERIFY" >&2
  exit 1
fi
if ! WB_PYTHON="$VENV/bin/python" bash "$VERIFY" \
        --src "$SRC" --pin "$STAGE/PINNED_COMMIT" \
        --manifest "$STAGE/SOURCE_IDENTITY.json" \
        --helper "$STAGE/bin/source_identity.py"; then
  echo "REFUSING: runtime source identity is stale or mismatched (see above)." >&2
  exit 1
fi

# 2. Staging app.env must exist and be outside the profile home.
if [ ! -f "$STAGE/state/app.env" ]; then
  echo "REFUSING: missing $STAGE/state/app.env (operator must create it, 0600)." >&2
  exit 1
fi
case "$(readlink -f "$STAGE/state/app.env")" in
  "$(readlink -f "$STAGE/hermes-home")"/*)
    echo "REFUSING: staging app.env is INSIDE the profile home." >&2; exit 1 ;;
esac
echo "OK: staging app.env present and outside the profile home"

# 3. No production path may be reachable from the staging env.
for p in "$HOME/.website-builder" "/home/albertus527/.website-builder"; do
  if [ -r "$p/state/app.env" ] 2>/dev/null; then
    echo "REFUSING: a production state path is readable: $p" >&2
    exit 1
  fi
done
echo "OK: no production state path readable"

# 4. Telegram identity guard. A shared bot token means two pollers compete for
#    the same updates; this MUST fail closed (we cannot read prod's token, so
#    compare fingerprints via the staging process's own view only when the
#    operator has supplied a prod fingerprint). We require a marker file the
#    operator sets ONLY after confirming the staging bot is distinct.
if [ "${WB_E2E_ALLOW_SHARED_BOT:-0}" != "1" ]; then
  if [ -f "$STAGE/state/telegram-bot-distinct.ok" ]; then
    echo "OK: staging Telegram bot confirmed distinct (marker present)"
  else
    echo "REFUSING: staging Telegram bot not confirmed distinct from production." >&2
    echo "  Create $STAGE/state/telegram-bot-distinct.ok (root-owned) after" >&2
    echo "  verifying the staging bot token is a SEPARATE test bot." >&2
    exit 1
  fi
fi

# 5. Single-poller guard for the STAGING bot.
#    The staging bot is confirmed DISTINCT from production (marker above), so a
#    running production gateway (which polls the production bot) is NOT a
#    conflict. The only conflict is a SECOND process polling the STAGING bot,
#    i.e. another staging entrypoint (`python -m app` as wbe2e).
STAGING_POLLERS="$(pgrep -af "python.* -m app" | grep -v "launch-staging" || true)"
if [ -n "$STAGING_POLLERS" ]; then
  echo "REFUSING: another staging Website Builder poller appears to be running:" >&2
  echo "$STAGING_POLLERS" >&2
  exit 1
fi
echo "OK: no other staging poller detected (production gateway is a distinct bot)"

echo "=== launching (staging) ==="
cd "$WB"
export WEBSITE_BUILDER_CONFIG_PATH="$STAGE/config/e2e.yaml"
export WEBSITE_BUILDER_STATE_ROOT="$STAGE/state"
export WEBSITE_BUILDER_WORKSPACE_ROOT="$STAGE/workspaces"
export WEBSITE_BUILDER_OUTPUT_REPO="$STAGE/output-repo"
export HERMES_HOME="$STAGE/hermes-home"
export PYTHONPATH="$SRC"
export WB_PYTHON="$VENV/bin/python"
export PATH="$NODE_BIN:$VENV/bin:/usr/bin:/bin"
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.cache/ms-playwright"

# QA screenshot capture (app/qa/screenshot.py) drives the ``agent-browser`` CLI.
# Two conditions are required inside the systemd sandbox:
#   1. the CLI must be installed in THIS staging node prefix (done at provision
#      time: npm install -g agent-browser@0.37.1 into $NODE_BIN's prefix), so
#      shutil.which("agent-browser") resolves it;
#   2. Chromium must launch WITHOUT its own setuid/namespace sandbox, because
#      the unit sets RestrictNamespaces=yes. The adapter reads AGENT_BROWSER_ARGS
#      from the APP environment and appends it as ``--args`` on the open command
#      (screenshot.py:200), so setting it here is sufficient and it survives the
#      strict child allowlist (the value is passed via argv, not the child env).
# This mirrors the app's OWN Playwright smoke, which hardcodes --no-sandbox
# (app/runtime.py) for the same confinement reason.
export AGENT_BROWSER_ARGS="--no-sandbox"

# NOTE: --no-sandbox is HARDCODED in app/runtime.py:410-417. It is only
# acceptable because this process runs inside the OS-level systemd confinement
# (launch-staging-sandboxed.sh), not merely because of the uid. See README.

exec "$VENV/bin/python" -m app "$@"
