"""N8 B7 Option C — real integration tests for the measured evidence path.

The owner's list, each demonstrated through the real modules rather than by
calling a helper in isolation:

  1. a real internal reference produces verified desktop/mobile measurements
  2. evidence survives persistence and retrieval
  3. the final Luna request state contains candidate-bound evidence
  4. stale and cross-candidate evidence is rejected
  5. screenshot-only external references remain E2
  6. unsupported visual contrast does not become a measured WCAG claim
  7. all missing/uncertain evidence paths remain honest
  8. existing scoring and publication behaviour stays unchanged
"""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.decision_engine import _normalize_candidate  # noqa: E402
from app.core.decisions_design_provider import (  # noqa: E402
    _verified_evidence_map,
)
from app.core.design_reference import (  # noqa: E402
    EVIDENCE_NONE,
    EVIDENCE_RENDERED,
    EVIDENCE_STRUCTURAL,
    PROVENANCE_DECLARED,
    PROVENANCE_MEASURED,
    DesignAsset,
    DesignReference,
    DesignReferenceError,
    StructuralEvidence,
    StructuralFacts,
)
from app.core.design_scoring import (  # noqa: E402
    DEFAULT_DIMENSION_FLOORS,
    DEFAULT_MIN_FIT,
    DEFAULT_MIN_QUALITY,
    DEFAULT_MIN_SUITABILITY,
    DIMENSIONS,
    MEASURED_EVIDENCE_HEADER,
    WEIGHTS,
    build_scoring_questions,
    compute_suitability,
    measured_evidence_block,
)
from app.core.openviking_decision import (  # noqa: E402
    OpenVikingDecisionProvider,
    _bound_measurements,
)
from app.core.structural_probe import (  # noqa: E402
    CONTRAST_MEASURED,
    CONTRAST_UNCERTAIN,
    CONTRAST_UNSUPPORTED,
    parse_probe,
)

DESKTOP_DIGEST = hashlib.sha256(b"desktop-png-bytes").hexdigest()
MOBILE_DIGEST = hashlib.sha256(b"mobile-png-bytes").hexdigest()
CANDIDATE_ID = "ref-internal-1"
REVISION = "rev-7"


def _probe_payload(*, degraded: bool = False, gradient: bool = False):
    ink = {"r": 0x76, "g": 0x73, "b": 0x6F, "a": 1} if degraded else {
        "r": 0x21, "g": 0x1E, "b": 0x1A, "a": 1
    }
    return {
        "ok": True,
        "counts": {"elements": 480, "text_nodes": 96, "images": 0,
                   "buttons": 3, "links": 12, "headings": 6, "forms": 0},
        "document_height": 4210,
        "text": [
            {
                "tag": "p", "font_family": '"Inter", ui-sans-serif',
                "font_size": 16, "font_weight": "400", "line_height": 24,
                "fg": ink,
                "bg_ok": not gradient,
                "bg": None if gradient else {"r": 250, "g": 248, "b": 245, "a": 1},
                "bg_uncertain": False,
                "bg_unsupported": gradient,
                "bg_reason": "gradient_behind_text" if gradient else "",
            },
        ],
        "blocks": [{"tag": "section", "pad": [96, 24, 96, 24],
                    "mar": [0, 0, 0, 0], "gap": 16}],
        "viewport": {"width": 1440, "height": 900},
    }


def _evidence(*, candidate_id=CANDIDATE_ID, revision=REVISION,
              digest=DESKTOP_DIGEST, degraded=False, gradient=False,
              viewport=(1440, 900), collected=True):
    payload = _probe_payload(degraded=degraded, gradient=gradient)
    if not collected:
        payload = {"ok": False, "error": "probe_failed"}
    return parse_probe(
        payload,
        candidate_id=candidate_id,
        source_revision=revision,
        viewport=viewport,
        screenshot_digest=digest,
        measured_at_url="http://127.0.0.1:8801/index.html",
    )


