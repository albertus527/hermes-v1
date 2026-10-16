"""V3-A regression tests — Design Guidance authoring & retrieval quality.

WHY
---
V2-A/B preserved the owner's design intent end-to-end, but the pre-build
``DesignGuidance.md`` then repeated the SAME raw directive across Composition,
Typography, and Color, with no implementable guidance. V3-A turns the preserved
intent into distinct, actionable, provenance-tagged guidance and steers
retrieval with the owner's aesthetic.

These tests lock in the V3-A contract, using the ORIGINAL Forma Atelier brief as
an isolated fixture:

* the owner's directive stays VERBATIM in its own section;
* Composition / Typography / Color are DISTINCT and actionable (no raw repeat);
* recommendations are specific to an architecture-studio landing page;
* the colour recommendation preserves the off-white / charcoal / terracotta
  ROLES without inventing a fixed hex;
* typography keeps its elegant editorial character;
* no invented projects, clients, services, or business claims;
* no generic directory descriptor appears as a design instruction;
* an irrelevant retrieved resource never overrides the user's intent;
* confidence / provenance / fallback states stay accurate;
* old projects without an explicit design intent still compile;
* the retrieval query carries domain + aesthetic.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.decision_engine import DesignDecision  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_guidance import (  # noqa: E402
    DESIGN_RECOMMENDED,
    REFERENCE_DERIVED,
    UNRESOLVED_MARKER,
    USER_SPECIFIED,
    compile_design_guidance,
)
from app.core.design_intent import (  # noqa: E402
    clauses_by_axis,
    recommendations,
    retrieval_query,
    split_clauses,
)
from app.core.document_contracts import (  # noqa: E402
    extract_sections,
    parse_front_matter,
    validate_design_guidance,
)

# The owner's ORIGINAL directive, verbatim, in their own language.
OWNER_INTENT = (
    "Gaya desain editorial minimalis, premium, dan modern. Gunakan tipografi "
    "elegan, warna off-white, charcoal, serta aksen terracotta."
)
BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur",
    "why": (
        "Menampilkan studio secara profesional: hero, showcase 3 proyek "
        "arsitektur, tentang studio, layanan, dan kontak agar pengunjung "
        "memahami karya dan menghubungi studio"
    ),
    "design_intent": OWNER_INTENT,
}
BRIEF_NO_INTENT = {k: v for k, v in BRIEF.items() if k != "design_intent"}

# The REAL boilerplate descriptor that reached Composition in V1.
BOILERPLATE = (
    "This directory contains a craft details guide for frontend polish, "
    "focused on implementation patterns that AI models commonly miss."
)
INFORMATIVE = "editorial grid, asymmetric layout, generous whitespace, smooth scroll"


def _sections(text):
    _, body = parse_front_matter(text)
    return extract_sections(body)


def _candidate(descriptor=BOILERPLATE, **over):
    base = dict(
        candidate_id="refero_craft_details",
        label="refero_craft_details",
        descriptor=descriptor,
        palette=(),
        source_uri=(
            "viking://resources/website-builder/projects/wb-design/"
            "components/refero_craft_details"
        ),
        source_revision="bc6b016a3ff3",
        score=0.9,
    )
    base.update(over)
    return DesignCandidate(**base)


def _decision(selected):
    ranked = (selected,) if selected else ()
    return DesignDecision(
        selected=selected, ranked=ranked, abstained=selected is None,
        abstain_reason="" if selected else "no_candidates",
        source="provider", provider="openviking",
    )


class ClauseRoutingTests(unittest.TestCase):
    """The owner's directive is split into clauses and routed by facet."""

    def test_clauses_are_verbatim_substrings(self):
        for clause in split_clauses(OWNER_INTENT):
            self.assertIn(clause, OWNER_INTENT)

    def test_typography_clause_routes_only_to_typography(self):
        by = clauses_by_axis(OWNER_INTENT)
        joined = " ".join(by["typography"]).lower()
        self.assertIn("tipografi", joined)
        # The colour sentence does not leak into typography.
        self.assertNotIn("terracotta", joined)

    def test_color_clauses_route_only_to_color(self):
        by = clauses_by_axis(OWNER_INTENT)
        joined = " ".join(by["color"]).lower()
        self.assertIn("off-white", joined)
        self.assertIn("charcoal", joined)
        self.assertIn("terracotta", joined)
        self.assertNotIn("tipografi", joined)


class GuidanceDistinctnessTests(unittest.TestCase):
    """The V3-A defect: the raw directive repeated in three sections."""

    def setUp(self):
        self.g = compile_design_guidance(BRIEF, _decision(_candidate()),
                                         project_id="p", source_revision=1)
        self.s = _sections(self.g)

    def test_owner_directive_is_verbatim_in_its_own_section(self):
        self.assertIn(OWNER_INTENT, self.s["design_intent"])
        self.assertIn(USER_SPECIFIED, self.s["design_intent"])

    def test_raw_directive_is_not_repeated_in_facets(self):
        for facet in ("composition", "typography", "color"):
            self.assertNotIn(OWNER_INTENT, self.s[facet])

    def test_each_facet_is_distinct(self):
        self.assertNotEqual(self.s["composition"], self.s["typography"])
        self.assertNotEqual(self.s["typography"], self.s["color"])
        self.assertNotEqual(self.s["composition"], self.s["color"])

    def test_composition_is_actionable_and_context_specific(self):
        c = self.s["composition"]
        self.assertIn("studio arsitektur", c)          # brief's own domain
        self.assertIn("editorial sequence", c)         # an implementable mechanism
        self.assertIn("whitespace", c)                 # from "minimalis"
        self.assertIn(DESIGN_RECOMMENDED, c)

    def test_typography_is_actionable_and_preserves_editorial_character(self):
        t = self.s["typography"]
        self.assertIn("display face", t)
        self.assertIn("body text", t)
        self.assertIn(DESIGN_RECOMMENDED, t)
        # The owner's own elegant-typography clause is carried verbatim.
        self.assertIn(USER_SPECIFIED, t)
        self.assertIn("tipografi elegan", t.lower())

    def test_color_preserves_roles_without_a_fixed_hex(self):
        c = self.s["color"]
        self.assertIn("off-white as surface / background", c)
        self.assertIn("charcoal as primary text / ink", c)
        self.assertIn("terracotta as accent", c)
        # No fabricated hex colour is introduced anywhere (markdown headings
        # use '#', so look for an actual hex token).
        import re
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", self.g))


