"""N8 B2: durable design-evidence schema.

Proves the evidence contract holds the line the owner drew:

* E0/E1 never count as verified visual quality;
* E3/E4 require an actual measurement or observation, not a populated field;
* declared claims and extracted measurements stay distinguishable;
* digests, revisions and provenance are immutable and validated;
* the schema round-trips, and old DesignCandidate construction still works.
"""
from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import design_reference as dr  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _asset(kind=dr.ASSET_SCREENSHOT_DESKTOP, digest=DIGEST_A, rev="rev-1",
           mime="image/png", uri=None):
    return dr.DesignAsset(
        kind=kind,
        uri=uri or f"viking://p/references/r1/{kind}.png",
        revision=rev,
        digest=digest,
        mime=mime,
        width=1440,
        height=900,
        bytes=180_000,
    )


def _structural(**over):
    base = dict(
        viewport=(1440, 900),
        font_families=("Inter",),
        type_scale=(("h1", 48.0, 52.0),),
        spacing_scale=(4.0, 8.0, 16.0),
        palette_pairs=(("#f5f5f4", "#0b0b0c", 17.2),),
        component_count=14,
    )
    base.update(over)
    return dr.StructuralFacts(**base)


def _ref(**over):
    base = dict(
        reference_id="r1",
        source_uri="https://example.test/ref",
        source_revision="rev-1",
        ingested_at="2026-10-18T00:00:00Z",
        trust=dr.TRUST_REVIEWED,
        assets=(_asset(),),
    )
    base.update(over)
    return dr.DesignReference(**base)


# ---------------------------------------------------------------------------
# Asset validation
# ---------------------------------------------------------------------------


def test_asset_requires_sha256_digest():
    with pytest.raises(dr.DesignReferenceError):
        _asset(digest="zz")


def test_asset_rejects_uppercase_digest():
    with pytest.raises(dr.DesignReferenceError):
        _asset(digest="A" * 64)


def test_asset_rejects_unsupported_mime():
    with pytest.raises(dr.DesignReferenceError):
        _asset(mime="image/gif")


def test_asset_rejects_empty_uri():
    with pytest.raises(dr.DesignReferenceError):
        dr.DesignAsset(kind=dr.ASSET_SCREENSHOT_DESKTOP, uri="  ",
                       revision="r", digest=DIGEST_A, mime="image/png",
                       width=1, height=1, bytes=1)


def test_asset_rejects_nonpositive_dimensions():
    with pytest.raises(dr.DesignReferenceError):
        dr.DesignAsset(kind=dr.ASSET_SCREENSHOT_DESKTOP, uri="u", revision="r",
                       digest=DIGEST_A, mime="image/png", width=0, height=1, bytes=1)


def test_asset_rejects_unknown_kind():
    with pytest.raises(dr.DesignReferenceError):
        _asset(kind="hologram")


def test_asset_is_frozen():
    with pytest.raises(FrozenInstanceError):
        _asset().digest = DIGEST_B  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------


def test_reference_requires_provenance():
    with pytest.raises(dr.DesignReferenceError):
        _ref(source_uri="")


def test_reference_requires_revision():
    with pytest.raises(dr.DesignReferenceError):
        _ref(source_revision="")


def test_reference_rejects_duplicate_asset_uri():
    a = _asset()
    with pytest.raises(dr.DesignReferenceError):
        _ref(assets=(a, a))


def test_reference_rejects_unknown_trust():
    with pytest.raises(dr.DesignReferenceError):
        _ref(trust="trusted-ish")


def test_reference_is_frozen():
    with pytest.raises(FrozenInstanceError):
        _ref().reference_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Visual evidence: declared metadata is NOT evidence
# ---------------------------------------------------------------------------


def test_palette_alone_is_not_visual_evidence():
    """A reference with declared colour metadata but no screenshot is E0."""
    ref = _ref(assets=(), declared=(("palette", "#fff,#000"),))
    assert ref.has_visual_evidence is False
    assert ref.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_NONE
    assert ref.is_scorable(dr.DIMENSION_COLOR) is False


def test_html_asset_is_not_a_screenshot():
    ref = _ref(assets=(_asset(kind=dr.ASSET_HTML, mime="image/png"),))
    assert ref.has_visual_evidence is False


def test_thumbnail_alone_is_not_scored_as_rendered():
    """A thumbnail is not in SCREENSHOT_KINDS, so it cannot carry E2."""
    ref = _ref(assets=(_asset(kind=dr.ASSET_THUMBNAIL),))
    assert ref.has_visual_evidence is False


def test_screenshot_makes_it_rendered():
    ref = _ref()
    assert ref.has_visual_evidence is True
    assert ref.evidence_for(dr.DIMENSION_HIERARCHY) == dr.EVIDENCE_RENDERED


# ---------------------------------------------------------------------------
# E3 requires an actual measurement
# ---------------------------------------------------------------------------