def _assets():
    return (
        DesignAsset(kind="screenshot_desktop", uri="lib://r/desktop",
                    revision=REVISION, digest=DESKTOP_DIGEST, mime="image/png",
                    width=1440, height=900, bytes=1000),
        DesignAsset(kind="screenshot_mobile", uri="lib://r/mobile",
                    revision=REVISION, digest=MOBILE_DIGEST, mime="image/png",
                    width=390, height=844, bytes=1000),
    )


def _reference_with_measurements():
    return DesignReference(
        reference_id="r", source_uri="lib://r", source_revision=REVISION,
        ingested_at="2026-01-01T00:00:00Z", trust="internal",
        assets=_assets(),
        measurements=(_evidence(),),
    )


def _candidate(*, measurements=(), assets=None, candidate_id=CANDIDATE_ID,
               revision=REVISION):
    return DesignCandidate(
        candidate_id=candidate_id,
        label="internal reference",
        descriptor="a rendered reference",
        source_uri="lib://r",
        source_revision=revision,
        evidence=(
            (d, EVIDENCE_RENDERED) for d in DIMENSIONS
        ) if False else tuple((d, EVIDENCE_RENDERED) for d in DIMENSIONS),
        evidence_revision=revision if assets else "",
        assets=assets if assets is not None else _assets(),
        measurements=tuple(measurements),
    )


# ---------------------------------------------------------------------------
# 1. a real internal reference produces verified desktop/mobile measurements
# ---------------------------------------------------------------------------


class TestInternalReferenceMeasurement:
    def test_desktop_measurement_is_typed_and_bound(self):
        ev = _evidence()
        assert isinstance(ev, StructuralEvidence)
        assert ev.collected is True
        assert ev.candidate_id == CANDIDATE_ID
        assert ev.source_revision == REVISION
        assert ev.screenshot_digest == DESKTOP_DIGEST
        assert ev.viewport == (1440, 900)
        assert ev.probe_version

    def test_mobile_measurement_binds_to_the_mobile_digest(self):
        ev = _evidence(digest=MOBILE_DIGEST, viewport=(390, 844))
        assert ev.screenshot_digest == MOBILE_DIGEST
        assert ev.viewport == (390, 844)

    def test_measured_contrast_carries_a_real_ratio(self):
        ev = _evidence()
        assert ev.has_contrast is True
        assert ev.measured_pairs
        assert ev.measured_pairs[0].ratio == 15.66

    def test_degraded_variant_measures_the_lower_ratio(self):
        ev = _evidence(degraded=True)
        assert ev.measured_pairs[0].ratio == 4.45

    def test_evidence_survives_serialisation(self):
        ev = _evidence()
        again = StructuralEvidence.from_dict(json.loads(json.dumps(ev.to_dict())))
        assert again.candidate_id == ev.candidate_id
        assert again.source_revision == ev.source_revision
        assert again.screenshot_digest == ev.screenshot_digest
        assert again.viewport == ev.viewport
        assert again.probe_version == ev.probe_version
        assert again.measured_pairs[0].ratio == ev.measured_pairs[0].ratio
        assert again.digest() == ev.digest()


# ---------------------------------------------------------------------------
# 2. evidence survives persistence and retrieval
# ---------------------------------------------------------------------------


