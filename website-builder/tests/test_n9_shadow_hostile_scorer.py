"""AUDIT A REMEDIATION — hostile scorer tests.

The audit proved that a SHALLOW copy (``dataclasses.replace``) let a hostile
scorer change the LIVE candidate: a measurement's ``counts`` dict and an
asset object were shared between the live candidate and the scoring copy.

These tests pin the deep, independent copy. A hostile scorer is given every
opportunity to mutate everything it can reach, and the live state is
asserted unchanged afterwards.
"""
import dataclasses
from types import MappingProxyType

import pytest

from app.core.decision_provider import DesignCandidate
from app.core.design_reference import (
    ContrastMeasurement,
    DesignAsset,
    StructuralEvidence,
)
from app.core.shadow_runner import ShadowConfig, run_shadow_scoring
from app.core.shadow_scoring import ShadowScoringError


def _artifact(tmp_path):
    from app.core.design_requirement_artifact import (
        RequirementArtifact,
        RequirementEntry,
        write_requirement_artifact,
    )
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


def _live_candidate(counts=None, asset=None):
    counts = counts if counts is not None else {"links": 3, "buttons": 2}
    asset = asset if asset is not None else DesignAsset(
        kind="screenshot_desktop", uri="n9lib://x", revision="7",
        digest="a" * 64, mime="image/png", width=1440, height=900, bytes=1000,
    )
    return DesignCandidate(
        candidate_id="c1", label="c1", descriptor="d",
        source_uri="n9lib://x", source_revision="7", score=0.5,
        suitability=0.42, suitability_detail=(("suitability", 1.0),),
        suitability_contract="n8-design-v1",
        evidence=(("color", "E3"),), evidence_revision="7",
        assets=(asset,), requirement_version="req-1",
        measurements=(StructuralEvidence(
            candidate_id="c1", source_revision="7", viewport=(1440, 900),
            screenshot_digest="d" * 64, measured_at_url="http://x/",
            probe_version="p", font_families=("Inter",),
            type_scale=(("p", 16.0, 24.0),), spacing_scale=(16.0,),
            contrast=(ContrastMeasurement(
                state="measured", ratio=15.66, foreground="#000",
                background="#fff", font_size_px=16.0),),
            component_count=12, counts=counts, document_height=900,
            collected=True,
        ),),
    )


def _run(tmp_path, candidate, scorer):
    return run_shadow_scoring(
        tmp_path, project_id="p1",
        config=ShadowConfig(enabled=True),
        candidates=[candidate], source_revision="7", score_fn=scorer,
    )


class TestTheLiveCandidateIsUnreachable:
    def test_the_counts_dict_is_not_shared_with_the_copy(self, tmp_path):
        """The bug the audit found: a shared, mutable dict."""
        _artifact(tmp_path)
        counts = {"links": 3, "buttons": 2}
        candidate = _live_candidate(counts=counts)
        seen = {}

        def _fn(copies, **kwargs):
            seen["copy"] = copies[0]
            return ()

        _run(tmp_path, candidate, _fn)
        assert seen["copy"] is not candidate
        assert seen["copy"].measurements[0].counts is not counts

    def test_a_hostile_scorer_cannot_mutate_the_live_counts(self, tmp_path):
        _artifact(tmp_path)
        counts = {"links": 3, "buttons": 2}
        candidate = _live_candidate(counts=counts)
        before = dict(counts)

        def _fn(copies, **kwargs):
            copy = copies[0]
            try:
                copy.measurements[0].counts["links"] = 999
            except (TypeError, AttributeError):
                pass
            return ()

        _run(tmp_path, candidate, _fn)
        assert counts == before, "the live counts dict was mutated"

    def test_a_hostile_scorer_cannot_mutate_the_live_asset(self, tmp_path):
        _artifact(tmp_path)
        asset = DesignAsset(
            kind="screenshot_desktop", uri="n9lib://x", revision="7",
            digest="a" * 64, mime="image/png", width=1440, height=900,
            bytes=1000)
        candidate = _live_candidate(asset=asset)

        def _fn(copies, **kwargs):
            copy = copies[0]
            for a in copy.assets:
                try:
                    object.__setattr__(a, "digest", "MUTATED")
                except (TypeError, AttributeError):
                    pass
            return ()

        _run(tmp_path, candidate, _fn)
        assert asset.digest == "a" * 64, "the live asset was mutated"

    def test_a_hostile_scorer_cannot_reach_the_live_measurement(self, tmp_path):
        _artifact(tmp_path)
        candidate = _live_candidate()
        live_measurement = candidate.measurements[0]
        seen = {}

        def _fn(copies, **kwargs):
            seen["m"] = copies[0].measurements[0]
            return ()

        _run(tmp_path, candidate, _fn)
        assert seen["m"] is not live_measurement

    def test_a_loose_mutable_asset_is_replaced_by_an_immutable_value(self,
                                                                     tmp_path):
        """``assets`` is typed ``Tuple[Any, ...]``, so a loose object can
        arrive. The copy must not share it."""
        _artifact(tmp_path)

        class Loose:
            kind = "screenshot_desktop"
            uri = "n9lib://x"
            revision = "7"
            digest = "a" * 64
            mime = "image/png"
            width = 1440
            height = 900
            bytes = 1000

        loose = Loose()
        candidate = _live_candidate(asset=loose)
        seen = {}

        def _fn(copies, **kwargs):
            seen["a"] = copies[0].assets[0]
            return ()

        _run(tmp_path, candidate, _fn)
        assert seen["a"] is not loose
        # and it is not mutable
        with pytest.raises((TypeError, AttributeError)):
            seen["a"]["digest"] = "MUTATED"

    def test_the_copy_carries_no_suitability(self, tmp_path):
        _artifact(tmp_path)
        candidate = _live_candidate()
        seen = {}

        def _fn(copies, **kwargs):
            seen["c"] = copies[0]
            return ()

        _run(tmp_path, candidate, _fn)
        assert seen["c"].suitability is None
        assert seen["c"].suitability_detail == ()
        assert seen["c"].suitability_contract == ""

    def test_the_evidence_and_palette_are_tuples_on_the_copy(self, tmp_path):
        _artifact(tmp_path)
        candidate = _live_candidate()
        seen = {}

        def _fn(copies, **kwargs):
            seen["c"] = copies[0]
            return ()

        _run(tmp_path, candidate, _fn)
        assert isinstance(seen["c"].evidence, tuple)
        assert isinstance(seen["c"].palette, tuple)


