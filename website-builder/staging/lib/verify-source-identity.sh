#!/usr/bin/env bash
# FAIL-CLOSED source-identity verifier for the staging runtime.
#
# The launcher's OLD guard trusted $STAGE/PINNED_COMMIT *text* only, so a stale
# executable tree ran silently while the marker claimed the new commit. This
# verifier checks the ACTUAL tree against the manifest written by the refresh
# procedure, and confirms the interpreter resolves `app` from the pinned source.
#
# Usage (launcher, or operator):
#   bash verify-source-identity.sh [--src DIR] [--manifest FILE] [--pin FILE]
#                                  [--helper PATH] [--python PATH] [--no-imports]
# Exits 0 only when the live tree matches the pinned manifest; otherwise 1.
set -euo pipefail

SRC=/home/wbe2e/src-r2
STAGE=/home/wbe2e/wb-e2e-r2
MANIFEST="$STAGE/SOURCE_IDENTITY.json"
PIN="$STAGE/PINNED_COMMIT"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$HERE/source_identity.py"
PY="${WB_PYTHON:-python3}"
DO_IMPORTS=1

while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$2"; shift 2 ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    --pin) PIN="$2"; shift 2 ;;
    --helper) HELPER="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --no-imports) DO_IMPORTS=0; shift ;;
    -h|--help) echo "usage: $0 [--src DIR] [--manifest FILE] [--pin FILE]"; exit 0 ;;
    *) echo "REFUSING: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f "$HELPER" ]; then
  echo "REFUSING: source-identity helper not found: $HELPER" >&2
  exit 1
fi

EXPECT=""
if [ -f "$PIN" ]; then EXPECT="$(cat "$PIN")"; fi

ARGS=(verify --src "$SRC" --manifest "$MANIFEST")
[ -n "$EXPECT" ] && ARGS+=(--expect-commit "$EXPECT")

if ! "$PY" "$HELPER" "${ARGS[@]}"; then
  echo "SOURCE-IDENTITY REFUSING: live tree does not match pinned manifest ($MANIFEST)" >&2
  exit 1
fi

if [ "$DO_IMPORTS" = "1" ]; then
  if ! "$PY" "$HELPER" verify-imports --src "$SRC"; then
    echo "SOURCE-IDENTITY REFUSING: interpreter resolves app modules outside $SRC" >&2
    exit 1
  fi
fi

echo "SOURCE-IDENTITY OK: $SRC matches $MANIFEST (commit ${EXPECT:-?})"