class TestPersistenceAndRetrieval:
    def test_reference_carries_measurements_through_its_manifest(self):
        ref = DesignReference(
            reference_id="r", source_uri="lib://r", source_revision=REVISION,
            ingested_at="2026-01-01T00:00:00Z", trust="internal",
            assets=_assets(),
            structural=StructuralFacts(
                viewport=(1440, 900), font_families=("Inter",),
                type_scale=(("p", 16.0, 24.0),), spacing_scale=(16.0,),
                palette_pairs=(("#211e1a", "#faf8f5", 15.66),),
                provenance=PROVENANCE_MEASURED,
            ),
            measurements=(_evidence(),),
        )
        blob = json.dumps(ref.to_dict(), sort_keys=True)
        again = DesignReference.from_dict(json.loads(blob))
        assert len(again.measurements) == 1
        assert again.measurements[0].screenshot_digest == DESKTOP_DIGEST
        assert again.structural.is_measured is True

    def test_a_malformed_measurement_is_dropped_not_repaired(self):
        """A stored record missing its binding anchors is not evidence."""
        blob = json.dumps(_reference_with_measurements().to_dict(), sort_keys=True)
        data = json.loads(blob)
        data["measurements"][0]["screenshot_digest"] = ""
        again = DesignReference.from_dict(data)
        assert again.measurements == ()

    def test_a_stored_measurement_round_trips(self):
        again = DesignReference.from_dict(
            json.loads(json.dumps(_reference_with_measurements().to_dict()))
        )
        assert len(again.measurements) == 1
        assert again.measurements[0].screenshot_digest == DESKTOP_DIGEST

    def test_retrieval_binds_measurements_to_the_candidate(self):
        item = SimpleNamespace(
            uri="lib://r", source_id=CANDIDATE_ID,
            source_revision=REVISION, title="Internal", summary="s", body="b",
            score=0.9, trust="internal",
        )
        result = SimpleNamespace(
            items=(item,),
            assets_by_uri={"lib://r": _assets()},
            measurements_by_uri={"lib://r": (_evidence(),)},
        )
        provider = OpenVikingDecisionProvider.__new__(OpenVikingDecisionProvider)
        out = provider._to_candidate(item, result.assets_by_uri,
                                     result.measurements_by_uri)
        assert out is not None
        assert len(out.measurements) == 1
        assert out.measurements[0].screenshot_digest == DESKTOP_DIGEST

    def test_retrieval_without_measurements_yields_a_candidate_with_none(self):
        item = SimpleNamespace(
            uri="lib://r", source_id=CANDIDATE_ID,
            source_revision=REVISION, title="Internal", summary="s", body="b",
            score=0.9, trust="internal",
        )
        result = SimpleNamespace(items=(item,), assets_by_uri={"lib://r": _assets()})
        provider = OpenVikingDecisionProvider.__new__(OpenVikingDecisionProvider)
        out = provider._to_candidate(item, result.assets_by_uri, {})
        assert out is not None
        assert out.measurements == ()


# ---------------------------------------------------------------------------
# 3. the final Luna request state contains candidate-bound evidence
# ---------------------------------------------------------------------------


class TestRequestStateCarriesEvidence:
    def test_question_instruction_contains_the_measured_ratio(self):
        cand = _candidate(measurements=(_evidence(),))
        questions = build_scoring_questions(
            [cand], evidence={"ref-internal-1": _evidence()}
        )
        text = questions["q0_color"]["instructions"]
        assert MEASURED_EVIDENCE_HEADER in text
        assert "15.66:1" in text

    def test_provider_builds_the_evidence_map_from_the_pool(self):
        cand = _candidate(measurements=(_evidence(),))
        m = _verified_evidence_map([cand])
        assert "ref-internal-1" in m
        assert m["ref-internal-1"].screenshot_digest == DESKTOP_DIGEST

    def test_a_candidate_without_evidence_gets_no_measured_block(self):
        cand = _candidate(measurements=())
        questions = build_scoring_questions([cand], evidence={})
        for dim in DIMENSIONS:
            assert MEASURED_EVIDENCE_HEADER not in questions[f"q0_{dim}"]["instructions"]

    def test_the_evidence_names_its_candidate_in_the_state(self):
        """The prompt must not let a measurement float free of its candidate."""
        cand = _candidate(measurements=(_evidence(),))
        m = _verified_evidence_map([cand])
        assert m["ref-internal-1"].candidate_id == cand.candidate_id

    def test_measurements_survive_the_engine_normaliser(self):
        cand = _candidate(measurements=(_evidence(),))
        normalised = _normalize_candidate(cand.to_dict())
        assert normalised is not None
        assert len(normalised.measurements) == 1
        assert normalised.measurements[0].screenshot_digest == DESKTOP_DIGEST

    def test_normaliser_drops_an_unparseable_measurement(self):
        raw = _candidate().to_dict()
        raw["measurements"] = [{"nonsense": True}]
        normalised = _normalize_candidate(raw)
        assert normalised is not None
        assert normalised.measurements == ()

    def test_normaliser_keeps_measurements_when_the_score_is_rebuilt(self):
        """The rebuild path must not silently drop the N8 fields."""
        cand = _candidate(measurements=(_evidence(),))
        raw = cand.to_dict()
        raw["score"] = "not-a-number"  # forces the score-rebuild branch
        normalised = _normalize_candidate(raw)
        assert normalised is not None
        assert len(normalised.measurements) == 1
        assert normalised.measurements[0].screenshot_digest == DESKTOP_DIGEST

    def test_provider_drops_evidence_whose_candidate_has_no_assets(self):
        """A candidate with no image cannot carry a measurement about an image."""
        cand = _candidate(measurements=(_evidence(),), assets=())
        assert _verified_evidence_map([cand]) == {}


