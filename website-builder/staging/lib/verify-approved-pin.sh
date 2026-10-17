#!/usr/bin/env bash
# FAIL-CLOSED approved-pin verifier for the staging runtime.
#
# WHY THIS EXISTS
# ---------------
# launch-staging.sh hardcoded `PINNED_COMMIT=<one G4 sha>`. Changing the staging
# source meant editing tracked tooling, and the constant could silently drift
# from the actual checkout. It also meant the environment could only ever run
# the single baked-in commit, so no experiment needing newer code could be
# staged without hand-editing a shell script.
#
# WHAT IS *NOT* AUTHORIZATION (measured on this host, not assumed)
# ---------------------------------------------------------------
#   $STAGE/PINNED_COMMIT          owner wbe2e -> WRITABLE BY THE RUNTIME IDENTITY
#   $STAGE/SOURCE_IDENTITY.json   owner wbe2e -> WRITABLE BY THE RUNTIME IDENTITY
# The staging runtime runs AS wbe2e, so it could rewrite both. They are EVIDENCE
# of the last refresh. Treating them as approval would let the runtime authorize
# itself. They are therefore never trusted alone.
#
# WHAT *IS* AUTHORIZATION
# -----------------------
#   /etc/wb-e2e/approved-pin      owner root, 0644 -> NOT writable by wbe2e
# A separate, independently protected record written by an operator with root
# privilege as an explicit reviewed approval step. That is the ONLY source of
# truth for "which commit may run".
#
# WHAT IS VERIFIED (every check must hold, otherwise exit 1)
# ---------------------------------------------------------
#   1. the approved record exists and names exactly one full 40-char lowercase
#      hex commit; unknown keys are refused rather than ignored
#   2. the approved branch (when recorded) equals the expected branch
#   3. the refreshed marker equals the approved commit
#   4. the source manifest parses as JSON and names the same commit
#   5. the manifest carries a usable 64-char tree digest
#
# Deliberately NOT done here: hashing the live tree. The launcher still runs
# verify-source-identity.sh afterwards, which re-hashes the actual checkout and
# confirms the interpreter resolves `app` from the pinned source. This script
# answers "is this commit allowed?", that one answers "is this really that
# commit?". Both must pass.
#
# The refresh script cannot satisfy this check by itself: it writes the marker
# and manifest, but only root can write the approved record. A refresh to an
# unapproved commit therefore fails closed at launch.
#
# Usage (launcher, or operator):
#   bash verify-approved-pin.sh [--approved FILE] [--marker FILE]
#                               [--manifest FILE] [--branch NAME] [--python PATH]
# Exits 0 only when the approved record, marker and manifest agree.
set -euo pipefail

APPROVED=/etc/wb-e2e/approved-pin
MARKER=/home/wbe2e/wb-e2e-r2/PINNED_COMMIT
MANIFEST=/home/wbe2e/wb-e2e-r2/SOURCE_IDENTITY.json
BRANCH=feature/website-r2-redesign
PY=python3

while [ $# -gt 0 ]; do
  case "$1" in
    --approved) APPROVED="$2"; shift 2 ;;
    --marker) MARKER="$2"; shift 2 ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    -h|--help)
      echo "usage: $0 [--approved FILE] [--marker FILE] [--manifest FILE] [--branch NAME] [--python PATH]"
      exit 0 ;;
    *) echo "APPROVED-PIN REFUSING: unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { echo "APPROVED-PIN REFUSING: $*" >&2; exit 1; }

# ---- 1. the approved record -------------------------------------------------
[ -e "$APPROVED" ] || fail "missing approved pin record: $APPROVED"
[ -f "$APPROVED" ] || fail "approved pin record is not a regular file: $APPROVED"
[ -r "$APPROVED" ] || fail "approved pin record is not readable: $APPROVED"

APPROVED_COMMIT=""
APPROVED_BRANCH=""
while IFS='=' read -r key value || [ -n "$key" ]; do
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  value="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  case "$key" in
    ''|\#*) continue ;;
    commit) APPROVED_COMMIT="$value" ;;
    branch) APPROVED_BRANCH="$value" ;;
    *) fail "unknown key in approved pin record: '$key'" ;;
  esac
done < "$APPROVED"

[ -n "$APPROVED_COMMIT" ] || fail "approved pin record names no commit"
case "$APPROVED_COMMIT" in
  *[!0-9a-f]*) fail "approved commit is not lowercase hex: '$APPROVED_COMMIT'" ;;
esac
[ "${#APPROVED_COMMIT}" -eq 40 ] || \
  fail "approved commit is not a full 40-character SHA: '$APPROVED_COMMIT'"

# ---- 2. the approved branch (optional, but enforced when recorded) ----------
if [ -n "$APPROVED_BRANCH" ] && [ "$APPROVED_BRANCH" != "$BRANCH" ]; then
  fail "approved branch '$APPROVED_BRANCH' does not match expected '$BRANCH'"
fi

# ---- 3. the refreshed marker must agree ------------------------------------
[ -f "$MARKER" ] || fail "missing refreshed marker: $MARKER"
MARKER_COMMIT="$(tr -d '[:space:]' < "$MARKER")"
[ -n "$MARKER_COMMIT" ] || fail "refreshed marker is empty: $MARKER"
[ "$MARKER_COMMIT" = "$APPROVED_COMMIT" ] || \
  fail "refreshed marker '$MARKER_COMMIT' != approved '$APPROVED_COMMIT'"

# ---- 4/5. the manifest must parse and agree --------------------------------
[ -f "$MANIFEST" ] || fail "missing source manifest: $MANIFEST"

if ! "$PY" - "$MANIFEST" "$APPROVED_COMMIT" <<'PYEOF'
import json
import sys

path, approved = sys.argv[1], sys.argv[2]

def refuse(msg):
    print("APPROVED-PIN REFUSING: " + msg, file=sys.stderr)
    raise SystemExit(1)

try:
    with open(path, "r", encoding="utf-8") as handle:
        man = json.load(handle)
except OSError as exc:
    refuse("manifest is unreadable (%s)" % exc)
except ValueError as exc:
    refuse("manifest is not valid JSON (%s)" % exc)

if not isinstance(man, dict):
    refuse("manifest is not a JSON object")

commit = man.get("commit")
if not isinstance(commit, str) or not commit:
    refuse("manifest carries no commit")
if commit != approved:
    refuse("manifest commit %r != approved %r" % (commit, approved))

digest = man.get("tree_digest")
if not isinstance(digest, str) or len(digest) != 64:
    refuse("manifest carries no usable tree_digest")
try:
    int(digest, 16)
except ValueError:
    refuse("manifest tree_digest is not hexadecimal")
PYEOF
then
  exit 1
fi

echo "APPROVED-PIN OK: $APPROVED_COMMIT (branch $BRANCH)" >&2
