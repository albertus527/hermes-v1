"""V4-B2 → Design Autonomy Restoration regression tests.

Locks the scoped correction approved as "Approve Design Autonomy Restoration":

* **A — design authority.** The owner's ``design_intent`` stays mandatory and
  verbatim; inferred composition/typography/layout/hierarchy/colour lines are
  marked OPTIONAL references; FRONTEND is told it owns the final art direction;
  OpenViking retrieval + provenance are untouched; the PRD requirements stay
  mandatory.
* **B — placeholder presentation.** Conceptual-asset disclosure stays truthful
  and anti-fabrication is preserved, but the instruction no longer demands a
  repeated, visually dominant PLACEHOLDER badge on every element.
* **C — font scanner.** CSS ``var()``/``@font-face`` indirection is now scanned
  (a used overused font is caught), literal declarations still work, and an
  UNUSED variable never creates a false positive.
* **D — invariants.** Guidance parity/digest, document lifecycle, and the
  scanner's CLEAN/DEGRADED/FAILED semantics are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.asset_strategy import (
    ASSET_CONCEPTUAL,
    plan_assets,
)
from app.core.design_guidance import (
    ART_DIRECTION_PREAMBLE,
    DESIGN_RECOMMENDED,
    REFERENCE_DERIVED,
    USER_SPECIFIED,
    compile_design_guidance,
)
from app.core.document_contracts import (
    DESIGN_GUIDANCE_REQUIRED_SECTIONS,
    extract_sections,
    validate_design_guidance,
)
from app.core.document_guidance import build_document_guidance, guidance_for_workspace
from app.core import font_audit


# ---------------------------------------------------------------------------
# Fixtures (kept minimal: a brief + a candidate like the existing suites)
# ---------------------------------------------------------------------------


class _Candidate:
    def __init__(self, descriptor="", label="", palette=(), source_uri="",
                 source_revision=""):
        self.descriptor = descriptor
        self.label = label
        self.palette = palette
        self.source_uri = source_uri
        self.source_revision = source_revision


class _Decision:
    def __init__(self, candidate):
        self.selected = candidate


BRIEF = {
    "name": "Atelier Forma",
    "what": "Landing page untuk studio arsitektur, showcase 3 proyek",
    "why": "Agar pengunjung memahami karya dan menghubungi studio",
    "design_intent": "editorial minimalis, premium. Tipografi elegan, off-white, charcoal, aksen terracotta.",
}
BRIEF_NO_INTENT = {
    "name": "Northcut",
    "what": "barbershop booking page",
    "why": "booking",
}


def _guidance(brief=BRIEF, candidate=None):
    return compile_design_guidance(
        brief, _Decision(candidate or _Candidate()),
        project_id="p", source_revision=1,
    )


# ---------------------------------------------------------------------------
# A — design authority
# ---------------------------------------------------------------------------


class TestDesignAuthority:
    def test_owner_intent_is_mandatory_and_verbatim(self):
        g = _guidance()
        s = extract_sections(g)
        assert BRIEF["design_intent"] in s["design_intent"]
        assert USER_SPECIFIED in s["design_intent"]

    def test_owner_intent_marks_only_user_specified_class_mandatory(self):
        g = _guidance()
        # The preamble names exactly one mandatory provenance class.
        assert "user-specified" in ART_DIRECTION_PREAMBLE
        assert "MANDATORY" in ART_DIRECTION_PREAMBLE

    def test_inferred_recommendations_are_explicitly_optional(self):
        g = _guidance()
        s = extract_sections(g)
        # The composition facet carries an inferred recommendation...
        assert DESIGN_RECOMMENDED in s["composition"]
        # ...and that marker states it is OPTIONAL and FRONTEND owns the choice.
        assert "optional" in DESIGN_RECOMMENDED.lower()
        assert "frontend owns" in DESIGN_RECOMMENDED.lower()

    def test_reference_derived_is_optional_not_user_specified(self):
        assert "optional" in REFERENCE_DERIVED.lower()
        assert "not user-specified" in REFERENCE_DERIVED

    def test_preamble_grants_frontend_art_direction(self):
        g = _guidance()
        assert ART_DIRECTION_PREAMBLE in g
        for owned in (
            "hero composition", "typography pairing", "imagery treatment",
            "section rhythm", "responsive layout", "colour-accent placement",
        ):
            assert owned in ART_DIRECTION_PREAMBLE, owned

    def test_preamble_precedes_the_first_section(self):
        """The authority statement is a preamble, not a contract section."""
        g = _guidance()
        body = g.split("---", 2)[-1]
        assert body.index(ART_DIRECTION_PREAMBLE) < body.index("## Brand Character")

    def test_section_contract_is_unchanged(self):
        g = _guidance()
        s = extract_sections(g)
        for required in DESIGN_GUIDANCE_REQUIRED_SECTIONS:
            assert required in s, required
        assert validate_design_guidance(g)[1] == []

    def test_frontend_prompt_states_art_direction_ownership(self):
        from app.hermes.adapter import HermesAdapter

        prompt = HermesAdapter.__dict__["_build_frontend_prompt"](
            object.__new__(HermesAdapter),
            BRIEF,
            Path("/tmp/ws"),
        )
        flat = " ".join(prompt.split())
        assert "You OWN the art direction" in flat
        assert "OPTIONAL" in flat
        # The mandatory owner intent still reaches the prompt verbatim.
        assert BRIEF["design_intent"] in flat

    def test_guidance_block_keeps_requirements_mandatory(self):
        block = build_document_guidance("PRD body", None, _guidance())
        assert "MANDATORY" in block
        assert "OPTIONAL" in block
        # The no-fabrication rule is preserved.
        assert "fabricate" in block.lower()

    def test_retrieval_and_provenance_are_preserved(self):
        cand = _Candidate(descriptor="editorial grid with generous whitespace",
                          label="Refero Editorial", source_uri="viking://design/editorial",
                          source_revision="r7")
        g = _guidance(candidate=cand)
        s = extract_sections(g)
        assert "viking://design/editorial@r7" in s["approved_resources"]


# ---------------------------------------------------------------------------
# B — placeholder presentation
# ---------------------------------------------------------------------------


class TestPlaceholderPresentation:
    def test_conceptual_state_and_antifabrication_preserved(self):
        plan = plan_assets(BRIEF)
        assert plan["states"]["showcase"] == ASSET_CONCEPTUAL
        joined = " ".join(plan["instructions"]).lower()
        # Still honest, still anti-fabrication, still varied.
        assert "conceptual" in joined
        assert "never imply" in joined
        assert "vary" in joined

    def test_no_per_element_dominant_badge_instruction(self):
        joined = " ".join(plan_assets(BRIEF)["instructions"]).lower()
        # The old, over-emphatic instruction is gone.
        assert "label each conceptual visual as a placeholder" not in joined
        # ...and the replacement explicitly forbids the dominant badge.
        assert "do not stamp a repeated" in joined

    def test_section_level_disclosure_is_allowed(self):
        joined = " ".join(plan_assets(BRIEF)["instructions"]).lower()
        assert "section level" in joined

    def test_missing_launch_critical_still_surfaced(self):
        plan = plan_assets(BRIEF)
        assert plan["missing_launch_critical"]
        assert "showcase content" in " ".join(plan["missing_launch_critical"])

    def test_no_business_fact_invented(self):
        plan = plan_assets(BRIEF)
        blob = " ".join(
            list(plan["instructions"]) + list(plan["missing_launch_critical"])
            + list(plan["missing_optional"])
        ).lower()
        for invented in ("proyek abc", "client xyz", "award", "founded in", "since 19"):
            assert invented not in blob


# ---------------------------------------------------------------------------
# C — font scanner
# ---------------------------------------------------------------------------


class TestFontScanner:
    def test_literal_overused_font_is_caught(self):
        css = "h1 { font-family: Fraunces, serif; }"
        found = {f["font"] for f in font_audit.audit_css(css)}
        assert "fraunces" in found

    def test_var_indirected_used_font_is_caught(self):
        css = (
            ":root { --font-display: 'Fraunces', serif; "
            "--font-sans: 'Inter', sans-serif; }\n"
            "h1 { font-family: var(--font-display); }\n"
            "body { font-family: var(--font-sans); }\n"
        )
        found = {f["font"] for f in font_audit.audit_css(css)}
        assert found == {"fraunces", "inter"}

    def test_unused_variable_never_false_positives(self):
        css = (
            ":root { --font-display: 'Fraunces', serif; }\n"
            "body { font-family: Georgia, serif; }\n"
        )
        assert font_audit.audit_css(css) == []

    def test_font_face_declaration_alone_is_not_a_usage(self):
        css = '@font-face { font-family: "Inter"; src: url(i.woff2); }'
        assert font_audit.audit_css(css) == []

    def test_undefined_variable_invents_nothing(self):
        assert font_audit.audit_css("body { font-family: var(--missing); }") == []

    def test_var_fallback_naming_overused_font_is_caught(self):
        css = "body { font-family: var(--x, 'Inter', sans-serif); }"
        assert {f["font"] for f in font_audit.audit_css(css)} == {"inter"}

    def test_distinctive_fonts_are_not_flagged(self):
        css = "h1 { font-family: 'Playfair Display', serif; }"
        assert font_audit.audit_css(css) == []

    def test_generic_fallbacks_are_not_flagged(self):
        css = "body { font-family: 'Familjen Grotesk', Roboto, Helvetica, sans-serif; }"
        assert font_audit.audit_css(css) == []

    def test_no_font_pairing_is_prescribed(self):
        # The audit reports the problem; it never names a replacement.
        found = font_audit.audit_css("h1 { font-family: Inter, sans-serif; }")
        assert found and set(found[0]) == {"font", "via_var", "source"}

    def test_workspace_audit_reads_src_css(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.css").write_text(
            ":root { --d: 'Fraunces', serif; }\nh1 { font-family: var(--d); }\n",
            encoding="utf-8",
        )
        found = font_audit.audit_workspace(tmp_path)
        assert {f["font"] for f in found} == {"fraunces"}
        assert found[0]["via_var"] is True


class TestScannerIntegration:
    """The audit feeds the scanner as ordinary overused-font findings."""

    def _scanner(self, tmp_path, **kw):
        from app.core.critic_repair import ImpeccableScanner

        root = tmp_path / "skills" / "impeccable"
        (root / "scripts" / "detector").mkdir(parents=True)
        (root / "scripts" / "detect.mjs").write_text("// e\n", encoding="utf-8")
        (root / "scripts" / "detector" / "detect-antipatterns.mjs").write_text(
            "// e\n", encoding="utf-8"
        )
        for pkg in ("htmlparser2", "css-select", "css-tree", "domutils"):
            (root / "node_modules" / pkg).mkdir(parents=True)
        return ImpeccableScanner(
            skill_root=root, node_executable="/usr/bin/node", **kw
        )

    def _clean_runner(self):
        from types import SimpleNamespace

        def runner(argv, **kwargs):
            return SimpleNamespace(stdout="[]", returncode=0, stderr="")

        return runner

    def test_clean_engine_plus_var_font_becomes_findings(self, tmp_path):
        ws = tmp_path / "project"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "index.css").write_text(
            ":root { --d: 'Fraunces', serif; }\nh1 { font-family: var(--d); }\n",
            encoding="utf-8",
        )
        scanner = self._scanner(tmp_path)
        scanner._runner = self._clean_runner()
        result = scanner.scan(ws)
        assert result.state == "FINDINGS"
        assert any(f.rule_id == "overused-font" for f in result.findings)

    def test_font_audit_disabled_restores_engine_only(self, tmp_path):
        ws = tmp_path / "project"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "index.css").write_text(
            ":root { --d: 'Fraunces', serif; }\nh1 { font-family: var(--d); }\n",
            encoding="utf-8",
        )
        scanner = self._scanner(tmp_path, font_audit_enabled=False)
        scanner._runner = self._clean_runner()
        assert scanner.scan(ws).state == "CLEAN"

    def test_audit_never_upgrades_a_failed_scan(self, tmp_path):
        from types import SimpleNamespace

        ws = tmp_path / "project"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "index.css").write_text(
            "h1 { font-family: Inter, sans-serif; }\n", encoding="utf-8"
        )
        scanner = self._scanner(tmp_path)
        scanner._runner = lambda *a, **k: SimpleNamespace(
            stdout="", returncode=1, stderr=""
        )
        # A failed engine scan stays FAILED even though a font IS present.
        assert scanner.scan(ws).state == "FAILED"

    def test_audit_does_not_duplicate_an_engine_finding(self, tmp_path):
        import json
        from types import SimpleNamespace

        ws = tmp_path / "project"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "index.css").write_text(
            "h1 { font-family: Inter, sans-serif; }\n", encoding="utf-8"
        )
        engine = json.dumps([{
            "antipattern": "overused-font",
            "name": "Overused font",
            "description": "Inter is overused.",
            "severity": "warning",
            "category": "slop",
            "file": "src/index.css",
            "line": 1,
            "snippet": "font-family: Inter",
        }])
        scanner = self._scanner(tmp_path)
        scanner._runner = lambda *a, **k: SimpleNamespace(
            stdout=engine, returncode=2, stderr=""
        )
        result = scanner.scan(ws)
        assert result.state == "FINDINGS"
        # Exactly one overused-font finding: the engine's, not a duplicate.
        assert sum(1 for f in result.findings if f.rule_id == "overused-font") == 1


# ---------------------------------------------------------------------------
# D — invariants (parity, lifecycle, digest)
# ---------------------------------------------------------------------------


class TestInvariants:
    def test_guidance_digest_is_stable(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "PRD.md").write_text(
            "---\ndocument: prd\nproject_id: p\nschema_version: 1\n"
            "source_revision: 1\ngenerator: x/y\ngenerator_version: 1\n---\n\n"
            "## Requirements\n\nr\n\n## Scope\n\ns\n\n## Acceptance Criteria\n\na\n",
            encoding="utf-8",
        )
        (ws / "DesignGuidance.md").write_text(_guidance(), encoding="utf-8")
        b1, d1 = guidance_for_workspace(ws)
        b2, d2 = guidance_for_workspace(ws)
        assert b1 == b2 and d1 == d2

    def test_document_lifecycle_reads_back_unchanged(self):
        g = _guidance()
        ident, violations = validate_design_guidance(g)
        assert violations == []
        assert ident is not None

    def test_asset_strategy_still_reaches_the_frontend_block(self):
        block = build_document_guidance(None, None, _guidance())
        assert "## Asset Strategy" in block
        assert ASSET_CONCEPTUAL in block
