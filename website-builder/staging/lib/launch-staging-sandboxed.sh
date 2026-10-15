#!/usr/bin/env bash
# OPERATOR entrypoint: launch the STAGING runtime inside a HARDENED transient
# systemd unit as the unprivileged wbe2e identity, WITH layered egress policy.
#
# LAYERS
#   1. OS filesystem/namespace confinement (systemd properties below):
#        ProtectHome=tmpfs      -> /home/albertus527 (prod home/.ssh) HIDDEN
#        ProtectSystem=strict   -> host filesystem read-only
#        BindPaths=/home/wbe2e  -> staging tree bound back in
#        ReadOnlyPaths=src-r2   -> pinned source read-only
#        NoNewPrivileges, RestrictSUIDSGID, RestrictNamespaces, ...
#   2. Egress policy (guard-and-run.sh, running as root INSIDE this unit):
#        L1 domain sinkhole: this launcher bind-mounts a staging /etc/hosts that
#           resolves the Vercel MANAGEMENT API hostnames to loopback.
#        L2 IP drop: the guard installs a cgroup-scoped nft rule dropping
#           tcp/443 to the Vercel management range for THIS unit's cgroup only.
#        L3 port drop: the guard drops direct loopback :1933 (OpenViking) so all
#           OpenViking traffic goes through the read-only :1934 proxy.
#        Telegram (api.telegram.org), the npm registry and the model endpoint
#        stay reachable.
#   3. Credential confinement (the broker, a SEPARATE root unit):
#        holds the Vercel token in a root-only 0600 file; the runtime reaches
#        it only over a UNIX socket. No Vercel token exists in this sandbox.
#
# NOTE: RestrictAddressFamilies must include AF_NETLINK (nft) and AF_UNIX
# (broker socket) in addition to INET/INET6.
#
# USAGE (operator, after explicit approval -- nothing here runs on its own):
#   bash /home/wbe2e/wb-e2e-r2/bin/launch-staging-sandboxed.sh --preflight
#   bash /home/wbe2e/wb-e2e-r2/bin/launch-staging-sandboxed.sh
set -euo pipefail

STAGE=/home/wbe2e/wb-e2e-r2
UNIT=wb-e2e-staging
MEMORY_MAX="${WB_E2E_MEMORY_MAX:-2500M}"
CPU_QUOTA="${WB_E2E_CPU_QUOTA:-300%}"
GUARD=/usr/local/lib/wb-e2e/guard-and-run.sh
BROKER_SOCK=/run/wb-e2e/vercel.sock
# Root-owned staging /etc/hosts (Vercel management hostnames -> loopback).
STAGING_HOSTS=/etc/wb-e2e/staging-hosts
# OpenViking allowlist proxy (separate unit) that the runtime must use.
OV_PROXY_PORT=1934

# --detach: start the unit and return immediately (for the long-running poller).
# The flag is OURS, not the app's, so strip it before forwarding.
DETACH=0
FORWARD=()
for a in "$@"; do
  if [ "$a" = "--detach" ]; then DETACH=1; else FORWARD+=("$a"); fi
done

# systemd-run wait/attach flags depend on the mode.
if [ "$DETACH" = "1" ]; then
  RUN_MODE=(--no-block)
else
  RUN_MODE=(--pty --wait --collect)
fi


if [ ! -x "$GUARD" ]; then
  echo "REFUSING: missing egress guard $GUARD" >&2
  exit 1
fi
if [ ! -S "$BROKER_SOCK" ]; then
  echo "REFUSING: Vercel broker socket $BROKER_SOCK not present (start wb-e2e-broker first)." >&2
  exit 1
fi
if [ ! -f "$STAGING_HOSTS" ]; then
  echo "REFUSING: missing staging hosts sinkhole $STAGING_HOSTS" >&2
  exit 1
fi
if ! ss -ltn 2>/dev/null | grep -q ":${OV_PROXY_PORT} "; then
  echo "REFUSING: OpenViking allowlist proxy not listening on :${OV_PROXY_PORT}" >&2
  echo "  start it: sudo systemctl start wb-e2e-ovproxy.service" >&2
  exit 1
fi

exec sudo -n systemd-run \
  --unit="$UNIT" \
  --property=ProtectSystem=strict \
  --property=ProtectHome=tmpfs \
  --property=BindPaths=/home/wbe2e \
  --property=BindReadOnlyPaths="$STAGING_HOSTS:/etc/hosts" \
  --property=PrivateTmp=yes \
  --property=PrivateDevices=yes \
  --property=NoNewPrivileges=yes \
  --property=RestrictNamespaces=yes \
  --property=ProtectKernelTunables=yes \
  --property=ProtectKernelModules=yes \
  --property=ProtectControlGroups=yes \
  --property=RestrictSUIDSGID=yes \
  --property=LockPersonality=yes \
  --property=RestrictAddressFamilies="AF_UNIX AF_INET AF_INET6 AF_NETLINK" \
  --property=ReadWritePaths="$STAGE/state $STAGE/workspaces $STAGE/logs $STAGE/output-repo" \
  --property=ReadOnlyPaths=/home/wbe2e/src-r2 \
  --property=MemoryMax="$MEMORY_MAX" \
  --property=CPUQuota="$CPU_QUOTA" \
  --setenv=HOME=/home/wbe2e \
  --setenv=WB_E2E_UNIT_NAME="$UNIT" \
  --setenv=WB_E2E_INNER_LAUNCHER="$STAGE/bin/launch-staging.sh" \
  --setenv=WEBSITE_BUILDER_STAGING_GUARD=1 \
  --setenv=WEBSITE_BUILDER_VERCEL_BROKER=/run/wb-e2e/vercel.sock \
  --setenv=PLAYWRIGHT_BROWSERS_PATH=/home/wbe2e/.cache/ms-playwright \
  "${RUN_MODE[@]}" \
  /bin/bash "$GUARD" "${FORWARD[@]}"
