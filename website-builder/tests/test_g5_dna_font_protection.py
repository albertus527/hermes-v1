"""G5 critic classification — Design DNA typography protection.

WHY THIS EXISTS
---------------
G5 (p4) failed with ``CRITIC_REPAIR_STALLED``. Two ``overused-font`` findings
(Inter, Fraunces) were classified ACTIONABLE -> repairable, so a repair was
authorised whose only possible outcome was to replace the very faces the
accepted Design DNA mandates. FRONTEND spent the whole 2700 s fuse, changed no
source file, and the build failed.

Root cause: ``_critic_protected_terms`` read ``dna["font_family"]``, a key that
does not exist. The canonical contract is ``typography.heading_font`` /
``typography.body_font`` (see ``design_dna._font_families``). The family names
therefore never reached the classifier, so the "Design DNA outranks the critic"
rule never fired for fonts.

WHAT THIS PINS
--------------
* The canonical typography fields ARE read.
* A descriptive value is reduced to its family name; the prose is never treated
  as a protected term.
* ONLY an ``overused-font`` finding naming a DNA-selected family becomes
  REQUIREMENT_CONFLICT / non-repairable.
* Every other font finding, every other rule, and every blocking severity keeps
  its previous classification. Nothing is suppressed from the report.
"""

from __future__ import annotations

import unittest

from app.core.critic_policy import (
    CLASS_ACTIONABLE,
    CLASS_ADVISORY,
    CLASS_BLOCKING,
    CLASS_REQUIREMENT_CONFLICT,
    classify_finding,
    classify_findings,
    extract_design_font_families,
)

#: The REAL p4 Design DNA typography block (descriptive values, as generated).
DNA_P4 = {
    "typography": {
        "heading_font": (
            "Fraunces (high-contrast display serif, light weights, occasional "
            "italic for emphasis words)"
        ),
        "body_font": (
            "Inter (restrained neutral sans, small sizes with widened tracking "
            "for labels)"
        ),
        "scale": "1.25",
    },
    "brand_personality": "Editorial, minimalist, premium, modern",
    "palette": {"accent": "#B45A38"},
}

#: The REAL p4 findings, verbatim.
REAL_INTER = (
    "Overused font 'inter' is applied to the page (detected through CSS "
    "variable/@font-face indirection, which the engine's literal-text scan "
    "does not resolve)."
)
REAL_FRAUNCES = (
    "Overused font 'fraunces' is applied to the page (detected through CSS "
    "variable/@font-face indirection, which the engine's literal-text scan "
    "does not resolve)."
)


class _Finding:
    """Minimal stand-in for a critic scan finding."""

    def __init__(self, rule_id, severity, finding, evidence="", category="slop"):
        self.rule_id = rule_id
        self.severity = severity
        self.finding = finding
        self.evidence = evidence
        self.category = category
        self.suggested_action = (
            "Choose a face that gives the interface personality; the "
            "application does not prescribe a specific pairing."
        )


# ---------------------------------------------------------------------------
# 1 & 2 — canonical extraction
# ---------------------------------------------------------------------------


class ExtractionTests(unittest.TestCase):
    def test_heading_font_is_extracted(self):
        self.assertIn("fraunces", extract_design_font_families(DNA_P4))

    def test_body_font_is_extracted(self):
        self.assertIn("inter", extract_design_font_families(DNA_P4))

    def test_descriptive_prose_is_reduced_to_the_family_name(self):
        families = extract_design_font_families(DNA_P4)
        self.assertEqual(set(families), {"fraunces", "inter"})
        for fam in families:
            self.assertNotIn("(", fam)
            self.assertNotIn("high-contrast", fam)
            self.assertNotIn("restrained", fam)

    def test_quoted_values_are_unwrapped(self):
        dna = {"typography": {"heading_font": "'Söhne'", "body_font": '"Inter"'}}
        self.assertEqual(
            set(extract_design_font_families(dna)), {"söhne", "inter"}
        )

    def test_duplicate_families_are_collapsed(self):
        dna = {
            "typography": {
                "heading_font": "Inter (display)",
                "body_font": "Inter (body)",
            }
        }
        self.assertEqual(extract_design_font_families(dna), ("inter",))

    def test_generic_keywords_are_never_a_selected_family(self):
        dna = {"typography": {"heading_font": "serif", "body_font": "sans-serif"}}
        self.assertEqual(extract_design_font_families(dna), ())