class TestTheShallowCopyWouldHaveFailed:
    """These pin the specific defect, so the deep copy cannot be reverted
    to a shallow one without a failure."""

    def test_a_shallow_copy_would_share_the_counts(self):
        candidate = _live_candidate()
        shallow = dataclasses.replace(candidate)
        assert shallow.measurements[0].counts is candidate.measurements[0].counts

    def test_the_deep_copy_does_not(self, tmp_path):
        from app.core.shadow_runner import _copy_candidate
        candidate = _live_candidate()
        deep = _copy_candidate(candidate)
        assert deep.measurements[0].counts is not candidate.measurements[0].counts
        assert deep.assets[0] is not candidate.assets[0]


class TestCandidateMutationAttempts:
    def test_a_scorer_that_replaces_its_own_copy_does_not_reach_the_live(
            self, tmp_path):
        _artifact(tmp_path)
        candidate = _live_candidate()
        live_before = (
            candidate.suitability, candidate.suitability_contract,
            candidate.assets, candidate.measurements,
        )

        def _fn(copies, **kwargs):
            # Try to swap the copy's whole identity.
            try:
                object.__setattr__(copies[0], "suitability", 0.99)
            except (TypeError, AttributeError):
                pass
            return ()

        _run(tmp_path, candidate, _fn)
        assert (
            candidate.suitability, candidate.suitability_contract,
            candidate.assets, candidate.measurements,
        ) == live_before

    def test_a_candidate_that_cannot_be_copied_is_refused_not_scored(
            self, tmp_path):
        """Fail-closed: a record is written, no suitability is produced,
        and the caller is not interrupted."""
        _artifact(tmp_path)

        class NotACandidate:
            candidate_id = "c1"

        result = run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True),
            candidates=[NotACandidate()], source_revision="7",
            score_fn=lambda *a, **k: (),
        )
        assert result.produced_suitability is False
        assert len(result.attempts) == 1
        assert result.attempts[0].candidate_id == "c1"

        from app.core.shadow_scoring import read_shadow_attempts
        assert len(read_shadow_attempts(tmp_path)) == 1

    def test_a_mapping_asset_becomes_an_immutable_mapping(self, tmp_path):
        from app.core.shadow_runner import _copy_candidate
        candidate = _live_candidate(asset={"kind": "screenshot_desktop",
                                          "digest": "a" * 64})
        deep = _copy_candidate(candidate)
        assert isinstance(deep.assets[0], MappingProxyType)
        with pytest.raises((TypeError, AttributeError)):
            deep.assets[0]["digest"] = "MUTATED"
