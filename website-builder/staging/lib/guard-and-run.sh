#!/usr/bin/env bash
# STAGING-ONLY egress guard + launcher handoff (root, runs INSIDE the staging unit).
#
# WHAT IT DOES
#   1. Installs a LAYERED, staging-only egress policy for THIS unit's cgroup:
#        L1 (domain) : a sinkhole /etc/hosts (bind-mounted by the outer launcher)
#                      resolves the Vercel MANAGEMENT API hostnames to loopback,
#                      where nothing listens. Survives Vercel IP changes.
#        L2 (IP)     : an nft rule drops tcp/443 to the Vercel management API
#                      address range for THIS cgroup only -- catches a caller
#                      that bypasses DNS by hardcoding an IP.
#        L3 (port)   : an nft rule drops direct loopback :1933 (OpenViking) for
#                      THIS cgroup only, forcing all OpenViking traffic through
#                      the read-only allowlist proxy on :1934. The proxy is a
#                      SEPARATE unit (different cgroup) so it is unaffected.
#      Preview deployments (<project>.vercel.app) live in a DIFFERENT range and
#      are intentionally NOT dropped, because the preview smoke test must reach
#      them. The model endpoint :20128 and the broker socket are untouched.
#   2. FAIL-CLOSED self-check: api.vercel.com MUST be blocked; api.telegram.org,
#      registry.npmjs.org, the model endpoint and the OpenViking proxy MUST be
#      reachable; direct :1933 MUST be blocked. If any check fails we refuse to
#      start.
#   3. Drops to the unprivileged wbe2e user IN THE SAME CGROUP and runs the
#      staging launcher, FORWARDING ALL ARGUMENTS (e.g. --preflight).
#
# WHAT THIS IS *NOT*
#   This is DEFENSE-IN-DEPTH, not comprehensive network isolation. Vercel's
#   address ranges can change and a determined caller could use an unlisted IP.
#   The REAL guarantee is CREDENTIAL isolation: no Vercel token exists anywhere
#   in this sandbox; every Vercel operation goes through the root broker, which
#   enforces a server-side allowlist and the pinned staging project id.
#
# WHY nft: systemd's IPAddressAllow/Deny filter by IP only (not port) and cannot
# separate OpenViking :1933 from the model endpoint :20128 (both 127.0.0.1).
# nft socket cgroupv2 matching filters by PORT and scopes to a single unit -- it
# does NOT touch the host or production.
set -uo pipefail

UNIT_CGROUP="system.slice/${WB_E2E_UNIT_NAME:-wb-e2e-staging}.service"
TABLE="inet wbe2e_egress"
STAGE=/home/wbe2e/wb-e2e-r2
INNER="${WB_E2E_INNER_LAUNCHER:-$STAGE/bin/launch-staging.sh}"
# Vercel management API anycast range (api.vercel.com). NOT *.vercel.app.
VERCEL_MGMT_V4="76.76.21.0/24"
# OpenViking direct port (must go through the :1934 allowlist proxy instead).
OV_DIRECT_PORT=1933

cleanup() {
  # Lifecycle owned by this script (never an exec): always remove the table so
  # no rule can outlive the unit.
  nft delete table $TABLE 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 1) Refuse if the cgroup is not the one we expect (fail closed).
if [ ! -d "/sys/fs/cgroup/$UNIT_CGROUP" ]; then
  echo "GUARD REFUSING: cgroup /sys/fs/cgroup/$UNIT_CGROUP not found" >&2
  exit 1
fi

# 2) Build the table/chain.
nft add table $TABLE 2>/dev/null || true
nft "add chain $TABLE output { type filter hook output priority 0; policy accept; }" 2>/dev/null || true

# 3) L2: drop tcp/443 to the Vercel management range, THIS cgroup only.
if ! nft add rule $TABLE output socket cgroupv2 level 2 "$UNIT_CGROUP" \
        ip daddr $VERCEL_MGMT_V4 tcp dport 443 drop; then
  echo "GUARD REFUSING: could not install Vercel egress rule" >&2
  exit 1
fi
echo "GUARD: egress drop tcp/443 -> $VERCEL_MGMT_V4 for $UNIT_CGROUP (cgroup-scoped)"

# 4) L3: drop DIRECT loopback OpenViking (:1933), forcing the :1934 proxy.
#    IPv4 and IPv6 loopback both covered. The proxy unit's own cgroup is
#    unaffected, so it can still reach :1933.
if ! nft add rule $TABLE output socket cgroupv2 level 2 "$UNIT_CGROUP" \
        ip daddr 127.0.0.1 tcp dport $OV_DIRECT_PORT drop; then
  echo "GUARD REFUSING: could not install OpenViking direct-port rule" >&2
  exit 1
fi
nft add rule $TABLE output socket cgroupv2 level 2 "$UNIT_CGROUP" \
    ip6 daddr ::1 tcp dport $OV_DIRECT_PORT drop 2>/dev/null || true
echo "GUARD: egress drop tcp/$OV_DIRECT_PORT (direct OpenViking) for $UNIT_CGROUP"

# 5) FAIL-CLOSED self-check. Run as wbe2e (same identity/children).
if ! runuser -u wbe2e -- /usr/bin/python3 - <<'PY'
import socket, sys, urllib.request, urllib.error

def https_blocked(host):
    try:
        urllib.request.urlopen("https://" + host + "/", timeout=8)
        return False                 # got a response -> reachable
    except urllib.error.HTTPError:
        return False                 # got an HTTP status -> reachable
    except Exception:
        return True                  # network failure -> blocked

def https_ok(host):
    try:
        urllib.request.urlopen("https://" + host + "/", timeout=8)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False

def tcp_ok(host, port):
    s = socket.socket(); s.settimeout(5)
    try:
        s.connect((host, port)); return True
    except Exception:
        return False
    finally:
        s.close()

checks = [
    ("vercel api blocked",        https_blocked("api.vercel.com"),      True),
    ("telegram reachable",        https_ok("api.telegram.org"),         True),
    ("npm registry reachable",    https_ok("registry.npmjs.org"),       True),
    ("openviking direct blocked", tcp_ok("127.0.0.1", 1933),            False),
    ("openviking proxy reachable", tcp_ok("127.0.0.1", 1934),           True),
    ("model router reachable",    tcp_ok("127.0.0.1", 20128),           True),
]
ok = True
for name, got, want in checks:
    if got != want:
        print(f"SELFCHECK FAIL: {name}: got={got} want={want}", file=sys.stderr)
        ok = False
sys.exit(0 if ok else 1)
PY
then
  echo "GUARD REFUSING: egress self-check failed (see above)" >&2
  exit 1
fi
echo "GUARD: self-check OK (vercel blocked; telegram/npm/model/proxy reachable; direct OV blocked)"

# 6) Hand off to the staging launcher as the unprivileged uid, SAME cgroup.
#    Run as a CHILD (never `exec`): the EXIT trap must fire to remove the rules.
#    Forward ALL arguments so `--preflight` (and any future flag) reaches the app.
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ] && [ -n "${WB_E2E_INNER_ARGS:-}" ]; then
  # Back-compat: accept the env form too, tokenised on whitespace.
  read -r -a ARGS <<< "${WB_E2E_INNER_ARGS}"
fi
echo "GUARD: handing off to $INNER ${ARGS[*]:-} as wbe2e"
set +e
runuser -u wbe2e -- /bin/bash "$INNER" "${ARGS[@]}"
rc=$?
set -e
echo "GUARD: inner launcher exited rc=$rc; removing egress rules"
exit "$rc"