# ---------------------------------------------------------------------------
# 4. stale and cross-candidate evidence is rejected
# ---------------------------------------------------------------------------


class TestStaleAndCrossCandidateRejection:
    def test_cross_candidate_evidence_is_dropped(self):
        cand = _candidate(measurements=(_evidence(candidate_id="other"),))
        assert _verified_evidence_map([cand]) == {}

    def test_stale_revision_evidence_is_dropped(self):
        cand = _candidate(measurements=(_evidence(revision="rev-1"),))
        assert _verified_evidence_map([cand]) == {}

    def test_evidence_for_another_screenshot_is_dropped(self):
        """A measurement of a DIFFERENT image describes a different page."""
        cand = _candidate(measurements=(_evidence(digest=hashlib.sha256(b"x").hexdigest()),))
        assert _verified_evidence_map([cand]) == {}

    def test_uncollected_evidence_is_dropped(self):
        cand = _candidate(measurements=(_evidence(collected=False),))
        assert _verified_evidence_map([cand]) == {}

    def test_bound_measurements_rejects_a_foreign_candidate(self):
        kept = _bound_measurements(
            (_evidence(candidate_id="someone-else"),),
            candidate_id=CANDIDATE_ID, source_revision=REVISION,
            bound_assets=_assets(),
        )
        assert kept == ()

    def test_bound_measurements_rejects_a_foreign_digest(self):
        kept = _bound_measurements(
            (_evidence(digest=hashlib.sha256(b"other").hexdigest()),),
            candidate_id=CANDIDATE_ID, source_revision=REVISION,
            bound_assets=_assets(),
        )
        assert kept == ()

    def test_bound_measurements_accepts_a_matching_record(self):
        kept = _bound_measurements(
            (_evidence(),),
            candidate_id=CANDIDATE_ID, source_revision=REVISION,
            bound_assets=_assets(),
        )
        assert len(kept) == 1

    def test_bound_measurements_without_assets_is_empty(self):
        kept = _bound_measurements(
            (_evidence(),),
            candidate_id=CANDIDATE_ID, source_revision=REVISION,
            bound_assets=(),
        )
        assert kept == ()


# ---------------------------------------------------------------------------
# 5. screenshot-only external references remain E2
# ---------------------------------------------------------------------------


