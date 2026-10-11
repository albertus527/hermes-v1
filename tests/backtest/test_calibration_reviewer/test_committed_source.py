"""Committed-source guards for the reviewer package.

Root cause of the P0 review finding: the repo-wide ``export*`` .gitignore
pattern silently excluded ``backtest/calibration_reviewer/exports.py`` from
the commit, so a clean checkout failed at import time while the author's
working tree (which had the ignored-but-present file) passed all tests.

These tests fail when the package's runtime modules are missing from the
committed tree again. They read the git index (not working-tree existence),
so an ignored-but-present file cannot mask the problem.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO_ROOT / "backtest" / "calibration_reviewer"

# Every runtime module server.py imports (or documents) must be committed.
REQUIRED_MODULES = (
    "__init__.py",
    "__main__.py",
    "datasets.py",
    "exports.py",
    "labels.py",
    "reveal.py",
    "rubric.py",
    "schema.py",
    "security.py",
    "server.py",
    "store.py",
)

# Static assets the UI cannot function without.
REQUIRED_STATIC = ("index.html", "app.js", "style.css")


def _tracked(relpath: str) -> bool:
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if probe.returncode != 0:
        # No git metadata (e.g. sdist/exported tree): the git-based guards
        # cannot run here; other tests cover import behavior.
        pytest.skip("not inside a git work tree")
    out = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relpath],
        cwd=REPO_ROOT, capture_output=True, text=True)
    return out.returncode == 0


@pytest.mark.parametrize("module", REQUIRED_MODULES)
def test_runtime_module_is_committed(module):
    rel = f"backtest/calibration_reviewer/{module}"
    assert (PACKAGE_DIR / module).is_file(), f"{rel} missing on disk"
    assert _tracked(rel), (
        f"{rel} exists on disk but is NOT committed — a .gitignore pattern "
        f"is swallowing it (this is how exports.py was lost)")


@pytest.mark.parametrize("asset", REQUIRED_STATIC)
def test_static_asset_is_committed(asset):
    rel = f"backtest/calibration_reviewer/static/{asset}"
    assert (PACKAGE_DIR / "static" / asset).is_file()
    assert _tracked(rel), f"{rel} exists on disk but is NOT committed"


def test_no_ignored_files_inside_package():
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("not inside a git work tree")
    out = subprocess.run(
        ["git", "ls-files", "-o", "--ignored", "--exclude-standard",
         "--directory", "backtest/calibration_reviewer"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    offenders = [
        line for line in out.stdout.splitlines()
        if line.endswith(".py") or line.endswith(".js")
    ]
    assert not offenders, (
        f"runtime files ignored by .gitignore inside the reviewer package: "
        f"{offenders}")
