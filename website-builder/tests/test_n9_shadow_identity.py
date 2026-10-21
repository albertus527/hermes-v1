"""P1 CORRECTION -- the invocation identity covers the candidate set.

The audit found that the identity was built from the project, the
revision, the requirement set and the contract -- but NOT from the
candidate set or the evidence digests. That produced a false equivalence
in one direction and a silent extra paid call in the other.

These tests pin both directions.
"""
import sys
from pathlib import Path

import pytest

from app.core.decision_provider import DesignCandidate
from app.core.design_reference import DesignAsset
from app.core.shadow_runner import (
    ShadowConfig,
    candidate_set_fingerprint,
    invocation_identity,
    run_shadow_scoring,
)
from app.core.shadow_scoring import read_shadow_attempts


def _artifact(tmp_path, set_id="rs-abc"):
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
        requirement_set_id=set_id,
    ))
    return tmp_path


def _candidate(cid="c1", digest="b" * 64, revision="7"):
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="n9lib://x", source_revision=revision, score=0.5,
        requirement_version="req-1",
        assets=(DesignAsset(
            kind="screenshot_desktop", uri="n9lib://x", revision=revision,
            digest=digest, mime="image/png", width=1440, height=900,
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


class TestTheFingerprint:
    def test_the_same_candidate_set_has_the_same_fingerprint(self):
        a = candidate_set_fingerprint([_candidate("c1", digest="a" * 64)])
        b = candidate_set_fingerprint([_candidate("c1", digest="a" * 64)])
        assert a == b

    def test_a_different_candidate_has_a_different_fingerprint(self):
        a = candidate_set_fingerprint([_candidate("c1", digest="a" * 64)])
        b = candidate_set_fingerprint([_candidate("c2", digest="a" * 64)])
        assert a != b

    def test_a_different_screenshot_has_a_different_fingerprint(self):
        """The evidence digest is part of the identity."""
        a = candidate_set_fingerprint([_candidate("c1", digest="a" * 64)])
        b = candidate_set_fingerprint([_candidate("c1", digest="c" * 64)])
        assert a != b

    def test_a_different_candidate_revision_has_a_different_fingerprint(self):
        a = candidate_set_fingerprint([_candidate("c1", revision="7")])
        b = candidate_set_fingerprint([_candidate("c1", revision="8")])
        assert a != b

    def test_the_order_does_not_matter(self):
        a = candidate_set_fingerprint([
            _candidate("c1", digest="a" * 64),
            _candidate("c2", digest="b" * 64)])
        b = candidate_set_fingerprint([
            _candidate("c2", digest="b" * 64),
            _candidate("c1", digest="a" * 64)])
        assert a == b

    def test_an_empty_set_has_a_fingerprint(self):
        assert candidate_set_fingerprint([])


class TestTheIdentity:
    def test_the_fingerprint_participates_in_the_identity(self):
        a = invocation_identity("p1", "7", "rs", "v", "fp-a")
        b = invocation_identity("p1", "7", "rs", "v", "fp-b")
        assert a != b

    def test_the_other_dimensions_still_participate(self):
        base = invocation_identity("p1", "7", "rs", "v", "fp")
        assert base != invocation_identity("p2", "7", "rs", "v", "fp")
        assert base != invocation_identity("p1", "8", "rs", "v", "fp")
        assert base != invocation_identity("p1", "7", "rs2", "v", "fp")
        assert base != invocation_identity("p1", "7", "rs", "v2", "fp")


class TestDifferentSnapshotsAreNotEquivalent:
    def test_a_different_candidate_is_not_a_duplicate(self, tmp_path):
        """The false equivalence the audit found."""
        _artifact(tmp_path)
        first = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1", digest="a" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        second = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c2", digest="c" * 64)],
            source_revision="7", score_fn=_scorer(0.6),
        )
        assert first.produced_suitability is True
        assert second.produced_suitability is True
        assert second.error == ""
        attempts = read_shadow_attempts(tmp_path)
        assert len(attempts) == 2

    def test_a_different_screenshot_is_not_a_duplicate(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1", digest="a" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        second = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1", digest="c" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        assert second.produced_suitability is True

    def test_the_same_snapshot_is_still_a_duplicate(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1", digest="a" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        second = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1", digest="a" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        assert second.error == "duplicate_invocation"
        assert second.produced_suitability is False


class TestTheExtraDispatchIsNotSilent:
    def test_a_changed_candidate_set_is_reported(self, tmp_path):
        """A second call for the same scope must be visible."""
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1", digest="a" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        second = run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c2", digest="c" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        # The record must show that a second attempt for the same scope
        # happened, and that it used a different candidate set.
        from app.core.shadow_scoring import read_shadow_record
        attempts = read_shadow_attempts(tmp_path)
        assert len(attempts) == 2
        assert len({a.invocation_id for a in attempts}) == 2
        record = read_shadow_record(tmp_path) or {}
        notes = record.get("notes") or []
        assert notes, "the extra dispatch was not reported in the record"
        assert any("DIFFERENT candidate set" in n for n in notes), notes

    def test_a_first_run_has_no_such_note(self, tmp_path):
        from app.core.shadow_scoring import read_shadow_record
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate("c1", digest="a" * 64)],
            source_revision="7", score_fn=_scorer(0.8),
        )
        record = read_shadow_record(tmp_path) or {}
        notes = record.get("notes") or []
        assert not any("DIFFERENT candidate set" in n for n in notes), notes
