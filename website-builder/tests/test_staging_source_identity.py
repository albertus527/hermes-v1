"""Regression tests for the staging SOURCE-IDENTITY contract.

A live incident proved the launcher's ``PINNED_COMMIT`` marker was INSUFFICIENT:
the marker named the new commit while the executable tree (``/home/wbe2e/src-r2``)
still held an OLDER commit, so un-pinned code ran silently (empty
DesignContextPack, spurious N6-E QA parity failure).

These tests pin the fix:
  * a manifest is content-addressed (any file change is detected);
  * ``verify`` FAILS CLOSED when a tree is substituted with a stale one;
  * ``verify-imports`` FAILS CLOSED when ``app`` resolves outside the source;
  * ``verify-runtime`` FAILS CLOSED when a PID's cwd/cmdline/env disagree;
  * the refresh script targets the launcher's source root (NOT a dead copy);
  * the launcher runs the verifier BEFORE polling.

All are PURE: no root, no network, no systemd.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "staging" / "lib"
HELPER = LIB / "source_identity.py"
VERIFY_SH = LIB / "verify-source-identity.sh"
LAUNCH_INNER = LIB / "launch-staging.sh"
REFRESH = LIB / "refresh-staging-source.sh"


def _load_helper():
    spec = importlib.util.spec_from_file_location("wb_source_identity", HELPER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def si():
    return _load_helper()


def _make_tree(root: Path, runtime_text: str, build_text: str) -> Path:
    app = root / "website-builder" / "app"
    (app / "core").mkdir(parents=True, exist_ok=True)
    (app / "runtime.py").write_text(runtime_text, encoding="utf-8")
    (app / "projects").mkdir(parents=True, exist_ok=True)
    (app / "projects" / "build.py").write_text(build_text, encoding="utf-8")
    (app / "core" / "design_context.py").write_text("x=1\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Content-addressed manifest + fail-closed verification
# ---------------------------------------------------------------------------

def test_manifest_is_deterministic(si, tmp_path):
    tree = _make_tree(tmp_path / "t", "a\n", "b\n")
    m1 = si.build_manifest(tree, "C1")
    m2 = si.build_manifest(tree, "C1")
    assert m1["tree_digest"] == m2["tree_digest"]
    assert m1["file_count"] == m2["file_count"] >= 3


def test_manifest_changes_when_content_changes(si, tmp_path):
    tree = _make_tree(tmp_path / "t", "a\n", "b\n")
    d1 = si.build_manifest(tree, "C1")["tree_digest"]
    (tree / "website-builder" / "app" / "runtime.py").write_text("a CHANGED\n")
    d2 = si.build_manifest(tree, "C1")["tree_digest"]
    assert d1 != d2


def test_verify_passes_for_matching_tree(si, tmp_path):
    tree = _make_tree(tmp_path / "t", "a\n", "b\n")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(si.build_manifest(tree, "C1")))
    assert si.verify_manifest(tree, man, "C1") == []


def test_verify_rejects_stale_substituted_tree(si, tmp_path):
    """P1.6 core: substitute a STALE tree under a manifest -> REFUSED."""
    tree = _make_tree(tmp_path / "t", "NEW runtime\n", "NEW build\n")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(si.build_manifest(tree, "NEW")))
    # Simulate the incident: the tree silently reverts to OLD content.
    (tree / "website-builder" / "app" / "runtime.py").write_text("OLD runtime\n")
    (tree / "website-builder" / "app" / "projects" / "build.py").write_text("OLD build\n")
    errors = si.verify_manifest(tree, man, "NEW")
    assert errors, "stale tree MUST be rejected"
    assert any("digest mismatch" in e for e in errors)


def test_verify_rejects_wrong_commit(si, tmp_path):
    tree = _make_tree(tmp_path / "t", "a\n", "b\n")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(si.build_manifest(tree, "C1")))
    errors = si.verify_manifest(tree, man, "OTHER")
    assert any("commit" in e for e in errors)


def test_verify_rejects_missing_manifest(si, tmp_path):
    tree = _make_tree(tmp_path / "t", "a\n", "b\n")
    errors = si.verify_manifest(tree, tmp_path / "absent.json", "C1")
    assert errors and "missing" in errors[0]


def test_verify_rejects_symlink_swapped_for_file(si, tmp_path):
    """A real file replaced by a symlink must change the digest (no following)."""
    tree = _make_tree(tmp_path / "t", "a\n", "b\n")
    man = tmp_path / "m.json"
    man.write_text(json.dumps(si.build_manifest(tree, "C1")))
    target = tree / "website-builder" / "app" / "runtime.py"
    outside = tmp_path / "outside.py"
    outside.write_text("a\n")  # SAME bytes, but now via a symlink
    target.unlink()
    target.symlink_to(outside)
    errors = si.verify_manifest(tree, man, "C1")
    assert errors, "symlink substitution must be detected"


def test_key_modules_recorded(si, tmp_path):
    tree = _make_tree(tmp_path / "t", "a\n", "b\n")
    man = si.build_manifest(tree, "C1")
    assert "app/runtime.py" in man["key_modules"]
    assert man["key_modules"]["app/runtime.py"] is not None


# ---------------------------------------------------------------------------
# Import-identity: `app` must resolve INSIDE the pinned source root
# ---------------------------------------------------------------------------

def test_verify_imports_passes_for_real_repo(si):
    # The dev repo's git root is a valid source root (website-builder/ lives in it).
    assert si.verify_imports(REPO.parent) == []


def test_verify_imports_rejects_empty_src(si, tmp_path):
    (tmp_path / "website-builder").mkdir()
    errors = si.verify_imports(tmp_path, modules=("app",))
    assert errors, "empty src must not satisfy import identity"


# ---------------------------------------------------------------------------
# Runtime-identity: /proc/<pid> cwd/cmdline/env
# ---------------------------------------------------------------------------

def _fake_proc(tmp_path: Path, pid: int, cwd: str, argv, env: dict):
    base = tmp_path / str(pid)
    base.mkdir(parents=True)
    (base / "cwd").symlink_to(cwd)
    (base / "cmdline").write_bytes(
        b"\0".join(a.encode() for a in argv) + b"\0")
    (base / "environ").write_bytes(
        b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    return tmp_path


def test_verify_runtime_passes_for_matching_pid(si, tmp_path):
    proc = _fake_proc(
        tmp_path, 4242, cwd=str(REPO.parent),
        argv=["/usr/bin/python3", "-m", "app", "--preflight"],
        env={"PYTHONPATH": str(REPO.parent)},
    )
    errors = si.verify_runtime(
        proc, 4242, expect_cwd=str(REPO.parent), expect_src=str(REPO.parent))
    assert errors == [], errors


def test_verify_runtime_rejects_wrong_cwd(si, tmp_path):
    """The exact incident: cwd points at a DIFFERENT (stale) tree."""
    proc = _fake_proc(
        tmp_path, 4243, cwd="/home/wbe2e/wb-e2e-r2/src-r2/website-builder",
        argv=["/usr/bin/python3", "-m", "app"],
        env={"PYTHONPATH": "/home/wbe2e/wb-e2e-r2/src-r2"},
    )
    errors = si.verify_runtime(
        proc, 4243,
        expect_cwd="/home/wbe2e/src-r2/website-builder",
        expect_src="/home/wbe2e/src-r2",
    )
    assert any("cwd" in e for e in errors)
    assert any("PYTHONPATH" in e for e in errors)


def test_verify_runtime_rejects_non_app_cmdline(si, tmp_path):
    proc = _fake_proc(
        tmp_path, 4244, cwd="/tmp",
        argv=["/usr/bin/python3", "-m", "pytest"],
        env={},
    )
    errors = si.verify_runtime(proc, 4244)
    assert any("cmdline" in e for e in errors)


def test_verify_runtime_rejects_missing_pid(si):
    errors = si.verify_runtime(Path("/proc"), 999999999)
    assert errors and "no such process" in errors[0]


# ---------------------------------------------------------------------------
# Script wiring: the refresh targets the real tree; the launcher verifies first
# ---------------------------------------------------------------------------

def test_refresh_targets_launcher_source_root():
    text = REFRESH.read_text(encoding="utf-8")
    assert "DEST=/home/wbe2e/src-r2" in text, (
        "refresh MUST target the launcher's source root, not a dead copy")
    # The known-dead path must NOT be the destination.
    assert 'DEST="$STAGE/src-r2"' not in text
    # Rollback sibling must be the real one.
    assert "src-r2.prev" in text
    # Must NOT chmod the source read-only (the regression we already fixed).
    # Check only EXECUTABLE lines: the string may legitimately appear in the
    # explanatory comment that documents why we removed it.
    code_lines = [
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    ]
    assert not any("chmod -R a-w" in ln for ln in code_lines), (
        "refresh must not chmod the source read-only")


def test_refresh_writes_manifest():
    text = REFRESH.read_text(encoding="utf-8")
    assert "SOURCE_IDENTITY.json" in text
    assert "manifest --src" in text


def test_launcher_runs_source_identity_verifier_before_polling():
    text = LAUNCH_INNER.read_text(encoding="utf-8")
    assert "verify-source-identity.sh" in text
    assert "SOURCE_IDENTITY.json" in text
    # The verifier must run BEFORE the app exec.
    assert text.index("verify-source-identity.sh") < text.index("exec \"$VENV/bin/python\"")


def test_launcher_refuses_when_verifier_missing():
    text = LAUNCH_INNER.read_text(encoding="utf-8")
    assert "missing source-identity verifier" in text


def test_verifier_script_exists_and_is_bash():
    assert VERIFY_SH.is_file()
    assert VERIFY_SH.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert "SOURCE-IDENTITY REFUSING" in VERIFY_SH.read_text(encoding="utf-8")
