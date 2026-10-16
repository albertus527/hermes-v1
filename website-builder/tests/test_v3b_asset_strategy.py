"""V3-B regression tests — honest asset strategy & missing-content handling.

WHY
---
The V1 audit showed the generated Forma Atelier site repeating three identical
placeholder project cards with generic names, because the pipeline had no
explicit, honest asset strategy: it neither requested owner material nor
produced visually intentional conceptual treatments, and it risked implying the
placeholders were real work.

V3-B adds an explicit asset strategy (``app/core/asset_strategy.py``) and carries
it to FRONTEND through the EXISTING pre-build DesignGuidance contract. These
tests lock in the four asset states, the licence boundary, missing-content
handling, and backward compatibility — using the ORIGINAL Forma Atelier brief.

No live model, browser, network, npm, or image download is required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.asset_strategy import (  # noqa: E402
    ASSET_CONCEPTUAL,
    ASSET_LICENSED_EDITORIAL,
    ASSET_MISSING,
    ASSET_OWNER_PROVIDED,
    ASSET_STATES,
    CONTENT_LAUNCH_CRITICAL,
    CONTENT_OPTIONAL,
    has_visual_content,
    is_verified_licensed_asset,
    owner_assets,
    plan_assets,
    showcase_item_count,
)
from app.core.decision_engine import DesignDecision  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_guidance import compile_design_guidance  # noqa: E402
from app.core.document_contracts import (  # noqa: E402
    extract_sections,
    parse_front_matter,
    validate_design_guidance,
)
from app.core.document_guidance import build_document_guidance  # noqa: E402
from app.core.requirement_creator import compile_prd  # noqa: E402

INTENT = (
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
    "design_intent": INTENT,
}
NON_PORTFOLIO = {
    "name": "Northcut",
    "what": "booking page for a barbershop",
    "why": "book an appointment",
}


def _candidate(**over):
    base = dict(
        candidate_id="refero_craft_details",
        label="refero_craft_details",
        descriptor="editorial grid, asymmetric layout, generous whitespace",
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


def _sections(text):
    _, body = parse_front_matter(text)
    return extract_sections(body)


# ---------------------------------------------------------------------------
# A — asset states
# ---------------------------------------------------------------------------

class AssetStateTests(unittest.TestCase):
    def test_four_states_are_explicit(self):
        self.assertEqual(
            set(ASSET_STATES),
            {ASSET_OWNER_PROVIDED, ASSET_LICENSED_EDITORIAL,
             ASSET_CONCEPTUAL, ASSET_MISSING},
        )

    def test_forma_atelier_is_conceptual_no_owner_assets(self):
        plan = plan_assets(BRIEF)
        self.assertEqual(plan["states"]["showcase"], ASSET_CONCEPTUAL)
        self.assertEqual(plan["owner_assets"], ())

    def test_owner_supplied_assets_take_priority(self):
        brief = dict(BRIEF)
        brief["why"] += " Foto proyek: https://cdn.example.com/projects/hero.jpg"
        plan = plan_assets(brief)
        self.assertEqual(plan["states"]["showcase"], ASSET_OWNER_PROVIDED)
        self.assertIn("https://cdn.example.com/projects/hero.jpg",
                      plan["owner_assets"])
        # The conceptual-CREATION instruction must NOT be issued when an asset
        # exists. (A prohibition mentioning the word is fine — the point is that
        # no instruction tells FRONTEND to invent a conceptual treatment.)
        self.assertFalse(
            any("create" in i.lower() and "conceptual" in i.lower()
                for i in plan["instructions"])
        )
        # ...and it explicitly says not to substitute a conceptual treatment.
        self.assertTrue(
            any("do not substitute" in i.lower() for i in plan["instructions"])
        )

    def test_verified_licensed_asset_is_used_as_editorial(self):
        licensed = [{
            "source_uri": "https://unsplash.com/photos/abc",
            "license": "CC0",
            "attribution": "Jane Doe / Unsplash",
        }]
        plan = plan_assets(BRIEF, licensed_assets=licensed)
        self.assertEqual(plan["states"]["showcase"], ASSET_LICENSED_EDITORIAL)
        self.assertEqual(len(plan["licensed_assets"]), 1)
        joined = " ".join(plan["instructions"]).lower()
        self.assertIn("editorial illustration", joined)
        self.assertIn("attribution", joined)

    def test_non_portfolio_site_is_not_forced_to_use_imagery(self):
        self.assertFalse(has_visual_content(NON_PORTFOLIO))
        plan = plan_assets(NON_PORTFOLIO)
        self.assertEqual(plan["states"], {})
        self.assertTrue(
            any("does not require imagery" in i for i in plan["instructions"])
        )

    def test_owner_assets_never_mistake_the_cta_destination(self):
        brief = {
            "name": "X", "what": "y", "why": "kontak",
            "why_destination": "https://wa.me/6281234567890",
        }
        self.assertEqual(owner_assets(brief), ())


# ---------------------------------------------------------------------------
# A4/C6 — the licence boundary
# ---------------------------------------------------------------------------

class LicenseVerificationTests(unittest.TestCase):
    def test_missing_fields_are_not_verified(self):
        for asset in (
            {},
            {"source_uri": "https://x/y.jpg"},
            {"source_uri": "https://x/y.jpg", "license": "CC0"},  # no attribution
            {"license": "CC0", "attribution": "A"},               # no source
        ):
            self.assertFalse(is_verified_licensed_asset(asset), asset)

    def test_unverified_licence_values_are_refused(self):
        for value in ("", "unknown", "unverified", "none", "N/A", "TBD", "?"):
            asset = {
                "source_uri": "https://x/y.jpg",
                "license": value,
                "attribution": "A",
            }
            self.assertFalse(is_verified_licensed_asset(asset), value)

    def test_unverified_asset_falls_back_to_conceptual(self):
        bad = [{"source_uri": "https://x/y.jpg", "license": "unknown",
                "attribution": "?"}]
        plan = plan_assets(BRIEF, licensed_assets=bad)
        self.assertEqual(plan["states"]["showcase"], ASSET_CONCEPTUAL)
        self.assertEqual(plan["licensed_assets"], ())

    def test_fully_verified_asset_is_accepted(self):
        good = {"source_uri": "https://x/y.jpg", "license": "CC-BY-4.0",
                "attribution": "Jane Doe"}
        self.assertTrue(is_verified_licensed_asset(good))


# ---------------------------------------------------------------------------
# B — missing-content handling
# ---------------------------------------------------------------------------

class MissingContentTests(unittest.TestCase):
    def test_launch_critical_is_surfaced_for_a_showcase_site(self):
        plan = plan_assets(BRIEF)
        self.assertTrue(plan["missing_launch_critical"])
        self.assertIn("showcase content",
                      " ".join(plan["missing_launch_critical"]))

    def test_optional_content_is_separated_from_launch_critical(self):
        plan = plan_assets(BRIEF)
        self.assertTrue(plan["missing_optional"])
        # Service/about prose is optional, never launch-critical.
        joined = " ".join(plan["missing_optional"]).lower()
        self.assertIn("service", joined)
        self.assertNotIn("showcase content", joined)

    def test_conceptual_instruction_discourages_repeated_cards(self):
        plan = plan_assets(BRIEF)
        joined = " ".join(plan["instructions"]).lower()
        self.assertIn("identical placeholder cards", joined)
        self.assertIn("vary", joined)
        # ...and it uses the brief's own stated count (3).
        self.assertIn("3 distinct", joined)

    def test_item_count_is_read_from_the_brief(self):
        self.assertEqual(showcase_item_count(BRIEF), 3)
        self.assertIsNone(showcase_item_count(NON_PORTFOLIO))

    def test_no_business_fact_is_invented(self):
        plan = plan_assets(BRIEF)
        blob = " ".join(
            list(plan["instructions"])
            + list(plan["missing_launch_critical"])
            + list(plan["missing_optional"])
        ).lower()
        for invented in ("proyek abc", "client xyz", "award", "founded in",
                         "testimonial", "since 19", "since 20"):
            self.assertNotIn(invented, blob)


# ---------------------------------------------------------------------------
# C — FRONTEND integration via the existing pre-build contract
# ---------------------------------------------------------------------------

class FrontendIntegrationTests(unittest.TestCase):
    def test_asset_strategy_reaches_frontend_via_design_guidance(self):
        guidance = compile_design_guidance(
            BRIEF, _decision(_candidate()), project_id="p", source_revision=1
        )
        self.assertIn("## Asset Strategy", guidance)
        self.assertIn(ASSET_CONCEPTUAL, guidance)
        self.assertIn(CONTENT_LAUNCH_CRITICAL, guidance)

        # ...and it survives into the bounded FRONTEND document-guidance block.
        prd = compile_prd(BRIEF, project_id="p", source_revision=1,
                          scope="WEBSITE", readiness="DISCOVERY_READY")
        block = build_document_guidance(prd, None, guidance)
        self.assertIn("## Asset Strategy", block)
        self.assertIn("CONCEPTUAL", block)
        self.assertIn("launch-critical", block)

    def test_asset_strategy_section_is_rendered_but_not_required(self):
        guidance = compile_design_guidance(
            BRIEF, _decision(_candidate()), project_id="p", source_revision=1
        )
        self.assertEqual(validate_design_guidance(guidance)[1], [])
        self.assertIn("asset_strategy", _sections(guidance))


# ---------------------------------------------------------------------------
# D — backward compatibility
# ---------------------------------------------------------------------------

class BackwardCompatibilityTests(unittest.TestCase):
    def test_old_guidance_without_asset_strategy_still_validates(self):
        """A pre-V3-B DesignGuidance.md must remain readable/valid."""
        old = (
            "---\n"
            "design_guidance_version: 0\n"
            "document: design_guidance\n"
            "generator: requirement-creator/design-guidance\n"
            "generator_version: 1\n"
            "project_id: p\n"
            "schema_version: 1\n"
            "source_revision: 1\n"
            "---\n\n"
            "## Brand Character\n\nA studio\n\n"
            "## Design Intent\n\nUNRESOLVED\n\n"
            "## Composition\n\nUNRESOLVED\n\n"
            "## Typography\n\nUNRESOLVED\n\n"
            "## Color\n\nUNRESOLVED\n\n"
            "## Imagery\n\nUNRESOLVED\n\n"
            "## Hierarchy\n\nUNRESOLVED\n\n"
            "## Interaction\n\nUNRESOLVED\n\n"
            "## Motion\n\nUNRESOLVED\n\n"
            "## Approved Resources\n\nUNRESOLVED\n"
        )
        self.assertEqual(validate_design_guidance(old)[1], [])

    def test_brief_without_intent_still_produces_an_asset_plan(self):
        plan = plan_assets({"name": "X", "what": "landing page", "why": "inform"})
        # No showcase described → no imagery forced, nothing invented.
        self.assertFalse(plan["visual_content"])
        self.assertEqual(plan["owner_assets"], ())

    def test_empty_brief_never_crashes(self):
        for brief in ({}, None, {"name": ""}):
            plan = plan_assets(brief)
            self.assertIn("states", plan)
            self.assertIn("instructions", plan)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
