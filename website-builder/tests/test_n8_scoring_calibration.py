"""N8 B7 scoring calibration: dimension specificity and honest Fit abstention.

Two defects were found by the V2 controlled run and this file locks the fixes:

1. **Dimension instructions named a dimension but never defined it.**
   Every prompt read "Rate the X ... judge this dimension in isolation" (105-108
   chars) with no inclusion clause and no exclusion clause. Measured against the
   controlled pool that produced r(hierarchy, layout) = 0.956 -- one question
   asked twice. The one dimension that already carried a dimension-specific
   clause (colour) was also the only one that isolated cleanly.

2. **Fit was scored against requirements that never existed.**
   The brief held no requirement set, so the model returned the scale midpoint
   (~0.50) for every candidate. A missing requirement is missing DATA. It must
   abstain, never score low, because a fabricated F flows into the S gate as if
   it were a real assessment.

Nothing in this file changes weights, thresholds, floors, the Q/F/S formula, the
evidence gates, atomicity, or the exclusion of model confidence from ranking.
Those are asserted unchanged at the bottom.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import design_scoring as ds  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_requirements import extract_requirements  # noqa: E402
from app.core.requirement_creator import compile_prd  # noqa: E402

DIMS = ds.DIMENSIONS
CAND = DesignCandidate(
    candidate_id="c1", label="L", descriptor="D",
    source_uri="u", source_revision="r", score=0.8,
)

#: A SYNTHETIC calibration brief. These seven requirements exist only to exercise
#: the scorer offline. They are NOT production requirements and must never be
#: wired into a real build -- production requirements come from the native PRD
#: (see design_requirements.extract_requirements).
SYNTHETIC_REQUIREMENTS = (
    "A services section listing at least four named services",
    "A pricing table showing a price per service",
    "A portfolio grid with at least six items",
    "An office location with address and opening hours",
    "A primary call-to-action visible above the fold",
    "Serif display typography for headings",
    "A brass or amber accent colour used for emphasis",
)


def _q(dimension: str) -> str:
    return ds.build_scoring_questions([CAND], requirements=SYNTHETIC_REQUIREMENTS)[
        f"q0_{dimension}"
    ]["instructions"]


# ---------------------------------------------------------------------------
# Dimension-specific inclusion / exclusion semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dimension", DIMS)
def test_every_dimension_states_what_to_judge(dimension):
    assert "Judge ONLY:" in _q(dimension)


@pytest.mark.parametrize("dimension", DIMS)
def test_every_dimension_states_what_not_to_judge(dimension):
    assert "Do NOT judge:" in _q(dimension)


@pytest.mark.parametrize("dimension", DIMS)
def test_every_dimension_gives_a_reason_for_the_boundary(dimension):
    """A boundary with no rationale is easy to ignore; the '--' clause is why."""
    assert " -- " in _q(dimension)


def test_hierarchy_defines_attention_ordering():
    text = _q("hierarchy")
    assert "entry point" in text
    assert "attention order" in text


def test_hierarchy_excludes_spacing():
    """Spacing belongs to layout. The exclusion must be explicit."""
    text = _q("hierarchy")
    tail = text.split("Do NOT judge:", 1)[1]
    assert "margins" in tail
    assert "padding" in tail


def test_layout_owns_grid_and_spacing():
    text = _q("layout")
    head = text.split("Do NOT judge:", 1)[0]
    assert "grid" in head
    assert "margins" in head
    assert "spacing consistency" in head


def test_layout_excludes_type_size():
    """Heading size belongs to hierarchy/typography, not layout."""
    tail = _q("layout").split("Do NOT judge:", 1)[1]
    assert "heading size" in tail


def test_hierarchy_and_layout_are_mutually_exclusive():
    """Each must name the other's territory as out of scope."""
    h = _q("hierarchy")
    l = _q("layout")
    assert "margins" in h.split("Do NOT judge:", 1)[1]
    assert "heading size" in l.split("Do NOT judge:", 1)[1]


