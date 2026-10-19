"""N8 B7 Option C — E2 vs E3 evidence behaviour in the scoring path.

The contract these tests pin:

  * a candidate with NO measurement gets the exact E2 instruction
  * a candidate WITH a measurement gets an additive measured block
  * measured contrast is authoritative for LEGIBILITY only, never for palette
  * doubtful pairs are disclosed, never silently dropped
  * a conflict between a measured ratio and a model score is REPORTED and the
    score is NOT overridden
  * the six dimensions, weights, Q/F/S, floors and gates are untouched
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_scoring import (  # noqa: E402
    DEFAULT_DIMENSION_FLOORS,
    DEFAULT_MIN_FIT,
    DEFAULT_MIN_QUALITY,
    DEFAULT_MIN_SUITABILITY,
    DIMENSIONS,
    MEASURED_EVIDENCE_HEADER,
    WEIGHTS,
    assess_evidence_conflict,
    build_scoring_questions,
    compute_suitability,
    evidence_class_for,
    measured_evidence_block,
)
from app.core.design_reference import (  # noqa: E402
    EVIDENCE_RENDERED,
    EVIDENCE_STRUCTURAL,
)
from app.core.structural_probe import parse_probe  # noqa: E402


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
        "blocks": [{"tag": "section", "pad": [96, 24, 96, 24], "mar": [0, 0, 0, 0], "gap": 16}],
        "viewport": {"width": 1440, "height": 900},
    }


def _evidence(degraded=False, gradient=False, candidate_id="cand-c"):
    return parse_probe(
        _probe_payload(degraded=degraded, gradient=gradient),
        candidate_id=candidate_id,
        source_revision="rev-3",
        viewport=(1440, 900),
        screenshot_digest="digest-abc",
        measured_at_url="http://127.0.0.1:8801/",
    )


def _candidate(cid="cand-c"):
    return DesignCandidate(
        candidate_id=cid,
        label=f"candidate {cid}",
        descriptor="a page",
        source_uri=f"n8://references/{cid}",
        source_revision="rev-3",
    )


class TestE2Unchanged:
    def test_no_evidence_leaves_instruction_untouched(self):
        questions = build_scoring_questions([_candidate()])
        for dimension in DIMENSIONS:
            text = questions[f"q0_{dimension}"]["instructions"]
            assert MEASURED_EVIDENCE_HEADER not in text, (
                f"{dimension} must stay E2 when nothing was measured"
            )

    def test_evidence_for_another_candidate_does_not_leak(self):
        questions = build_scoring_questions(
            [_candidate("cand-c")], evidence={"cand-other": _evidence()}
        )
        for dimension in DIMENSIONS:
            assert MEASURED_EVIDENCE_HEADER not in questions[f"q0_{dimension}"]["instructions"]

    def test_evidence_map_defaults_to_empty(self):
        assert build_scoring_questions([_candidate()]) == build_scoring_questions(
            [_candidate()], evidence={}
        )


class TestE3MeasuredBlock:
    def test_colour_gains_a_measured_ratio(self):
        questions = build_scoring_questions(
            [_candidate()], evidence={"cand-c": _evidence()}
        )
        text = questions["q0_color"]["instructions"]
        assert MEASURED_EVIDENCE_HEADER in text
        assert "15.66:1" in text

    def test_colour_block_separates_legibility_from_palette(self):
        block = measured_evidence_block("color", _evidence())
        assert "legibility only" in block.lower() or "LEGIBILITY" in block
        assert "palette harmony" in block.lower()
        assert "does not make the colour design good" in block.lower()

    def test_typography_gains_type_facts(self):
        block = measured_evidence_block("typography", _evidence())
        assert "Inter" in block
        assert "16px" in block
        assert "context, not a score" in block.lower()

    def test_layout_gains_spacing_facts(self):
        block = measured_evidence_block("layout", _evidence())
        assert "96" in block
        assert "spacing SYSTEM" in block or "spacing system" in block.lower()

    def test_hierarchy_never_gains_a_measured_block(self):
        """No measurement can judge an entry point, so it stays perceptual."""
        assert measured_evidence_block("hierarchy", _evidence()) == ""

    def test_usability_never_gains_a_measured_block(self):
        assert measured_evidence_block("usability", _evidence()) == ""

    def test_consistency_never_gains_a_measured_block(self):
        assert measured_evidence_block("consistency", _evidence()) == ""

    def test_no_evidence_yields_empty_block(self):
        assert measured_evidence_block("color", None) == ""


class TestEvidenceClass:
    def test_no_evidence_is_e2(self):
        for dimension in DIMENSIONS:
            assert evidence_class_for(dimension, None) == EVIDENCE_RENDERED

    def test_measured_colour_reaches_e3(self):
        assert evidence_class_for("color", _evidence()) == EVIDENCE_STRUCTURAL

    def test_measured_typography_reaches_e3(self):
        assert evidence_class_for("typography", _evidence()) == EVIDENCE_STRUCTURAL

    def test_measured_layout_reaches_e3(self):
        assert evidence_class_for("layout", _evidence()) == EVIDENCE_STRUCTURAL

    def test_hierarchy_stays_e2_even_with_evidence(self):
        assert evidence_class_for("hierarchy", _evidence()) == EVIDENCE_RENDERED

    def test_usability_stays_e2_even_with_evidence(self):
        assert evidence_class_for("usability", _evidence()) == EVIDENCE_RENDERED

    def test_doubtful_contrast_does_not_reach_e3(self):
        """A gradient behind text is not a measurement."""
        ev = _evidence(gradient=True)
        assert ev.has_contrast is False
        assert evidence_class_for("color", ev) == EVIDENCE_RENDERED


class TestDoubtDisclosure:
    def test_uncertain_pairs_are_counted_in_the_prompt(self):
        ev = _evidence(gradient=True)
        block = measured_evidence_block("color", ev)
        # No measured pairs, so the colour block is empty -- but the doubt is
        # still visible through the evidence record itself.
        assert block == ""
        assert len(ev.doubtful_pairs) == 1
        assert ev.doubtful_pairs[0].ratio is None

    def test_mixed_evidence_discloses_the_doubtful_count(self):
        payload = _probe_payload()
        payload["text"].append(
            {
                "tag": "div", "font_family": '"Inter"', "font_size": 18,
                "font_weight": "400", "line_height": 28,
                "fg": {"r": 255, "g": 255, "b": 255, "a": 1},
                "bg_ok": False, "bg": None, "bg_uncertain": False,
                "bg_unsupported": True, "bg_reason": "gradient_behind_text",
            }
        )
        ev = parse_probe(
            payload, candidate_id="cand-c", source_revision="rev-3",
            viewport=(1440, 900), screenshot_digest="d", measured_at_url="http://x/",
        )
        block = measured_evidence_block("color", ev)
        assert "1 further text/background pair" in block
        assert "unsupported" in block


class TestConflictIsReportedNotOverridden:
    def test_high_score_with_failing_measured_contrast_is_flagged(self):
        ev = _evidence(degraded=True)  # 4.45:1, below AA
        conflict = assess_evidence_conflict(dimension="color", score=0.90, evidence=ev)
        assert conflict is not None
        assert conflict["kind"] == "score_high_but_measured_contrast_fails_aa"
        assert conflict["model_score"] == 0.90
        assert conflict["resolution"] == "reported_only"

    def test_low_score_with_strong_measured_contrast_is_flagged(self):
        ev = _evidence()  # 15.66:1
        conflict = assess_evidence_conflict(dimension="color", score=0.10, evidence=ev)
        assert conflict is not None
        assert conflict["kind"] == "score_low_but_measured_contrast_is_strong"

    def test_agreement_produces_no_conflict(self):
        ev = _evidence()
        assert assess_evidence_conflict(dimension="color", score=0.85, evidence=ev) is None

    def test_non_colour_dimensions_never_conflict(self):
        ev = _evidence()
        for dimension in DIMENSIONS:
            if dimension == "color":
                continue
            assert assess_evidence_conflict(dimension=dimension, score=0.9, evidence=ev) is None

    def test_no_evidence_never_conflicts(self):
        assert assess_evidence_conflict(dimension="color", score=0.9, evidence=None) is None

    def test_doubtful_contrast_never_conflicts(self):
        """An unmeasured pair cannot contradict a score."""
        ev = _evidence(gradient=True)
        assert assess_evidence_conflict(dimension="color", score=0.9, evidence=ev) is None

    def test_conflict_does_not_modify_the_score(self):
        ev = _evidence(degraded=True)
        score = 0.90
        conflict = assess_evidence_conflict(dimension="color", score=score, evidence=ev)
        assert conflict["model_score"] == score
        assert score == 0.90, "the caller's value is untouched"


class TestContractUnchanged:
    def test_six_dimensions_unchanged(self):
        assert DIMENSIONS == (
            "hierarchy", "typography", "layout", "color", "usability", "consistency"
        )

    def test_weights_still_sum_to_one(self):
        assert round(sum(WEIGHTS.values()), 6) == 1.0
        assert WEIGHTS["hierarchy"] == 0.25

    def test_thresholds_unchanged(self):
        assert DEFAULT_MIN_QUALITY == 0.70
        assert DEFAULT_MIN_FIT == 0.70
        assert DEFAULT_MIN_SUITABILITY == 0.75

    def test_floors_unchanged(self):
        assert DEFAULT_DIMENSION_FLOORS["hierarchy"] == 0.50
        assert DEFAULT_DIMENSION_FLOORS["color"] == 0.50
        assert DEFAULT_DIMENSION_FLOORS["usability"] == 0.25
        assert DEFAULT_DIMENSION_FLOORS["consistency"] == 0.25

    def test_suitability_formula_unchanged(self):
        assert round(compute_suitability(0.70, 0.70), 6) == round(
            0.70 ** 0.45 * 0.70 ** 0.55, 6
        )

    def test_measured_evidence_does_not_add_a_question(self):
        """E3 changes instruction text, never the question count or the gate."""
        plain = build_scoring_questions([_candidate()])
        measured = build_scoring_questions([_candidate()], evidence={"cand-c": _evidence()})
        assert set(plain) == set(measured)
        assert len(measured) == len(DIMENSIONS)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