class ProvenanceSeparationTests(unittest.TestCase):
    def test_three_classes_are_distinguishable(self):
        g = compile_design_guidance(BRIEF, _decision(_candidate(INFORMATIVE)),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        self.assertIn(USER_SPECIFIED, s["typography"])
        self.assertIn(DESIGN_RECOMMENDED, s["typography"])
        self.assertIn(REFERENCE_DERIVED, s["composition"])
        self.assertIn("not user-specified", REFERENCE_DERIVED)

    def test_boilerplate_directory_descriptor_is_not_composition(self):
        g = compile_design_guidance(BRIEF, _decision(_candidate(BOILERPLATE)),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        self.assertNotIn("craft details guide", s["composition"])
        self.assertNotIn("This directory", s["composition"])
        # ...its provenance is still recorded.
        self.assertIn("refero_craft_details@bc6b016a3ff3", s["approved_resources"])

    def test_irrelevant_resource_never_overrides_user_intent(self):
        # A descriptor that contradicts the owner (a loud, high-contrast look)
        # must not replace the owner's own clause.
        conflicting = "loud maximalist high-contrast bold neon editorial grid"
        g = compile_design_guidance(BRIEF, _decision(_candidate(conflicting)),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        # The owner's clause is present and tagged user-specified...
        self.assertIn("editorial minimalis", s["composition"].lower())
        self.assertIn(USER_SPECIFIED, s["composition"])
        # ...and the conflicting reference is offered only as a lower-authority
        # pattern, never as the owner's own instruction.
        self.assertIn(REFERENCE_DERIVED, s["composition"])


class NoFabricationTests(unittest.TestCase):
    def test_no_invented_business_facts(self):
        g = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1).lower()
        # No concrete fabricated fact: a named client, an award, a founding
        # year, or contact details. (The anti-fabrication *prohibition* itself
        # may mention these words, so we test for a CLAIM, not a bare word.)
        for fabricated in (
            "our clients include", "award-winning", "founded in",
            "since 19", "since 20", "+62", "@gmail", "@example",
        ):
            self.assertNotIn(fabricated, g)
        # A concrete project/client name the brief never supplied.
        for invented_name in ("proyek abc", "client xyz", "pt ", "studio kami adalah"):
            self.assertNotIn(invented_name, g)

    def test_no_hardcoded_recipe(self):
        g = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1).lower()
        for forbidden in ("70/20/10", "#a8674a", "mandatory hero", "must use 3",
                          "must include 3", "exactly three"):
            self.assertNotIn(forbidden, g)

    def test_recommendations_never_emit_a_fixed_hex(self):
        recs = recommendations(BRIEF, OWNER_INTENT)
        for value in recs.values():
            self.assertNotIn("#", value)


class DegradedAndFallbackTests(unittest.TestCase):
    def test_abstention_keeps_owner_intent_and_is_honest(self):
        g = compile_design_guidance(BRIEF, _decision(None),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        self.assertIn(OWNER_INTENT, s["design_intent"])
        self.assertNotIn("viking://", g)
        self.assertIn(UNRESOLVED_MARKER, s["approved_resources"])
        # Grounded recommendations still appear even with no retrieval.
        self.assertIn(DESIGN_RECOMMENDED, s["composition"])
        self.assertIn(DESIGN_RECOMMENDED, s["color"])

    def test_old_brief_without_intent_still_compiles(self):
        g = compile_design_guidance(BRIEF_NO_INTENT, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        self.assertEqual(validate_design_guidance(g)[1], [])
        s = _sections(g)
        self.assertIn(UNRESOLVED_MARKER, s["design_intent"])
        # Nothing about the owner is invented.
        self.assertNotIn("terracotta", g.lower())
        self.assertNotIn("editorial", g.lower())


class RetrievalQueryTests(unittest.TestCase):
    def test_query_carries_domain_and_aesthetic(self):
        q = retrieval_query(BRIEF)
        self.assertIn("Forma Atelier", q)
        self.assertIn("studio arsitektur", q)
        self.assertIn("terracotta", q.lower())  # the intended aesthetic
        self.assertIn("editorial", q.lower())

    def test_query_without_intent_is_domain_only(self):
        q = retrieval_query(BRIEF_NO_INTENT)
        self.assertIn("Forma Atelier", q)
        self.assertNotIn("terracotta", q.lower())


class LifecycleAndContractTests(unittest.TestCase):
    def test_document_validates_and_is_deterministic(self):
        a = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        b = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        self.assertEqual(a, b)
        self.assertEqual(validate_design_guidance(a)[1], [])

    def test_frontend_receives_guidance_with_resource_content(self):
        """The guidance reaches FRONTEND, carrying resource content + provenance."""
        g = compile_design_guidance(BRIEF, _decision(_candidate(INFORMATIVE)),
                                    project_id="p", source_revision=1)
        self.assertIn("refero_craft_details@bc6b016a3ff3", g)
        self.assertIn("asymmetric layout", g)  # the reference's real content


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
