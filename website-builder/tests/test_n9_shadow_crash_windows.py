"""AUDIT B — the crash windows and the durable invocation identity.

The property under test: a crash must never cause a PAID request to be
silently re-dispatched, and an unknown outcome must be reported as unknown
rather than retried.

The crash windows:
    1. before the dispatch
    2. during the provider call
    3. after the provider response but before the record write
    4. during the atomic record replacement
"""
import json

import pytest

from app.core.decision_provider import DesignCandidate
from app.core.shadow_runner import (
    STATUS_DUPLICATE,
    STATUS_INDETERMINATE,
    STATUS_OK,
    ShadowConfig,
    invocation_identity,
    run_shadow_scoring,
)
from app.core.shadow_scoring import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_INDETERMINATE,
    STATE_PENDING,
    ShadowScoringError,
    read_shadow_attempts,
    read_shadow_record,
    shadow_path,
    write_shadow_record,
)


def _artifact(tmp_path):
    from app.core.design_requirement_artifact import (
        RequirementArtifact, RequirementEntry, write_requirement_artifact)
    write_requirement_artifact(tmp_path, RequirementArtifact(
        project_id="p1", source_revision=7, requirements_version=1,
        prd_digest="d" * 64,
        entries=(RequirementEntry(
            id="R1", text="R1 real requirement", kind="content",
            evidence_kind="visual", verifiable_by="screenshot",
            origin="user_brief", trace="brief.what", mandatory=True,
            discriminating=True,
        ),),
        requirement_set_id="rs-abc",
    ))
    return tmp_path


def _candidate(cid="c1"):
    from app.core.design_reference import DesignAsset
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="n9lib://x", source_revision="7", score=0.5,
        requirement_version="req-1",
        assets=(DesignAsset(
            kind="screenshot_desktop", uri="n9lib://x", revision="7",
            digest="b" * 64, mime="image/png", width=1440, height=900,
            bytes=1000),),
    )


def _scorer(suitability=0.8):
    def _fn(copies, **kwargs):
        from dataclasses import replace
        return tuple(
            replace(c, suitability=suitability,
                    suitability_detail=(("suitability", 1.0),),
                    suitability_contract="n8-design-v1")
            for c in copies
        )
    return _fn


