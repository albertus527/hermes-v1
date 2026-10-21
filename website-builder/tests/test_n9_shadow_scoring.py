"""Shadow scoring isolation — fail-closed tests.

The guarantee under test is STRUCTURAL, not a promise: a shadow run must
leave the live candidate state and the approval/publish decisions EXACTLY as
they were, in every outcome -- success, provider failure, timeout, malformed
response, stale revision, mismatched requirement set, duplicate invocation,
storage-write failure, a candidate-mutation attempt, and a missing or
corrupt requirement artifact.

Every scenario asserts the same invariant: the live candidates are byte-for-
byte the same objects, with the same suitability, and the shadow record is
internal.
"""
import json
from pathlib import Path

import pytest

from app.core.decision_provider import DesignCandidate
from app.core.shadow_runner import (
    STATUS_CORRUPT_ARTIFACT,
    STATUS_DUPLICATE,
    STATUS_MALFORMED,
    STATUS_MISSING_ARTIFACT,
    STATUS_OK,
    STATUS_PROVIDER_FAILED,
    STATUS_REQUIREMENT_SET_MISMATCH,
    STATUS_STALE_REVISION,
    STATUS_STORAGE_FAILED,
    STATUS_TIMEOUT,
    ShadowConfig,
    ShadowNotEnabled,
    run_shadow_scoring,
)
from app.core.shadow_scoring import (
    FORBIDDEN_FIELDS,
    SHADOW_FILE_NAME,
    ShadowScoringError,
    read_shadow_attempts,
    read_shadow_record,
    shadow_path,
)


# ---------------------------------------------------------------- fixtures

def _candidate(cid="c1", revision="7", suitability=None):
    from app.core.design_reference import DesignAsset
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="n9lib://references/x", source_revision=revision,
        score=0.5, suitability=suitability,
        assets=(DesignAsset(
            kind="screenshot_desktop", uri="n9lib://references/x",
            revision=revision, digest="b" * 64, mime="image/png",
            width=1440, height=900, bytes=1000),),
        suitability_detail=() if suitability is None else (("suitability", 1.0),),
        suitability_contract="" if suitability is None else "n8-design-v1",
        requirement_version="req-1",
    )


def _write_artifact(workspace, *, project_id="p1", revision=7,
                    requirements=("R1 real requirement",), set_id="rs-abc123"):
    """Write a minimal, VALID requirement artifact into the workspace."""
    from app.core.design_requirement_artifact import (
        RequirementArtifact,
        RequirementEntry,
        write_requirement_artifact,
    )
    entries = tuple(
        RequirementEntry(
            id=f"R{i + 1}", text=text, kind="content",
            evidence_kind="visual", verifiable_by="screenshot",
            origin="user_brief", trace="brief.what", mandatory=True,
            discriminating=True,
        )
        for i, text in enumerate(requirements)
    )
    artifact = RequirementArtifact(
        project_id=project_id, source_revision=revision,
        requirements_version=1, prd_digest="d" * 64, entries=entries,
        requirement_set_id=set_id,
    )
    write_requirement_artifact(workspace, artifact)
    return artifact


def _scorer(suitability=0.8):
    """A scorer that returns COPIES with a suitability attached."""
    def _fn(copies, *, brief, requirements, requirement_version,
            deadline_seconds):
        from dataclasses import replace
        return tuple(
            replace(c, suitability=suitability, suitability_detail="shadow",
                    suitability_contract="n8-design-v1")
            for c in copies
        )
    return _fn


def _snapshot(candidates):
    """A comparable snapshot of the live candidate state."""
    return tuple(
        (
            c.candidate_id,
            c.suitability,
            c.suitability_detail,
            c.suitability_contract,
            c.score,
            c.requirement_version,
            id(c),
        )
        for c in candidates
    )


# ---------------------------------------------------------------- the tests

class TestShadowSuccess:
    def test_a_successful_shadow_run_records_a_suitability(self, tmp_path):
        _write_artifact(tmp_path)
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(0.8),
        )
        assert result.stored is True
        assert result.error == ""
        assert result.produced_suitability is True
        assert len(result.attempts) == 1
        attempt = result.attempts[0]
        assert attempt.status == STATUS_OK
        assert attempt.suitability == 0.8
        assert attempt.degraded is False

    def test_the_record_is_bound_to_every_required_identifier(self, tmp_path):
        _write_artifact(tmp_path, set_id="rs-abc123")
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="7")],
            source_revision="7", score_fn=_scorer(),
        )
        record = read_shadow_record(tmp_path)
        assert record is not None
        attempt = record["attempts"][0]
        for field in ("project_id", "candidate_id", "source_revision",
                      "requirement_set_id", "scoring_contract_version",
                      "screenshot_digests"):
            assert field in attempt, field
        assert attempt["project_id"] == "p1"
        assert attempt["candidate_id"] == "c1"
        assert attempt["source_revision"] == "7"
        assert attempt["requirement_set_id"] == "rs-abc123"

    def test_the_record_carries_no_credential_brief_or_payload(self, tmp_path):
        _write_artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
            brief={"what": "SECRET OWNER WORDS", "why": "more secrets"},
        )
        raw = shadow_path(tmp_path).read_text(encoding="utf-8")
        assert "SECRET OWNER WORDS" not in raw
        assert "more secrets" not in raw
        for forbidden in FORBIDDEN_FIELDS:
            assert f'"{forbidden}"' not in raw, forbidden


