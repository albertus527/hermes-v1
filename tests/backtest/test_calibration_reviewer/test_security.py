"""Pairing lifecycle: strictly one-time code, rotation, session preservation.

Regressions for the review finding that ``pair()`` set ``_pairing_used`` but
never rejected subsequent pairing attempts (code remained indefinitely
reusable while flagged one-time).
"""

from __future__ import annotations

import pytest

from backtest.calibration_reviewer.schema import open_reviewer_db
from backtest.calibration_reviewer.security import (
    SecurityError,
    SecurityManager,
)


@pytest.fixture
def sec(tmp_path):
    conn = open_reviewer_db(tmp_path / "reviewer.sqlite3")
    return SecurityManager(conn), conn


def test_first_pairing_succeeds(sec):
    mgr, _ = sec
    token = mgr.pair(mgr.pairing_code)
    assert token


def test_second_use_of_same_code_fails(sec):
    mgr, _ = sec
    mgr.pair(mgr.pairing_code)
    with pytest.raises(SecurityError) as exc:
        mgr.pair(mgr.pairing_code)
    assert exc.value.status == 409


def test_rotated_code_permits_new_pairing(sec):
    mgr, _ = sec
    first_token = mgr.pair(mgr.pairing_code)
    with pytest.raises(SecurityError):
        mgr.pair(mgr.pairing_code)
    new_code = mgr.rotate_pairing_code()
    assert new_code
    second_token = mgr.pair(new_code)
    assert second_token != first_token


def test_wrong_code_still_401_and_does_not_burn(sec):
    mgr, _ = sec
    with pytest.raises(SecurityError) as exc:
        mgr.pair("not-the-code")
    assert exc.value.status == 401
    # A failed attempt must not consume the one-time code.
    assert mgr.pair(mgr.pairing_code)


def test_existing_session_survives_reuse_attempt_and_rotation(sec):
    mgr, conn = sec
    token = mgr.pair(mgr.pairing_code)
    with pytest.raises(SecurityError):
        mgr.pair(mgr.pairing_code)
    mgr.rotate_pairing_code()
    # The originally issued session must remain valid (revocation is explicit).
    session = mgr.validate_session(token)
    assert session["revoked"] == 0


def test_rate_limit_still_applies_before_burn(sec):
    from backtest.calibration_reviewer import security as sec_mod
    mgr, _ = sec
    code = mgr.pairing_code
    for _ in range(sec_mod.PAIRING_MAX_ATTEMPTS):
        with pytest.raises(SecurityError) as exc:
            mgr.pair("wrong")
        assert exc.value.status == 401
    with pytest.raises(SecurityError) as exc:
        mgr.pair(code)
    assert exc.value.status == 429
