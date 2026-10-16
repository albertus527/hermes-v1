"""V2-A/V2-B regression tests — requirement fidelity + design-guidance quality.

WHY
---
The V1 audit proved the owner's explicit style directive
("Gaya desain editorial minimalis, premium, dan modern. Gunakan tipografi
elegan, warna off-white, charcoal, serta aksen terracotta.") vanished between
the original Telegram input and the persisted brief, and that DesignGuidance.md
presented an OpenViking DIRECTORY description ("This directory contains a craft
details guide …") as Composition guidance.

These tests lock in the two fixes:

* V2-A — an optional, backward-compatible ``design_intent`` brief field carries
  the owner's OWN words end-to-end (intake → brief → PRD → DesignGuidance →
  FRONTEND prompt), for new projects AND revisions; the newest explicit
  preference wins; older states without the field still load; nothing is
  inferred or fabricated.
* V2-B — DesignGuidance is actionable: the owner's words are carried VERBATIM
  and tagged ``user-specified``, separated from ``reference-derived`` guidance;
  a document/directory meta-descriptor is EXCLUDED from Composition; provenance
  is retained; a retrieval failure keeps honest degraded status.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.decision_engine import DesignDecision  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_guidance import (  # noqa: E402
    DESIGN_RECOMMENDED,
    REFERENCE_DERIVED,
    UNRESOLVED_MARKER,
    USER_SPECIFIED,
    compile_design_guidance,
    guidance_evidence,
)
from app.core.document_contracts import (  # noqa: E402
    extract_sections,
    parse_front_matter,
    validate_design_guidance,
    validate_prd,
)
from app.core.intake import IntakeProcessor, clean_design_intent  # noqa: E402
from app.core.requirement_creator import compile_prd  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402

# The owner's ORIGINAL directive, verbatim, in their own language.
OWNER_INTENT = (
    "Gaya desain editorial minimalis, premium, dan modern. Gunakan tipografi "
    "elegan, warna off-white, charcoal, serta aksen terracotta."
)

BRIEF_V1 = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur",
    "why": (
        "Menampilkan studio secara profesional: hero, showcase 3 proyek "
        "arsitektur, tentang studio, layanan, dan kontak agar pengunjung "
        "memahami karya dan menghubungi studio"
    ),
}
BRIEF_V2 = dict(BRIEF_V1, design_intent=OWNER_INTENT)

# The REAL boilerplate descriptor that reached Composition in V1.
BOILERPLATE = (
    "This directory contains a craft details guide for frontend polish, "
    "focused on implementation patterns that AI models commonly miss."
)
# A genuinely design-informative descriptor (should still be usable).
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


# ---------------------------------------------------------------------------
# V2-A — requirement fidelity
# ---------------------------------------------------------------------------

class DesignIntentNormaliserTests(unittest.TestCase):
    def test_non_string_and_blank_yield_empty(self):
        for value in (None, 123, "", "   ", ["x"], {"a": 1}):
            self.assertEqual(clean_design_intent(value), "")

    def test_whitespace_is_collapsed_and_trimmed(self):
        self.assertEqual(clean_design_intent("  a\n\n b\tc  "), "a b c")

    def test_owner_words_are_preserved_verbatim(self):
        self.assertEqual(clean_design_intent(OWNER_INTENT), OWNER_INTENT)

    def test_length_is_bounded(self):
        self.assertEqual(len(clean_design_intent("x" * 5000)), 2000)


def _msg(text, event_id="e"):
    from app.channels.telegram import NormalizedMessage
    return NormalizedMessage(event_id=event_id, user_id="1",
                             conversation_id="c", text=text)


class IntakeDesignIntentTests(unittest.TestCase):
    """The intake merge is the single persistence authority for the brief."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStateStore(Path(self.tmp.name) / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def _processor(self, fast_result):
        adapter = MagicMock()
        adapter.fast_interpret.return_value = fast_result
        return IntakeProcessor(self.store, hermes_adapter=adapter)

    def _create(self, project_id):
        from app.core.authz import ProjectAccess
        ProjectAccess(self.store).create(project_id, "owner")

    def test_fast_design_intent_is_extracted(self):
        p = self._processor({
            "scope": "WEBSITE", "name": "Forma Atelier",
            "what": "studio arsitektur", "why": "kontak",
            "design_intent": OWNER_INTENT, "source": "hermes_fast",
        })
        result = p.process(_msg("buat website"))
        self.assertEqual(result.brief["design_intent"], OWNER_INTENT)

    def test_absent_design_intent_does_not_block_readiness(self):
        """design_intent is OPTIONAL: its absence never forces clarification."""
        p = self._processor({
            "scope": "WEBSITE", "name": "Forma Atelier",
            "what": "studio arsitektur", "why": "kontak",
            "design_intent": None, "source": "hermes_fast",
        })
        result = p.process(_msg("buat website"))
        self.assertFalse(result.brief.get("design_intent"))
        self.assertEqual(result.readiness.value, "DISCOVERY_READY")

    def test_newer_intent_supersedes_older_and_absent_never_erases(self):
        """A later explicit preference wins; a silent turn preserves it."""
        self._create("proj-1")
        # Turn 1: an explicit (old) preference.
        p1 = self._processor({
            "scope": "WEBSITE", "name": "Forma Atelier", "what": "x", "why": "y",
            "design_intent": "playful and colourful", "source": "hermes_fast",
        })
        r1 = p1.process(_msg("t1", "e1"))
        p1.apply_to_project("proj-1", r1, principal_id="owner", event_id="e1")

        # Turn 2: a NEW explicit preference supersedes it.
        p2 = self._processor({
            "scope": "WEBSITE", "name": "Forma Atelier", "what": "x", "why": "y",
            "design_intent": OWNER_INTENT, "source": "hermes_fast",
        })
        r2 = p2.process(_msg("t2", "e2"), project_id="proj-1")
        p2.apply_to_project("proj-1", r2, principal_id="owner", event_id="e2")
        loaded = self.store.load("proj-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.brief["design_intent"], OWNER_INTENT)

        # Turn 3: NO preference stated — the stored one must NOT be erased.
        p3 = self._processor({
            "scope": "WEBSITE", "name": "Forma Atelier", "what": "x", "why": "y",
            "design_intent": None, "source": "hermes_fast",
        })
        r3 = p3.process(_msg("t3", "e3"), project_id="proj-1")
        p3.apply_to_project("proj-1", r3, principal_id="owner", event_id="e3")
        loaded = self.store.load("proj-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.brief["design_intent"], OWNER_INTENT)

    def test_fallback_never_guesses_a_design_intent(self):
        """The deterministic fallback has no semantic authority: it stays None."""
        p = self._processor({
            "scope": "WEBSITE", "name": "Northcut", "what": "barbershop",
            "why": "booking", "design_intent": None, "source": "fallback_heuristic",
        })
        result = p.process(_msg("buat website"))
        self.assertFalse(result.brief.get("design_intent"))


class OldStateCompatibilityTests(unittest.TestCase):
    """A pre-V2-A state (no design_intent) must load and behave unchanged."""

    def test_state_without_design_intent_loads(self):
        with tempfile.TemporaryDirectory() as d:
            store = ProjectStateStore(Path(d) / "state")
            with store.acquire_writer("p-old") as state:
                state.brief = {"name": "Old", "what": "x", "why": "y"}
                store.save(state)
            loaded = store.load("p-old")
            self.assertIsNotNone(loaded)
            self.assertNotIn("design_intent", loaded.brief)

    def test_prd_and_guidance_compile_for_old_brief(self):
        prd = compile_prd(BRIEF_V1, project_id="p", source_revision=1)
        self.assertEqual(validate_prd(prd)[1], [])
        g = compile_design_guidance(BRIEF_V1, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        self.assertEqual(validate_design_guidance(g)[1], [])
        self.assertIn(UNRESOLVED_MARKER, _sections(g)["design_intent"])


class PRDDesignIntentTests(unittest.TestCase):
    def test_owner_words_reach_the_prd(self):
        prd = compile_prd(BRIEF_V2, project_id="p", source_revision=1)
        self.assertEqual(validate_prd(prd)[1], [])
        self.assertIn(OWNER_INTENT, _sections(prd)["design_intent"])

    def test_absent_intent_is_unresolved_not_fabricated(self):
        prd = compile_prd(BRIEF_V1, project_id="p", source_revision=1)
        self.assertIn(UNRESOLVED_MARKER, _sections(prd)["design_intent"])
        # No design word is invented.
        self.assertNotIn("terracotta", prd.lower())
        self.assertNotIn("editorial", prd.lower())


class FrontendPromptDesignIntentTests(unittest.TestCase):
    """The owner's words must reach the FRONTEND invocation prompt."""

    def _prompt(self, brief):
        from app.hermes.adapter import HermesAdapter
        adapter = HermesAdapter.__new__(HermesAdapter)
        return HermesAdapter._build_frontend_prompt(
            adapter, brief, Path("/tmp/ws"),
        )

    def test_owner_intent_is_present_verbatim(self):
        prompt = self._prompt(BRIEF_V2)
        self.assertIn(OWNER_INTENT, prompt)
        self.assertIn("Design intent", prompt)

    def test_absent_intent_leaves_the_prompt_unchanged(self):
        prompt = self._prompt(BRIEF_V1)
        self.assertNotIn("Design intent", prompt)
        self.assertNotIn("terracotta", prompt.lower())


class RevisionDesignIntentTests(unittest.TestCase):
    """A revision that states a new preference must update the brief."""

    def _orchestrator(self, adapter):
        from app.projects.revise import RevisionOrchestrator
        from app.sandbox.runner import ProjectRunner
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = ProjectStateStore(root / "state")
        runner = ProjectRunner(root / "workspaces", store)
        return RevisionOrchestrator(runner, store, hermes_adapter=adapter), store

    def test_build_revision_instructions_carries_intent(self):
        orch, _ = self._orchestrator(MagicMock())
        text = orch._build_revision_instructions({}, "make hero bigger",
                                                 design_intent=OWNER_INTENT)
        self.assertIn(OWNER_INTENT, text)
        self.assertIn("preserve unless the requested change explicitly supersedes", text)

    def test_absent_intent_leaves_instructions_unchanged(self):
        orch, _ = self._orchestrator(MagicMock())
        text = orch._build_revision_instructions({}, "make hero bigger")
        self.assertNotIn("Owner's stated design intent", text)


# ---------------------------------------------------------------------------
# V2-B — design-guidance quality
# ---------------------------------------------------------------------------

class CompositionBoilerplateTests(unittest.TestCase):
    def test_directory_description_is_never_composition_guidance(self):
        g = compile_design_guidance(BRIEF_V2, _decision(_candidate(BOILERPLATE)),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        self.assertNotIn("craft details guide", s["composition"])
        self.assertNotIn("This directory", s["composition"])
        # ...but its provenance is still recorded.
        self.assertIn("refero_craft_details@bc6b016a3ff3", s["approved_resources"])

    def test_informative_descriptor_is_still_usable(self):
        g = compile_design_guidance(BRIEF_V1, _decision(_candidate(INFORMATIVE)),
                                    project_id="p", source_revision=1)
        self.assertIn("asymmetric layout", _sections(g)["composition"])
        self.assertIn(REFERENCE_DERIVED, _sections(g)["composition"])

    def test_no_intent_and_no_informative_descriptor_is_honest(self):
        # V3-A: with no owner intent and no informative reference descriptor,
        # Composition is honestly UNRESOLVED. (V2-B dumped the raw `what` string
        # here, which was itself non-actionable filler — V3-A removes that.)
        g = compile_design_guidance(BRIEF_V1, _decision(_candidate(BOILERPLATE)),
                                    project_id="p", source_revision=1)
        self.assertIn(UNRESOLVED_MARKER, _sections(g)["composition"])


class GuidanceActionabilityTests(unittest.TestCase):
    def test_owner_intent_is_carried_verbatim_and_tagged(self):
        g = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        self.assertIn(OWNER_INTENT, s["design_intent"])
        self.assertIn(USER_SPECIFIED, s["design_intent"])

    def test_axes_reflect_the_owner_words_where_stated(self):
        g = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        # The owner explicitly spoke to typography, color, and composition.
        self.assertIn(USER_SPECIFIED, s["typography"])
        self.assertIn(USER_SPECIFIED, s["color"])
        self.assertIn(USER_SPECIFIED, s["composition"])
        # V3-A: the brief names a "showcase 3 proyek", so imagery guidance is
        # grounded and emitted as a design recommendation...
        self.assertIn(DESIGN_RECOMMENDED, s["imagery"])
        # ...while interaction (never auto-fired) and motion (no evidence) stay
        # honestly UNRESOLVED.
        self.assertIn(UNRESOLVED_MARKER, s["interaction"])
        self.assertIn(UNRESOLVED_MARKER, s["motion"])

    def test_user_specified_is_separated_from_reference_derived(self):
        # Owner intent that does NOT mention composition → the informative
        # reference descriptor supplies Composition, tagged reference-derived.
        brief = dict(BRIEF_V1, design_intent="warm and inviting, lots of light")
        g = compile_design_guidance(brief, _decision(_candidate(INFORMATIVE)),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        self.assertIn(USER_SPECIFIED, s["design_intent"])
        # The reference-derived marker never claims to be user-specified.
        self.assertIn(REFERENCE_DERIVED, s["composition"])
        self.assertIn("not user-specified", REFERENCE_DERIVED)

    def test_owner_intent_does_not_override_provenance(self):
        g = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        self.assertIn("refero_craft_details@bc6b016a3ff3",
                      _sections(g)["approved_resources"])

    def test_no_hardcoded_recipe_is_introduced(self):
        """Guidance must not invent ratios, fixed hexes, or a mandatory hero."""
        g = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        low = g.lower()
        for forbidden in ("70/20/10", "#a8674a", "mandatory hero", "must use 3"):
            self.assertNotIn(forbidden, low)


class DegradedRetrievalTests(unittest.TestCase):
    def test_abstention_keeps_owner_intent_and_is_honest(self):
        g = compile_design_guidance(BRIEF_V2, _decision(None),
                                    project_id="p", source_revision=1)
        s = _sections(g)
        # The owner's words survive a retrieval failure.
        self.assertIn(OWNER_INTENT, s["design_intent"])
        # No reference is invented.
        self.assertNotIn("viking://", g)
        self.assertIn(UNRESOLVED_MARKER, s["approved_resources"])

    def test_evidence_carries_intent_and_provenance(self):
        ev = guidance_evidence(BRIEF_V2, _decision(_candidate()))
        self.assertEqual(ev["design_intent"], OWNER_INTENT)
        self.assertEqual(ev["source_uri"].startswith("viking://"), True)

    def test_meta_descriptor_is_excluded_from_selection_input(self):
        from app.core.design_guidance import guidance_selection_input
        ev = guidance_evidence(BRIEF_V2, _decision(_candidate(BOILERPLATE)))
        mapping = guidance_selection_input(ev)
        self.assertNotIn("layout", mapping)  # uninformative descriptor excluded
        ev2 = guidance_evidence(BRIEF_V2, _decision(_candidate(INFORMATIVE)))
        self.assertIn("layout", guidance_selection_input(ev2))


class GuidanceContractTests(unittest.TestCase):
    def test_new_sections_are_present_and_valid(self):
        g = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        self.assertEqual(validate_design_guidance(g)[1], [])
        s = _sections(g)
        for required in ("design_intent", "hierarchy", "interaction"):
            self.assertIn(required, s)

    def test_deterministic(self):
        a = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        b = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        self.assertEqual(a, b)

    def test_guidance_precedes_frontend_and_design_is_after(self):
        """Lifecycle ordering is unchanged: guidance exists pre-FRONTEND."""
        g = compile_design_guidance(BRIEF_V2, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        # DesignGuidance is a distinct kind from the post-build Design.md.
        self.assertIn("design_guidance", g)


class DecisionProviderLoggingTests(unittest.TestCase):
    """V2-B: a retrieval-only intake provider is debug, not a warning."""

    def test_not_implemented_intake_is_debug_not_warning(self):
        import logging
        from app.core.decision_engine import DecisionEngine
        from app.core.decision_provider import DecisionProviderRegistry
        from app.core.openviking_decision import OpenVikingDecisionProvider

        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append((record.levelname, record.getMessage()))

        logger = logging.getLogger("app.core.decision_engine")
        handler = _Handler()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            registry = DecisionProviderRegistry()
            registry.register(OpenVikingDecisionProvider(adapter=object()))
            engine = DecisionEngine(registry=registry, provider_name="openviking")
            decision = engine.decide_intake("buat website landing page", {})
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        # The fallback still happens, honestly reported as degraded.
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.provider, "deterministic")
        # ...but it is NOT logged as a failure.
        self.assertFalse([m for lvl, m in records if lvl == "WARNING"])
        self.assertTrue([m for lvl, m in records if lvl == "DEBUG"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
