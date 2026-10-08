"""The D3a.5 mutation drivers must classify a KILL by a FAILED test.

A driver's whole job is to prove a guard is load-bearing: it mutates the source
and checks the focused tests go RED. But "red" has two very different causes:

  * a real assertion FAILURE  -> the guard is load-bearing  (KILLED)
  * a COLLECTION ERROR / no tests ran -> the tests never produced an outcome, so
                                         the mutation proved NOTHING  (INVALID)

pytest prints "1 failed, 38 passed in Xs" for a failure but "1 error in Xs"
(SINGULAR) or "N errors in Xs" (plural) for collection errors. The earlier check
matched only the PLURAL " errors in ", so a mutation that broke collection was
misreported as KILLED -- a false kill, which would weaken every "N/N guards
killed" claim. The reliable signal is the summary token "failed".

Scope: the D3a.5 drivers. The older D0/D1/D2 drivers are separate work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
D3A5_DRIVERS = (
    "mutation_check_d3a5_parta.py",
    "mutation_check_d3a5_partbc.py",
    "mutation_check_d3a5_partc.py",
    "mutation_check_d3a5_partd.py",
    "mutation_check_d3a5_parti.py",
)


@pytest.mark.parametrize("driver", D3A5_DRIVERS)
def test_every_d3a5_driver_classifies_a_kill_by_a_failed_test(driver):
    """A KILL must be keyed on pytest's "failed" summary token."""
    path = ROOT / "tools" / driver
    if not path.exists():
        pytest.skip(f"{driver} not present")
    source = path.read_text(encoding="utf-8")

    assert 'elif "failed" in tail:' in source, (
        f"{driver} does not classify a kill on the 'failed' summary token, so a "
        "collection error (no test outcome) could be reported as KILLED"
    )


@pytest.mark.parametrize("driver", D3A5_DRIVERS)
def test_every_d3a5_driver_has_an_invalid_arm(driver):
    """There must be an INVALID path, not a bare `else: KILLED`."""
    path = ROOT / "tools" / driver
    if not path.exists():
        pytest.skip(f"{driver} not present")
    source = path.read_text(encoding="utf-8")

    assert "[INVALID]" in source, driver


def test_the_summary_forms_are_distinct():
    """Sanity: a failure and a collection error really are different strings."""
    failure = "1 failed, 38 passed in 0.74s"
    singular_error = "1 error in 0.10s"
    plural_error = "2 errors in 0.10s"

    assert "failed" in failure
    assert "failed" not in singular_error
    assert "failed" not in plural_error
    # The plural form does NOT contain the singular ('error' vs 'errors').
    assert " error in " not in plural_error
