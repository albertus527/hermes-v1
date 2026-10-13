"""N4 — bounded PRD/Design guidance for FRONTEND.

Covers the N4 deliverables:

* the guidance block is **bounded** (hard character ceiling) and says so when
  truncated;
* it is **DATA** — clearly labelled application-generated, lower-trust;
* it is **deterministic** (same documents ⇒ byte-identical block and digest);
* an empty document set yields ``""`` so the legacy/pre-N4 prompt is preserved;
* the **guidance digest** is a stable parity anchor that changes when either
  document changes, so QA can prove it evaluated the same version FRONTEND did.

No live LLM, browser, npm, network, or subprocess is required.
"""
from __future__ import annotations

import unittest

from app.core.document_guidance import (
    GUIDANCE_FOOTER,
    GUIDANCE_HEADER,
    MAX_GUIDANCE_CHARS,
    build_document_guidance,
    guidance_digest,
    guidance_versions,
)

_PRD = "---\ndocument: prd\n---\n\n## Requirements\n\nA boutique landing page.\n"
_DESIGN = "---\ndocument: design\n---\n\n## Palette\n\nInk and bone.\n"


class GuidanceBlockTests(unittest.TestCase):
    def test_empty_documents_yield_empty_block(self):
        self.assertEqual(build_document_guidance(None, None), "")
        self.assertEqual(build_document_guidance("", ""), "")

    def test_block_is_labelled_and_delimited(self):
        block = build_document_guidance(_PRD, _DESIGN)
        self.assertIn(GUIDANCE_HEADER, block)
        self.assertIn(GUIDANCE_FOOTER, block)
        # DATA framing, not an instruction.
        self.assertIn("DATA supplied by the application", block)

    def test_both_documents_appear(self):
        block = build_document_guidance(_PRD, _DESIGN)
        self.assertIn("A boutique landing page.", block)
        self.assertIn("Ink and bone.", block)

    def test_only_prd_still_renders(self):
        block = build_document_guidance(_PRD, None)
        self.assertIn("A boutique landing page.", block)
        self.assertNotIn("Ink and bone.", block)

    def test_block_is_bounded(self):
        huge = "x" * (MAX_GUIDANCE_CHARS * 3)
        block = build_document_guidance(huge, huge)
        self.assertLessEqual(len(block), MAX_GUIDANCE_CHARS + 200)
        self.assertIn("truncated", block)

    def test_hard_backstop_holds_even_when_overhead_alone_exceeds_ceiling(self):
        # A ceiling smaller than the fixed framing cannot be satisfied by trimming
        # the body; the hard backstop must still hold the line.
        block = build_document_guidance(_PRD, _DESIGN, max_chars=40)
        self.assertLessEqual(len(block), 40)

    def test_deterministic(self):
        a = build_document_guidance(_PRD, _DESIGN)
        b = build_document_guidance(_PRD, _DESIGN)
        self.assertEqual(a, b)


class GuidanceDigestTests(unittest.TestCase):
    def test_digest_is_stable(self):
        self.assertEqual(
            guidance_digest(_PRD, _DESIGN), guidance_digest(_PRD, _DESIGN)
        )

    def test_digest_changes_when_prd_changes(self):
        self.assertNotEqual(
            guidance_digest(_PRD, _DESIGN),
            guidance_digest(_PRD + "\nextra", _DESIGN),
        )

    def test_digest_changes_when_design_changes(self):
        self.assertNotEqual(
            guidance_digest(_PRD, _DESIGN),
            guidance_digest(_PRD, _DESIGN + "\nextra"),
        )

    def test_prd_and_design_are_not_interchangeable(self):
        # Swapping the two documents must change the digest, or the parity
        # anchor would not distinguish which document is which.
        self.assertNotEqual(
            guidance_digest(_PRD, _DESIGN),
            guidance_digest(_DESIGN, _PRD),
        )

    def test_versions_returns_per_document_digests(self):
        prd_digest, design_digest = guidance_versions(_PRD, _DESIGN)
        self.assertIsNotNone(prd_digest)
        self.assertIsNotNone(design_digest)
        self.assertNotEqual(prd_digest, design_digest)

    def test_versions_none_for_absent_documents(self):
        self.assertEqual(guidance_versions(None, None), (None, None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
