"""G5 Patch D — asset-honesty false positive: schedule vs. claim.

WHY THIS EXISTS
---------------
G5 (p4) was rejected by Design Fidelity with ``conceptual-presented-as-real``
because the service line

    "Koordinasi dan pengawasan berkala hingga proyek selesai."

contains the substring "proyek selesai". Read as a claim, it said the studio
presents conceptual visuals as its own completed work — on a page that states
outright "bukan proyek yang telah dibangun" (not a built project).

Probing the old implementation showed it was wrong in BOTH directions: it fired
on that schedule, and it MISSED all four explicit claims the owner requires
("Proyek yang telah kami selesaikan untuk klien", "Portofolio proyek nyata kami",
"Karya yang sudah kami bangun", "Completed client projects") because none of them
contained a listed phrase as a substring.

THE POLICY CORRECTION (one rule, no phrase list)
------------------------------------------------
A clause asserts authorship only when it carries BOTH:
  * an ownership marker (kami / kita / our / ours / klien / client), AND
  * an ATTRIBUTION-BEARING work signal — a completion verb (selesaikan,
    diselesaikan, dibangun, dikerjakan, diserahkan, completed, delivered, built)
    or a portfolio noun — OR a possessive state word ("proyek selesai kami").

The bare state word "selesai"/"completed" is deliberately NOT an attribution
signal. That single distinction is what separates a schedule from a claim, and it
means there is no list of exempted prepositions ("hingga", "sampai", "until",
"through") anywhere: a genuine claim sitting next to such a word still fires.
"""

from __future__ import annotations

import unittest

from app.core.design_fidelity import (
    CONFIDENCE_HIGH,
    DIM_ASSET_HONESTY,
    SEVERITY_HARD,
    _detect_unlabeled_conceptual,
    _real_work_claims,
)

#: The asset strategy that makes this detector active at all.
ASSET_CONCEPTUAL = {"states": {"showcase": "CONCEPTUAL"}}


def _claims(text: str):
    return _real_work_claims(text.lower())


def _rules(text: str):
    return [f.rule_id for f in _detect_unlabeled_conceptual(text, ASSET_CONCEPTUAL)]


# ---------------------------------------------------------------------------
# 1. Ordinary service descriptions must NOT be read as claims
# ---------------------------------------------------------------------------


class ServiceDescriptionsAreNotClaims(unittest.TestCase):
    MUST_NOT = (
        # the exact G5 p4 line
        "Koordinasi dan pengawasan berkala hingga proyek selesai.",
        "Koordinasi dan pengawasan hingga proyek selesai",
        "Pendampingan sampai pembangunan selesai",
        "Desain konsep, studi massa, dan pengembangan rancangan bersama Anda.",
        "Gambar kerja, spesifikasi, dan kelengkapan teknis untuk pelaksanaan.",
        # English schedule forms
        "Ongoing supervision until the project is completed",
        "We provide supervision until the project is completed.",
        "Our team assists until construction is finished.",
        # conceptual study labels
        "Visual konseptual — studi eksplorasi atelier, bukan proyek yang telah dibangun.",
        "Tiga studi bentuk & cahaya",
    )

    def test_no_service_description_is_a_claim(self):
        for text in self.MUST_NOT:
            with self.subTest(text=text):
                self.assertEqual(_claims(text), [], text)

    def test_no_service_description_fires_the_detector(self):
        for text in self.MUST_NOT:
            with self.subTest(text=text):
                self.assertNotIn(
                    "conceptual-presented-as-real", _rules(f"<p>{text}</p>"), text
                )


# ---------------------------------------------------------------------------
# 2. Explicit portfolio claims must STILL fire, at hard/high
# ---------------------------------------------------------------------------


class ExplicitClaimsStillFire(unittest.TestCase):
    MUST = (
        # the four the owner named
        "Proyek yang telah kami selesaikan untuk klien",
        "Portofolio proyek nyata kami",
        "Karya yang sudah kami bangun",
        "Completed client projects",
        # attributed phrases (unchanged behaviour)
        "Lihat karya kami dan proyek kami.",
        "Browse our portfolio of completed projects.",
        "Klien kami mempercayakan proyek selesai kepada kami.",
        # possessive completion
        "Proyek selesai kami sudah banyak.",
    )

    def test_every_explicit_claim_is_detected(self):
        for text in self.MUST:
            with self.subTest(text=text):
                self.assertTrue(_claims(text), text)

    def test_every_explicit_claim_fires_the_detector(self):
        for text in self.MUST:
            with self.subTest(text=text):
                self.assertIn(
                    "conceptual-presented-as-real", _rules(f"<p>{text}</p>"), text
                )

    def test_genuine_claim_keeps_hard_severity_and_high_confidence(self):
        findings = _detect_unlabeled_conceptual(
            "<p>Karya yang sudah kami bangun untuk klien.</p>", ASSET_CONCEPTUAL
        )
        finding = next(
            f for f in findings if f.rule_id == "conceptual-presented-as-real"
        )
        self.assertEqual(finding.severity, SEVERITY_HARD)
        self.assertEqual(finding.confidence, CONFIDENCE_HIGH)
        self.assertEqual(finding.dimension, DIM_ASSET_HONESTY)