# ---------------------------------------------------------------------------
# 3 — matching overused-font becomes REQUIREMENT_CONFLICT
# ---------------------------------------------------------------------------


class RequirementConflictTests(unittest.TestCase):
    def test_inter_matching_dna_is_requirement_conflict(self):
        c = classify_finding(
            _Finding("overused-font", "warning", REAL_INTER),
            design_font_families=extract_design_font_families(DNA_P4),
        )
        self.assertEqual(c.klass, CLASS_REQUIREMENT_CONFLICT)
        self.assertFalse(c.repairable)
        self.assertEqual(c.reason, "font_is_required_by_design_dna")

    def test_fraunces_matching_dna_is_requirement_conflict(self):
        c = classify_finding(
            _Finding("overused-font", "warning", REAL_FRAUNCES),
            design_font_families=extract_design_font_families(DNA_P4),
        )
        self.assertEqual(c.klass, CLASS_REQUIREMENT_CONFLICT)
        self.assertFalse(c.repairable)

    def test_the_finding_is_preserved_verbatim_for_the_report(self):
        """Protection changes REPAIRABILITY, never the evidence."""
        c = classify_finding(
            _Finding("overused-font", "warning", REAL_INTER),
            design_font_families=extract_design_font_families(DNA_P4),
        )
        self.assertEqual(c.finding, REAL_INTER)
        self.assertEqual(c.rule_id, "overused-font")
        self.assertEqual(c.severity, "warning")
        self.assertTrue(c.suggested_action)

    def test_matching_is_word_boundary_not_substring(self):
        """'internet' must not be protected by a DNA family 'inter'."""
        c = classify_finding(
            _Finding("overused-font", "warning", "Overused font 'internet' is applied."),
            design_font_families=("inter",),
        )
        self.assertEqual(c.klass, CLASS_ACTIONABLE)
        self.assertTrue(c.repairable)


# ---------------------------------------------------------------------------
# 4 — unmatched overused-font stays actionable
# ---------------------------------------------------------------------------


class UnmatchedFontTests(unittest.TestCase):
    def test_font_outside_the_dna_stays_actionable(self):
        c = classify_finding(
            _Finding("overused-font", "warning", "Overused font 'roboto' is applied."),
            design_font_families=extract_design_font_families(DNA_P4),
        )
        self.assertEqual(c.klass, CLASS_ACTIONABLE)
        self.assertTrue(c.repairable)
        self.assertEqual(c.reason, "actionable_severity")

    def test_font_not_in_dna_is_repairable_even_when_dna_has_fonts(self):
        c = classify_finding(
            _Finding("overused-font", "warning", "Overused font 'geist' is applied."),
            design_font_families=("fraunces", "inter"),
        )
        self.assertTrue(c.repairable)


# ---------------------------------------------------------------------------
# 5 — descriptive DNA text is not protected
# ---------------------------------------------------------------------------


class DescriptiveTextTests(unittest.TestCase):
    def test_prose_tail_is_not_a_protected_term(self):
        families = extract_design_font_families(DNA_P4)
        self.assertNotIn(
            "fraunces (high-contrast display serif, light weights, occasional "
            "italic for emphasis words)",
            families,
        )

    def test_a_finding_about_the_prose_alone_is_not_protected(self):
        """A finding that mentions no family name must not be shielded."""
        c = classify_finding(
            _Finding(
                "overused-font",
                "warning",
                "Overused font 'comic sans' is applied; avoid high-contrast "
                "display serif monoculture.",
            ),
            design_font_families=extract_design_font_families(DNA_P4),
        )
        self.assertEqual(c.klass, CLASS_ACTIONABLE)
        self.assertTrue(c.repairable)


# ---------------------------------------------------------------------------
# 6 — empty / missing DNA keeps prior behaviour
# ---------------------------------------------------------------------------


