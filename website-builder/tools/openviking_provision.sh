#!/usr/bin/env bash
# D4a.1 OpenViking provisioning -- idempotent, approval-gated, secret-safe.
#
# Run ONLY after the D4a.1 mandatory approval checkpoint. It:
#   1. verifies the isolated venv holds the EXACT pinned OpenViking version;
#   2. writes an application-owned env file (0600) reusing the existing 9router
#      credential and generating a fresh root API key;
#   3. renders ~/.website-builder/openviking/ov.conf (0600) from the template;
#   4. installs a systemd USER unit and starts the service on loopback;
#   5. waits for /health.
#
# It NEVER: installs globally, touches Hermes Trade, edits the Hermes gateway or
# global config, prints a secret, or writes inside the git tree.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/deploy/openviking"
BASE="$HOME/.website-builder/openviking"
VENV="$BASE/venv"
ENV_FILE="$BASE/openviking.env"
OV_CONF="$BASE/ov.conf"
UNIT_SRC="$DEPLOY/openviking-website.service"
UNIT_DST="$HOME/.config/systemd/user/openviking-website.service"
PINNED="0.4.23"

say() { printf '%s\n' "$*" >&2; }
die() { say "ERROR: $*"; exit 1; }

[ "${1:-}" = "--yes" ] || die "refusing to provision without --yes (approval-gated)"

# 1. Isolated venv + exact version.
[ -x "$VENV/bin/python" ] || die "isolated venv missing at $VENV (create it first)"
INSTALLED="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("openviking"))')"
[ "$INSTALLED" = "$PINNED" ] || die "installed openviking $INSTALLED != pinned $PINNED"
say "verified openviking $INSTALLED in isolated venv"

# 2. Env file (0600): reuse the website credential; generate a root key.
mkdir -p "$BASE"
NINEROUTER="$(grep -E '^NINEROUTER_API_KEY=' "$HOME/.hermes-website/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
[ -n "$NINEROUTER" ] || die "NINEROUTER_API_KEY not found in ~/.hermes-website/.env"
if [ -f "$ENV_FILE" ] && grep -q '^OPENVIKING_ROOT_KEY=.\+' "$ENV_FILE"; then
    ROOT_KEY="$(grep -E '^OPENVIKING_ROOT_KEY=' "$ENV_FILE" | cut -d= -f2-)"
    say "reusing existing OpenViking root key"
else
    ROOT_KEY="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
    say "generated a new OpenViking root key"
fi
umask 077
cat > "$ENV_FILE" <<EOF
NINEROUTER_API_KEY=$NINEROUTER
OPENVIKING_ROOT_KEY=$ROOT_KEY
EOF
chmod 600 "$ENV_FILE"
say "wrote $ENV_FILE (0600)"

# 3. ov.conf (0600) from template; ${VAR} is expanded by OpenViking at load.
umask 077
sed -e "s#/home/albertus527#${HOME}#g" "$DEPLOY/ov.conf.template" > "$OV_CONF"
chmod 600 "$OV_CONF"
say "wrote $OV_CONF (0600)"

# 4. systemd user unit.
mkdir -p "$(dirname "$UNIT_DST")"
sed -e "s#/home/albertus527#${HOME}#g" "$UNIT_SRC" > "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable --now openviking-website.service
say "installed + started openviking-website.service"

# 5. Wait for health (bounded).
for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:1933/health" >/dev/null 2>&1; then
        say "healthy after ${i}s"
        curl -fsS "http://127.0.0.1:1933/health" | "$VENV/bin/python" -m json.tool >&2 || true
        exit 0
    fi
    sleep 1
done
die "service did not become healthy within 60s (journalctl --user -u openviking-website)"