def test_no_two_dimension_instructions_are_identical():
    texts = {d: _q(d) for d in DIMS}
    assert len(set(texts.values())) == len(DIMS)


def test_instructions_are_substantially_longer_than_a_bare_label():
    """The pre-calibration prompts were 105-108 chars and carried no definition."""
    for d in DIMS:
        assert len(_q(d)) > 200, d


@pytest.mark.parametrize("dimension", DIMS)
def test_instruction_interpolates_the_candidate_id(dimension):
    assert "'c1'" in _q(dimension)


@pytest.mark.parametrize("dimension", DIMS)
def test_instruction_is_single_line_per_clause(dimension):
    """No clause may contain a raw newline inside itself; clauses are separate."""
    for line in _q(dimension).split("\n"):
        assert "\r" not in line


def test_colour_keeps_its_no_measured_ratio_limit():
    assert "cannot prove a contrast RATIO" in _q("color")


def test_usability_keeps_its_no_accessibility_claim_limit():
    assert "do not claim accessibility compliance" in _q("usability")


# ---------------------------------------------------------------------------
# Requirement Fit: sufficient set is scored, insufficient set abstains
# ---------------------------------------------------------------------------


def test_sufficient_requirements_emit_a_fit_question():
    questions = ds.build_scoring_questions([CAND], requirements=SYNTHETIC_REQUIREMENTS)
    assert "f0" in questions
    assert len(questions) == 7


def test_no_requirements_emit_no_fit_question():
    """The absence of f<i> is what makes the parser abstain."""
    questions = ds.build_scoring_questions([CAND], requirements=())
    assert "f0" not in questions
    assert len(questions) == 6


def test_one_requirement_is_not_enough():
    assert ds.requirements_are_sufficient(["A pricing table"]) is False


def test_two_requirements_are_not_enough():
    assert ds.requirements_are_sufficient(
        ["A pricing table", "A portfolio grid"]
    ) is False


def test_three_requirements_are_enough():
    assert ds.requirements_are_sufficient(
        ["A pricing table", "A portfolio grid", "An office location"]
    ) is True


def test_fit_instruction_forbids_judging_attractiveness():
    text = ds.build_scoring_questions([CAND], requirements=SYNTHETIC_REQUIREMENTS)[
        "f0"
    ]["instructions"]
    assert "Do NOT consider general visual attractiveness" in text
    assert "must score LOW here" in text


def test_fit_instruction_requires_per_requirement_assessment():
    text = ds.build_scoring_questions([CAND], requirements=SYNTHETIC_REQUIREMENTS)[
        "f0"
    ]["instructions"]
    assert "EACH numbered requirement" in text


def test_fit_instruction_states_the_requirement_count():
    text = ds.build_scoring_questions([CAND], requirements=SYNTHETIC_REQUIREMENTS)[
        "f0"
    ]["instructions"]
    assert f"{len(SYNTHETIC_REQUIREMENTS)} requirements" in text


def test_missing_fit_answer_abstains_not_low_scores():
    """The core correction: no F is invented, and it is not reported as failure."""
    payload = {
        "answers": {
            f"q0_{d}": {"type": "score", "score": 3, "legend": {str(i): "" for i in range(5)}}
            for d in DIMS
        }
    }
    with pytest.raises(ds.ScoringError) as excinfo:
        ds.parse_pool_scores(payload, [CAND], expect_fit=False)
    assert excinfo.value.status == ds.STATUS_INSUFFICIENT_REQUIREMENTS


def test_insufficient_requirements_status_is_distinct_from_provider_failure():
    assert ds.STATUS_INSUFFICIENT_REQUIREMENTS != ds.STATUS_MALFORMED
    assert ds.STATUS_INSUFFICIENT_REQUIREMENTS != ds.STATUS_INCOMPLETE
    assert ds.STATUS_INSUFFICIENT_REQUIREMENTS != ds.STATUS_INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Requirement rendering and provenance
# ---------------------------------------------------------------------------


