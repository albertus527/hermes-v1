"""Store state machine: drafts, locking, revisions, concurrency, audit."""

from __future__ import annotations

import pytest

from backtest.calibration_reviewer.schema import (
    PRODUCTION_DB,
    assert_isolated_path,
    open_reviewer_db,
)
from backtest.calibration_reviewer.store import (
    LockedLabelError,
    NotLockedError,
    StaleWriteError,
    Store,
    StoreError,
)

from .conftest import make_dataset
from backtest.calibration_reviewer.datasets import load_dataset

LABEL = {"category": "EARNINGS", "direction": "BULLISH", "severity": "MEDIUM",
         "ma_role": "NEITHER"}
SID = "A_CANONICAL_240-0000"


def _store(tmp_path):
    spec = make_dataset(tmp_path, n=3)
    conn = open_reviewer_db(tmp_path / "reviewer.sqlite3")
    store = Store(conn)
    store.initialize_from_datasets({"A_CANONICAL_240": load_dataset(spec)})
    return store, conn


def test_initial_status_unreviewed(tmp_path):
    store, _ = _store(tmp_path)
    rec = store.get_sample("A_CANONICAL_240", SID)
    assert (rec.get("status") or "UNREVIEWED") == "UNREVIEWED"
    assert rec["version"] == 0


def test_save_draft_partial(tmp_path):
    store, _ = _store(tmp_path)
    v = store.save_draft("A_CANONICAL_240", SID,
                         label={"category": "EARNINGS"}, certainty="UNSURE",
                         notes="wip", expected_version=0)
    assert v == 1
    rec = store.get_sample("A_CANONICAL_240", SID)
    assert rec["status"] == "DRAFT"
    assert rec["category"] == "EARNINGS"
    assert rec["direction"] is None


def test_draft_does_not_count_as_locked(tmp_path):
    store, _ = _store(tmp_path)
    store.save_draft("A_CANONICAL_240", SID, label={"category": "EARNINGS"},
                     expected_version=0)
    prog = store.progress("A_CANONICAL_240")
    assert prog["locked"] == 0
    assert prog["draft"] == 1
    assert prog["canonical_locked"] == 0


def test_stale_write_rejected(tmp_path):
    store, _ = _store(tmp_path)
    store.save_draft("A_CANONICAL_240", SID, label={"category": "EARNINGS"},
                     expected_version=0)  # version -> 1
    with pytest.raises(StaleWriteError):
        store.save_draft("A_CANONICAL_240", SID, label={"category": "GUIDANCE"},
                         expected_version=0)  # stale


def test_lock_requires_complete_label(tmp_path):
    store, _ = _store(tmp_path)
    with pytest.raises(Exception):
        store.lock("A_CANONICAL_240", SID,
                   label={"category": "EARNINGS", "direction": "BULLISH",
                          "severity": "MEDIUM"},  # missing ma_role
                   expected_version=0, rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)


def test_lock_then_draft_refused(tmp_path):
    store, _ = _store(tmp_path)
    v = store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    with pytest.raises(LockedLabelError):
        store.save_draft("A_CANONICAL_240", SID, label={"category": "GUIDANCE"},
                         expected_version=v)


def test_duplicate_lock_refused(tmp_path):
    store, _ = _store(tmp_path)
    v = store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    with pytest.raises(LockedLabelError):
        store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=v,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)


def test_lock_counts_only_when_rubric_approved(tmp_path):
    store, _ = _store(tmp_path)
    store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
               rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    prog = store.progress("A_CANONICAL_240")
    assert prog["locked"] == 1
    assert prog["canonical_locked"] == 0  # not approved -> not canonical

    store.approve_rubric("r", rubric_sha256="0" * 64)
    assert store.is_rubric_approved("r")
    # Already-locked labels are not retroactively canonical; only future locks
    # under an approved rubric carry rubric_approved=1.
    prog2 = store.progress("A_CANONICAL_240")
    assert prog2["canonical_locked"] == 0


def test_lock_under_approved_rubric_is_canonical(tmp_path):
    store, _ = _store(tmp_path)
    store.approve_rubric("r", rubric_sha256="0" * 64)
    store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
               rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=True)
    assert store.progress("A_CANONICAL_240")["canonical_locked"] == 1


def test_reveal_requires_lock(tmp_path):
    store, _ = _store(tmp_path)
    with pytest.raises(NotLockedError):
        store.reveal("A_CANONICAL_240", SID, kind="consensus",
                     expected_version=0)


