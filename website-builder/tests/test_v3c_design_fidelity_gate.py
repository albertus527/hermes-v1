"""V3-C regression tests — Visual Fidelity Gate & bounded repair.

WHY
---
V2-A/B preserved owner intent; V3-A/B made it actionable and honest. V3-C closes
the loop with a bounded, evidence-based gate that separates four verdicts
(technical / requirement-fidelity / visual-execution / asset-honesty) and allows
AT MOST ONE repair, verified against the ACTUAL repaired result.

These tests lock in the contract, using the REAL Forma Atelier revision-1
weaknesses as fixtures (read-only: the fixtures are strings, never the real
project). They also prove the gate never turns QA into a rigid aesthetic
checklist: intentional minimalism is not rejected.

No live model, browser, network, npm, or image download is required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_fidelity import (  # noqa: E402
    CONFIDENCE_HIGH,
    DIM_ASSET_HONESTY,
    DIM_REQUIREMENT_FIDELITY,
    DIM_TECHNICAL,
    DIM_VISUAL_EXECUTION,
    DIMENSIONS,
    FidelityRepairBudget,
    MAX_FIDELITY_REPAIR_ATTEMPTS,
    SEVERITY_HARD,
    SEVERITY_SOFT,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    evaluate_design_fidelity,
    overall_terminal_outcome,
    plan_repair,
    repair_allowed,
    run_bounded_fidelity_gate,
    verify_repair,
)

# The owner's ORIGINAL directive + brief (Forma Atelier).
OWNER_INTENT = (
    "Gaya desain editorial minimalis, premium, dan modern. Gunakan tipografi "
    "elegan, warna off-white, charcoal, serta aksen terracotta."
)
BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur",
    "why": "Menampilkan studio secara profesional: hero, showcase 3 proyek arsitektur",
    "design_intent": OWNER_INTENT,
}
ASSET_PLAN_CONCEPTUAL = {"states": {"showcase": "CONCEPTUAL"}}

# The REAL revision-1 weaknesses, reproduced faithfully as a fixture.
FA_REV1_SOURCE = """
const projects = [
  { label: 'Proyek 01', title: 'Contoh Proyek 01', note: 'Nama proyek menyusul.' },
  { label: 'Proyek 02', title: 'Contoh Proyek 02', note: 'Nama proyek menyusul.' },
  { label: 'Proyek 03', title: 'Contoh Proyek 03', note: 'Nama proyek menyusul.' },
];
const services = [
  { label: 'Layanan 01', title: 'Layanan Studio', note: 'Daftar layanan menyusul.' },
  { label: 'Layanan 02', title: 'Layanan Studio', note: 'Daftar layanan menyusul.' },
  { label: 'Layanan 03', title: 'Layanan Studio', note: 'Daftar layanan menyusul.' },
];
export default function App() {
  return (<main>
    <h1>Forma Atelier</h1>
    <p>Kami menampilkan karya kami di halaman ini.</p>
    <a href="#proyek">Lihat Proyek</a>
  </main>);
}
"""
FA_REV1_CSS = ":root{--color-surface:#faf9f7;--color-ink:#1c1b1a;--color-accent:#a8674a}" \
              "h1{font-family:Georgia,serif}"

# A strong, intentional, minimal site (must NOT be rejected).
MINIMAL_SOURCE = """
export default function App() {
  return (<main>
    <h1>Forma Atelier</h1>
    <p>Studio arsitektur.</p>
    <a href="#kontak">Hubungi studio</a>
  </main>);
}
"""
MINIMAL_CSS = ":root{--bg:#faf9f7;--ink:#1c1b1a;--accent:#a8674a} h1{font-family:Georgia,serif}"


def _eval(**over):
    kwargs = dict(brief=BRIEF, asset_plan=ASSET_PLAN_CONCEPTUAL,
                  source_text=FA_REV1_SOURCE, css_text=FA_REV1_CSS,
                  technical_ok=True)
    kwargs.update(over)
    return evaluate_design_fidelity(**kwargs)


class VerdictDimensionTests(unittest.TestCase):
    def test_four_dimensions_are_reported_separately(self):
        report = _eval()
        self.assertEqual(set(report.dimensions), set(DIMENSIONS))

    def test_forma_atelier_rev1_is_flagged(self):
        report = _eval()
        self.assertEqual(report.dimensions[DIM_VISUAL_EXECUTION], VERDICT_FAIL)
        self.assertEqual(report.dimensions[DIM_ASSET_HONESTY], VERDICT_FAIL)
        rules = {f.rule_id for f in report.findings}
        self.assertIn("repeated-card-content", rules)
        self.assertIn("conceptual-presented-as-real", rules)

    def test_technical_failure_is_reported_and_not_visually_repairable(self):
        report = _eval(technical_ok=False)
        self.assertEqual(report.dimensions[DIM_TECHNICAL], VERDICT_FAIL)
        allowed, reason = repair_allowed(report)
        self.assertFalse(allowed)
        self.assertEqual(reason, "technical_failure_is_not_visually_repairable")

    def test_strong_intentional_minimalism_is_not_rejected(self):
        report = _eval(source_text=MINIMAL_SOURCE, css_text=MINIMAL_CSS,
                       asset_plan={"states": {}})
        self.assertEqual(report.overall, VERDICT_PASS)
        self.assertEqual(report.findings, ())

    def test_absent_render_is_unknown_not_fail(self):
        report = _eval(source_text="", css_text="")
        self.assertEqual(report.dimensions[DIM_VISUAL_EXECUTION], VERDICT_UNKNOWN)
        self.assertEqual(report.dimensions[DIM_REQUIREMENT_FIDELITY], VERDICT_UNKNOWN)


class RepetitionTests(unittest.TestCase):
    def test_repeated_generic_service_cards_produce_a_finding(self):
        report = _eval(source_text=(
            "<div>{['Layanan Studio','Layanan Studio','Layanan Studio']}</div>"
        ))
        rules = [f.rule_id for f in report.findings]
        self.assertIn("repeated-card-content", rules)
        finding = next(f for f in report.findings if f.rule_id == "repeated-card-content")
        self.assertEqual(finding.severity, SEVERITY_HARD)
        self.assertEqual(finding.confidence, CONFIDENCE_HIGH)
        self.assertTrue(finding.suggested_action)

    def test_two_similar_items_are_not_a_defect(self):
        report = _eval(source_text="<div>['Karya Satu','Karya Satu']</div>")
        self.assertNotIn("repeated-card-content", [f.rule_id for f in report.findings])

    def test_code_attributes_are_not_treated_as_repeated_labels(self):
        # SVG coordinates repeat in source but are NOT visible card text.
        svg = "".join(f'<line x1="{i}" y1="{i}" x2="140" y2="230"/>' for i in range(20))
        report = _eval(source_text=svg)
        self.assertNotIn("repeated-card-content", [f.rule_id for f in report.findings])


class AssetHonestyTests(unittest.TestCase):
    def test_conceptual_presented_as_real_is_a_finding(self):
        report = _eval(source_text="<p>Lihat karya kami dan proyek kami.</p>")
        finding = next(f for f in report.findings
                       if f.rule_id == "conceptual-presented-as-real")
        self.assertEqual(finding.dimension, DIM_ASSET_HONESTY)

    def test_undisclosed_conceptual_is_a_finding(self):
        report = _eval(source_text="<p>Selamat datang di studio kami.</p>")
        self.assertIn("conceptual-not-disclosed", [f.rule_id for f in report.findings])

    def test_labelled_conceptual_is_allowed(self):
        report = _eval(source_text=(
            "<p>Karya terpilih</p>"
            "<div>Contoh proyek — placeholder, materi menyusul dari studio.</div>"
        ))
        self.assertNotIn("conceptual-presented-as-real", [f.rule_id for f in report.findings])
        self.assertNotIn("conceptual-not-disclosed", [f.rule_id for f in report.findings])

    def test_non_conceptual_site_skips_the_asset_check(self):
        report = _eval(source_text="<p>Selamat datang di studio kami.</p>",
                       asset_plan={"states": {"showcase": "OWNER_PROVIDED"}})
        self.assertNotIn("conceptual-not-disclosed", [f.rule_id for f in report.findings])


class RequirementFidelityTests(unittest.TestCase):
    def test_launch_critical_cta_placeholder_is_classified(self):
        report = _eval(source_text='<a href="#kontak">Lorem ipsum</a>')
        finding = next(f for f in report.findings
                       if f.rule_id == "launch-critical-placeholder")
        self.assertEqual(finding.dimension, DIM_REQUIREMENT_FIDELITY)
        self.assertEqual(finding.location, "primary call-to-action")

    def test_owner_colour_contradiction_is_detected(self):
        # Owner requires terracotta; the render declares none.
        report = _eval(source_text="<main>x</main>",
                       css_text=":root{--a:#000;--b:#fff} h1{font-family:Georgia,serif}")
        rules = [f.rule_id for f in report.findings]
        self.assertTrue(any(r.startswith("intent-colour-contradiction") for r in rules))

    def test_owner_intent_matched_produces_no_contradiction(self):
        report = _eval(source_text="<main>x</main>", css_text=FA_REV1_CSS)
        rules = [f.rule_id for f in report.findings]
        self.assertFalse(any("contradiction" in r for r in rules))

    def test_no_intent_means_no_invented_contradiction(self):
        report = _eval(brief={"name": "X", "what": "y", "why": "z"},
                       source_text="<main>x</main>", css_text=":root{--a:#000}")
        self.assertFalse(any("contradiction" in f.rule_id for f in report.findings))


class RepairBoundaryTests(unittest.TestCase):
    def test_max_one_repair_attempt(self):
        self.assertEqual(MAX_FIDELITY_REPAIR_ATTEMPTS, 1)
        budget = FidelityRepairBudget()
        self.assertEqual(budget.remaining, 1)
        self.assertTrue(budget.consume().exhausted)

    def test_repair_allowed_only_for_high_confidence_actionable_findings(self):
        # A SOFT warning alone never authorizes a repair.
        report = _eval(source_text="<main><h1>A</h1><h2>B</h2></main>",
                       css_text=FA_REV1_CSS, asset_plan={"states": {}},
                       screenshot_evidence=("uniform repetitive composition",))
        self.assertTrue(report.findings)  # the soft weak-hierarchy finding
        self.assertTrue(all(f.severity == SEVERITY_SOFT for f in report.findings))
        allowed, reason = repair_allowed(report)
        self.assertFalse(allowed)
        self.assertEqual(reason, "no_high_confidence_findings")

    def test_plan_repair_is_bounded_and_deterministic(self):
        report = _eval()
        plan = plan_repair(report)
        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["budget"]["attempts_used"], 1)
        self.assertEqual(plan["budget"]["max_attempts"], 1)

    def test_repair_verified_requires_the_findings_actually_resolved(self):
        before = _eval()
        # A "repair" that changed nothing must NOT verify.
        after_same = _eval()
        ok, reason = verify_repair(before, after_same)
        self.assertFalse(ok)
        self.assertEqual(reason, "repair_stalled")

    def test_repair_verified_when_findings_resolved(self):
        before = _eval()
        after = _eval(source_text=MINIMAL_SOURCE, css_text=MINIMAL_CSS,
                      asset_plan={"states": {}})
        ok, reason = verify_repair(before, after)
        self.assertTrue(ok)
        self.assertEqual(reason, "repair_verified")

    def test_repair_never_verified_on_technical_regression(self):
        before = _eval()
        after = _eval(technical_ok=False)
        ok, reason = verify_repair(before, after, technical_ok=False)
        self.assertFalse(ok)


class BoundedGateDriverTests(unittest.TestCase):
    def test_no_findings_runs_no_repair(self):
        calls = {"repair": 0}
        result = run_bounded_fidelity_gate(
            evaluate=lambda: _eval(source_text=MINIMAL_SOURCE, css_text=MINIMAL_CSS,
                                   asset_plan={"states": {}}),
            repair=lambda findings: calls.__setitem__("repair", calls["repair"] + 1) or True,
        )
        self.assertEqual(result.outcome, "FIDELITY_PASS")
        self.assertFalse(result.repair_used)
        self.assertEqual(calls["repair"], 0)

    def test_one_repair_then_verified(self):
        state = {"repaired": False}

        def _evaluate():
            if state["repaired"]:
                return _eval(source_text=MINIMAL_SOURCE, css_text=MINIMAL_CSS,
                             asset_plan={"states": {}})
            return _eval()

        def _repair(findings):
            state["repaired"] = True
            return True

        result = run_bounded_fidelity_gate(evaluate=_evaluate, repair=_repair)
        self.assertTrue(result.repair_used)
        self.assertTrue(result.repair_verified)
        self.assertEqual(result.outcome, "FIDELITY_REPAIRED")
        self.assertFalse(result.failed)
        self.assertEqual(result.attempts_used, 1)

    def test_repair_that_does_not_fix_terminates_cleanly(self):
        # The repair runs but the findings persist -> not verified, no loop.
        attempts = {"n": 0}

        def _repair(findings):
            attempts["n"] += 1
            return True

        result = run_bounded_fidelity_gate(evaluate=lambda: _eval(), repair=_repair)
        self.assertTrue(result.failed)
        self.assertFalse(result.repair_verified)
        self.assertEqual(attempts["n"], 1)   # exactly ONE repair
        self.assertEqual(result.attempts_used, 1)

    def test_repair_execution_failure_terminates_cleanly(self):
        def _repair(findings):
            raise RuntimeError("frontend blew up")

        result = run_bounded_fidelity_gate(evaluate=lambda: _eval(), repair=_repair)
        self.assertTrue(result.failed)
        self.assertEqual(result.reason, "repair_execution_failed")
        self.assertEqual(result.attempts_used, 1)

    def test_technical_failure_never_triggers_a_visual_repair(self):
        called = {"n": 0}

        def _repair(findings):
            called["n"] += 1
            return True

        result = run_bounded_fidelity_gate(
            evaluate=lambda: _eval(technical_ok=False), repair=_repair,
        )
        self.assertEqual(result.outcome, "TECHNICAL_FAILED")
        self.assertEqual(called["n"], 0)

    def test_soft_warning_does_not_fail_the_build(self):
        result = run_bounded_fidelity_gate(
            evaluate=lambda: _eval(source_text="<main><h1>A</h1></main>",
                                   css_text=FA_REV1_CSS, asset_plan={"states": {}},
                                   screenshot_evidence=("uniform repetitive composition",)),
        )
        self.assertEqual(result.outcome, "FIDELITY_WARN")
        self.assertFalse(result.failed)


class TerminalOutcomeTests(unittest.TestCase):
    def test_outcome_vocabulary_is_closed(self):
        from app.core.design_fidelity import TERMINAL_OUTCOMES
        report = _eval()
        for used, verified, expected in (
            (False, False, "FIDELITY_FAILED"),
            (True, True, "FIDELITY_REPAIRED"),
            (True, False, "FIDELITY_REPAIR_FAILED"),
        ):
            out = overall_terminal_outcome(report, repair_used=used, repair_verified=verified)
            self.assertEqual(out, expected)
            self.assertIn(out, TERMINAL_OUTCOMES)

    def test_technical_terminal_outcome(self):
        report = _eval(technical_ok=False)
        self.assertEqual(
            overall_terminal_outcome(report, repair_used=False, repair_verified=False),
            "TECHNICAL_FAILED",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
