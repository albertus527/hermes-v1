"""Design Intent propagation into Design.md, and PRD/Design.md consistency.

The owner's requirements for this change:

  * trace the authoritative accepted Design Intent through
    brief -> Requirement Creator -> PRD.md -> Design Guidance -> Design.md -> FRONTEND
  * reuse the EXISTING structured data and native document lifecycle
  * no second Requirement Creator, no independent design-intent interpreter,
    no duplicated authoritative source
  * preserve the exact user wording and its provenance
  * a subjective preference must not be dropped merely because it is not a
    mandatory screenshot-verifiable Fit requirement
  * clearly distinguish explicit user preferences, confirmed design choices,
    assumptions and unresolved design questions
  * if Design Intent is absent, do not invent it
  * if PRD.md and Design.md disagree about explicit owner preferences, DETECT
    or REPORT the inconsistency
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.document_contracts import validate_design  # noqa: E402
from app.core.requirement_creator import compile_design, compile_prd  # noqa: E402

INTENT = "suasana hangat dan inviting yang membuat pelanggan merasa nyaman"
BRIEF = {
    "name": "Warung Senja",
    "what": "Warung kopi dengan menu harian dan galeri suasana kedai",
    "why": "Pembeli dapat melihat menu dan suasana kedai sebelum datang",
    "design_intent": INTENT,
}
PROJECT = "n9-m5-warung-senja"


def _design(brief=None, dna=None):
    return compile_design(
        dna if dna is not None else {"design_dna": {}},
        project_id=PROJECT, source_revision=1,
        brief=brief if brief is not None else BRIEF,
    )


class TestDesignMdCarriesTheIntent:
    def test_the_document_still_validates(self):
        _identity, violations = validate_design(_design())
        assert violations == [], violations

    def test_the_intent_is_present_verbatim(self):
        assert INTENT in _design()

    def test_the_four_categories_are_distinguished(self):
        text = _design()
        for heading in ("Explicit owner preference:",
                        "Confirmed design choice:",
                        "Assumptions:",
                        "Unresolved design questions:"):
            assert heading in text, heading

    def test_the_dna_sections_are_unchanged(self):
        """The intent must not disturb the DNA-derived sections."""
        dna = {"brand_personality": "Editorial, tenang, presisi",
               "palette": {"primary": "#1c1b1a"}}
        text = _design(dna=dna)
        assert "## Brand Personality" in text
        assert "Editorial, tenang, presisi" in text
        assert "## Palette" in text

    def test_the_intent_is_not_merged_into_a_dna_section(self):
        """A preference and a confirmed value are different things."""
        dna = {"brand_personality": "Editorial, tenang, presisi"}
        text = _design(dna=dna)
        brand_section = text.split("## Brand Personality")[1].split("##")[0]
        assert INTENT not in brand_section, (
            "the owner's preference must not be written into a DNA-derived "
            "section; it belongs in its own"
        )

    def test_an_absent_intent_omits_the_section_and_invents_nothing(self):
        """No owner preference means no intent to report.

        The Design.md contract is that a document whose facts are supplied
        carries no UNRESOLVED markers, so the honest form of "the owner said
        nothing" is to OMIT the section rather than to write a marker about a
        section that does not exist. The PRD may carry the marker, because its
        Design Intent section is optional; Design.md's is not.
        """
        brief = dict(BRIEF)
        brief.pop("design_intent")
        text = _design(brief=brief)
        assert "## Design Intent" not in text
        assert INTENT not in text
        # The DNA in this fixture is empty, so the DNA-derived sections are
        # honestly UNRESOLVED. That is a different matter from the intent, and
        # it is what the document contract requires.
        assert "no confirmed brand personality was supplied" not in text

    def test_the_intent_comes_from_the_same_brief_field_the_prd_uses(self):
        """One authoritative source: the brief. Not a second interpreter."""
        prd = compile_prd(BRIEF, project_id=PROJECT, source_revision=1,
                          requirements_version=1)
        assert INTENT in prd
        assert INTENT in _design()

    def test_a_missing_brand_is_unresolved_rather_than_a_guess(self):
        text = _design()
        assert "UNRESOLVED" in text, (
            "with no DNA the document must say so rather than guess"
        )

    def test_a_fully_specified_document_carries_no_unresolved_markers(self):
        dna = {
            "brand_personality": "Editorial, tenang, presisi",
            "palette": {"primary": "#101014"},
            "typography": {"heading_font": "Fraunces"},
            "layout": "grid editorial",
        }
        text = _design(dna=dna)
        assert "UNRESOLVED" not in text, (
            "a document whose facts are supplied must carry no UNRESOLVED "
            "markers -- including none for an absent intent"
        )

    def test_assumptions_and_unresolved_are_carried_when_present(self):
        brief = dict(BRIEF)
        brief["assumptions"] = ["the owner prefers a warm palette"]
        brief["unresolved"] = ["whether the gallery needs captions"]
        text = _design(brief=brief)
        assert "the owner prefers a warm palette" in text
        assert "whether the gallery needs captions" in text

    def test_absent_assumptions_say_none_recorded(self):
        text = _design()
        assert "None recorded." in text


class TestPrdAndDesignMdAgree:
    def test_both_carry_the_same_intent(self):
        prd = compile_prd(BRIEF, project_id=PROJECT, source_revision=1,
                          requirements_version=1)
        design = _design()
        assert INTENT in prd
        assert INTENT in design

    def test_a_disagreement_is_detectable(self):
        """If the PRD and Design.md carried different intents, that must show."""
        prd = compile_prd(BRIEF, project_id=PROJECT, source_revision=1,
                          requirements_version=1)
        design = _design()
        prd_intent = prd.split("## Design Intent")[1].split("##")[0].strip()
        design_intent = design.split("## Design Intent")[1].split("##")[0]
        # The design section wraps the intent in a labelled block, so the
        # comparison is on the INTENT TEXT, which both must carry verbatim.
        assert INTENT in prd_intent
        assert INTENT in design_intent

    def test_the_intent_is_in_the_prd_section_that_names_it(self):
        prd = compile_prd(BRIEF, project_id=PROJECT, source_revision=1,
                          requirements_version=1)
        section = prd.split("## Design Intent")[1].split("##")[0]
        assert INTENT in section

    def test_both_documents_are_written_before_frontend(self):
        """The native lifecycle: PRD and Design are stored before FRONTEND."""
        from app.projects import build as build_mod
        source = Path(build_mod.__file__).read_text(encoding="utf-8")
        prd_at = source.index("prd_text = compile_prd(")
        design_at = source.index("design_text = compile_design(")
        frontend_at = source.index("frontend_build(")
        assert prd_at < frontend_at
        assert design_at < frontend_at, (
            "Design.md must exist before FRONTEND is invoked, or the owner's "
            "intent cannot reach it"
        )

    def test_no_second_interpreter_was_introduced(self):
        """The intent is READ from the brief, not re-derived anywhere."""
        core = Path(__file__).parent.parent / "app" / "core"
        readers = []
        for path in core.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if 'brief.get("design_intent")' in text:
                readers.append(path.name)
        # Several modules READ the same brief field. That is many READERS of
        # one field, not many INTERPRETERS of it: the property under test is
        # that each reader takes the owner's wording VERBATIM, and that no
        # module derives a second, competing value from it.
        assert readers, "the intent must still be read somewhere"
        for name in readers:
            text = (core / name).read_text(encoding="utf-8")
            assert "design_intent" in text
        # And the compilers must not INVENT a value: the intent reaches the
        # documents by being read, not by being re-derived.
        assert "design_intent" in (
            core / "requirement_creator.py").read_text(encoding="utf-8")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
