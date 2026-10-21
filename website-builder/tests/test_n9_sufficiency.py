"""N9 sufficiency policy — the behaviour the owner asked to be pinned.

The policy is EVIDENCE-BASED: a requirement set is sufficient when it carries
at least one specific, screenshot-verifiable requirement. It is not a count.

These tests pin the behaviour across BOTH call sites, because the two used to
disagree: the provider judged sufficiency by evidence while the question
builder judged it by count, so a brief with fewer than three requirements
produced a response with no Fit answer at all -- a Fit that was ABSENT rather
than a Fit that was honestly zero.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.design_scoring import (  # noqa: E402
    MIN_FIT_REQUIREMENTS,
    assess_requirement_sufficiency,
    build_scoring_questions,
    build_scoring_state,
    requirements_are_sufficient,
    usable_requirements,
)
from app.core.decision_provider import DesignCandidate  # noqa: E402


def _candidate(cid="c1"):
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="lib://x", source_revision="1", score=0.5,
    )


ONE = "A three-column product grid on the desktop viewport"
TWO = "Album covers shown as cards in the catalogue"
TEMPLATE = "Primary call-to-action is reachable from the landing page"


class TestEvidenceBasedSufficiency:
    def test_one_screenshot_verifiable_requirement_is_sufficient(self):
        sufficient, reason, usable, excluded = assess_requirement_sufficiency([ONE])
        assert sufficient is True
        assert reason == ""
        assert usable == (ONE,)
        assert excluded == ()

    def test_two_screenshot_verifiable_requirements_are_sufficient(self):
        sufficient, _, usable, _ = assess_requirement_sufficiency([ONE, TWO])
        assert sufficient is True
        assert len(usable) == 2

    def test_no_valid_visual_requirement_is_insufficient(self):
        sufficient, reason, usable, _ = assess_requirement_sufficiency([])
        assert sufficient is False
        assert reason
        assert usable == ()

    def test_template_boilerplate_alone_is_insufficient(self):
        """Compiler boilerplate must not make Fit look answerable."""
        sufficient, reason, _, excluded = assess_requirement_sufficiency(
            [TEMPLATE])
        assert sufficient is False
        assert reason
        assert excluded

    def test_the_legacy_count_gate_is_not_the_policy(self):
        """The count gate says False for one good requirement."""
        assert requirements_are_sufficient([ONE]) is False
        assert assess_requirement_sufficiency([ONE])[0] is True
        assert MIN_FIT_REQUIREMENTS == 3, (
            "the constant is kept for compatibility but is not the policy"
        )


class TestNoDuplicateSufficiencyRules:
    def test_the_question_builder_uses_the_evidence_policy(self):
        """ONE requirement must still produce a Fit question."""
        questions = build_scoring_questions([_candidate()], requirements=[ONE])
        assert "f0" in questions, "a Fit question must be emitted"

    def test_two_requirements_produce_a_fit_question(self):
        questions = build_scoring_questions([_candidate()], requirements=[ONE, TWO])
        assert "f0" in questions

    def test_no_requirement_produces_no_fit_question(self):
        questions = build_scoring_questions([_candidate()], requirements=[])
        assert "f0" not in questions, (
            "with nothing to judge, no Fit question may be asked -- asking it "
            "and discarding the answer would invite a fabricated score"
        )

    def test_the_provider_and_the_builder_agree(self):
        """The two call sites must not disagree about the same set."""
        for requirements in ([], [ONE], [ONE, TWO], [TEMPLATE],
                             [TEMPLATE, ONE]):
            policy = assess_requirement_sufficiency(requirements)[0]
            questions = build_scoring_questions([_candidate()],
                                               requirements=requirements)
            assert ("f0" in questions) is policy, (
                f"the provider and the question builder disagree about "
                f"{requirements!r}"
            )

    def test_the_legacy_gate_has_no_production_call_site(self):
        """It stays exported for compatibility, but nothing may call it."""
        core = Path(__file__).parent.parent / "app" / "core"
        callers = []
        for path in core.rglob("*.py"):
            if path.name == "design_scoring.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "requirements_are_sufficient(" in text:
                callers.append(path.name)
        assert callers == [], (
            f"the deprecated count gate is still called from {callers}"
        )


class TestMissingEvidenceDoesNotFabricateFit:
    def test_an_empty_set_produces_no_fit_question(self):
        questions = build_scoring_questions([_candidate()], requirements=())
        assert "f0" not in questions

    def test_a_set_of_only_unresolved_markers_produces_no_fit_question(self):
        questions = build_scoring_questions(
            [_candidate()], requirements=["UNRESOLVED: FAST supplied no what/why"])
        assert "f0" not in questions

    def test_the_state_carries_only_usable_requirements(self):
        state = build_scoring_state(
            brief={"name": "x", "what": ONE, "why": "y"},
            candidates=[_candidate()], requirements=[ONE, TEMPLATE])
        text = " ".join(str(part) for part in state)
        assert ONE in text
        assert TEMPLATE not in text, (
            "template boilerplate must not reach the model as a requirement"
        )

    def test_an_unresolvable_set_reports_a_reason(self):
        sufficient, reason, _, _ = assess_requirement_sufficiency([TEMPLATE])
        assert not sufficient
        assert reason


class TestLegacyAndSchema3StaySafe:
    def test_usable_requirements_drops_unresolved_and_duplicates(self):
        kept = usable_requirements(
            [ONE, ONE.lower(), "UNRESOLVED: nothing", "  "])
        assert kept == (ONE,)

    def test_the_question_count_is_unchanged_by_the_policy(self):
        """Six dimension questions always; the Fit question is conditional."""
        one = build_scoring_questions([_candidate()], requirements=[ONE])
        two = build_scoring_questions([_candidate()], requirements=[ONE, TWO])
        assert len(one) == len(two) == 7

    def test_a_legacy_caller_asking_for_the_count_gate_still_works(self):
        assert requirements_are_sufficient([ONE, TWO, "third requirement"]) is True

    def test_a_schema3_artifact_set_is_read_as_schema3(self):
        from app.core.design_requirement_artifact import (
            ARTIFACT_SCHEMA_VERSION,
            ARTIFACT_SCHEMA_VERSION_LEGACY,
            parse_artifact,
        )
        legacy = {
            "schema_version": 2, "project_id": "p", "source_revision": 1,
            "requirements_version": 1, "prd_digest": "",
            "entries": [{"id": "R1", "text": ONE, "kind": "content",
                         "evidence_kind": "visual", "verifiable_by": "screenshot",
                         "origin": "user_brief", "trace": "brief.what",
                         "mandatory": True, "discriminating": True}],
            "assumptions": [], "unresolved": [],
        }
        artifact = parse_artifact(legacy)
        assert artifact.schema_version == ARTIFACT_SCHEMA_VERSION_LEGACY
        assert artifact.entries[0].text == ONE
        assert artifact.entries[0].group_id == "", (
            "a legacy entry stands alone; it is not decomposed on read"
        )
        assert ARTIFACT_SCHEMA_VERSION == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