def test_reveal_after_lock(tmp_path):
    store, _ = _store(tmp_path)
    v = store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    v2 = store.reveal("A_CANONICAL_240", SID, kind="consensus",
                      expected_version=v)
    rec = store.get_sample("A_CANONICAL_240", SID)
    assert rec["revealed_consensus"] == 1
    assert rec["revealed_model"] == 0


def test_revise_requires_reason(tmp_path):
    store, _ = _store(tmp_path)
    v = store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    with pytest.raises(StoreError):
        store.revise("A_CANONICAL_240", SID,
                     label={**LABEL, "severity": "HIGH"}, reason="",
                     expected_version=v, rubric_version="r", rubric_sha256="0" * 64,
                     rubric_approved=False)


def test_revise_after_reveal_tagged(tmp_path):
    store, _ = _store(tmp_path)
    v = store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    v = store.reveal("A_CANONICAL_240", SID, kind="consensus",
                     expected_version=v)
    store.revise("A_CANONICAL_240", SID, label={**LABEL, "severity": "HIGH"},
                 reason="reconsidered after seeing consensus", expected_version=v,
                 rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    ledger = store.audit_ledger("A_CANONICAL_240")
    revise_events = [e for e in ledger if e["event"] == "REVISE"]
    assert revise_events and revise_events[-1]["post_reveal"] == 1


def test_revision_history_append_only(tmp_path):
    store, _ = _store(tmp_path)
    v = store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    v = store.revise("A_CANONICAL_240", SID, label={**LABEL, "severity": "HIGH"},
                     reason="first", expected_version=v, rubric_version="r", rubric_sha256="0" * 64,
                     rubric_approved=False)
    store.revise("A_CANONICAL_240", SID, label={**LABEL, "severity": "CRITICAL"},
                 reason="second", expected_version=v, rubric_version="r", rubric_sha256="0" * 64,
                 rubric_approved=False)
    ledger = store.audit_ledger("A_CANONICAL_240")
    events = [e["event"] for e in ledger]
    assert events.count("LOCK") == 1
    assert events.count("REVISE") == 2
    # No DELETE/UPDATE audit path exists; ledger only grows.
    revises = [e for e in ledger if e["event"] == "REVISE"]
    assert revises[0]["new_label"] != revises[1]["new_label"]


def test_revise_does_not_bypass_validation(tmp_path):
    store, _ = _store(tmp_path)
    v = store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
                   rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    with pytest.raises(Exception):
        store.revise("A_CANONICAL_240", SID,
                     label={"category": "BOGUS", "direction": "BULLISH",
                            "severity": "HIGH", "ma_role": "NEITHER"},
                     reason="bad", expected_version=v, rubric_version="r", rubric_sha256="0" * 64,
                     rubric_approved=False)


def test_flag_toggle(tmp_path):
    store, _ = _store(tmp_path)
    v = store.set_flag("A_CANONICAL_240", SID, flagged=True, expected_version=0)
    assert store.get_sample("A_CANONICAL_240", SID)["flagged"] == 1
    store.set_flag("A_CANONICAL_240", SID, flagged=False, expected_version=v)
    assert store.get_sample("A_CANONICAL_240", SID)["flagged"] == 0


def test_resume_skips_locked(tmp_path):
    store, _ = _store(tmp_path)
    first = store.ordered_sample_ids("A_CANONICAL_240")[0]
    store.lock("A_CANONICAL_240", first, label=LABEL, expected_version=0,
               rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    assert store.resume_sample_id("A_CANONICAL_240") != first


def test_isolation_guard_rejects_production_db():
    with pytest.raises(RuntimeError):
        assert_isolated_path(PRODUCTION_DB)


def test_isolation_guard_rejects_production_root():
    with pytest.raises(RuntimeError):
        assert_isolated_path(PRODUCTION_DB.parent / "reviewer.sqlite3")


def test_interrupted_session_recovery(tmp_path):
    """Reopening the DB preserves locked state (no in-memory dependence)."""
    spec = make_dataset(tmp_path, n=3)
    db_path = tmp_path / "reviewer.sqlite3"
    conn = open_reviewer_db(db_path)
    store = Store(conn)
    store.initialize_from_datasets({"A_CANONICAL_240": load_dataset(spec)})
    store.lock("A_CANONICAL_240", SID, label=LABEL, expected_version=0,
               rubric_version="r", rubric_sha256="0" * 64,
                   rubric_approved=False)
    conn.close()

    conn2 = open_reviewer_db(db_path)
    store2 = Store(conn2)
    store2.initialize_from_datasets({"A_CANONICAL_240": load_dataset(spec)})
    rec = store2.get_sample("A_CANONICAL_240", SID)
    assert rec["status"] == "LOCKED"
    assert store2.progress("A_CANONICAL_240")["locked"] == 1