class TestTheInvocationIdentity:
    def test_the_same_measurement_has_the_same_identity(self):
        a = invocation_identity("p1", "7", "rs-abc")
        b = invocation_identity("p1", "7", "rs-abc")
        assert a == b

    def test_a_different_revision_has_a_different_identity(self):
        assert invocation_identity("p1", "7", "rs") != invocation_identity(
            "p1", "8", "rs")

    def test_a_different_requirement_set_has_a_different_identity(self):
        assert invocation_identity("p1", "7", "rs-a") != invocation_identity(
            "p1", "7", "rs-b")

    def test_a_different_contract_has_a_different_identity(self):
        assert invocation_identity("p1", "7", "rs", "n8-design-v1") != (
            invocation_identity("p1", "7", "rs", "n8-design-v2"))

    def test_a_different_project_has_a_different_identity(self):
        assert invocation_identity("p1", "7", "rs") != invocation_identity(
            "p2", "7", "rs")

    def test_the_identity_is_recorded(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        record = read_shadow_record(tmp_path)
        attempt = record["attempts"][0]
        from app.core.shadow_runner import candidate_set_fingerprint
        fingerprint = candidate_set_fingerprint([_candidate()])
        assert attempt["invocation_id"] == invocation_identity(
            "p1", "7", "rs-abc", "n8-design-v1", fingerprint)
        assert attempt["invocation_state"] == STATE_COMPLETED


class TestTheTwoPhaseWrite:
    def test_a_pending_record_exists_before_the_dispatch(self, tmp_path):
        """The scorer can see the pending marker while it runs."""
        _artifact(tmp_path)
        seen = {}

        def _fn(copies, **kwargs):
            record = read_shadow_record(tmp_path)
            seen["state"] = record["attempts"][0]["invocation_state"]
            return _scorer()(copies, **kwargs)

        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7", score_fn=_fn,
        )
        assert seen["state"] == STATE_PENDING

    def test_the_pending_record_names_the_candidate_and_the_binding(self, tmp_path):
        _artifact(tmp_path)
        seen = {}

        def _fn(copies, **kwargs):
            seen["a"] = read_shadow_record(tmp_path)["attempts"][0]
            return ()

        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1")], source_revision="7", score_fn=_fn,
        )
        assert seen["a"]["candidate_id"] == "c1"
        assert seen["a"]["source_revision"] == "7"
        assert seen["a"]["requirement_set_id"] == "rs-abc"
        assert seen["a"]["scoring_contract_version"] == "n8-design-v1"

    def test_the_terminal_record_replaces_the_pending_one(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        attempts = read_shadow_attempts(tmp_path)
        assert len(attempts) == 1
        assert attempts[0].invocation_state == STATE_COMPLETED

    def test_a_post_dispatch_failure_is_recorded_as_indeterminate(self, tmp_path):
        """The call WAS sent. The request may already have been billed, so
        the outcome is unknown -- not merely 'failed'."""
        _artifact(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("down")

        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7", score_fn=_boom,
        )
        attempts = read_shadow_attempts(tmp_path)
        assert attempts[0].invocation_state == STATE_INDETERMINATE

    def test_a_pre_dispatch_refusal_is_recorded_as_failed(self, tmp_path):
        """Nothing was sent, so the outcome is known: it failed."""
        _artifact(tmp_path)
        result = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(), requirement_set_id="rs-OTHER",
        )
        attempts = read_shadow_attempts(tmp_path)
        assert attempts[0].invocation_state == STATE_FAILED


class TestCrashWindowTwoDuringTheCall:
    def test_a_crash_during_the_call_leaves_a_pending_record(self, tmp_path):
        """The process died inside the paid call. The outcome is UNKNOWN,
        and the record must say so -- a missing record would make a resume
        indistinguishable from a call that never happened."""
        _artifact(tmp_path)
        calls = []

        def _crash(copies, **kwargs):
            calls.append(1)
            raise KeyboardInterrupt("process died mid-call")

        with pytest.raises(KeyboardInterrupt):
            run_shadow_scoring(
                tmp_path, project_id="p1",
                config=ShadowConfig(enabled=True),
                candidates=[_candidate()], source_revision="7",
                score_fn=_crash,
            )
        attempts = read_shadow_attempts(tmp_path)
        assert len(attempts) == 1
        assert attempts[0].invocation_state == STATE_PENDING

    def test_a_resume_after_a_crash_does_not_re_dispatch(self, tmp_path):
        """This is the property that matters: no second paid call."""
        _artifact(tmp_path)
        calls = []

        def _crash(copies, **kwargs):
            calls.append(1)
            raise KeyboardInterrupt("process died mid-call")

        with pytest.raises(KeyboardInterrupt):
            run_shadow_scoring(
                tmp_path, project_id="p1",
                config=ShadowConfig(enabled=True),
                candidates=[_candidate()], source_revision="7",
                score_fn=_crash,
            )
        assert len(calls) == 1

        # The ordinary resume. It must NOT re-dispatch.
        def _would_call(copies, **kwargs):
            calls.append(1)
            return _scorer()(copies, **kwargs)

        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_would_call,
        )
        assert len(calls) == 1, "a paid request was re-dispatched"
        assert result.error == STATUS_INDETERMINATE
        assert result.produced_suitability is False

    def test_the_indeterminate_state_is_reported_distinctly(self, tmp_path):
        _artifact(tmp_path)

        def _crash(copies, **kwargs):
            raise KeyboardInterrupt("died")

        with pytest.raises(KeyboardInterrupt):
            run_shadow_scoring(
                tmp_path, project_id="p1",
                config=ShadowConfig(enabled=True),
                candidates=[_candidate()], source_revision="7",
                score_fn=_crash,
            )
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        assert result.error == STATUS_INDETERMINATE
        assert "indeterminate" in result.error