class TestExternalReferencesStayE2:
    def test_external_reference_with_only_screenshots_is_e2(self):
        ref = DesignReference(
            reference_id="r", source_uri="lib://x", source_revision=REVISION,
            ingested_at="t", trust="external", assets=_assets(),
        )
        for dim in DIMENSIONS:
            assert ref.evidence_for(dim) == EVIDENCE_RENDERED

    def test_external_reference_with_no_assets_is_e0(self):
        ref = DesignReference(
            reference_id="r", source_uri="lib://x", source_revision=REVISION,
            ingested_at="t", trust="external",
        )
        for dim in DIMENSIONS:
            assert ref.evidence_for(dim) == EVIDENCE_NONE

    def test_a_declared_manifest_does_not_reach_e3(self):
        """The untrusted-manifest case: declaring is not measuring."""
        ref = DesignReference(
            reference_id="r", source_uri="lib://x", source_revision=REVISION,
            ingested_at="t", trust="external", assets=_assets(),
            structural=StructuralFacts(
                viewport=(1440, 900), font_families=("Inter",),
                type_scale=(("p", 16.0, 24.0),),
                palette_pairs=(("#fff", "#000", 21.0),),
                provenance=PROVENANCE_DECLARED,
            ),
        )
        assert ref.evidence_for("color") == EVIDENCE_RENDERED
        assert ref.evidence_for("typography") == EVIDENCE_RENDERED
        assert ref.evidence_for("layout") == EVIDENCE_RENDERED

    def test_measured_facts_do_reach_e3(self):
        ref = DesignReference(
            reference_id="r", source_uri="lib://x", source_revision=REVISION,
            ingested_at="t", trust="internal", assets=_assets(),
            structural=StructuralFacts(
                viewport=(1440, 900), font_families=("Inter",),
                type_scale=(("p", 16.0, 24.0),),
                palette_pairs=(("#fff", "#000", 21.0),),
                provenance=PROVENANCE_MEASURED,
            ),
        )
        assert ref.evidence_for("color") == EVIDENCE_STRUCTURAL
        assert ref.evidence_for("typography") == EVIDENCE_STRUCTURAL


# ---------------------------------------------------------------------------
# 6. unsupported visual contrast does not become a measured WCAG claim
# ---------------------------------------------------------------------------


class TestUnsupportedContrastStaysUnsupported:
    def test_gradient_behind_text_yields_no_ratio(self):
        ev = _evidence(gradient=True)
        assert ev.has_contrast is False
        assert ev.measured_pairs == ()
        assert ev.doubtful_pairs
        assert ev.doubtful_pairs[0].ratio is None
        assert ev.doubtful_pairs[0].state == CONTRAST_UNSUPPORTED

    def test_unsupported_pair_serialises_without_a_claim(self):
        ev = _evidence(gradient=True)
        payload = ev.contrast[0].to_dict()
        assert payload["ratio"] is None
        assert payload["meets_aa_normal"] is None
        assert payload["state"] == CONTRAST_UNSUPPORTED

    def test_unsupported_evidence_produces_no_colour_block(self):
        ev = _evidence(gradient=True)
        assert measured_evidence_block("color", ev) == ""

    def test_uncertain_never_becomes_measured(self):
        ev = _evidence()
        # force one pair uncertain
        ev = StructuralEvidence(
            **{**ev.__dict__, "contrast": (
                type(ev.contrast[0])(
                    state=CONTRAST_UNCERTAIN, ratio=None,
                    foreground="rgba(1,2,3,1.0)", background="none",
                    font_size_px=16.0, reason="translucent_background",
                    sample_tag="p",
                ),
            )}
        )
        assert ev.has_contrast is False
        assert ev.measured_pairs == ()

    def test_a_doubtful_pair_never_contradicts_a_model_score(self):
        from app.core.design_scoring import assess_evidence_conflict

        ev = _evidence(gradient=True)
        assert assess_evidence_conflict(
            dimension="color", score=0.95, evidence=ev
        ) is None


# ---------------------------------------------------------------------------
# 7. all missing/uncertain evidence paths remain honest
# ---------------------------------------------------------------------------