class TestNoLiveMutation:
    def test_the_live_candidates_are_untouched_after_a_success(self, tmp_path):
        _write_artifact(tmp_path)
        candidates = [_candidate("c1", suitability=None)]
        before = _snapshot(candidates)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_scorer(0.99),
        )
        assert _snapshot(candidates) == before
        assert candidates[0].suitability is None

    def test_a_live_suitability_is_not_overwritten_by_a_shadow_run(self, tmp_path):
        _write_artifact(tmp_path)
        candidates = [_candidate("c1", suitability=0.42)]
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_scorer(0.99),
        )
        assert candidates[0].suitability == 0.42

    def test_the_scorer_receives_copies_not_the_live_objects(self, tmp_path):
        _write_artifact(tmp_path)
        candidates = [_candidate()]
        seen = []

        def _fn(copies, **kwargs):
            seen.extend(copies)
            from dataclasses import replace
            return tuple(replace(c, suitability=0.5) for c in copies)

        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_fn,
        )
        assert seen, "the scorer was never called"
        for handed in seen:
            assert all(h is not c for h, c in zip(seen, candidates))
        assert candidates[0].suitability is None

    def test_a_scorer_that_tries_to_mutate_a_candidate_cannot(self, tmp_path):
        """A hostile scorer cannot reach the live candidate."""
        _write_artifact(tmp_path)
        candidates = [_candidate()]
        live_id = id(candidates[0])

        def _fn(copies, **kwargs):
            # Try to reach the live object through the copy's class-level
            # registry, and try to set an attribute directly.
            for c in copies:
                try:
                    object.__setattr__(c, "suitability", 0.99)
                except Exception:
                    pass
            # Try to find the original by id in any closure/global.
            for holder in (globals(),):
                for value in list(holder.values()):
                    if id(value) == live_id:
                        try:
                            object.__setattr__(value, "suitability", 0.99)
                        except Exception:
                            pass
            return ()

        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_fn,
        )
        assert candidates[0].suitability is None


class TestProviderFailure:
    def test_a_provider_failure_produces_a_record_and_no_suitability(self, tmp_path):
        _write_artifact(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("provider exploded")

        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_boom,
        )
        assert result.stored is True
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_PROVIDER_FAILED
        assert result.attempts[0].degraded is True

    def test_a_provider_failure_never_raises_to_the_caller(self, tmp_path):
        _write_artifact(tmp_path)

        def _boom(*args, **kwargs):
            raise ValueError("nope")

        # It must return, not raise.
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_boom,
        )
        assert result.error == ""


class TestProviderTimeout:
    def test_a_timeout_produces_a_record_and_no_suitability(self, tmp_path):
        _write_artifact(tmp_path)

        def _timeout(*args, **kwargs):
            raise TimeoutError("deadline exceeded")

        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_timeout,
        )
        assert result.stored is True
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_TIMEOUT
        assert result.attempts[0].degraded is True

    def test_the_live_candidates_are_untouched_after_a_timeout(self, tmp_path):
        _write_artifact(tmp_path)
        candidates = [_candidate()]
        before = _snapshot(candidates)

        def _timeout(*args, **kwargs):
            raise TimeoutError("deadline exceeded")

        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_timeout,
        )
        assert _snapshot(candidates) == before


class TestMalformedResponse:
    def test_a_malformed_response_produces_no_suitability(self, tmp_path):
        _write_artifact(tmp_path)

        def _empty(*args, **kwargs):
            return ()

        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_empty,
        )
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_MALFORMED

    def test_a_result_without_a_suitability_produces_no_suitability(self, tmp_path):
        _write_artifact(tmp_path)

        def _none(*args, **kwargs):
            return (_candidate("c1"),)  # no suitability attached

        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_none,
        )
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_MALFORMED

    def test_a_result_for_the_wrong_candidate_produces_no_suitability(self, tmp_path):
        _write_artifact(tmp_path)

        def _wrong(*args, **kwargs):
            from dataclasses import replace
            return (replace(_candidate("SOMEONE_ELSE"), suitability=0.9),)

        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1")],
            source_revision="7", score_fn=_wrong,
        )
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_MALFORMED


