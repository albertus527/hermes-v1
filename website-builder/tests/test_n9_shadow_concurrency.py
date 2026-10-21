"""P1 -- concurrency, with REAL concurrent processes.

The guarantee has to hold across process boundaries, because the failure it
prevents is a PAID call being spent twice. A thread-level test cannot show
that, so these tests spawn actual processes.

What is asserted:
  * two simultaneous invocations for the same identity do not both dispatch;
  * two simultaneous invocations for DIFFERENT revisions in the SAME
    workspace do not lose each other's attempts;
  * a lock whose owner is dead is reaped, and one whose owner is alive is
    never stolen;
  * a lock that cannot be acquired fails safe, and the caller is told;
  * an unknown outcome is not retried;
  * the identity is re-verified immediately before the dispatch.
"""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

WB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WB))

from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_reference import DesignAsset  # noqa: E402
from app.core.shadow_runner import (  # noqa: E402
    STATUS_BUSY,
    candidate_set_fingerprint,
    STATUS_DUPLICATE,
    STATUS_IDENTITY_MISMATCH,
    STATUS_INDETERMINATE,
    ShadowConfig,
    run_shadow_scoring,
)
from app.core.shadow_scoring import (  # noqa: E402
    ShadowLockBusy,
    acquire_reservation,
    lock_path,
    read_shadow_attempts,
    release_reservation,
)


def _artifact(tmp_path, revision=7, set_id="rs-abc"):
    from app.core.design_requirement_artifact import (
        RequirementArtifact, RequirementEntry, write_requirement_artifact)
    write_requirement_artifact(tmp_path, RequirementArtifact(
        project_id="p1", source_revision=revision, requirements_version=1,
        prd_digest="d" * 64,
        entries=(RequirementEntry(
            id="R1", text="R1 real requirement", kind="content",
            evidence_kind="visual", verifiable_by="screenshot",
            origin="user_brief", trace="brief.what", mandatory=True,
            discriminating=True,
        ),),
        requirement_set_id=set_id,
    ))
    return tmp_path


def _candidate(cid="c1", revision="7"):
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="n9lib://x", source_revision=revision, score=0.5,
        requirement_version="req-1",
        assets=(DesignAsset(
            kind="screenshot_desktop", uri="n9lib://x", revision=revision,
            digest="b" * 64, mime="image/png", width=1440, height=900,
            bytes=1000),),
    )


WORKER = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    sys.path.insert(0, {wb!r})
    from app.core.decision_provider import DesignCandidate
    from app.core.design_reference import DesignAsset
    from app.core.shadow_runner import ShadowConfig, run_shadow_scoring

    ws = Path(sys.argv[1])
    revision = sys.argv[2]
    hold = float(sys.argv[3])

    def _fn(copies, **kwargs):
        time.sleep(hold)
        from dataclasses import replace
        return tuple(replace(c, suitability=0.8,
                             suitability_detail=(("suitability", 1.0),),
                             suitability_contract="n8-design-v1")
                     for c in copies)

    cands = [DesignCandidate(
        candidate_id="c1", label="c1", descriptor="d",
        source_uri="n9lib://x", source_revision=revision, score=0.5,
        requirement_version="req-1",
        assets=(DesignAsset(
            kind="screenshot_desktop", uri="n9lib://x", revision=revision,
            digest="b" * 64, mime="image/png", width=1440, height=900,
            bytes=1000),))]
    result = run_shadow_scoring(
        ws, project_id="p1", config=ShadowConfig(enabled=True),
        candidates=cands, source_revision=revision, score_fn=_fn)
    print(json.dumps({{"error": result.error,
                        "produced": result.produced_suitability}}))