def test_structural_escalates_measured_dimensions():
    ref = _ref(structural=_structural())
    assert ref.evidence_for(dr.DIMENSION_TYPOGRAPHY) == dr.EVIDENCE_STRUCTURAL
    assert ref.evidence_for(dr.DIMENSION_LAYOUT) == dr.EVIDENCE_STRUCTURAL
    assert ref.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_STRUCTURAL


def test_empty_structural_does_not_escalate():
    """Populated fields are not enough: the measurement must exist."""
    ref = _ref(structural=dr.StructuralFacts())
    assert ref.evidence_for(dr.DIMENSION_TYPOGRAPHY) == dr.EVIDENCE_RENDERED
    assert ref.evidence_for(dr.DIMENSION_LAYOUT) == dr.EVIDENCE_RENDERED
    assert ref.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_RENDERED


def test_typography_needs_both_families_and_scale():
    ref = _ref(structural=_structural(type_scale=()))
    assert ref.evidence_for(dr.DIMENSION_TYPOGRAPHY) == dr.EVIDENCE_RENDERED


def test_color_needs_a_measured_ratio():
    ref = _ref(structural=_structural(palette_pairs=(("#fff", "#000", 0.0),)))
    assert ref.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_RENDERED


def test_contrast_ratio_of_zero_is_not_a_measurement():
    facts = dr.StructuralFacts(palette_pairs=(("#fff", "#000", 0.0),))
    assert facts.has_contrast is False


# ---------------------------------------------------------------------------
# Usability can never be claimed from a picture
# ---------------------------------------------------------------------------


def test_usability_never_exceeds_rendered():
    ref = _ref(structural=_structural())
    assert ref.evidence_for(dr.DIMENSION_USABILITY) == dr.EVIDENCE_RENDERED


def test_consistency_never_exceeds_rendered():
    ref = _ref(structural=_structural())
    assert ref.evidence_for(dr.DIMENSION_CONSISTENCY) == dr.EVIDENCE_RENDERED


def test_no_dimension_reaches_interactive():
    """This schema cannot manufacture E4 from a picture or a measurement."""
    ref = _ref(assets=(_asset(), _asset(kind=dr.ASSET_SCREENSHOT_MOBILE,
                                        digest=DIGEST_B,
                                        uri="viking://p/references/r1/m.png")),
               structural=_structural())
    for dim in dr.DIMENSIONS:
        assert ref.evidence_for(dim) != dr.EVIDENCE_INTERACTIVE


def test_unknown_dimension_is_e0():
    assert _ref().evidence_for("vibes") == dr.EVIDENCE_NONE


def test_two_viewports_are_recorded():
    ref = _ref(assets=(
        _asset(),
        _asset(kind=dr.ASSET_SCREENSHOT_MOBILE, digest=DIGEST_B,
               uri="viking://p/references/r1/m.png"),
    ))
    assert ref.viewports == (dr.ASSET_SCREENSHOT_DESKTOP,
                             dr.ASSET_SCREENSHOT_MOBILE)


# ---------------------------------------------------------------------------
# Declared vs extracted stay separate
# ---------------------------------------------------------------------------


def test_declared_and_extracted_are_separate():
    ref = _ref(
        declared=(("palette", "#fff,#000"), ("style", "editorial")),
        extracted=(("palette_pairs", "1"),),
    )
    data = ref.to_dict()
    assert data["declared"] == {"palette": "#fff,#000", "style": "editorial"}
    assert data["extracted"] == {"palette_pairs": "1"}
    assert data["declared"] != data["extracted"]


def test_declared_does_not_affect_evidence():
    """A rich declared map must not upgrade any dimension's class."""
    ref = _ref(assets=(), declared=(("screenshot", "yes"), ("contrast", "21:1")))
    assert ref.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_NONE


# ---------------------------------------------------------------------------
# Rights
# ---------------------------------------------------------------------------


def test_rights_default_false():
    assert _ref().rights_confirmed is False


def test_rights_and_licence_round_trip():
    ref = _ref(rights_confirmed=True, licence="CC-BY-4.0")
    data = ref.to_dict()
    assert data["rights_confirmed"] is True
    assert data["licence"] == "CC-BY-4.0"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_to_dict_is_json_serialisable():
    ref = _ref(structural=_structural(), declared=(("a", "b"),))
    json.dumps(ref.to_dict())  # must not raise


def test_round_trip_preserves_everything():
    ref = _ref(
        assets=(_asset(), _asset(kind=dr.ASSET_SCREENSHOT_MOBILE,
                                 digest=DIGEST_B,
                                 uri="viking://p/references/r1/m.png")),
        structural=_structural(),
        declared=(("palette", "x"),),
        extracted=(("pairs", "1"),),
        rights_confirmed=True,
        licence="MIT",
    )
    again = dr.DesignReference.from_dict(ref.to_dict())
    assert again.to_dict() == ref.to_dict()
    assert again.evidence_map() == ref.evidence_map()