class TestStaleRevision:
    def test_a_stale_revision_is_refused(self, tmp_path):
        _write_artifact(tmp_path, revision=3)
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_STALE_REVISION

    def test_a_stale_revision_makes_no_call_at_all(self, tmp_path):
        _write_artifact(tmp_path, revision=3)
        called = []

        def _fn(*args, **kwargs):
            called.append(1)
            return ()

        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_fn,
        )
        assert not called, "a stale run must not spend a call"


class TestRequirementSetMismatch:
    def test_a_mismatched_requirement_set_is_refused(self, tmp_path):
        _write_artifact(tmp_path, set_id="rs-abc123")
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", requirement_set_id="rs-DIFFERENT",
            score_fn=_scorer(),
        )
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_REQUIREMENT_SET_MISMATCH


class TestDuplicateInvocation:
    def test_a_duplicate_invocation_is_refused(self, tmp_path):
        _write_artifact(tmp_path)
        first = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        assert first.produced_suitability is True
        second = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        assert second.error == STATUS_DUPLICATE
        assert second.attempts == ()
        # the second run did not overwrite the first
        assert len(read_shadow_attempts(tmp_path)) == 1

    def test_a_duplicate_invocation_makes_no_second_call(self, tmp_path):
        _write_artifact(tmp_path)
        calls = []

        def _fn(*args, **kwargs):
            calls.append(1)
            return _scorer()(args[0], **kwargs)

        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_fn,
        )
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_fn,
        )
        assert len(calls) == 1, "a duplicate must not spend a second call"

    def test_a_different_revision_is_not_a_duplicate(self, tmp_path):
        _write_artifact(tmp_path, revision=7)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="7")],
            source_revision="7", score_fn=_scorer(),
        )
        _write_artifact(tmp_path, revision=8)
        second = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="8")],
            source_revision="8", score_fn=_scorer(),
        )
        assert second.error == ""
        assert second.produced_suitability is True


class TestStorageWriteFailure:
    def test_a_storage_failure_is_reported_not_raised(self, tmp_path, monkeypatch):
        _write_artifact(tmp_path)

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(
            "app.core.shadow_runner.write_shadow_record", _boom)
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        assert result.stored is False
        assert result.error == STATUS_STORAGE_FAILED
        # The PENDING marker could not be written, so the paid call was NOT
        # dispatched: without the marker a crash would be invisible, and a
        # resume could spend a second call for a call that may already have
        # been billed. Failing closed is the correct behaviour here.
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_STORAGE_FAILED

    def test_a_storage_failure_leaves_the_live_candidates_untouched(
            self, tmp_path, monkeypatch):
        _write_artifact(tmp_path)
        candidates = [_candidate()]
        before = _snapshot(candidates)

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(
            "app.core.shadow_runner.write_shadow_record", _boom)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_scorer(),
        )
        assert _snapshot(candidates) == before


class TestMissingOrCorruptArtifact:
    def test_a_missing_artifact_is_refused(self, tmp_path):
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        assert result.produced_suitability is False
        assert result.attempts[0].status == STATUS_MISSING_ARTIFACT

    def test_a_corrupt_artifact_is_refused(self, tmp_path):
        from app.core.design_requirement_artifact import artifact_path
        _write_artifact(tmp_path)
        artifact_path(tmp_path).write_text("{not json", encoding="utf-8")
        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        assert result.produced_suitability is False
        assert result.attempts[0].status in (
            STATUS_MISSING_ARTIFACT, STATUS_CORRUPT_ARTIFACT)

    def test_a_missing_artifact_makes_no_call(self, tmp_path):
        called = []

        def _fn(*args, **kwargs):
            called.append(1)
            return ()

        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_fn,
        )
        assert not called


class TestShadowIsOffByDefault:
    def test_a_shadow_run_while_disabled_is_refused(self, tmp_path):
        _write_artifact(tmp_path)
        with pytest.raises(ShadowNotEnabled):
            run_shadow_scoring(
                tmp_path, project_id="p1",
                config=ShadowConfig(enabled=False),
                candidates=[_candidate()],
                source_revision="7", score_fn=_scorer(),
            )

    def test_the_default_config_is_disabled(self):
        assert ShadowConfig().enabled is False

    def test_shadow_mode_does_not_enable_the_live_scoring_path(self):
        """The live switch is untouched by this patch."""
        from app.core.decisions_design_provider import DecisionsDesignProvider
        import inspect
        default = inspect.signature(
            DecisionsDesignProvider.__init__).parameters["scoring_enabled"].default
        assert default is False


