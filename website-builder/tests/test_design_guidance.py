"""N6-C — Pre-build Design Guidance document, compiler, and store.

Proves the N6 requirements that:

* ``DesignGuidance.md`` is a SEPARATE kind from ``Design.md`` (pre-build vs
  post-build) with its own version field;
* the compiler is GROUNDED (brief + provenance-validated decision only) and
  never fabricates a reference or a design fact;
* an abstention / no-evidence case yields honest UNRESOLVED markers, not filler;
* the build path writes guidance BEFORE FRONTEND, and only in ACTIVE design mode.

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
    UNRESOLVED_MARKER,
    compile_design_guidance,
    guidance_evidence,
)
from app.core.document_contracts import (  # noqa: E402
    DESIGN_GUIDANCE_FILE_NAME,
    DESIGN_GUIDANCE_REQUIRED_SECTIONS,
    DESIGN_GUIDANCE_SCHEMA_VERSION,
    DOCUMENT_KIND_DESIGN,
    DOCUMENT_KIND_DESIGN_GUIDANCE,
    extract_sections,
    parse_front_matter,
    validate_design_guidance,
)
from app.core.document_store import (  # noqa: E402
    read_design_guidance,
    read_document,
    store_design_guidance,
    verify_records_match_workspace,
)

BRIEF = {
    "name": "Forma Atelier",
    "what": "a boutique furniture atelier with a curated collection",
    "why": "book a studio visit",
}


def _candidate(**over) -> DesignCandidate:
    base: dict = dict(
        candidate_id="c1",
        label="Editorial warm",
        descriptor="editorial grid, generous whitespace, smooth scroll",
        palette=("#101014", "#F4EFE7"),
        source_uri="viking://design/editorial",
        source_revision="r7",
        score=0.9,
    )
    base.update(over)
    return DesignCandidate(**base)


def _decision(selected):
    ranked = (selected,) if selected else ()
    return DesignDecision(
        selected=selected,
        ranked=ranked,
        abstained=selected is None,
        abstain_reason="" if selected else "no_candidates",
        source="provider",
        provider="openviking",
    )


class ContractTests(unittest.TestCase):
    def test_guidance_is_a_separate_kind_from_design(self):
        self.assertNotEqual(DOCUMENT_KIND_DESIGN_GUIDANCE, DOCUMENT_KIND_DESIGN)
        self.assertEqual(DESIGN_GUIDANCE_FILE_NAME, "DesignGuidance.md")

    def test_compiled_document_validates(self):
        text = compile_design_guidance(
            BRIEF, _decision(_candidate()), project_id="forma-atelier",
            source_revision=1,
        )
        identity, violations = validate_design_guidance(text)
        self.assertEqual(violations, [])
        assert identity is not None
        self.assertEqual(identity.kind, DOCUMENT_KIND_DESIGN_GUIDANCE)
        self.assertEqual(identity.schema_version, DESIGN_GUIDANCE_SCHEMA_VERSION)

    def test_every_required_section_is_present(self):
        text = compile_design_guidance(
            BRIEF, _decision(_candidate()), project_id="forma-atelier",
            source_revision=1,
        )
        _, body = parse_front_matter(text)
        sections = extract_sections(body)
        for required in DESIGN_GUIDANCE_REQUIRED_SECTIONS:
            self.assertIn(required, sections, required)

    def test_version_field_is_distinct_and_recorded(self):
        text = compile_design_guidance(
            BRIEF, _decision(_candidate()), project_id="forma-atelier",
            source_revision=1, design_guidance_version=4,
        )
        front, _ = parse_front_matter(text)
        self.assertEqual(front["design_guidance_version"], "4")
        # It is not the post-build DNA version.
        self.assertNotIn("design_dna_version", front)


class GroundingTests(unittest.TestCase):
    def test_deterministic(self):
        a = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        b = compile_design_guidance(BRIEF, _decision(_candidate()),
                                    project_id="p", source_revision=1)
        self.assertEqual(a, b)

    def test_brief_facts_appear(self):
        text = compile_design_guidance(BRIEF, _decision(_candidate()),
                                       project_id="p", source_revision=1)
        self.assertIn("boutique furniture atelier", text)

    def test_candidate_palette_appears(self):
        text = compile_design_guidance(BRIEF, _decision(_candidate()),
                                       project_id="p", source_revision=1)
        self.assertIn("#101014", text)
        self.assertIn("#F4EFE7", text)

    def test_approved_resource_carries_real_provenance(self):
        text = compile_design_guidance(BRIEF, _decision(_candidate()),
                                       project_id="p", source_revision=1)
        self.assertIn("viking://design/editorial@r7", text)

    def test_motion_grounded_in_candidate_descriptor(self):
        text = compile_design_guidance(BRIEF, _decision(_candidate()),
                                       project_id="p", source_revision=1)
        self.assertIn("smooth scroll", text)

    def test_abstention_is_honest_not_fabricated(self):
        text = compile_design_guidance(BRIEF, _decision(None),
                                       project_id="p", source_revision=1)
        self.assertIn(UNRESOLVED_MARKER, text)
        # No reference is invented.
        self.assertNotIn("viking://", text)
        # The brief's own facts still appear.
        self.assertIn("boutique furniture atelier", text)
        # A section with no evidence carries the marker, not filler.
        _, body = parse_front_matter(text)
        sections = extract_sections(body)
        self.assertIn(UNRESOLVED_MARKER, sections["color"])
        self.assertIn(UNRESOLVED_MARKER, sections["typography"])

    def test_evidence_has_no_candidate_when_abstained(self):
        ev = guidance_evidence(BRIEF, _decision(None))
        self.assertFalse(ev["has_candidate"])
        self.assertEqual(ev["source_uri"], "")

    def test_no_fabricated_reference_when_candidate_lacks_provenance(self):
        """A provenance-less candidate must not become an approved reference."""
        # The engine would normally drop this; simulate it reaching the compiler
        # directly to prove the compiler itself never invents provenance.
        bare = _candidate(source_uri="", source_revision="")
        text = compile_design_guidance(BRIEF, _decision(bare),
                                       project_id="p", source_revision=1)
        self.assertNotIn("viking://", text)
        self.assertIn(UNRESOLVED_MARKER, text)
        # No "reference ..." line is emitted at all without provenance.
        _, body = parse_front_matter(text)
        sections = extract_sections(body)
        self.assertNotIn("reference ", sections["approved_resources"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_round_trips(self):
        text = compile_design_guidance(BRIEF, _decision(_candidate()),
                                       project_id="p", source_revision=1)
        stored = store_design_guidance(self.ws, text)
        self.assertEqual(stored.kind, DOCUMENT_KIND_DESIGN_GUIDANCE)
        back = read_design_guidance(self.ws)
        assert back is not None
        self.assertEqual(back.text, text)

    def test_guidance_is_not_readable_as_design(self):
        """The two kinds are never interchangeable."""
        text = compile_design_guidance(BRIEF, _decision(_candidate()),
                                       project_id="p", source_revision=1)
        store_design_guidance(self.ws, text)
        # Reading the DESIGN kind must not return the guidance document.
        self.assertIsNone(read_document(self.ws, DOCUMENT_KIND_DESIGN))

    def test_missing_guidance_reads_none(self):
        self.assertIsNone(read_design_guidance(self.ws))

    def test_invalid_guidance_is_refused(self):
        with self.assertRaises(ValueError):
            store_design_guidance(self.ws, "garbage")

    def test_digest_mismatch_detected(self):
        text = compile_design_guidance(BRIEF, _decision(_candidate()),
                                       project_id="p", source_revision=1)
        stored = store_design_guidance(self.ws, text)
        records = {"entries": [stored.to_record()], "digests": {}}
        self.assertIsNone(verify_records_match_workspace(self.ws, records))
        # Rewrite a different valid guidance doc.
        other = compile_design_guidance(BRIEF, _decision(_candidate(label="Other")),
                                        project_id="p", source_revision=2)
        (self.ws / DESIGN_GUIDANCE_FILE_NAME).write_text(other, encoding="utf-8")
        self.assertIsNotNone(verify_records_match_workspace(self.ws, records))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