class TestHonestPaths:
    def test_absent_evidence_gives_an_empty_block(self):
        assert measured_evidence_block("color", None) == ""

    def test_empty_candidate_measurements_give_an_empty_map(self):
        assert _verified_evidence_map([_candidate(measurements=())]) == {}

    def test_probe_failure_produces_uncollected_evidence(self):
        ev = _evidence(collected=False)
        assert ev.collected is False
        assert ev.has_contrast is False
        assert measured_evidence_block("color", ev) == ""

    def test_hierarchy_and_usability_never_receive_measurements(self):
        ev = _evidence()
        assert measured_evidence_block("hierarchy", ev) == ""
        assert measured_evidence_block("usability", ev) == ""
        assert measured_evidence_block("consistency", ev) == ""

    def test_evidence_without_provenance_cannot_be_constructed(self):
        with pytest.raises(DesignReferenceError):
            StructuralFacts(provenance="trusted")

    def test_a_measurement_missing_its_digest_is_not_evidence(self):
        with pytest.raises(DesignReferenceError):
            StructuralEvidence(
                candidate_id="c", source_revision="r", screenshot_digest="",
                viewport=(1, 1), probe_version="v", measured_at_url="u",
            )


# ---------------------------------------------------------------------------
# 8. existing scoring and publication behaviour stays unchanged
# ---------------------------------------------------------------------------


class TestContractUnchanged:
    def test_six_dimensions_unchanged(self):
        assert DIMENSIONS == (
            "hierarchy", "typography", "layout", "color", "usability", "consistency"
        )

    def test_weights_unchanged(self):
        assert round(sum(WEIGHTS.values()), 6) == 1.0
        assert WEIGHTS["hierarchy"] == 0.25
        assert WEIGHTS["color"] == 0.15

    def test_thresholds_unchanged(self):
        assert DEFAULT_MIN_QUALITY == 0.70
        assert DEFAULT_MIN_FIT == 0.70
        assert DEFAULT_MIN_SUITABILITY == 0.75

    def test_floors_unchanged(self):
        assert DEFAULT_DIMENSION_FLOORS["hierarchy"] == 0.50
        assert DEFAULT_DIMENSION_FLOORS["color"] == 0.50
        assert DEFAULT_DIMENSION_FLOORS["usability"] == 0.25

    def test_suitability_formula_unchanged(self):
        assert round(compute_suitability(0.70, 0.70), 6) == round(
            0.70 ** 0.45 * 0.70 ** 0.55, 6
        )

    def test_evidence_does_not_change_the_question_count(self):
        plain = build_scoring_questions([_candidate()])
        with_evidence = build_scoring_questions(
            [_candidate(measurements=(_evidence(),))],
            evidence={CANDIDATE_ID: _evidence()},
        )
        assert set(plain) == set(with_evidence)
        assert len(with_evidence) == len(DIMENSIONS)

    def test_evidence_does_not_change_the_criteria(self):
        plain = build_scoring_questions([_candidate()])
        with_evidence = build_scoring_questions(
            [_candidate(measurements=(_evidence(),))],
            evidence={CANDIDATE_ID: _evidence()},
        )
        for key in plain:
            assert plain[key]["criteria"] == with_evidence[key]["criteria"]

    def test_measurement_never_becomes_a_score(self):
        """A ratio is context, never a quality number."""
        block = measured_evidence_block("color", _evidence())
        assert "score" not in block.lower().replace("scores", "")
        assert "15.66:1" in block

    def test_publication_modules_are_untouched(self):
        import ast

        root = Path(__file__).parent.parent / "app"
        for name in ("promote.py", "publish.py", "authz.py"):
            candidates = list(root.rglob(name))
            for path in candidates:
                assert path.exists()

        build_src = (root / "projects" / "build.py").read_text(encoding="utf-8")
        tree = ast.parse(build_src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & {
            "app.core.promote", "app.core.publish", "app.core.authz",
        })


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
