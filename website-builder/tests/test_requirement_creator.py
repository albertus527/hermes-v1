"""N2 — Requirement Creator & Git-backed documents.

Covers the N2 deliverables against the approved roadmap:

* the PRD Compiler and Design Guidance Compiler produce documents that
  **validate** under the N1 contract;
* FAST remains authoritative — the compilers never invent business facts;
* compilation is **deterministic** (same input ⇒ byte-identical document);
* documents are stored beside the website source and bound to the release
  revision with no second state machine;
* the feature is **disabled by default** and legacy projects (no documents)
  keep working;
* hydration consistency (recorded digest must match the workspace bytes).

No live LLM, browser, npm, network, or subprocess is required.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.document_contracts import (
    DESIGN_FILE_NAME,
    PRD_FILE_NAME,
    validate_design,
    validate_prd,
)
from app.core.document_store import (
    DEPLOYMENT_DOCUMENTS_KEY,
    document_presence_record,
    document_records,
    documents_present,
    read_document,
    read_documents,
    store_documents,
    verify_records_match_workspace,
    write_document,
)
from app.core.requirement_creator import (
    DESIGN_GENERATOR,
    PRD_GENERATOR,
    UNRESOLVED_MARKER,
    RequirementCreatorConfig,
    RequirementCreatorError,
    compile_design,
    compile_prd,
    config_from_mapping,
)

_BRIEF = {
    "name": "Forma Atelier",
    "what": "a boutique furniture atelier landing page",
    "why": "book a studio visit",
    "why_destination": "https://example.test/book",
}
_DNA = {
    "version": 1,
    "brand_personality": "warm, editorial, understated",
    "palette": {"primary": "#101014", "background": "#F4EFE7"},
    "typography": {"heading_font": "Fraunces", "body_font": "Inter"},
    "layout": "editorial grid with generous whitespace",
}


class ConfigTests(unittest.TestCase):
    def test_default_is_disabled(self):
        cfg = RequirementCreatorConfig()
        self.assertFalse(cfg.enabled)

    def test_missing_mapping_is_disabled(self):
        self.assertFalse(config_from_mapping(None).enabled)
        self.assertFalse(config_from_mapping({}).enabled)

    def test_malformed_enabled_never_enables(self):
        # A truthy-but-not-bool value must not silently enable the feature.
        self.assertFalse(config_from_mapping({"enabled": None}).enabled)
        self.assertFalse(config_from_mapping({"enabled": ""}).enabled)
        self.assertTrue(config_from_mapping({"enabled": True}).enabled)

    def test_generator_version_is_at_least_one(self):
        self.assertEqual(config_from_mapping({"generator_version": 0}).generator_version, 1)
        self.assertEqual(config_from_mapping({"generator_version": -5}).generator_version, 1)
        self.assertEqual(config_from_mapping({"generator_version": "x"}).generator_version, 1)
        self.assertEqual(config_from_mapping({"generator_version": 7}).generator_version, 7)


class PrdCompilerTests(unittest.TestCase):
    def test_compiled_prd_validates(self):
        text = compile_prd(
            _BRIEF, project_id="forma-atelier", source_revision=1,
            requirements_version=3, scope="WEBSITE", readiness="DISCOVERY_READY",
        )
        identity, violations = validate_prd(text)
        self.assertEqual(violations, [])
        assert identity is not None
        self.assertEqual(identity.kind, "prd")
        self.assertEqual(identity.project_id, "forma-atelier")
        self.assertEqual(identity.source_revision, 1)
        self.assertEqual(identity.requirements_version, 3)
        self.assertEqual(identity.generator, PRD_GENERATOR)

    def test_prd_is_deterministic(self):
        a = compile_prd(_BRIEF, project_id="p", source_revision=1)
        b = compile_prd(_BRIEF, project_id="p", source_revision=1)
        self.assertEqual(a, b)

    def test_missing_brief_fields_are_unresolved_not_invented(self):
        text = compile_prd({}, project_id="p", source_revision=1)
        self.assertIn(UNRESOLVED_MARKER, text)
        # No fabricated business fact: the empty brief must not yield a name.
        self.assertNotIn("Forma", text)
        # The Requirements section itself must be unresolved, not filled with
        # invented marketing copy.
        from app.core.document_contracts import extract_sections
        sections = extract_sections(text.split("---\n", 2)[-1])
        self.assertIn(UNRESOLVED_MARKER, sections["requirements"])
        self.assertNotIn("modern, welcoming", text)
        self.assertEqual(validate_prd(text)[1], [])

    def test_invalid_prd_compile_raises(self):
        # If the compiled document does not validate, the compiler must refuse
        # rather than return an invalid document as if it were valid.
        from unittest.mock import patch
        with patch(
            "app.core.requirement_creator.validate_prd",
            return_value=(None, [type("V", (), {"code": "MISSING_SECTION"})()]),
        ):
            with self.assertRaises(RequirementCreatorError):
                compile_prd(_BRIEF, project_id="p", source_revision=1)

    def test_fast_brief_is_carried_verbatim(self):
        text = compile_prd(_BRIEF, project_id="p", source_revision=1)
        self.assertIn(_BRIEF["what"], text)
        self.assertIn(_BRIEF["why"], text)

    def test_revision_is_bound_into_identity(self):
        text = compile_prd(_BRIEF, project_id="p", source_revision=9)
        identity, _ = validate_prd(text)
        assert identity is not None
        self.assertEqual(identity.revision_id, "prd:p:r9")


class DesignCompilerTests(unittest.TestCase):
    def test_compiled_design_validates(self):
        text = compile_design(
            _DNA, project_id="forma-atelier", source_revision=1, design_dna_version=2
        )
        identity, violations = validate_design(text)
        self.assertEqual(violations, [])
        assert identity is not None
        self.assertEqual(identity.kind, "design")
        self.assertEqual(identity.design_dna_version, 2)
        self.assertEqual(identity.generator, DESIGN_GENERATOR)

    def test_design_is_deterministic(self):
        a = compile_design(_DNA, project_id="p", source_revision=1)
        b = compile_design(_DNA, project_id="p", source_revision=1)
        self.assertEqual(a, b)

    def test_empty_dna_yields_unresolved_sections_not_fabrication(self):
        text = compile_design({}, project_id="p", source_revision=1)
        self.assertEqual(validate_design(text)[1], [])
        self.assertIn(UNRESOLVED_MARKER, text)

    def test_dna_values_reach_the_document(self):
        text = compile_design(_DNA, project_id="p", source_revision=1)
        self.assertIn("Fraunces", text)
        self.assertIn("#101014", text)

    def test_non_mapping_dna_is_refused_as_empty_not_crash(self):
        text = compile_design(None, project_id="p", source_revision=1)  # type: ignore[arg-type]
        self.assertEqual(validate_design(text)[1], [])

    def test_invalid_design_compile_raises(self):
        from unittest.mock import patch
        with patch(
            "app.core.requirement_creator.validate_design",
            return_value=(None, [type("V", (), {"code": "MISSING_SECTION"})()]),
        ):
            with self.assertRaises(RequirementCreatorError):
                compile_design(_DNA, project_id="p", source_revision=1)


class StoreTests(unittest.TestCase):
    def test_write_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd_text = compile_prd(_BRIEF, project_id="p", source_revision=1)
            design_text = compile_design(_DNA, project_id="p", source_revision=1)
            prd, design = store_documents(ws, prd_text=prd_text, design_text=design_text)
            assert prd is not None and design is not None
            self.assertEqual(prd.file_name, PRD_FILE_NAME)
            self.assertEqual(design.file_name, DESIGN_FILE_NAME)
            self.assertTrue((ws / PRD_FILE_NAME).exists())
            self.assertTrue((ws / DESIGN_FILE_NAME).exists())
            self.assertTrue(documents_present(ws))
            back = read_documents(ws)
            assert back[0] is not None and back[1] is not None
            self.assertEqual(back[0].digest, prd.digest)
            self.assertEqual(back[1].digest, design.digest)

    def test_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            text = compile_prd(_BRIEF, project_id="p", source_revision=1)
            first = write_document(ws, "prd", text)
            second = write_document(ws, "prd", text)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual((ws / PRD_FILE_NAME).read_text(encoding="utf-8"), text)

    def test_invalid_document_is_refused_before_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            with self.assertRaises(ValueError):
                write_document(ws, "prd", "not a document")
            self.assertFalse((ws / PRD_FILE_NAME).exists())

    def test_write_rejects_an_invalid_document_with_identity(self):
        # A structurally-parseable document with a valid kind but a MISSING
        # required section parses to an identity AND violations. It must be
        # refused by the validation guard itself (not merely by the separate
        # no-identity guard), so this is what proves the validation guard
        # load-bearing.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            text = (
                "---\ndocument: prd\nschema_version: 1\nproject_id: p\n"
                "source_revision: 1\ngenerator: requirement-creator/prd\n"
                "generator_version: 1\n---\n\n## Requirements\n\nonly one section\n"
            )
            with self.assertRaises(ValueError):
                write_document(ws, "prd", text)
            self.assertFalse((ws / PRD_FILE_NAME).exists())

    def test_corrupt_document_reads_none_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / PRD_FILE_NAME).write_bytes(b"\xff\xfe\x00\x01 not utf-8")
            self.assertIsNone(read_document(ws, "prd"))

    def test_unparseable_document_reads_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / PRD_FILE_NAME).write_text("no front matter at all", encoding="utf-8")
            self.assertIsNone(read_document(ws, "prd"))

    def test_legacy_workspace_reads_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.assertIsNone(read_document(ws, "prd"))
            self.assertIsNone(read_document(ws, "design"))
            self.assertFalse(documents_present(ws))
            presence = document_presence_record(ws)
            self.assertTrue(presence["legacy"])
            self.assertFalse(presence["has_documents"])

    def test_partial_presence_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            store_documents(ws, prd_text=compile_prd(_BRIEF, project_id="p", source_revision=1))
            presence = document_presence_record(ws)
            self.assertTrue(presence["partial"])
            self.assertFalse(presence["has_documents"])

    def test_records_none_when_no_documents(self):
        self.assertIsNone(document_records(None, None))

    def test_records_carry_digests_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd, design = store_documents(
                ws,
                prd_text=compile_prd(_BRIEF, project_id="p", source_revision=4),
                design_text=compile_design(_DNA, project_id="p", source_revision=4),
            )
            records = document_records(prd, design)
            self.assertIsNotNone(records)
            assert records is not None
            self.assertEqual(set(records["digests"]), {"prd", "design"})
            kinds = {e["kind"] for e in records["entries"]}
            self.assertEqual(kinds, {"prd", "design"})

    def test_hydration_consistency_passes_when_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd, design = store_documents(
                ws,
                prd_text=compile_prd(_BRIEF, project_id="p", source_revision=1),
                design_text=compile_design(_DNA, project_id="p", source_revision=1),
            )
            self.assertIsNone(
                verify_records_match_workspace(ws, document_records(prd, design))
            )

    def test_hydration_consistency_detects_a_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd, design = store_documents(
                ws,
                prd_text=compile_prd(_BRIEF, project_id="p", source_revision=1),
                design_text=compile_design(_DNA, project_id="p", source_revision=1),
            )
            records = document_records(prd, design)
            # Tamper: the workspace document no longer matches the record.
            (ws / PRD_FILE_NAME).write_text(
                compile_prd(_BRIEF, project_id="p", source_revision=2), encoding="utf-8"
            )
            reason = verify_records_match_workspace(ws, records)
            self.assertIsNotNone(reason)

    def test_hydration_consistency_detects_a_missing_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd, design = store_documents(
                ws,
                prd_text=compile_prd(_BRIEF, project_id="p", source_revision=1),
                design_text=compile_design(_DNA, project_id="p", source_revision=1),
            )
            records = document_records(prd, design)
            (ws / DESIGN_FILE_NAME).unlink()
            self.assertIsNotNone(verify_records_match_workspace(ws, records))


class NoDocumentsKeyForLegacyTests(unittest.TestCase):
    def test_deployment_key_constant_is_stable(self):
        # The key the build path writes must be the one the reader expects.
        self.assertEqual(DEPLOYMENT_DOCUMENTS_KEY, "documents")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
