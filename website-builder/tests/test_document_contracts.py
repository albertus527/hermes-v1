"""N1 — canonical PRD.md / Design.md document contract tests.

These tests exercise the real, side-effect-free validators in
``app.core.document_contracts``. They are the N1 gate: the document contract
must be deterministic and tested, and existing behaviour must remain unchanged.

No live LLM, browser, npm, network, or subprocess is required.

Coverage:

* front-matter parsing is deterministic and fail-closed;
* serialisation is byte-stable (same document ⇒ identical bytes);
* identity is stable across revisions and revision-bound per release;
* each required field/section is individually load-bearing;
* legacy (no documents) and partial-presence states are handled explicitly;
* Design.md ↔ design-dna.json compatibility is non-contradiction, not equality;
* validation never raises on bad content — it reports stable codes.
"""
from __future__ import annotations

import unittest

from app.core.document_contracts import (
    DESIGN_SCHEMA_VERSION,
    DOCUMENT_KIND_DESIGN,
    DOCUMENT_KIND_PRD,
    ERR_EMPTY_SECTION,
    ERR_INVALID_IDENTIFIER,
    ERR_INVALID_KIND,
    ERR_INVALID_SCHEMA_VERSION,
    ERR_INVALID_VERSION_COUNTER,
    ERR_LEGACY_CONTRADICTION,
    ERR_MALFORMED_FRONT_MATTER,
    ERR_MISSING_FIELD,
    ERR_MISSING_FRONT_MATTER,
    ERR_MISSING_SECTION,
    ERR_UNKNOWN_FIELD,
    ERR_UNTERMINATED_FRONT_MATTER,
    PRD_SCHEMA_VERSION,
    DocumentContractError,
    assess_presence,
    design_dna_compatible,
    document_digest,
    extract_sections,
    identity_from_front_matter,
    is_valid,
    parse_front_matter,
    serialise_document,
    stable_document_id,
    validate_design,
    validate_document,
    validate_prd,
)


def _prd(**overrides) -> str:
    front = {
        "document": "prd",
        "schema_version": str(PRD_SCHEMA_VERSION),
        "project_id": "forma-atelier",
        "source_revision": "1",
        "generator": "requirement-creator/prd",
        "generator_version": "1",
        "requirements_version": "1",
    }
    front.update(overrides)
    body = (
        "## Requirements\n\n"
        "A boutique atelier landing page with three featured collections.\n\n"
        "## Scope\n\n"
        "Home, collections index, and contact.\n\n"
        "## Acceptance Criteria\n\n"
        "- Primary CTA is reachable.\n"
    )
    return serialise_document(front, body)


def _design(**overrides) -> str:
    front = {
        "document": "design",
        "schema_version": str(DESIGN_SCHEMA_VERSION),
        "project_id": "forma-atelier",
        "source_revision": "1",
        "generator": "requirement-creator/design",
        "generator_version": "1",
        "design_dna_version": "1",
    }
    front.update(overrides)
    body = (
        "## Brand Personality\n\nWarm, editorial, understated.\n\n"
        "## Palette\n\nInk #101014 and bone #F4EFE7.\n\n"
        "## Typography\n\nHeading: Fraunces. Body: Inter.\n\n"
        "## Layout\n\nEditorial grid, generous whitespace.\n"
    )
    return serialise_document(front, body)