def test_round_trip_preserves_evidence_classes():
    ref = _ref(structural=_structural())
    again = dr.DesignReference.from_dict(ref.to_dict())
    for dim in dr.DIMENSIONS:
        assert again.evidence_for(dim) == ref.evidence_for(dim)


def test_from_dict_rejects_future_schema_version():
    data = _ref().to_dict()
    data["schema_version"] = 99
    with pytest.raises(dr.DesignReferenceError):
        dr.DesignReference.from_dict(data)


def test_from_dict_rejects_missing_digest():
    data = _ref().to_dict()
    data["assets"][0]["digest"] = ""
    with pytest.raises(dr.DesignReferenceError):
        dr.DesignReference.from_dict(data)


def test_schema_version_is_declared():
    assert _ref().schema_version == dr.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Evidence vocabulary
# ---------------------------------------------------------------------------


def test_evidence_levels_are_ordered():
    assert dr.EVIDENCE_LEVELS == ("E0", "E1", "E2", "E3", "E4")
    assert dr.EVIDENCE_RANK["E0"] == 0
    assert dr.EVIDENCE_RANK["E4"] == 4


def test_scorable_floor_is_rendered():
    assert dr.EVIDENCE_SCORABLE_FROM == dr.EVIDENCE_RENDERED


def test_e1_is_never_scorable():
    assert dr.EVIDENCE_RANK[dr.EVIDENCE_DECLARED] < dr.EVIDENCE_RANK[dr.EVIDENCE_SCORABLE_FROM]


def test_dimensions_are_a_closed_set():
    assert dr.DIMENSIONS == (
        "hierarchy", "typography", "layout", "color", "usability", "consistency",
    )


# ---------------------------------------------------------------------------
# DesignCandidate compatibility
# ---------------------------------------------------------------------------


def test_candidate_old_construction_unchanged():
    c = DesignCandidate(
        candidate_id="c1", label="L", descriptor="D",
        source_uri="okv://a", source_revision="r1", score=0.8,
    )
    assert c.candidate_id == "c1"
    assert c.score == 0.8
    assert c.suitability is None


def test_candidate_positional_construction_unchanged():
    """The original eight positional fields keep their order."""
    c = DesignCandidate("c1", "L", "D", ("#fff",), "okv://a", "r1", 0.7, "")
    assert c.palette == ("#fff",)
    assert c.score == 0.7


def test_candidate_new_fields_default():
    c = DesignCandidate(candidate_id="c1", label="L", descriptor="D")
    assert c.suitability is None
    assert c.suitability_detail == ()
    assert c.suitability_contract == ""
    assert c.evidence == ()
    assert c.evidence_revision == ""
    assert c.assets == ()
    assert c.requirement_version == ""


def test_candidate_to_dict_gains_keys():
    data = DesignCandidate(candidate_id="c1", label="L", descriptor="D").to_dict()
    for key in ("suitability", "suitability_detail", "suitability_contract",
                "evidence", "evidence_revision", "assets", "requirement_version"):
        assert key in data


def test_candidate_to_dict_preserves_score():
    c = DesignCandidate(candidate_id="c1", label="L", descriptor="D", score=0.63)
    assert c.to_dict()["score"] == 0.63


def test_candidate_score_and_suitability_are_independent():
    c = DesignCandidate(candidate_id="c1", label="L", descriptor="D",
                        score=0.42, suitability=0.91)
    assert c.score == 0.42
    assert c.suitability == 0.91
    assert c.to_dict()["score"] == 0.42
    assert c.to_dict()["suitability"] == 0.91


def test_candidate_evidence_lookup():
    c = DesignCandidate(candidate_id="c1", label="L", descriptor="D",
                        evidence=(("hierarchy", "E2"), ("color", "E3")))
    assert c.evidence_for("hierarchy") == "E2"
    assert c.evidence_for("color") == "E3"
    assert c.evidence_for("usability") == "E0"
    assert c.evidence_map == {"hierarchy": "E2", "color": "E3"}


def test_candidate_has_no_confidence_field():
    """Model confidence must never reach the candidate, so it cannot be ranked."""
    assert "confidence" not in DesignCandidate.__dataclass_fields__


def test_candidate_assets_serialise():
    c = DesignCandidate(candidate_id="c1", label="L", descriptor="D",
                        assets=(_asset(),))
    data = c.to_dict()
    assert len(data["assets"]) == 1
    assert data["assets"][0]["digest"] == DIGEST_A


def test_candidate_is_frozen():
    c = DesignCandidate(candidate_id="c1", label="L", descriptor="D")
    with pytest.raises(FrozenInstanceError):
        c.suitability = 0.5  # type: ignore[misc]


def test_candidate_to_dict_is_json_serialisable():
    c = DesignCandidate(candidate_id="c1", label="L", descriptor="D",
                        suitability=0.8,
                        suitability_detail=(("design_quality", 0.9),),
                        evidence=(("hierarchy", "E2"),),
                        assets=(_asset(),))
    json.dumps(c.to_dict())
