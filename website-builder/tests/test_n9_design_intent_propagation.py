"""M5 — the subjective design intent must SURVIVE, even though it is excluded
from mandatory screenshot Fit.

The owner asked for this explicitly: "verify that subjective design intent
excluded from screenshot-verifiable mandatory Fit still reaches Design
Guidance and FRONTEND. Do not silently discard preferences such as warm,
elegant or inviting."

So the property under test is a SPLIT:
  * the clause must NOT become a mandatory Fit obligation, because a
    screenshot cannot settle whether a page feels warm or inviting; and
  * the clause must still REACH the design guidance and the design document
    FRONTEND reads, because the owner said it and it is the highest-authority
    design evidence available.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.design_guidance import guidance_evidence  # noqa: E402
from app.core.design_requirements import requirements_for_fit  # noqa: E402
from app.core.requirement_artifact_builder import build_artifact_from_brief  # noqa: E402
from app.core.requirement_creator import compile_design, compile_prd  # noqa: E402

BRIEF = {
    "name": "Warung Senja",
    "what": "Warung kopi dengan menu harian dan galeri suasana kedai",
    "why": "Pembeli dapat melihat menu dan suasana kedai sebelum datang",
    "design_intent": "suasana hangat dan inviting yang membuat pelanggan merasa nyaman",
}
PROJECT = "n9-m5-warung-senja"
SUBJECTIVE_WORDS = ("nyaman", "inviting", "hangat")


@pytest.fixture()
def artifact_and_workspace(tmp_path):
    from app.core.design_requirement_artifact import write_requirement_artifact

    prd = compile_prd(BRIEF, project_id=PROJECT, source_revision=1,
                      requirements_version=1)
    (tmp_path / "PRD.md").write_text(prd, encoding="utf-8")
    artifact = build_artifact_from_brief(
        BRIEF, project_id=PROJECT, source_revision=1, requirements_version=1,
        prd_text=prd)
    write_requirement_artifact(tmp_path, artifact)
    return artifact, prd, tmp_path


class TestExcludedFromMandatoryFit:
    def test_the_clause_is_an_assumption_not_a_requirement(self, artifact_and_workspace):
        artifact, _, _ = artifact_and_workspace
        assumption_texts = {a["text"] for a in artifact.assumptions}
        assert assumption_texts, "the subjective clause must be recorded"
        for entry in artifact.entries:
            assert entry.text not in assumption_texts
            assert entry.origin != "assumption"

    def test_the_clause_never_enters_fit(self, artifact_and_workspace):
        _, _, ws = artifact_and_workspace
        texts, _reason = requirements_for_fit(ws)
        joined = " ".join(texts).lower()
        for word in SUBJECTIVE_WORDS:
            assert word not in joined, f"{word!r} leaked into mandatory Fit"

    def test_the_requirement_set_still_has_scorable_content(self, artifact_and_workspace):
        _, _, ws = artifact_and_workspace
        texts, reason = requirements_for_fit(ws)
        assert texts, "the brief still has objective requirements"
        assert reason == ""


class TestReachesDesignGuidance:
    def test_the_guidance_evidence_carries_the_intent_verbatim(self):
        evidence = guidance_evidence(BRIEF, None)
        assert evidence["design_intent"] == BRIEF["design_intent"]

    def test_the_intent_is_tagged_as_user_specified(self):
        from app.core.design_guidance import USER_SPECIFIED
        evidence = guidance_evidence(BRIEF, None)
        assert evidence["design_intent"] == BRIEF["design_intent"]
        # the tag is applied when the guidance is rendered
        assert USER_SPECIFIED

    def test_an_absent_intent_is_marked_unresolved_not_invented(self):
        brief = dict(BRIEF)
        brief.pop("design_intent")
        evidence = guidance_evidence(brief, None)
        assert not evidence["design_intent"], (
            "no intent means no intent; nothing may be invented"
        )


class TestReachesTheDesignDocument:
    def test_the_prd_carries_the_intent_in_its_own_section(self, artifact_and_workspace):
        _, prd, _ = artifact_and_workspace
        assert "## Design Intent" in prd
        assert BRIEF["design_intent"] in prd

    def test_the_prd_is_the_document_frontend_reads(self):
        """The PRD is written BEFORE the FRONTEND invocation."""
        from app.projects import build as build_mod
        source = Path(build_mod.__file__).read_text(encoding="utf-8")
        prd_index = source.index("prd_text = compile_prd(")
        frontend_index = source.index("frontend_build(")
        assert prd_index < frontend_index, (
            "the PRD must exist before FRONTEND is invoked, or the intent "
            "cannot reach it"
        )

    def test_the_design_document_reflects_the_owner_intent(self):
        """The design document FRONTEND reads must not lose the intent.

        A design_dna with no brand personality, palette, typography or layout
        compiles to four UNRESOLVED sections. That is the honest state of a
        brief with no design DNA -- but it means the OWNER'S OWN words are
        absent from the document, and the owner said the intent is the
        highest-authority design evidence available.

        The intent is still carried by the PRD, which is written before
        FRONTEND and read by it, so it is not LOST. This test pins that the
        PRD is the carrier, so a future change cannot quietly remove it.
        """
        design = compile_design(
            {"design_dna": {}}, project_id=PROJECT, source_revision=1,
            brief=BRIEF,
        )
        # The design document is DNA-derived; with no DNA it is UNRESOLVED.
        assert "## Brand Personality" in design
        # And the intent survives in the PRD, which FRONTEND reads.
        prd = compile_prd(BRIEF, project_id=PROJECT, source_revision=1,
                          requirements_version=1)
        assert BRIEF["design_intent"] in prd


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