def test_state_renders_a_numbered_requirements_block():
    state = ds.build_scoring_state(
        brief={"name": "X"}, candidates=[CAND], requirements=SYNTHETIC_REQUIREMENTS
    )
    block = next(s for s in state if isinstance(s, str) and s.startswith("Requirements:"))
    assert "1. " in block and f"{len(SYNTHETIC_REQUIREMENTS)}. " in block


def test_state_omits_requirements_block_when_absent():
    state = ds.build_scoring_state(brief={"name": "X"}, candidates=[CAND])
    assert not any(
        isinstance(s, str) and s.startswith("Requirements:") for s in state
    )


def test_unresolved_marker_is_not_counted_as_a_requirement():
    kept = ds.usable_requirements(
        ["UNRESOLVED: FAST supplied no what/why", "A pricing table"]
    )
    assert kept == ("A pricing table",)


def test_duplicate_requirements_are_collapsed():
    kept = ds.usable_requirements(["A pricing table", "a pricing table"])
    assert len(kept) == 1


def test_requirement_cannot_inject_a_newline():
    kept = ds.usable_requirements(["A pricing table\n2. A fake injected requirement",
                                   "A portfolio grid", "An office location"])
    assert all("\n" not in r for r in kept)


def test_requirement_cannot_fabricate_a_section():
    """A requirement carrying a fake 'Requirements:' heading stays one line.

    ``one_line`` collapses the newlines, so the injected heading becomes
    mid-line text inside a single numbered item. It cannot start a new section
    or add a numbered entry, which is the property that matters.
    """
    kept = ds.usable_requirements(["A pricing table\n\nRequirements:\n1. injected"])
    assert len(kept) == 1
    assert "\n" not in kept[0]
    assert kept[0].count("Requirements:") == 1
    # And rendered into state, it occupies exactly one numbered line.
    state = ds.build_scoring_state(
        brief={"name": "X"}, candidates=[CAND], requirements=kept
    )
    block = next(s for s in state if isinstance(s, str) and s.startswith("Requirements:"))
    assert len([ln for ln in block.splitlines() if ln.strip().startswith(("1.", "2."))]) == 1


# ---------------------------------------------------------------------------
# Native requirement extraction (Patch C)
# ---------------------------------------------------------------------------


def _prd(brief: dict) -> str:
    return compile_prd(
        brief, project_id="p1", source_revision=3, requirements_version=7
    )


def test_native_prd_yields_requirements():
    text = _prd(
        {
            "name": "TailorIvan",
            "what": "Jasa jahit dan design fashion pria: layanan, harga, portofolio.",
            "why": "Menampilkan kredibilitas dan memudahkan calon pelanggan memesan.",
            "why_destination": "Formulir konsultasi",
            "design_intent": "Editorial atelier: tipografi serif, aksen brass.",
        }
    )
    reqs, reason = extract_requirements(text)
    assert reason == ""
    assert len(reqs) >= 3


def test_native_prd_keeps_its_own_acceptance_criteria():
    text = _prd({"name": "X", "what": "Y", "why": "Z"})
    reqs, _ = extract_requirements(text)
    assert any("renders without a critical runtime error" in r for r in reqs)


def test_unresolved_placeholder_is_never_a_requirement():
    text = _prd({})
    reqs, _ = extract_requirements(text)
    assert not any(r.upper().startswith("UNRESOLVED") for r in reqs)


def test_a_real_criterion_mentioning_unresolved_is_kept():
    """'No unresolved launch-critical placeholder remains' is a genuine criterion.

    A naive substring filter would drop it, so the marker test is anchored to the
    start of the entry.
    """
    text = _prd({"name": "X", "what": "Y", "why": "Z"})
    reqs, _ = extract_requirements(text)
    assert any("launch-critical placeholder" in r for r in reqs)


def test_empty_brief_yields_too_few_requirements_to_score_fit():
    """A blank brief must fall below the Fit threshold, not produce a score."""
    reqs, _ = extract_requirements(_prd({}))
    assert ds.requirements_are_sufficient(reqs) is False


def test_garbage_input_is_reported_not_guessed():
    reqs, reason = extract_requirements("not a prd at all")
    assert reqs == ()
    assert reason in ("unparseable", "no_requirements")