class FrontMatterParsingTests(unittest.TestCase):
    def test_round_trip_is_byte_stable(self):
        text = _prd()
        front, body = parse_front_matter(text)
        self.assertEqual(serialise_document(front, body), text)

    def test_serialisation_is_deterministic_regardless_of_key_order(self):
        body = "## Requirements\n\nX\n\n## Scope\n\nY\n\n## Acceptance Criteria\n\nZ\n"
        a = serialise_document(
            {"document": "prd", "schema_version": "1", "project_id": "p",
             "source_revision": "1", "generator": "g", "generator_version": "1"},
            body,
        )
        b = serialise_document(
            {"generator_version": "1", "generator": "g", "source_revision": "1",
             "project_id": "p", "schema_version": "1", "document": "prd"},
            body,
        )
        self.assertEqual(a, b)
        self.assertEqual(document_digest(a), document_digest(b))

    def test_missing_fence_is_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            parse_front_matter("## Requirements\n\nNo front matter.\n")
        self.assertEqual(ctx.exception.code, ERR_MISSING_FRONT_MATTER)

    def test_unterminated_fence_is_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            parse_front_matter("---\ndocument: prd\n\nbody with no close\n")
        self.assertEqual(ctx.exception.code, ERR_UNTERMINATED_FRONT_MATTER)

    def test_non_scalar_line_is_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            parse_front_matter("---\ndocument: prd\njust a bare line\n---\n")
        self.assertEqual(ctx.exception.code, ERR_MALFORMED_FRONT_MATTER)

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            parse_front_matter("---\ndocument: prd\ndocument: design\n---\n")
        self.assertEqual(ctx.exception.code, ERR_MALFORMED_FRONT_MATTER)

    def test_values_are_raw_strings_not_coerced(self):
        # A YAML loader would turn "no" into False and "1.0" into 1.0. The
        # restricted parser keeps the literal text so meaning never depends on
        # the parser.
        front, _ = parse_front_matter("---\nflag: no\nnum: 1.0\n---\nbody\n")
        self.assertEqual(front["flag"], "no")
        self.assertEqual(front["num"], "1.0")


class SectionExtractionTests(unittest.TestCase):
    def test_sections_are_normalised(self):
        sections = extract_sections(
            "## Acceptance Criteria\n\nA\n\n## scope\n\nB\n\n### deeper\n\nC\n"
        )
        self.assertEqual(set(sections), {"acceptance_criteria", "scope"})
        self.assertIn("### deeper", sections["scope"])  # deeper heading stays inside

    def test_preamble_before_first_heading_is_ignored(self):
        sections = extract_sections("Preamble text.\n\n## Scope\n\nBody.\n")
        self.assertEqual(set(sections), {"scope"})


class IdentityTests(unittest.TestCase):
    def test_document_id_is_stable_across_revisions(self):
        id1 = identity_from_front_matter(parse_front_matter(_prd(source_revision="1"))[0])
        id2 = identity_from_front_matter(parse_front_matter(_prd(source_revision="7"))[0])
        self.assertEqual(id1.document_id, id2.document_id)
        self.assertEqual(id1.document_id, stable_document_id(DOCUMENT_KIND_PRD, "forma-atelier"))

    def test_revision_id_changes_with_source_revision(self):
        id1 = identity_from_front_matter(parse_front_matter(_prd(source_revision="1"))[0])
        id2 = identity_from_front_matter(parse_front_matter(_prd(source_revision="2"))[0])
        self.assertNotEqual(id1.revision_id, id2.revision_id)

    def test_missing_required_field_reports_code(self):
        text = _prd()
        text = text.replace("generator_version: 1\n", "")
        _, violations = validate_prd(text)
        self.assertTrue(any(v.code == ERR_MISSING_FIELD for v in violations))

    def test_unknown_field_reports_code(self):
        text = _prd(typo_field="x")
        _, violations = validate_prd(text)
        self.assertTrue(any(v.code == ERR_UNKNOWN_FIELD for v in violations))

    def test_invalid_kind_reports_code(self):
        text = _prd(document="poster")
        _, violations = validate_prd(text)
        self.assertTrue(any(v.code == ERR_INVALID_KIND for v in violations))

    def test_unknown_kind_rejected_without_expected_kind(self):
        # The closed set must reject an unknown kind even when no expected_kind
        # is pinned — otherwise only the kind-pin check is load-bearing.
        _, violations = validate_document(_prd(document="poster"))
        self.assertTrue(any(v.code == ERR_INVALID_KIND for v in violations))

    def test_wrong_schema_version_reports_code(self):
        text = _prd(schema_version="999")
        _, violations = validate_prd(text)
        self.assertTrue(any(v.code == ERR_INVALID_SCHEMA_VERSION for v in violations))

    def test_non_integer_counter_reports_code(self):
        text = _prd(source_revision="one")
        _, violations = validate_prd(text)
        self.assertTrue(any(v.code == ERR_INVALID_VERSION_COUNTER for v in violations))

    def test_invalid_project_id_reports_code(self):
        text = _prd(project_id="Not A Slug!")
        _, violations = validate_prd(text)
        self.assertTrue(any(v.code == ERR_INVALID_IDENTIFIER for v in violations))


