"""Regression: scripts/run_tests.sh must forward AGENT_BROWSER_ARGS through env -i.

The canonical runner intentionally executes the suite under ``env -i`` and
only forwards an explicit non-secret allowlist. ``AGENT_BROWSER_ARGS`` is a
browser-runtime/test-infrastructure knob (e.g. ``--no-sandbox`` on
sandbox-less VPS hosts) consumed by ``website-builder/app/qa/screenshot.py``.
Without forwarding, the real-browser QA tests lose the flag and Chromium
refuses to launch — while the same tests pass under direct ``pytest``.

Two layers:

* A *behavioral* layer: source the script's real allowlist loop (extracted
  verbatim from the file between its section markers) in a bash subprocess
  with a marker env var set, then assert what ``env -i ... printenv``
  forwards. This executes the actual loop logic rather than parsing source
  shape. Skipped when bash is unavailable (native Windows without Git Bash).

* A lightweight invariant layer: the loop remains an explicit allowlist (no
  ``HERMES_TEST_*`` glob), so the "no credential can leak" property stays
  auditable at a glance.

Neither layer hardcodes any host-specific browser flag value — the flag
content belongs to the caller's environment, not the repo.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

RUN_TESTS_SH = Path(__file__).resolve().parents[2] / "scripts" / "run_tests.sh"

# Explicitly forwarded non-secret knobs in the TEST_ENV allowlist loop.
# If you add another knob to the script's loop, extend this expectation.
EXPECTED_ALLOWLIST = {
    "HERMES_TEST_IMAGE",
    "HERMES_TEST_WORKERS",
    "HERMES_TEST_PATHS",
    "HERMES_TEST_FILE_TIMEOUT",
    "HERMES_TEST_FILE_RETRIES",
    "HERMES_TEST_SLICE",
    "AGENT_BROWSER_ARGS",
}

# The allowlist loop block: from the TEST_ENV=() init through the loop's
# closing `done`. Extracted verbatim so the behavioral test executes the
# script's real logic instead of a hand-maintained copy.
_LOOP_RE = re.compile(r"(TEST_ENV=\(\)\nfor _test_var in .*?done)", re.DOTALL)


def _extract_allowlist_loop(script: str) -> str:
    match = _LOOP_RE.search(script)
    assert match, "could not find the TEST_ENV allowlist loop in run_tests.sh"
    return match.group(1)


def _extract_allowlist_vars(script: str) -> set[str]:
    loop = _extract_allowlist_loop(script)
    match = re.search(r"for _test_var in (?P<vars>[^;]+); do", loop)
    assert match
    return set(match.group("vars").replace("\\", " ").split())


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_allowlist_loop_forwards_agent_browser_args_through_env_i():
    """Execute the script's real allowlist loop, then cross the env -i boundary.

    A marker value is set in the outer environment; the loop (sourced from
    the real script) must capture it into TEST_ENV, and the forwarded
    expansion must carry it across ``env -i`` to the child process.
    """
    script = RUN_TESTS_SH.read_text(encoding="utf-8")
    loop = _extract_allowlist_loop(script)

    # The harness mirrors the script's exec boundary: same TEST_ENV capture,
    # same `${TEST_ENV[@]+"${TEST_ENV[@]}"}` guarded expansion, same env -i.
    harness = (
        "set -euo pipefail\n"
        + loop
        + "\n"
        'exec env -i PATH="$PATH" '
        '${TEST_ENV[@]+"${TEST_ENV[@]}"} '
        "printenv AGENT_BROWSER_ARGS"
    )
    proc = subprocess.run(
        ["bash", "-c", harness],
        env={"PATH": "/usr/bin:/bin", "AGENT_BROWSER_ARGS": "--marker-flag"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "--marker-flag"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_allowlist_loop_drops_unset_knob():
    """When the caller never exports the knob, nothing propagates.

    The guarded `${VAR:+...}` expansion must not invent an empty
    AGENT_BROWSER_ARGS in the child environment.
    """
    script = RUN_TESTS_SH.read_text(encoding="utf-8")
    loop = _extract_allowlist_loop(script)

    harness = (
        "set -euo pipefail\n"
        + loop
        + "\n"
        'exec env -i PATH="$PATH" '
        '${TEST_ENV[@]+"${TEST_ENV[@]}"} '
        "printenv AGENT_BROWSER_ARGS"
    )
    proc = subprocess.run(
        ["bash", "-c", harness],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    # `printenv NAME` exits 1 and prints nothing when the var is unset —
    # proving the guarded expansion does not invent an empty value.
    assert proc.stdout.strip() == ""
    assert proc.returncode == 1


def test_env_i_boundary_and_explicit_allowlist_invariants():
    """env -i stays, the knob is allowlisted, and no glob weakens the audit."""
    script = RUN_TESTS_SH.read_text(encoding="utf-8")

    # env -i must be preserved — the hermetic boundary is intentional.
    assert "exec env -i" in script

    allowlist = _extract_allowlist_vars(script)
    assert "AGENT_BROWSER_ARGS" in allowlist

    # Invariant: the loop remains an explicit allowlist (no glob), so the
    # "no credential can leak" property stays auditable at a glance.
    assert "HERMES_TEST_*" not in allowlist
    assert allowlist == EXPECTED_ALLOWLIST
