#!/usr/bin/env bash
# Refresh the PINNED staging source tree from a verified commit of this repo.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# The staging runtime runs as the unprivileged ``wbe2e`` identity from a
# git-less archive of this repository. A live incident showed that refreshing
# the WRONG directory silently ran stale code: the launcher reads its source
# from ``/home/wbe2e/src-r2`` (see launch-staging.sh ``SRC=`` and the systemd
# ``ReadOnlyPaths=``), NOT from any ``wb-e2e-r2/src-r2`` copy. This script
# therefore targets ``/home/wbe2e/src-r2`` EXCLUSIVELY, swaps the previous tree
# to ``/home/wbe2e/src-r2.prev`` for rollback, and writes BOTH the pinned-commit
# marker and a content manifest that the launcher verifies before polling.
#
# READ-ONLY ENFORCEMENT
# ---------------------
# The source tree is made read-only to the runtime by the systemd
# ``ReadOnlyPaths=/home/wbe2e/src-r2`` boundary in the transient unit started by
# ``launch-staging-sandboxed.sh`` -- NOT by filesystem permission bits. An
# earlier ad-hoc refresh additionally ran ``chmod -R a-w`` on the source. That
# was REDUNDANT (the mount is already read-only) and actively harmful: the
# frontend starter is copied out of this tree, so read-only source bits
# propagated into project workspaces and broke REBUILDS (``shutil.copy2``
# raised ``PermissionError`` over an existing read-only file). This script
# therefore does NOT chmod the source.
#
# USAGE (operator):
#   sudo -n bash website-builder/staging/lib/refresh-staging-source.sh
#   # optional: sudo -n bash .../refresh-staging-source.sh <commit-ish>
set -euo pipefail

REPO=/home/albertus527/hermes-website-r2
STAGE=/home/wbe2e/wb-e2e-r2
# The ONE executable source root the launcher/systemd read.
DEST=/home/wbe2e/src-r2
PREV=/home/wbe2e/src-r2.prev
PIN="$STAGE/PINNED_COMMIT"
MANIFEST="$STAGE/SOURCE_IDENTITY.json"
HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/source_identity.py"
OWNER=wbe2e:wbe2e

cd "$REPO"

# 1. Resolve the commit to pin (default: current HEAD).
NEW="${1:-$(git rev-parse HEAD)}"
if ! git cat-file -e "${NEW}^{commit}" 2>/dev/null; then
  echo "REFUSING: '$NEW' is not a commit in $REPO" >&2
  exit 1
fi
NEW="$(git rev-parse "$NEW")"
echo "OK: pinning $NEW"

# 2. Guard: never touch any production path. The destination must be EXACTLY
#    the launcher's source root.
case "$(readlink -f "$DEST" 2>/dev/null || echo "$DEST")" in
  /home/albertus527/*) echo "REFUSING: destination resolves under the production home" >&2; exit 1 ;;
esac
if [ "$DEST" != "/home/wbe2e/src-r2" ]; then
  echo "REFUSING: unexpected destination $DEST" >&2; exit 1
fi
echo "OK: destination is the launcher source root ($DEST)"

# 3. Build a fresh archive of the pinned commit (no autodev in the staging
#    source: it is not part of the Website Builder runtime).
TMP="$(mktemp -d /tmp/src-r2-new.XXXXXX)"
git archive "$NEW" | tar -x -C "$TMP"
rm -rf "$TMP/autodev" "$TMP/tests/autodev"

# 4. Sanity-check the payload BEFORE swapping it in.
test -f "$TMP/website-builder/app/runtime.py" || {
  echo "REFUSING: archive missing website-builder/app/runtime.py" >&2; exit 1; }
echo "  runtime.py present"
echo "  design_capability_report wiring: $(grep -c 'design_capability_report=design_capability_report' "$TMP/website-builder/app/runtime.py" || true)"
echo "  DesignEntryError present       : $(grep -c 'class DesignEntryError' "$TMP/website-builder/app/core/design_retrieval.py" || true)"
echo "  rebuild-safe starter merge     : $(grep -c 'def _copy_tree_merge' "$TMP/website-builder/app/projects/build.py" || true)"

# 5. Atomic-ish swap, keeping the previous tree for rollback.
rm -rf "$PREV"
if [ -d "$DEST" ]; then mv "$DEST" "$PREV"; fi
mv "$TMP" "$DEST"
chown -R "$OWNER" "$DEST"
# NOTE: deliberately NO chmod -R a-w here. See README above.

# 6. Record the pinned commit AND a content manifest of the live tree.
printf '%s\n' "$NEW" > "$PIN"
chown "$OWNER" "$PIN"
if [ -f "$HELPER" ]; then
  python3 "$HELPER" manifest --src "$DEST" --commit "$NEW" --out "$MANIFEST"
  chown "$OWNER" "$MANIFEST"
else
  echo "WARNING: source-identity helper missing; manifest NOT written" >&2
fi

echo "OK: swapped; PINNED_COMMIT=$(cat "$PIN")"
echo "OK: source modes are now the archive defaults (writable by owner);"
echo "    read-only enforcement is the systemd ReadOnlyPaths mount."
