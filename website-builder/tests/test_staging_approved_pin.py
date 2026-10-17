"""Staging approved-pin gate — fail-closed authorization for the runtime commit.

WHY THIS EXISTS
---------------
``launch-staging.sh`` used to hardcode ``PINNED_COMMIT=<one G4 sha>``. That made
the staging environment able to run exactly one commit, so any experiment
needing newer code required hand-editing tracked tooling, and the constant could
silently drift from the checkout that was actually refreshed.

THE TRUST BOUNDARY THIS PINS (measured on the host, not assumed)
---------------------------------------------------------------
``$STAGE/PINNED_COMMIT`` and ``$STAGE/SOURCE_IDENTITY.json`` are owned by
``wbe2e`` — the same identity the staging runtime runs as. They are therefore
WRITABLE BY THE RUNTIME ITSELF and cannot authorize anything; treating them as
approval would let the runtime approve its own commit. They are evidence of what
was last refreshed.

The only authorization source is ``/etc/wb-e2e/approved-pin``, root-owned and
not writable by ``wbe2e``. A refresh can update the marker and manifest, but
only root can update the approved record — so a refresh to an unapproved commit
fails closed at launch.

WHAT THESE TESTS COVER
----------------------
valid pin, mismatched marker, unapproved SHA, malformed manifest, stale/missing
tree digest, unexpected branch, malformed approved record, missing record.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "staging" / "lib" / "verify-approved-pin.sh"

APPROVED = "1e727016b1c1cc88d95d4c556240f69be7e95cb9"
OTHER = "310a029c5c7a0198d88c3b3fdb9a000cd929432b"
DIGEST = "b" * 64


def _manifest(path: Path, commit: str = APPROVED, digest: str | None = DIGEST) -> Path:
    payload = {"schema": 1, "commit": commit}
    if digest is not None:
        payload["tree_digest"] = digest
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(tmp_path, *, approved_text=None, marker=APPROVED, manifest_commit=APPROVED,
         manifest_digest=DIGEST, manifest_raw=None, branch="feature/website-r2-redesign",
         approved_present=True):
    approved = tmp_path / "approved-pin"
    if approved_present:
        approved.write_text(
            approved_text if approved_text is not None
            else f"commit={APPROVED}\nbranch=feature/website-r2-redesign\n",
            encoding="utf-8",
        )
    marker_path = tmp_path / "PINNED_COMMIT"
    marker_path.write_text(marker + "\n", encoding="utf-8")
    manifest = tmp_path / "SOURCE_IDENTITY.json"
    if manifest_raw is not None:
        manifest.write_text(manifest_raw, encoding="utf-8")
    else:
        _manifest(manifest, commit=manifest_commit, digest=manifest_digest)

    return subprocess.run(
        ["bash", str(SCRIPT),
         "--approved", str(approved),
         "--marker", str(marker_path),
         "--manifest", str(manifest),
         "--branch", branch,
         "--python", "python3"],
        capture_output=True, text=True, timeout=60,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_pin_passes(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "APPROVED-PIN OK" in r.stderr


def test_approved_record_without_branch_line_is_accepted(tmp_path):
    """The branch line is optional; a commit-only record still authorizes."""
    r = _run(tmp_path, approved_text=f"commit={APPROVED}\n")
    assert r.returncode == 0, r.stderr


def test_comments_and_blank_lines_are_ignored(tmp_path):
    r = _run(tmp_path, approved_text=f"# approved by operator\n\ncommit={APPROVED}\n")
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# Fail-closed cases
# ---------------------------------------------------------------------------


def test_mismatched_marker_is_refused(tmp_path):
    """Marker says a different commit than the approved record."""
    r = _run(tmp_path, marker=OTHER)
    assert r.returncode == 1
    assert "refreshed marker" in r.stderr


def test_unapproved_sha_is_refused(tmp_path):
    """A refresh to an unapproved commit cannot authorize itself."""
    r = _run(tmp_path, marker=OTHER, manifest_commit=OTHER)
    assert r.returncode == 1
    assert "APPROVED-PIN REFUSING" in r.stderr


def test_manifest_commit_mismatch_is_refused(tmp_path):
    """Marker was updated but the manifest still describes the old tree."""
    r = _run(tmp_path, manifest_commit=OTHER)
    assert r.returncode == 1
    assert "manifest commit" in r.stderr


def test_malformed_manifest_is_refused(tmp_path):
    r = _run(tmp_path, manifest_raw="{not json")
    assert r.returncode == 1
    assert "not valid JSON" in r.stderr


def test_manifest_that_is_not_an_object_is_refused(tmp_path):
    r = _run(tmp_path, manifest_raw="[1, 2, 3]")
    assert r.returncode == 1
    assert "not a JSON object" in r.stderr


def test_manifest_without_tree_digest_is_refused(tmp_path):
    """A manifest that cannot prove a tree must not authorize a launch."""
    r = _run(tmp_path, manifest_digest=None)
    assert r.returncode == 1
    assert "tree_digest" in r.stderr


def test_manifest_with_wrong_length_digest_is_refused(tmp_path):
    r = _run(tmp_path, manifest_digest="abc")
    assert r.returncode == 1
    assert "tree_digest" in r.stderr


def test_unexpected_branch_is_refused(tmp_path):
    r = _run(tmp_path, approved_text=f"commit={APPROVED}\nbranch=main\n")
    assert r.returncode == 1
    assert "does not match expected" in r.stderr


def test_malformed_commit_in_approved_record_is_refused(tmp_path):
    r = _run(tmp_path, approved_text="commit=not-a-sha\n")
    assert r.returncode == 1
    assert "lowercase hex" in r.stderr


def test_short_commit_is_refused(tmp_path):
    r = _run(tmp_path, approved_text="commit=1e727016\n", marker="1e727016")
    assert r.returncode == 1
    assert "40-character" in r.stderr


def test_missing_approved_record_is_refused(tmp_path):
    r = _run(tmp_path, approved_present=False)
    assert r.returncode == 1
    assert "missing approved pin record" in r.stderr


def test_unknown_key_in_approved_record_is_refused(tmp_path):
    """Unknown keys are refused, never silently ignored."""
    r = _run(tmp_path, approved_text=f"commit={APPROVED}\nallow_anything=true\n")
    assert r.returncode == 1
    assert "unknown key" in r.stderr


def test_empty_approved_record_is_refused(tmp_path):
    r = _run(tmp_path, approved_text="\n\n")
    assert r.returncode == 1
    assert "names no commit" in r.stderr


# ---------------------------------------------------------------------------
# The launcher must actually use the gate
# ---------------------------------------------------------------------------


def test_launcher_no_longer_hardcodes_a_commit():
    launcher = SCRIPT.parent / "launch-staging.sh"
    text = launcher.read_text(encoding="utf-8")
    # No literal 40-char SHA may be baked in as the pin.
    assert not re.search(r"PINNED_COMMIT=[0-9a-f]{40}", text), (
        "the launcher must not bake in a commit; the approved record is the "
        "single source of truth"
    )
    # The value must be read from the marker AFTER the gate approved it.
    assert 'PINNED_COMMIT="$(tr -d' in text


def test_launcher_calls_the_approved_pin_gate():
    launcher = SCRIPT.parent / "launch-staging.sh"
    text = launcher.read_text(encoding="utf-8")
    assert "verify-approved-pin.sh" in text
    assert "/etc/wb-e2e/approved-pin" in text
    # and it must still re-hash the live tree afterwards
    assert "verify-source-identity.sh" in text