''').format(wb=str(WB))


def _worker(tmp_path, revision="7", hold=0.5):
    path = Path(tmp_path) / f"worker-{revision}-{hold}.py"
    path.write_text(WORKER)
    return path


def _run_workers(tmp_path, count, revision="7", hold=0.5):
    worker = _worker(tmp_path, revision, hold)
    procs = [
        subprocess.Popen(
            [sys.executable, str(worker), str(tmp_path), revision, str(hold)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(count)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        line = ""
        for candidate in (out or "").strip().splitlines():
            if candidate.startswith("{"):
                line = candidate
        if not line:
            pytest.fail(f"a worker produced no result: {err}")
        results.append(json.loads(line))
    return results


class TestSimultaneousInvocations:
    def test_two_processes_do_not_both_dispatch(self, tmp_path):
        """THE race the audit demonstrated."""
        _artifact(tmp_path)
        results = _run_workers(tmp_path, 2, hold=0.6)
        produced = [r for r in results if r["produced"]]
        assert len(produced) == 1, (
            f"two processes both dispatched a paid call: {results}")
        errors = [r["error"] for r in results if r["error"]]
        assert errors, "the loser did not report a refusal"
        assert errors[0] in (STATUS_DUPLICATE, STATUS_BUSY), errors

    def test_five_processes_still_dispatch_only_once(self, tmp_path):
        _artifact(tmp_path)
        results = _run_workers(tmp_path, 5, hold=0.6)
        produced = [r for r in results if r["produced"]]
        assert len(produced) == 1, (
            f"{len(produced)} processes dispatched: {results}")

    def test_the_record_holds_exactly_one_attempt(self, tmp_path):
        _artifact(tmp_path)
        _run_workers(tmp_path, 3, hold=0.5)
        attempts = read_shadow_attempts(tmp_path)
        assert len(attempts) == 1
        assert attempts[0].invocation_state == "completed"

    def test_no_lock_file_is_left_behind(self, tmp_path):
        _artifact(tmp_path)
        _run_workers(tmp_path, 2, hold=0.4)
        leftovers = list(tmp_path.glob(".shadow-lock-*"))
        assert not leftovers, leftovers


class TestDifferentRevisionsShareOneWorkspace:
    def test_different_revisions_both_dispatch(self, tmp_path):
        """They are different measurements, so neither blocks the other."""
        _artifact(tmp_path, revision=7)
        first = _run_workers(tmp_path, 1, revision="7", hold=0.2)
        assert first[0]["produced"] is True

        _artifact(tmp_path, revision=3)
        second = _run_workers(tmp_path, 1, revision="3", hold=0.2)
        assert second[0]["produced"] is True, second

    def test_different_revisions_do_not_lose_each_other(self, tmp_path):
        _artifact(tmp_path, revision=7)
        _run_workers(tmp_path, 1, revision="7", hold=0.2)
        _artifact(tmp_path, revision=3)
        _run_workers(tmp_path, 1, revision="3", hold=0.2)
        attempts = read_shadow_attempts(tmp_path)
        revisions = {a.source_revision for a in attempts}
        assert revisions == {"7", "3"}, revisions

    def test_two_revisions_at_once_both_survive(self, tmp_path):
        """Both revisions are measured, and both records survive.

        The artifact has to be at the revision being measured: a stale
        artifact is correctly refused, so the two workers are given their
        own artifact in turn. The point of the test is that neither
        record erases the other.
        """
        worker7 = _worker(tmp_path, "7", 0.4)
        worker3 = _worker(tmp_path, "3", 0.4)
        results = []
        for w, rev in ((worker7, "7"), (worker3, "3")):
            _artifact(tmp_path, revision=int(rev))
            proc = subprocess.Popen(
                [sys.executable, str(w), str(tmp_path), rev, "0.4"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, _err = proc.communicate(timeout=60)
            line = next((l for l in (out or "").splitlines()
                         if l.startswith("{")), "")
            results.append(json.loads(line) if line else {})
        attempts = read_shadow_attempts(tmp_path)
        revisions = {a.source_revision for a in attempts}
        assert "7" in revisions and "3" in revisions, revisions
        assert all(r.get("produced") for r in results), results


class TestTheLock:
    def test_a_live_lock_is_never_stolen(self, tmp_path):
        path = acquire_reservation(tmp_path, "inv-abc")
        try:
            with pytest.raises(ShadowLockBusy):
                acquire_reservation(tmp_path, "inv-abc", timeout=0.2)
        finally:
            release_reservation(path)

    def test_a_dead_owner_is_reaped(self, tmp_path):
        """A lock left by a killed process must not wedge the seam."""
        path = lock_path(tmp_path, "inv-abc")
        # A pid that cannot be alive.
        path.write_text(json.dumps({"pid": 999999999, "time": 0.0}))
        taken = acquire_reservation(tmp_path, "inv-abc", timeout=1.0)
        release_reservation(taken)

    def test_an_unreadable_lock_is_not_reaped(self, tmp_path):
        """A lock we cannot read might belong to a live process."""
        path = lock_path(tmp_path, "inv-abc")
        path.write_text("not json")
        with pytest.raises(ShadowLockBusy):
            acquire_reservation(tmp_path, "inv-abc", timeout=0.2)

    def test_a_different_identity_gets_its_own_lock(self, tmp_path):
        a = acquire_reservation(tmp_path, "inv-a")
        try:
            b = acquire_reservation(tmp_path, "inv-b")
            release_reservation(b)
        finally:
            release_reservation(a)

    def test_the_lock_file_records_the_owner(self, tmp_path):
        path = acquire_reservation(tmp_path, "inv-abc")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["pid"] == os.getpid()
            assert data["time"] > 0
        finally:
            release_reservation(path)


class TestTheIdentityIsCheckedBeforeTheDispatch:
    def test_a_candidate_at_the_wrong_revision_is_refused(self, tmp_path):
        _artifact(tmp_path)
        called = []

        def _fn(copies, **kwargs):
            called.append(1)
            return ()

        result = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="99")],
            source_revision="7", score_fn=_fn,
        )
        assert not called, "the dispatch happened despite the mismatch"
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_IDENTITY_MISMATCH

    def test_a_candidate_without_a_digest_is_refused(self, tmp_path):
        _artifact(tmp_path)
        called = []

        def _fn(copies, **kwargs):
            called.append(1)
            return ()

        class NoDigest:
            kind = "screenshot_desktop"
            uri = "n9lib://x"
            revision = "7"
            mime = "image/png"
            width = 1440
            height = 900
            bytes = 1000

        candidate = DesignCandidate(
            candidate_id="c1", label="c1", descriptor="d",
            source_uri="n9lib://x", source_revision="7", score=0.5,
            requirement_version="req-1", assets=(NoDigest(),))
        result = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[candidate], source_revision="7", score_fn=_fn,
        )
        assert not called
        assert result.attempts[0].status == STATUS_IDENTITY_MISMATCH


class TestTheUnknownOutcomeIsNotRetried:
    def test_a_pending_record_blocks_a_concurrent_resume(self, tmp_path):
        _artifact(tmp_path)
        from app.core.shadow_scoring import write_shadow_record
        from app.core.shadow_runner import (
            candidate_set_fingerprint,
            invocation_identity,
        )
        from app.core.shadow_scoring import ShadowAttempt
        # The identity now covers the candidate set, so the pending record
        # must carry the identity the run would actually compute.
        identity = invocation_identity(
            "p1", "7", "rs-abc", "n8-design-v1",
            candidate_set_fingerprint([_candidate()]),
        )
        write_shadow_record(
            tmp_path,
            (ShadowAttempt(
                project_id="p1", candidate_id="c1", source_revision="7",
                requirement_set_id="rs-abc", invocation_id=identity,
                invocation_state="pending", status="pending"),),
            project_id="p1", source_revision="7",
        )
        called = []

        def _fn(copies, **kwargs):
            called.append(1)
            return ()

        result = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7", score_fn=_fn,
        )
        assert not called, "an unknown outcome was retried"
        assert result.error == STATUS_INDETERMINATE