class SectionRequirementTests(unittest.TestCase):
    def test_valid_prd_passes(self):
        identity, violations = validate_prd(_prd())
        self.assertEqual(violations, [])
        self.assertIsNotNone(identity)
        assert identity is not None  # narrow for the type checker
        self.assertEqual(identity.kind, DOCUMENT_KIND_PRD)

    def test_valid_design_passes(self):
        identity, violations = validate_design(_design())
        self.assertEqual(violations, [])
        self.assertIsNotNone(identity)
        assert identity is not None  # narrow for the type checker
        self.assertEqual(identity.kind, DOCUMENT_KIND_DESIGN)

    def test_each_prd_section_is_load_bearing(self):
        for heading in ("Requirements", "Scope", "Acceptance Criteria"):
            with self.subTest(heading=heading):
                body = _prd().replace(f"## {heading}\n", f"## Not{heading}\n")
                _, violations = validate_prd(body)
                self.assertTrue(any(v.code == ERR_MISSING_SECTION for v in violations))

    def test_empty_required_section_reports_code(self):
        front = {
            "document": "prd", "schema_version": "1", "project_id": "forma-atelier",
            "source_revision": "1", "generator": "requirement-creator/prd",
            "generator_version": "1",
        }
        text = serialise_document(
            front,
            "## Requirements\n\nR\n\n## Scope\n\n## Acceptance Criteria\n\nA\n",
        )
        _, violations = validate_prd(text)
        self.assertTrue(any(v.code == ERR_EMPTY_SECTION for v in violations))

    def test_kind_pinning_rejects_cross_kind_document(self):
        _, violations = validate_prd(_design())
        self.assertTrue(any(v.code == ERR_INVALID_KIND for v in violations))


class NoRaiseContractTests(unittest.TestCase):
    def test_validators_report_rather_than_raise(self):
        for text in (
            "",
            "not a document",
            "---\n",
            "---\ndocument: prd\n---\n",
            "---\ngarbage\n---\nbody\n",
        ):
            with self.subTest(text=text):
                identity, violations = validate_document(text)
                # Either an identity with violations, or no identity and exactly
                # one structural violation. Never an exception.
                self.assertTrue(identity is None or isinstance(violations, list))
                if identity is None:
                    self.assertEqual(len(violations), 1)

    def test_is_valid_predicate_matches_violations(self):
        self.assertTrue(is_valid(_prd(), expected_kind=DOCUMENT_KIND_PRD))
        self.assertFalse(is_valid("", expected_kind=DOCUMENT_KIND_PRD))


class LegacyCompatibilityTests(unittest.TestCase):
    def test_legacy_project_has_no_documents_and_is_supported(self):
        presence = assess_presence(prd_text=None, design_text=None)
        self.assertFalse(presence.has_documents)
        self.assertTrue(presence.legacy)
        self.assertFalse(presence.partial)

    def test_partial_presence_is_flagged_not_legacy(self):
        presence = assess_presence(prd_text=_prd(), design_text=None)
        self.assertFalse(presence.has_documents)
        self.assertFalse(presence.legacy)
        self.assertTrue(presence.partial)

    def test_full_presence(self):
        presence = assess_presence(prd_text=_prd(), design_text=_design())
        self.assertTrue(presence.has_documents)
        self.assertFalse(presence.legacy)
        self.assertFalse(presence.partial)

    def test_design_dna_none_is_compatible(self):
        sections = extract_sections(_design())
        self.assertEqual(design_dna_compatible(sections, None), [])

    def test_design_dna_contradiction_is_reported(self):
        sections = extract_sections(_design())
        dna = {"palette": {"primary": "midnight blue #000080"}}
        violations = design_dna_compatible(sections, dna)
        self.assertTrue(any(v.code == ERR_LEGACY_CONTRADICTION for v in violations))

    def test_design_dna_agreement_is_compatible(self):
        sections = extract_sections(_design())
        dna = {"palette": {"primary": "bone #F4EFE7"}}
        self.assertEqual(design_dna_compatible(sections, dna), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