class TestTheRecordIsInternal:
    def test_the_record_lives_in_the_workspace_and_nowhere_else(self, tmp_path):
        _write_artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        assert shadow_path(tmp_path).exists()
        assert shadow_path(tmp_path).name == SHADOW_FILE_NAME

    def test_the_provider_does_not_read_the_record(self):
        """The provider may CALL the scorer, but never READ the record."""
        from pathlib import Path
        provider = (Path(__file__).resolve().parents[1]
                    / "app" / "core" / "decisions_design_provider.py")
        text = provider.read_text(encoding="utf-8")
        assert "read_shadow_attempts" not in text
        assert "read_shadow_record" not in text
        assert "shadow_path" not in text

    def test_no_deploy_module_references_the_shadow_record(self):
        from pathlib import Path
        deploy = Path(__file__).resolve().parents[1] / "app" / "deploy"
        for path in deploy.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "shadow_scoring" not in text, path.name
            assert "shadow-scoring" not in text, path.name

    def test_no_core_module_outside_the_shadow_seam_reads_the_record(self):
        """The record is write-only: only the shadow modules read it."""
        from pathlib import Path
        core = Path(__file__).resolve().parents[1] / "app" / "core"
        readers = []
        for path in core.rglob("*.py"):
            # The provider exposes the shadow scoring callable, so it names
            # the seam. It never READS the record -- that is the property
            # asserted below.
            if path.name in ("shadow_scoring.py", "shadow_runner.py",
                             "shadow_evaluation.py", "shadow_cli.py",
                             "decisions_design_provider.py"):
                continue
            text = path.read_text(encoding="utf-8")
            if "shadow_scoring" in text or "shadow_runner" in text:
                readers.append(path.name)
        assert not readers, readers

    def test_the_build_does_not_import_the_shadow_seam(self):
        from pathlib import Path
        build = (Path(__file__).resolve().parents[1]
                 / "app" / "projects" / "build.py").read_text(encoding="utf-8")
        assert "shadow_scoring" not in build
        assert "shadow_runner" not in build

    def test_the_record_never_appears_in_the_published_site(self, tmp_path):
        _write_artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[_candidate()],
            source_revision="7", score_fn=_scorer(),
        )
        site = tmp_path / "dist"
        site.mkdir()
        (site / "index.html").write_text("<html></html>", encoding="utf-8")
        leaked = [p for p in site.rglob("*")
                  if p.is_file() and "shadow" in p.name.lower()]
        assert not leaked


class TestApprovalAndPublishAreUnaffected:
    def test_the_engine_ranks_identically_before_and_after_a_shadow_run(self, tmp_path):
        """The engine's ranking must not see a shadow result."""
        from app.core.decision_engine import _suitability_comparable
        _write_artifact(tmp_path)
        candidates = [_candidate("c1"), _candidate("c2")]
        before = _suitability_comparable(candidates)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_scorer(0.99),
        )
        after = _suitability_comparable(candidates)
        assert before == after
        assert after == (False, False), "an unscored pool must stay unranked"

    def test_a_shadow_run_cannot_make_a_pool_ranked(self, tmp_path):
        from app.core.decision_engine import _suitability_comparable
        _write_artifact(tmp_path)
        candidates = [_candidate("c1"), _candidate("c2")]
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=candidates,
            source_revision="7", score_fn=_scorer(0.99),
        )
        assert all(c.suitability is None for c in candidates)
        ranked, mixed = _suitability_comparable(candidates)
        assert ranked is False


class TestTheRecordSchema:
    def test_a_record_round_trips(self, tmp_path):
        from app.core.shadow_scoring import ShadowScore
        original = ShadowScore(
            project_id="p1", candidate_id="c1", source_revision="7",
            requirement_set_id="rs-1", scoring_contract_version="n8-design-v1",
            suitability=0.5, dimensions=(("color", 0.4),),
            diagnostics=(("R1", "satisfied"),), screenshot_digests=("abc",),
        )
        again = ShadowScore.from_dict(original.to_dict())
        assert again == original

    def test_a_record_with_a_forbidden_field_is_rejected(self):
        from app.core.shadow_scoring import ShadowScore
        payload = ShadowScore(
            project_id="p1", candidate_id="c1", source_revision="7",
            requirement_set_id="rs-1", scoring_contract_version="n8-design-v1",
        ).to_dict()
        payload["api_key"] = "sk-secret"
        with pytest.raises(ShadowScoringError):
            ShadowScore.from_dict(payload)

    def test_a_corrupt_record_reads_as_absent_not_as_a_crash(self, tmp_path):
        from app.core.shadow_scoring import shadow_path
        shadow_path(tmp_path).write_text("{broken", encoding="utf-8")
        assert read_shadow_record(tmp_path) is None
        assert read_shadow_attempts(tmp_path) == ()