class EmptyDnaTests(unittest.TestCase):
    def test_missing_dna_yields_no_families(self):
        for dna in (None, {}, {"typography": None}, {"typography": {}}):
            with self.subTest(dna=dna):
                self.assertEqual(extract_design_font_families(dna), ())

    def test_malformed_typography_is_ignored(self):
        self.assertEqual(
            extract_design_font_families({"typography": "Fraunces"}), ()
        )
        self.assertEqual(
            extract_design_font_families({"typography": {"heading_font": 42}}), ()
        )

    def test_without_families_the_old_behaviour_is_unchanged(self):
        c = classify_finding(_Finding("overused-font", "warning", REAL_INTER))
        self.assertEqual(c.klass, CLASS_ACTIONABLE)
        self.assertTrue(c.repairable)
        self.assertEqual(c.reason, "actionable_severity")


# ---------------------------------------------------------------------------
# 7 — blocking / critical stays blocking
# ---------------------------------------------------------------------------


class BlockingStaysBlockingTests(unittest.TestCase):
    def test_blocker_severity_is_never_downgraded(self):
        for sev in ("blocker", "critical"):
            with self.subTest(severity=sev):
                c = classify_finding(
                    _Finding("overused-font", sev, REAL_INTER),
                    design_font_families=("inter", "fraunces"),
                )
                self.assertEqual(c.klass, CLASS_BLOCKING)
                self.assertEqual(c.reason, "blocking_severity")

    def test_advisory_severity_stays_advisory(self):
        c = classify_finding(
            _Finding("overused-font", "note", REAL_INTER),
            design_font_families=("inter",),
        )
        self.assertEqual(c.klass, CLASS_ADVISORY)
        self.assertFalse(c.repairable)


# ---------------------------------------------------------------------------
# 8 — no broad suppression of unrelated findings
# ---------------------------------------------------------------------------


class NoBroadSuppressionTests(unittest.TestCase):
    UNRELATED = (
        ("contrast", "warning", "Low contrast text in the footer."),
        ("aria-disabled", "warning", "Actionable control looks disabled."),
        ("load-failure", "blocker", "Hero image failed to load."),
        ("repeated-card-content", "warning", "3 cards share the same label."),
        ("conceptual-presented-as-real", "warning", "Conceptual art shown as real work."),
        ("weak-hierarchy", "warning", "Flat hierarchy across sections."),
    )

    def test_unrelated_rules_are_never_affected(self):
        families = extract_design_font_families(DNA_P4)
        for rule_id, sev, text in self.UNRELATED:
            with self.subTest(rule=rule_id):
                c = classify_finding(
                    _Finding(rule_id, sev, text), design_font_families=families
                )
                self.assertNotEqual(c.klass, CLASS_REQUIREMENT_CONFLICT)
                # a warning stays actionable, a blocker stays blocking
                if sev in ("blocker", "critical"):
                    self.assertEqual(c.klass, CLASS_BLOCKING)
                else:
                    self.assertEqual(c.klass, CLASS_ACTIONABLE)

    def test_accessibility_finding_naming_a_dna_font_is_still_actionable(self):
        """Only overused-font is protected — an a11y finding is not."""
        c = classify_finding(
            _Finding(
                "contrast",
                "warning",
                "Body text set in Inter has insufficient contrast against the surface.",
            ),
            design_font_families=("inter",),
        )
        self.assertEqual(c.klass, CLASS_ACTIONABLE)
        self.assertTrue(c.repairable)

    def test_batch_classification_is_consistent_with_single(self):
        findings = [
            _Finding("overused-font", "warning", REAL_INTER),
            _Finding("overused-font", "warning", "Overused font 'roboto' is applied."),
            _Finding("contrast", "warning", "Low contrast text."),
        ]
        families = extract_design_font_families(DNA_P4)
        batch = classify_findings(findings, design_font_families=families)
        singles = [
            classify_finding(f, design_font_families=families) for f in findings
        ]
        self.assertEqual([c.klass for c in batch], [c.klass for c in singles])
        self.assertEqual([c.repairable for c in batch], [c.repairable for c in singles])


if __name__ == "__main__":
    unittest.main()
