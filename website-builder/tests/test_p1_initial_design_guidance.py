"""P1 — initial-generation design guidance: action affordance, section rhythm,
responsive continuity.

WHY
---
P0 (b24f4e1b2) improved FRONTEND *repair* context. It does nothing for a first
build that passes QA on attempt one. G5 Phase 1 audited the guidance FRONTEND
actually receives and found one concern with ZERO coverage — action affordance:
the emitted DesignGuidance never contained the words "CTA", "button" or
"call to action" at all, while two real defects showed up in the G-series
renders (two co-equal hero actions; a contact action that looked disabled).
Two further concerns were weakly covered (section rhythm, responsive
continuity).

WHAT THIS PINS
--------------
* Action guidance fires from the brief's own ACTION semantics (contact, book,
  buy, sign up, explore) — NOT from the literal words "CTA"/"button", which the
  owner almost never writes.
* Action guidance is ABSENT for a page that asks the visitor to do nothing, so
  no action is invented.
* The rhythm and reflow clauses state PROPERTIES only. No fixed tone
  alternation, featured layout, section order, grid, breakpoint, height, or
  spacing value.
* Owner design intent stays verbatim and user-specified; every new line is
  tagged optional with FRONTEND owning the final choice.
* The ten-section DesignGuidance contract is unchanged.
* P0 propagation is untouched: no observations still means a byte-identical
  repair prompt.
* No model call, no new dependency, no extra FRONTEND invocation.
"""

from __future__ import annotations

import unittest

from app.core import critic_policy as cp
from app.core.design_guidance import compile_design_guidance
from app.core.design_intent import (
    AXES,
    recommendations,
    owner_intent,
)
from app.core.document_contracts import (
    DESIGN_GUIDANCE_REQUIRED_SECTIONS,
    extract_sections,
    validate_design_guidance,
)

#: The real G4 brief. It asks the visitor to CONTACT the studio and to
#: understand its work — and it never says "CTA" or "button".
BRIEF_G4 = {
    "name": "Lumen Studio",
    "what": (
        "Landing page untuk studio arsitektur, menampilkan studio secara "
        "profesional: hero, showcase 3 proyek arsitektur, tentang studio, "
        "layanan, dan kontak"
    ),
    "why": "Agar pengunjung memahami karya dan menghubungi studio",
    "design_intent": (
        "editorial minimalis, premium, dan modern. Gunakan tipografi elegan, "
        "warna off-white, charcoal, serta aksen terracotta"
    ),
}

#: A page that asks the visitor to do NOTHING. Any action guidance here would
#: be an invented requirement.
BRIEF_NO_ACTION = {
    "name": "Arsip Puisi",
    "what": "A single page that reproduces one out-of-copyright poem",
    "why": "So a reader can read it without distraction",
    "design_intent": "minimalis dan bersih",
}


def _recs(brief):
    return recommendations(brief, owner_intent(brief))


def _guidance(brief):
    return compile_design_guidance(
        brief, None, project_id="p", source_revision=1
    )


def _sections(brief):
    return extract_sections(_guidance(brief))


# ---------------------------------------------------------------------------
# 1. CTA guidance fires from ACTION SEMANTICS, not from "CTA"/"button"
# ---------------------------------------------------------------------------


class ActionGuidanceTriggerTests(unittest.TestCase):
    def test_fires_for_the_g4_brief_without_the_word_cta_or_button(self):
        recs = _recs(BRIEF_G4)
        self.assertIn("action", recs)
        # The brief itself never contains the literal trigger words.
        blob = " ".join(BRIEF_G4.values()).lower()
        self.assertNotIn("cta", blob)
        self.assertNotIn("button", blob)
        self.assertNotIn("tombol", blob)

    def test_fires_for_each_domain_specific_action(self):
        """Reservation, booking, sign-up, purchase and contact all qualify."""
        cases = {
            "contact": {"what": "a studio site", "why": "so people can contact us"},
            "booking": {"what": "a salon site", "why": "so clients can book an appointment"},
            "reservation": {"what": "a restaurant site", "why": "so guests can make a reservation"},
            "signup": {"what": "a saas site", "why": "so visitors can sign up"},
            "purchase": {"what": "a shop", "why": "so customers can buy online"},
            "explore": {"what": "a portfolio", "why": "so visitors explore the work"},
        }
        for label, extra in cases.items():
            with self.subTest(action=label):
                brief = {"name": "X", "design_intent": "", **extra}
                self.assertIn("action", recommendations(brief, ""), label)

    def test_absent_when_no_action_exists(self):
        self.assertNotIn("action", _recs(BRIEF_NO_ACTION))

    def test_absent_when_brief_is_empty(self):
        self.assertNotIn("action", recommendations({}, ""))

    def test_does_not_invent_a_cta_in_the_compiled_document(self):
        s = _sections(BRIEF_NO_ACTION)
        self.assertNotIn("main action", s["hierarchy"])
        self.assertNotIn("primary", s["hierarchy"].lower())

    def test_action_line_names_the_project_not_a_paragraph(self):
        line = _recs(BRIEF_G4)["action"]
        self.assertIn("Lumen Studio", line)
        # the long `what` description must not be pasted into the sentence
        self.assertNotIn("showcase 3 proyek arsitektur", line)