class TestCrashWindowThreeAfterTheResponse:
    def test_a_crash_before_the_terminal_write_leaves_pending(self, tmp_path):
        _artifact(tmp_path)
        original = write_shadow_record
        calls = {"n": 0}

        def _flaky(workspace, attempts, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return original(workspace, attempts, **kwargs)
            raise OSError("disk gone")

        import app.core.shadow_runner as runner
        runner.write_shadow_record = _flaky
        try:
            def _fn(copies, **kwargs):
                return _scorer()(copies, **kwargs)

            result = run_shadow_scoring(
                tmp_path, project_id="p1",
                config=ShadowConfig(enabled=True),
                candidates=[_candidate()], source_revision="7",
                score_fn=_fn,
            )
        finally:
            runner.write_shadow_record = original
        assert result.stored is False
        # The pending marker survives, so the next run refuses to re-dispatch.
        attempts = read_shadow_attempts(tmp_path)
        assert attempts[0].invocation_state == STATE_PENDING


class TestCrashWindowFourDuringTheReplacement:
    def test_a_failed_replacement_leaves_no_half_written_record(self, tmp_path,
                                                               monkeypatch):
        _artifact(tmp_path)
        import app.core.shadow_scoring as shadow_scoring
        real_replace = shadow_scoring.os.replace

        def _boom(a, b):
            raise OSError("replace failed")

        monkeypatch.setattr(shadow_scoring.os, "replace", _boom)
        with pytest.raises(ShadowScoringError):
            write_shadow_record(
                tmp_path, (),
                project_id="p1", source_revision="7",
            )
        # The target was never created, and the temporary file was removed.
        assert not shadow_path(tmp_path).exists()
        leftovers = list(tmp_path.glob(".shadow-scoring-*"))
        assert not leftovers, leftovers

    def test_a_corrupt_record_reads_as_absent(self, tmp_path):
        shadow_path(tmp_path).write_text("{truncated", encoding="utf-8")
        assert read_shadow_record(tmp_path) is None
        assert read_shadow_attempts(tmp_path) == ()


class TestExactlyOnceIsNotClaimed:
    def test_a_completed_run_is_not_repeated(self, tmp_path):
        _artifact(tmp_path)
        calls = []

        def _fn(copies, **kwargs):
            calls.append(1)
            return _scorer()(copies, **kwargs)

        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7", score_fn=_fn)
        second = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7", score_fn=_fn)
        assert len(calls) == 1
        assert second.error == STATUS_DUPLICATE

    def test_a_post_dispatch_failure_may_NOT_be_retried_automatically(self,
                                                                      tmp_path):
        """The call was SENT, so it may already have been billed. An
        ordinary resume must not spend a second one."""
        _artifact(tmp_path)
        calls = []

        def _boom(copies, **kwargs):
            calls.append(1)
            raise RuntimeError("transient")

        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7", score_fn=_boom)
        assert len(calls) == 1

        result = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer())
        assert len(calls) == 1, "a paid request was re-dispatched"
        assert result.error == STATUS_INDETERMINATE

    def test_a_pre_dispatch_refusal_may_be_retried(self, tmp_path):
        """Nothing was sent, so nothing was billed. Retrying is safe."""
        _artifact(tmp_path)
        calls = []

        def _fn(copies, **kwargs):
            calls.append(1)
            return _scorer()(copies, **kwargs)

        # First: a stale revision, refused before the dispatch.
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="3", score_fn=_fn)
        assert len(calls) == 0

        # Then the correct revision, which is a DIFFERENT identity.
        result = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7", score_fn=_fn)
        assert len(calls) == 1
        assert result.produced_suitability is True
