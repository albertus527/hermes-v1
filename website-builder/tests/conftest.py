"""Website Builder test-suite hygiene fixtures.

The Website Builder tests live under a sub-directory of the Hermes repo but
exercise Hermes runtime seams (``hermes_cli.config.load_config``,
``hermes_constants.get_hermes_home``). Two pieces of process-global state can
leak between test modules and change the outcome of an unrelated test that
happens to run later in the same process:

1. ``sys.path`` — ``app.hermes.adapter`` resolves ``hermes_cli``/``run_agent``
   at import time. If the repo root is not importable when the module is
   (re-)imported, those names become ``None`` and every role-configuration
   resolution silently degrades to "profile configuration unavailable".
2. The context-local ``HERMES_HOME`` override installed by
   ``hermes_constants.set_hermes_home_override`` — a test that sets it without
   resetting redirects every later ``get_hermes_home()`` call.

Both are enforced here so the suite is order-independent (mirrors the
equivalent guards in the top-level ``tests/conftest.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --- 1. Guarantee the repo root is importable ---------------------------------
# parents[2] == repository root (website-builder/tests/conftest.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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
