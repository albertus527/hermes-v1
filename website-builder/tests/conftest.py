"""Website Builder test-suite hygiene fixtures.

The Website Builder tests live under a sub-directory of the Hermes repo but
exercise Hermes runtime seams (``hermes_cli.config.load_config``,
``hermes_constants.get_hermes_home``). Process-global state can leak between
test modules and change the outcome of an unrelated test that happens to run
later in the same process, or make a test depend on the ambient host instead of
the toolchain this project actually declares:

1. ``sys.path`` — ``app.hermes.adapter`` resolves ``hermes_cli``/``run_agent``
   at import time. If the repo root is not importable when the module is
   (re-)imported, those names become ``None`` and every role-configuration
   resolution silently degrades to "profile configuration unavailable".
2. The context-local ``HERMES_HOME`` override installed by
   ``hermes_constants.set_hermes_home_override`` — a test that sets it without
   resetting redirects every later ``get_hermes_home()`` call.
3. The active ``node``/``agent-browser`` toolchain. ``run.sh`` activates the
   Node version pinned in ``templates/frontend-starter/.nvmrc`` before it starts
   the app; a bare ``pytest`` invocation does not, so it inherits whatever
   ``node`` is first on the ambient PATH.

All three are enforced here so the suite is order-independent and runs against
the toolchain the project declares (mirrors the equivalent guards in the
top-level ``tests/conftest.py`` and the activation in ``run.sh``).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# --- 1. Guarantee the repo root is importable ---------------------------------
# parents[2] == repository root (website-builder/tests/conftest.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --- 2. Put the project's DECLARED Node toolchain on PATH ---------------------
# ``templates/frontend-starter/.nvmrc`` pins the runtime's Node, and ``run.sh``
# activates exactly that version (``nvm use``) before it starts the app. Two
# production contracts are defined against it and cannot be exercised without
# it: ``app.runtime.preflight_node_toolchain`` fails closed on any other major,
# and the QA capture path shells out to the ``agent-browser`` CLI, which is
# installed into that Node's prefix.
#
# A bare ``python -m pytest`` inherits whatever ``node`` is first on the ambient
# PATH. On a host whose system Node (e.g. an apt/nodesource 22) precedes nvm,
# those two tests then fail for a reason unrelated to the code under test.
# Activate the same declared toolchain production does, so the tests run against
# the environment they describe -- instead of skipping them, which would
# silently drop the only real-toolchain coverage. Offline: this reads one local
# file and reorders PATH; no network, no install.
def _declared_node_version() -> str | None:
    """The Node version pinned by the starter's ``.nvmrc`` (what production uses)."""
    nvmrc = REPO_ROOT / "templates" / "frontend-starter" / ".nvmrc"
    try:
        version = nvmrc.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return version or None


def _declared_node_bin_dir(version: str) -> Path | None:
    """The ``bin/`` of the installed Node that satisfies ``version``, if any.

    Searches the nvm tree (the exact pin first, then any build of the same major,
    so a ``26.5.0`` pin is satisfied by an installed ``v26.9.0``) and the
    Hermes-managed tree that ``scripts/lib/node-bootstrap.sh`` installs into.
    """
    candidates: list[Path] = []
    nvm_dir = Path(os.environ.get("NVM_DIR") or Path.home() / ".nvm")
    versions_dir = nvm_dir / "versions" / "node"
    for name in (version, f"v{version}"):
        candidates.append(versions_dir / name / "bin")
    if versions_dir.is_dir():
        major = version.split(".")[0]
        for child in sorted(versions_dir.iterdir(), reverse=True):
            if child.name.lstrip("v").split(".")[0] == major:
                candidates.append(child / "bin")
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    candidates.append(hermes_home / "node" / "bin")
    for candidate in candidates:
        node = candidate / "node"
        if node.is_file() and os.access(node, os.X_OK):
            return candidate
    return None


def _activate_declared_node_toolchain() -> None:
    """Prepend the declared Node's ``bin/`` to PATH (idempotent; no-op if absent).

    Done at import time, before any test module binds a ``shutil.which`` result
    at module scope. If the declared Node is not installed the PATH is left
    untouched -- the host genuinely cannot run the production contract, and a
    loud failure is more honest than a silent skip.
    """
    version = _declared_node_version()
    if version is None:
        return
    bin_dir = _declared_node_bin_dir(version)
    if bin_dir is None:
        return
    resolved = str(bin_dir)
    entries = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and entry != resolved
    ]
    os.environ["PATH"] = os.pathsep.join([resolved, *entries])


_activate_declared_node_toolchain()


@pytest.fixture(autouse=True)
def _wb_restore_global_state():
    """Snapshot/restore process-global Hermes state around every WB test."""
    # Snapshot the context-local override so a leaked one cannot survive.
    try:
        from hermes_constants import get_hermes_home_override
        had_override = get_hermes_home_override()
    except Exception:
        had_override = None

    yield

    # Reset a leaked context-local HERMES_HOME override (a test that set it
    # without a matching reset would otherwise redirect later profile/config
    # resolution to its now-deleted tmpdir).
    try:
        from hermes_constants import get_hermes_home_override, set_hermes_home_override
        if get_hermes_home_override() != had_override:
            set_hermes_home_override(had_override)
    except Exception:
        pass

    # Ensure the repo root stays on sys.path for later Hermes imports.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