# ---------------------------------------------------------------------------
# 2. The guidance is non-prescriptive
# ---------------------------------------------------------------------------


class NonPrescriptiveTests(unittest.TestCase):
    FORBIDDEN_VALUES = (
        "#", "px", "rem", "em;", "12-column", "12 column", "grid-template",
        "max-width:", "font-family:", "exactly one", "must use", "must have",
        "mandatory", "required hero", "alternating light", "always dark",
    )

    def test_new_lines_carry_no_fixed_design_values(self):
        recs = _recs(BRIEF_G4)
        for axis in ("action", "hierarchy", "composition"):
            for token in self.FORBIDDEN_VALUES:
                with self.subTest(axis=axis, token=token):
                    self.assertNotIn(token, recs[axis].lower())

    def test_rhythm_does_not_demand_alternating_tones_or_a_featured_item(self):
        text = _recs(BRIEF_G4)["composition"].lower()
        # it names a PROPERTY (rhythm / density / priority)...
        self.assertIn("rhythm", text)
        self.assertIn("density", text)
        self.assertIn("priority", text)
        # ...without prescribing tone alternation, a hero feature, or an order
        for banned in ("alternat", "dark section", "light section",
                       "featured", "first section must", "fixed order"):
            self.assertNotIn(banned, text)

    def test_reflow_clause_fixes_no_breakpoint_height_or_mobile_layout(self):
        text = _recs(BRIEF_G4)["hierarchy"].lower()
        self.assertIn("reflows", text) if "reflows" in text else self.assertIn("reflow", text)
        for banned in ("px", "breakpoint at", "768", "480", "1024",
                       "vh", "hero height", "single column is required"):
            self.assertNotIn(banned, text)

    def test_action_guidance_is_principle_not_recipe(self):
        text = _recs(BRIEF_G4)["action"].lower()
        # the affordance principle is stated...
        self.assertIn("primary", text)
        self.assertIn("secondary", text)
        self.assertIn("look inert", text)
        # ...but no colour, size, radius or state value is dictated
        for banned in ("#", "px", "border-radius", "fill", "outline colour",
                       "always use", "must be terracotta"):
            self.assertNotIn(banned, text)


# ---------------------------------------------------------------------------
# 3. Owner intent stays authoritative; FRONTEND keeps art direction
# ---------------------------------------------------------------------------


class OwnershipTests(unittest.TestCase):
    def test_owner_intent_remains_verbatim_and_user_specified(self):
        s = _sections(BRIEF_G4)
        self.assertIn(BRIEF_G4["design_intent"], s["design_intent"])
        self.assertIn("user-specified", s["design_intent"])

    def test_every_new_line_is_tagged_optional_with_frontend_owning_choice(self):
        s = _sections(BRIEF_G4)
        for line in s["hierarchy"].splitlines():
            if line.strip():
                self.assertIn("design-recommended", line)
                self.assertIn("optional", line.lower())
                self.assertIn("frontend owns the final choice", line.lower())
        self.assertIn("optional", s["composition"].lower())

    def test_owner_intent_is_not_rewritten_by_the_new_clauses(self):
        before = owner_intent(BRIEF_G4)
        _recs(BRIEF_G4)
        self.assertEqual(owner_intent(BRIEF_G4), before)

    def test_compiled_document_still_validates(self):
        self.assertEqual(validate_design_guidance(_guidance(BRIEF_G4))[1], [])


