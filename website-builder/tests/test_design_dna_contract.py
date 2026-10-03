"""A Design DNA artifact produced to the FRONTEND contract must round-trip.

This is the contract/consumer seam. ``.hermes/skills/website-builder-design-dna/
SKILL.md`` is what FRONTEND is told to produce; ``validate_composed_dna`` and
every persisted-state consumer read top-level keys. Those two documents used to
disagree — the contract nested every field under a ``design_dna`` key — and the
disagreement was invisible in exactly the worst way: a nested document reads as
an EMPTY one, so ``typography`` validation returned "nothing to violate" and
reference-bearing projects failed with "Missing or invalid reference evidence"
instead of a statement about the design.

Reading the SKILL.md here is deliberate and is not a source-regex test: the
contract document is the artifact under test. The application is exercised for
real — the real loader, the real parser, the real validators — against a file
written the way the contract says to write it.

No live LLM, browser, or npm is required.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

import yaml

from app.core.composition import ReferenceSnapshot, validate_composed_dna
from app.core.design_dna import (
    CANONICAL_DESIGN_DNA_KEYS,
    DESIGN_DNA_WRAPPER_KEY,
    load_persisted_design_dna,
    unwrap_design_dna,
    validate_typography,
)
from app.core.references import ReferenceItem
from app.core.state import ProjectStateStore
from app.hermes.adapter import HermesAdapter
from app.qa.deterministic import check_design_dna


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / ".hermes" / "skills" / "website-builder-design-dna" / "SKILL.md"

NO_REFERENCES = ReferenceSnapshot({})


def _one_reference() -> ReferenceSnapshot:
    return ReferenceSnapshot({
        "UX": {
            "item": ReferenceItem("UX", "upload", "a" * 64, "image/png", 100).to_dict(),
            "evidence": "Clear hierarchy",
        }
    })


def _contract_document() -> dict:
    """The Minimal Fields block from SKILL.md, as parsed by a real YAML reader."""
    text = SKILL_MD.read_text(encoding="utf-8")
    try:
        section = text.split("## Minimal Fields", 1)[1]
    except IndexError:  # pragma: no cover - a renamed section is a contract change
        raise AssertionError("SKILL.md no longer has a '## Minimal Fields' section")
    fenced = re.search(r"```(?:ya?ml)?\n(.*?)```", section, re.DOTALL)
    assert fenced, "the Minimal Fields section is no longer a fenced code block"
    document = yaml.safe_load(fenced.group(1))
    assert isinstance(document, dict), type(document)
    return document


def _document(**overrides) -> dict:
    """A realistic canonical document, not the empty contract skeleton."""
    document = {
        "version": 1,
        "brand_personality": "premium, modern, approachable",
        "palette": {"primary": "#1a1a2e", "background": "#ffffff", "text": "#111111"},
        "typography": {"heading_font": "Inter", "body_font": "Source Sans 3"},
        "spacing": {"density": "comfortable"},
        "page_inventory": ["home"],
        "layout": {"navigation": "top-bar"},
        "motion": {"enabled": True},
        "primary_cta": {"label": "Book now", "destination": None},
        "assets": [],
        "verified_content": {"name": "Northcut"},
        "unresolved_facts": [],
    }
    document.update(overrides)
    return document


class TestContractShape(unittest.TestCase):
    """The documented contract IS the shape the application reads."""

    def test_contract_fields_are_at_the_top_level(self):
        """Regression: the contract nested everything under a ``design_dna`` key.

        That is not a cosmetic difference — a top-level reader sees no
        ``typography``, so the "at most 2 font families" rule silently stops
        being enforced for every artifact FRONTEND writes to contract.
        """
        document = _contract_document()
        self.assertNotIn(
            DESIGN_DNA_WRAPPER_KEY,
            document,
            "SKILL.md must document the flat shape: wrapping the document makes "
            "every top-level contract field invisible to the application",
        )
        missing = [k for k in CANONICAL_DESIGN_DNA_KEYS if k not in document]
        self.assertEqual(missing, [], f"contract no longer documents: {missing}")

    def test_documented_canonical_keys_match_the_loader(self):
        """The key list and the contract cannot drift apart silently."""
        self.assertEqual(
            set(_contract_document()),
            set(CANONICAL_DESIGN_DNA_KEYS),
            "CANONICAL_DESIGN_DNA_KEYS and the SKILL.md contract have diverged",
        )


class TestRoundTrip(unittest.TestCase):
    """Contract artifact -> disk -> application parser -> validate_composed_dna."""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.adapter = HermesAdapter(
            ProjectStateStore(self.tmpdir / "state"),
            hermes_home=self.tmpdir / ".hermes-website",
            repo_root=self.tmpdir / "repo",
        )
        self.workspace = self.tmpdir / "ws"
        (self.workspace / "src").mkdir(parents=True)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _write(self, document: dict) -> Path:
        path = self.workspace / "design-dna.json"
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    def test_a_canonical_artifact_round_trips_through_the_parser(self):
        self._write(_document())

        parsed = self.adapter._parse_frontend_response(
            '{"success": true, "design_dna_path": "design-dna.json"}', self.workspace
        )

        self.assertTrue(parsed["success"], parsed)
        self.assertEqual(parsed["design_dna"], _document())
        validate_composed_dna(parsed["design_dna"], NO_REFERENCES)

    def test_a_nested_artifact_still_round_trips_through_the_parser(self):
        """A legacy/wrapped artifact is accepted, at the loader boundary only."""
        self._write({DESIGN_DNA_WRAPPER_KEY: _document()})

        parsed = self.adapter._parse_frontend_response(
            '{"success": true, "design_dna_path": "design-dna.json"}', self.workspace
        )

        self.assertTrue(parsed["success"], parsed)
        self.assertEqual(parsed["design_dna"], _document())
        validate_composed_dna(parsed["design_dna"], NO_REFERENCES)

    def test_a_wrapped_artifact_does_not_bypass_reference_validation(self):
        """THE defect: a wrapped document validated as if it were empty.

        With references assigned, the top-level read of ``reference_synthesis``
        found nothing and Phase 7 failed with "Missing or invalid reference
        evidence" — a statement about the document that was false. The nested
        document carries a valid synthesis, so unwrapping is the only thing that
        makes it pass.
        """
        self._write({DESIGN_DNA_WRAPPER_KEY: _document(reference_synthesis={
            "UX": "Original used a strict two-column grid with a large hero.",
        })})

        parsed = self.adapter._parse_frontend_response(
            '{"success": true, "design_dna_path": "design-dna.json"}', self.workspace
        )

        validate_composed_dna(parsed["design_dna"], _one_reference())

    def test_a_wrapped_artifact_cannot_hide_a_reference_violation(self):
        """The mirror image: unwrapping must expose a real violation, not hide one."""
        self._write({DESIGN_DNA_WRAPPER_KEY: _document(reference_synthesis={})})

        parsed = self.adapter._parse_frontend_response(
            '{"success": true, "design_dna_path": "design-dna.json"}', self.workspace
        )

        with self.assertRaises(ValueError) as caught:
            validate_composed_dna(parsed["design_dna"], _one_reference())
        self.assertIn("reference", str(caught.exception).lower())

    def test_the_loader_surfaces_the_fields_a_reader_needs(self):
        """What the unwrap is FOR: top-level contract fields become readable.

        ``typography`` is the clearest case — it is the only contract field the
        application reasons about structurally.
        """
        self._write({DESIGN_DNA_WRAPPER_KEY: _document()})

        parsed = self.adapter._parse_frontend_response(
            '{"success": true, "design_dna_path": "design-dna.json"}', self.workspace
        )

        self.assertEqual(
            parsed["design_dna"]["typography"],
            {"heading_font": "Inter", "body_font": "Source Sans 3"},
        )
        self.assertEqual(parsed["design_dna"]["version"], 1)
        self.assertEqual(parsed["design_dna"]["palette"]["primary"], "#1a1a2e")

    def test_a_wrapped_artifact_keeps_sibling_contract_fields(self):
        """An observed artifact hoisted two fields out of the wrapper.

        Dropping them would silently lose recorded facts, so a sibling fills a
        gap only — the wrapped body stays authoritative for what it defines.
        """
        document = _document()
        self._write({
            DESIGN_DNA_WRAPPER_KEY: {k: v for k, v in document.items()
                                     if k != "unresolved_facts"},
            "unresolved_facts": ["pricing"],
        })

        parsed = self.adapter._parse_frontend_response(
            '{"success": true, "design_dna_path": "design-dna.json"}', self.workspace
        )

        self.assertEqual(parsed["design_dna"]["unresolved_facts"], ["pricing"])

    def test_qa_and_the_build_read_the_same_document(self):
        """QA must not disagree with the build about what the file contains."""
        for document in (_document(), {DESIGN_DNA_WRAPPER_KEY: _document()}):
            with self.subTest(wrapped=DESIGN_DNA_WRAPPER_KEY in document):
                self._write(document)
                self.assertTrue(check_design_dna(self.workspace))


class TestLoaderBoundary(unittest.TestCase):
    """``unwrap_design_dna`` never guesses, and never rewrites a good document."""

    def test_a_canonical_document_is_returned_unchanged(self):
        document = _document()
        unwrapped, was_wrapped = unwrap_design_dna(document)
        self.assertIs(unwrapped, document)
        self.assertFalse(was_wrapped)

    def test_unusable_input_is_returned_untouched_rather_than_reinterpreted(self):
        for raw in ([], "dna", 7, None, {}, {DESIGN_DNA_WRAPPER_KEY: {}},
                    {DESIGN_DNA_WRAPPER_KEY: []}, {DESIGN_DNA_WRAPPER_KEY: None}):
            with self.subTest(raw=raw):
                unwrapped, was_wrapped = unwrap_design_dna(raw)
                self.assertIs(unwrapped, raw)
                self.assertFalse(was_wrapped)

    def test_missing_unreadable_or_empty_documents_are_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            absent = workspace / "design-dna.json"
            self.assertIsNone(load_persisted_design_dna(absent))

            broken = workspace / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_persisted_design_dna(broken))

            for raw in ("[]", "null", "{}", '"dna"'):
                with self.subTest(raw=raw):
                    path = workspace / "raw.json"
                    path.write_text(raw, encoding="utf-8")
                    self.assertIsNone(load_persisted_design_dna(path))


class TestTypographySeesTheDocument(unittest.TestCase):
    """Whatever shape the artifact arrived in, the same document is validated.

    Note the rule is narrower than its prose: ``validate_typography`` reads only
    ``heading_font`` and ``body_font``, so two keys can never exceed its own
    limit of 2. These tests pin the shape contract, not the font rule.
    """

    def test_both_shapes_reach_the_same_typography_document(self):
        self.assertEqual(validate_typography(_document()), True)
        unwrapped, _ = unwrap_design_dna({DESIGN_DNA_WRAPPER_KEY: _document()})
        self.assertEqual(validate_typography(unwrapped), True)

    def test_an_empty_typography_block_is_still_valid(self):
        self.assertEqual(validate_typography(_document(typography={})), True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()