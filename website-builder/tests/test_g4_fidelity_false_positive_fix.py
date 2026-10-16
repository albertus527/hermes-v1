"""G4 recovery — P1 fidelity detector fixes (negation + non-rendered strings).

Locks the two confirmed false-positive fixes approved after the Lumen Studio p3
(G4) build:

* ``conceptual-presented-as-real`` must be NEGATION-AWARE: an honest disclaimer
  ("bukan proyek selesai") is not a claim, while a genuine (even MIXED-sentence)
  claim still blocks.
* ``repeated-card-content`` must only consider RENDERED visible content: import /
  require module specifiers are never card labels, while genuine repeated
  visible labels (including numbered placeholders) still block.

It also re-verifies that the ORIGINAL p3 source no longer produces the two
unsupported findings, and that the corrected pure evaluator reports every
dimension.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_fidelity import (  # noqa: E402
    CONFIDENCE_HIGH,
    DIM_ASSET_HONESTY,
    DIMENSIONS,
    DIM_VISUAL_EXECUTION,
    SEVERITY_HARD,
    VERDICT_PASS,
    evaluate_design_fidelity,
)

ASSET_PLAN = {"states": {"showcase": "CONCEPTUAL"}}
BRIEF = {
    "name": "Lumen Studio",
    "design_intent": (
        "editorial minimalis, premium, dan modern. Gunakan tipografi elegan, "
        "warna off-white, charcoal, serta aksen terracotta"
    ),
}
P3_SRC = Path(
    "/home/albertus527/hermes-website-r2/evidence-rebuild-v4b2/lumen-p3/src"
)


def _rules(source_text, asset_plan=ASSET_PLAN):
    report = evaluate_design_fidelity(
        brief=BRIEF, asset_plan=asset_plan, source_text=source_text,
        css_text=":root{--color-accent:#b45a38}", technical_ok=True,
    )
    return {f.rule_id for f in report.findings}, report


# ---------------------------------------------------------------------------
# P1a — negation-aware real-work claim detection
# ---------------------------------------------------------------------------


class TestNegationAwareClaims:
    @pytest.mark.parametrize("text", [
        "Studi konseptual — bukan proyek selesai",
        "Ini bukan karya kami, hanya studi.",
        "Belum menjadi proyek selesai.",
        "Tidak ada klien kami yang tampil di sini.",
        "This is not our completed projects.",
        "No clients here — not our portfolio.",
        "These are concepts, never our work.",
    ])
    def test_negated_claims_do_not_fire(self, text):
        rules, _ = _rules(f"<p>{text}</p>")
        assert "conceptual-presented-as-real" not in rules

    @pytest.mark.parametrize("text", [
        "Lihat karya kami dan proyek kami.",
        "Browse our portfolio of completed projects.",
        "Klien kami mempercayakan proyek selesai kepada kami.",
    ])
    def test_genuine_claims_still_block(self, text):
        rules, report = _rules(f"<p>{text}</p>")
        assert "conceptual-presented-as-real" in rules
        finding = next(f for f in report.findings
                       if f.rule_id == "conceptual-presented-as-real")
        assert finding.severity == SEVERITY_HARD
        assert finding.confidence == CONFIDENCE_HIGH
        assert finding.dimension == DIM_ASSET_HONESTY

    @pytest.mark.parametrize("text", [
        # Negation in one clause, an affirmative claim in another.
        "Bukan proyek selesai, ini karya kami.",
        "Karya kami nyata, bukan sekadar konsep.",
        "Not a mockup — these are our completed projects.",
    ])
    def test_mixed_sentences_still_fire(self, text):
        rules, _ = _rules(f"<p>{text}</p>")
        assert "conceptual-presented-as-real" in rules

    def test_negated_phrase_counts_as_disclosure(self):
        # "bukan proyek selesai" is an honest disclaimer: neither a claim nor an
        # undisclosed-conceptual finding.
        rules, _ = _rules("<p>Studi arsitektur. Bukan proyek selesai.</p>")
        assert "conceptual-presented-as-real" not in rules
        assert "conceptual-not-disclosed" not in rules

    def test_distant_negation_does_not_launder_a_claim(self):
        # A negation far away (previous sentence) must NOT suppress a claim.
        rules, _ = _rules(
            "<p>Kami tidak menjual jasa fotografi. Lihat karya kami.</p>"
        )
        assert "conceptual-presented-as-real" in rules


# ---------------------------------------------------------------------------
# P1b — repeated-card detection is visible-content only
# ---------------------------------------------------------------------------


class TestVisibleContentOnly:
    @pytest.mark.parametrize("src", [
        "import ConceptArt from './ConceptArt'\n"
        "import C2 from './ConceptArt'\nimport C3 from './ConceptArt'",
        "const a = require('./ConceptArt');"
        "const b = require('./ConceptArt');const c = require('./ConceptArt')",
        "import './styles.css'\nimport './styles.css'\nimport './styles.css'",
        "export { x } from '../content'\nexport { y } from '../content'"
        "\nexport { z } from '../content'",
        "const m = import('./ConceptArt');import('./ConceptArt');import('./ConceptArt')",
    ])
    def test_module_specifiers_are_never_card_labels(self, src):
        rules, _ = _rules(src)
        assert "repeated-card-content" not in rules

    def test_import_path_does_not_mask_real_repetition(self):
        # A real repeated visible label still fires even alongside imports.
        src = (
            "import A from './ConceptArt'\n"
            "const cards = ['Layanan Studio','Layanan Studio','Layanan Studio']"
        )
        rules, _ = _rules(src)
        assert "repeated-card-content" in rules

    @pytest.mark.parametrize("src", [
        "<div>{['Layanan Studio','Layanan Studio','Layanan Studio']}</div>",
        "<div>{['Contoh Proyek 01','Contoh Proyek 02','Contoh Proyek 03']}</div>",
        "const s = ['Nama proyek menyusul','Nama proyek menyusul','Nama proyek menyusul']",
    ])
    def test_genuine_repeated_visible_content_still_fires(self, src):
        rules, report = _rules(src)
        assert "repeated-card-content" in rules
        finding = next(f for f in report.findings
                       if f.rule_id == "repeated-card-content")
        assert finding.severity == SEVERITY_HARD
        assert finding.dimension == DIM_VISUAL_EXECUTION

    def test_two_similar_items_are_still_not_a_defect(self):
        rules, _ = _rules("<div>['Karya Satu','Karya Satu']</div>")
        assert "repeated-card-content" not in rules


# ---------------------------------------------------------------------------
# P2 — the ORIGINAL p3 artifact no longer produces unsupported findings
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not P3_SRC.exists(), reason="p3 evidence not present")
class TestP3Reverification:
    def _p3_source(self):
        return "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted(P3_SRC.rglob("*"))
            if p.suffix in (".tsx", ".ts", ".css")
        )

    def test_p3_no_longer_flags_the_two_false_positives(self):
        rules, _ = _rules(self._p3_source())
        assert "conceptual-presented-as-real" not in rules
        assert "repeated-card-content" not in rules

    def test_p3_all_dimensions_reported_and_pass(self):
        css = "\n".join(p.read_text(encoding="utf-8")
                        for p in P3_SRC.rglob("*.css"))
        report = evaluate_design_fidelity(
            brief=BRIEF, asset_plan=ASSET_PLAN, source_text=self._p3_source(),
            css_text=css, technical_ok=True,
        )
        assert set(report.dimensions) == set(DIMENSIONS)
        assert report.dimensions[DIM_ASSET_HONESTY] == VERDICT_PASS
        assert report.dimensions[DIM_VISUAL_EXECUTION] == VERDICT_PASS
        assert report.overall == VERDICT_PASS
        assert report.hard_findings() == ()