# ---------------------------------------------------------------------------
# 4. Ten-section contract unchanged
# ---------------------------------------------------------------------------


class DocumentStructureTests(unittest.TestCase):
    def test_all_ten_required_sections_are_present(self):
        s = _sections(BRIEF_G4)
        for name in DESIGN_GUIDANCE_REQUIRED_SECTIONS:
            with self.subTest(section=name):
                self.assertIn(name, s)

    def test_contract_itself_is_unchanged(self):
        self.assertEqual(len(DESIGN_GUIDANCE_REQUIRED_SECTIONS), 10)
        self.assertIn("hierarchy", DESIGN_GUIDANCE_REQUIRED_SECTIONS)
        # no new section was added for action guidance
        self.assertNotIn("action", DESIGN_GUIDANCE_REQUIRED_SECTIONS)

    def test_action_rides_in_the_existing_hierarchy_section(self):
        s = _sections(BRIEF_G4)
        self.assertIn("main action", s["hierarchy"])
        # and it does not leak into other facets
        for other in ("interaction", "motion", "imagery", "color", "typography"):
            self.assertNotIn("main action", s[other])

    def test_axes_tuple_is_unchanged(self):
        self.assertEqual(
            AXES,
            ("composition", "typography", "color", "imagery",
             "hierarchy", "interaction", "motion"),
        )

    def test_interaction_still_honestly_unresolved(self):
        """P1 must not smuggle action guidance into the interaction facet."""
        self.assertIn("UNRESOLVED", _sections(BRIEF_G4)["interaction"])


# ---------------------------------------------------------------------------
# 5. P0 propagation untouched
# ---------------------------------------------------------------------------


class P0UnchangedTests(unittest.TestCase):
    def _prompt(self, **kw):
        class F:
            rule_id = "repeated-card-content"
            category = "visual"
            severity = "blocker"
            finding = "3 cards share the same label"
            evidence = "label appears 3 times"
            suggested_action = "vary the labels"

        base = dict(
            requirements={"name": "X", "why": "y"},
            design_dna={"palette": {"accent": "#a8674a"}},
            findings=(cp.classify_finding(F()),),
            project_id="p",
            source_revision=1,
            budget=cp.RepairBudget(),
        )
        base.update(kw)
        return cp.build_repair_request(**base)

    def test_repair_prompt_is_byte_identical_without_observations(self):
        self.assertEqual(self._prompt(), self._prompt(vision_observations=()))
        self.assertEqual(self._prompt(), self._prompt(vision_observations=None))

    def test_repair_prompt_still_carries_observations_when_present(self):
        p = self._prompt(vision_observations=["the contact action looks inert"])
        self.assertIn("VISION observations", p)
        self.assertIn("the contact action looks inert", p)
        self.assertIn("NOT mandatory requirements", p)

    def test_p0_constants_unchanged(self):
        self.assertEqual(cp.MAX_REPAIR_VISION_OBSERVATIONS, 5)
        self.assertEqual(cp.MAX_VISION_OBSERVATION_CHARS, 200)


# ---------------------------------------------------------------------------
# 6. No new model call / dependency / invocation
# ---------------------------------------------------------------------------


class NoNewCallsTests(unittest.TestCase):
    def test_recommendations_is_pure(self):
        """Same input, same output; no I/O, no clock, no randomness."""
        self.assertEqual(_recs(BRIEF_G4), _recs(BRIEF_G4))

    def test_recommendation_keys_are_a_subset_of_known_facets(self):
        known = set(AXES) | {"action"}
        self.assertTrue(set(_recs(BRIEF_G4)) <= known)

    def test_compiling_twice_is_deterministic(self):
        self.assertEqual(_guidance(BRIEF_G4), _guidance(BRIEF_G4))

    def test_no_frontend_budget_is_touched(self):
        import inspect
        from app.core import design_intent
        src = inspect.getsource(design_intent)
        for banned in ("frontend_invocation_limit", "subprocess", "requests.",
                       "httpx", "urllib", "os.environ", "time.time"):
            self.assertNotIn(banned, src, banned)


if __name__ == "__main__":
    unittest.main()