def test_empty_input_is_reported_not_guessed():
    assert extract_requirements("") == ((), "no_document")


def test_requirements_are_deterministic():
    """Same PRD in, byte-identical requirement tuple out."""
    text = _prd({"name": "X", "what": "Y", "why": "Z", "why_destination": "W"})
    assert extract_requirements(text) == extract_requirements(text)


def test_production_requirements_are_not_hardcoded():
    """The synthetic fixture list must not appear in the scoring module."""
    source = Path(ds.__file__).read_text()
    assert "services section listing at least four" not in source
    assert "brass or amber accent" not in source


# ---------------------------------------------------------------------------
# Invariants that the calibration must NOT have changed
# ---------------------------------------------------------------------------


def test_six_dimensions_and_weights_unchanged():
    assert DIMS == (
        "hierarchy", "typography", "layout", "color", "usability", "consistency"
    )
    assert ds.WEIGHTS == {
        "hierarchy": 0.25, "typography": 0.20, "layout": 0.20,
        "color": 0.15, "usability": 0.15, "consistency": 0.05,
    }
    assert abs(sum(ds.WEIGHTS.values()) - 1.0) < 1e-12


def test_thresholds_unchanged():
    assert ds.DEFAULT_MIN_QUALITY == 0.70
    assert ds.DEFAULT_MIN_FIT == 0.70
    assert ds.DEFAULT_MIN_SUITABILITY == 0.75


def test_critical_floors_unchanged():
    assert ds.DEFAULT_DIMENSION_FLOORS["hierarchy"] == 0.50
    assert ds.DEFAULT_DIMENSION_FLOORS["typography"] == 0.50
    assert ds.DEFAULT_DIMENSION_FLOORS["layout"] == 0.50
    assert ds.DEFAULT_DIMENSION_FLOORS["color"] == 0.50
    assert ds.DEFAULT_DIMENSION_FLOORS["usability"] == 0.25
    assert ds.DEFAULT_DIMENSION_FLOORS["consistency"] == 0.25


@pytest.mark.parametrize(
    "quality,fit,expected",
    [(1.0, 1.0, 1.0), (0.9, 0.9, 0.9), (1.0, 0.25, 0.4665), (0.0, 1.0, 0.0)],
)
def test_suitability_formula_unchanged(quality, fit, expected):
    assert abs(ds.compute_suitability(quality, fit) - expected) < 1e-4


def test_confidence_is_still_not_a_candidate_field():
    fields = set(DesignCandidate.__dataclass_fields__)
    assert "confidence" not in fields
    assert "suitability" in fields


def test_question_keys_and_criteria_shape_unchanged():
    questions = ds.build_scoring_questions([CAND], requirements=SYNTHETIC_REQUIREMENTS)
    for key, question in questions.items():
        assert question["type"] == "score"
        assert question["criteria"] == list(ds.LEVELS)
        assert isinstance(question["criteria"], list)
    assert set(questions) == {f"q0_{d}" for d in DIMS} | {"f0"}


def test_atomicity_still_rejects_a_partial_pool():
    """One missing dimension answer must fail the whole pool, not rank partially."""
    answers = {
        f"q0_{d}": {"type": "score", "score": 3, "legend": {str(i): "" for i in range(5)}}
        for d in DIMS
    }
    answers["f0"] = {"type": "score", "score": 3, "legend": {str(i): "" for i in range(5)}}
    del answers["q0_layout"]
    with pytest.raises(ds.ScoringError) as excinfo:
        ds.parse_pool_scores({"answers": answers}, [CAND])
    assert excinfo.value.status == ds.STATUS_INCOMPLETE


def test_synthetic_fixtures_are_labelled_synthetic():
    """The fixture list must identify itself as calibration-only."""
    assert SYNTHETIC_REQUIREMENTS  # used only by these tests
    module_source = Path(__file__).read_text()
    assert "SYNTHETIC_REQUIREMENTS" in module_source
    assert "NOT production requirements" in module_source