# ---------------------------------------------------------------------------
# 3. Contextual distinction is preserved in BOTH directions
# ---------------------------------------------------------------------------


class ContextualDistinctionTests(unittest.TestCase):
    def test_a_temporal_word_near_a_real_claim_does_not_suppress_it(self):
        """"Hingga kini, proyek selesai kami ..." is a claim, not a schedule."""
        self.assertTrue(_claims("Hingga kini, proyek selesai kami sudah banyak."))
        self.assertIn(
            "conceptual-presented-as-real",
            _rules("<p>Hingga kini, proyek selesai kami sudah banyak.</p>"),
        )

    def test_an_owner_in_the_clause_does_not_claim_a_bare_schedule(self):
        """An owner present but no attribution verb -> still a schedule."""
        text = "Kami melakukan pengawasan hingga proyek selesai."
        self.assertEqual(_claims(text), [], text)

    def test_owner_and_attribution_verb_together_do_claim(self):
        text = "Kami telah menyelesaikan proyek untuk klien."
        self.assertTrue(_claims(text), text)

    def test_negated_claims_stay_disclaimers(self):
        for text in (
            "Studi konseptual — bukan proyek selesai",
            "Ini bukan karya kami, hanya studi.",
            "Belum menjadi proyek selesai.",
            "Tidak ada klien kami yang tampil di sini.",
            "This is not our completed projects.",
            "No clients here — not our portfolio.",
            "These are concepts, never our work.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_claims(text), [], text)

    def test_negated_phrase_still_counts_as_disclosure(self):
        rules = _rules("<p>Studi arsitektur. Bukan proyek selesai.</p>")
        self.assertNotIn("conceptual-presented-as-real", rules)
        self.assertNotIn("conceptual-not-disclosed", rules)

    def test_mixed_sentence_still_fires(self):
        for text in (
            "Bukan proyek selesai, ini karya kami.",
            "Karya kami nyata, bukan sekadar konsep.",
            "Not a mockup — these are our completed projects.",
        ):
            with self.subTest(text=text):
                self.assertIn(
                    "conceptual-presented-as-real", _rules(f"<p>{text}</p>"), text
                )

    def test_distant_negation_does_not_launder_a_claim(self):
        rules = _rules("<p>Kami tidak menjual jasa fotografi. Lihat karya kami.</p>")
        self.assertIn("conceptual-presented-as-real", rules)


# ---------------------------------------------------------------------------
# 4. No blanket exemption list, and the detector stays gated
# ---------------------------------------------------------------------------


class NoBlanketExemptionTests(unittest.TestCase):
    def test_no_temporal_word_appears_in_the_module_policy(self):
        """The fix must not be a list of exempted prepositions.

        Checks the EXECUTABLE policy (regexes and tuples), not docstrings: the
        words legitimately appear in the prose explaining why no exemption list
        is used.
        """
        import inspect
        import re as _re

        from app.core import design_fidelity as df

        source = inspect.getsource(df)
        # Strip docstrings and comments, leaving only executable statements.
        code = _re.sub(r'"""(?:.|\n)*?"""', "", source)
        code = _re.sub(r"'''(?:.|\n)*?'''", "", code)
        code = _re.sub(r"#[^\n]*", "", code)

        for banned in (
            "_TEMPORAL_EXEMPT",
            "_SCHEDULE_WORDS",
            "_EXEMPT_PRECEDING",
            '("hingga"',
            '("sampai"',
            '("until"',
            '("through"',
        ):
            self.assertNotIn(banned, code, banned)

    def test_non_conceptual_asset_state_never_fires(self):
        for state in ("REAL", "LICENSED", None):
            with self.subTest(state=state):
                plan = {"states": {"showcase": state}} if state else {}
                findings = _detect_unlabeled_conceptual(
                    "<p>Karya yang sudah kami bangun.</p>", plan
                )
                self.assertEqual(
                    [f.rule_id for f in findings if f.rule_id == "conceptual-presented-as-real"],
                    [],
                )

    def test_disclosure_without_claims_produces_no_finding(self):
        findings = _detect_unlabeled_conceptual(
            "<p>Visual konseptual — studi eksplorasi, bukan proyek yang telah dibangun.</p>",
            ASSET_CONCEPTUAL,
        )
        self.assertEqual([f.rule_id for f in findings], [])

    def test_undisclosed_conceptual_still_reported(self):
        findings = _detect_unlabeled_conceptual(
            "<p>Selamat datang di studio kami.</p>", ASSET_CONCEPTUAL
        )
        self.assertIn(
            "conceptual-not-disclosed", [f.rule_id for f in findings]
        )


if __name__ == "__main__":
    unittest.main()
