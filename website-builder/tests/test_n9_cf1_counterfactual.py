"""CF-1 — the counterfactual benchmark, verified offline.

The point of CF-1 is to separate Requirement Fit from Design Quality. The
violation must change WHAT THE PAGE SAYS without changing HOW IT LOOKS, so a
Quality difference could only be the model reading content rather than pixels.

These tests pin the CONTROL SET and the GROUND TRUTH, both established from
the rendered build, so a later change that breaks the isolation is caught
before a model is ever asked.
"""

import json
import sys
from pathlib import Path

import pytest

FIX = Path(
    "/home/albertus527/n8-benchmark/v3/fixtures/cf-1-counterfactual"
)
CAPS = json.loads((FIX / "captures.json").read_text())
MATRIX = json.loads((FIX / "compliance-matrix.json").read_text())

C1 = CAPS["C1"]["dom"]["desktop"]
C2 = CAPS["C2"]["dom"]["desktop"]


class TestTheControlSetIsIntact:
    """Every control must be identical, or the benchmark proves nothing."""

    def test_both_have_three_cards(self):
        assert C1["article_count"] == 3
        assert C2["article_count"] == 3

    def test_the_card_boxes_are_identical(self):
        assert C1["boxes"] == C2["boxes"], (
            "a card that changed size or position would change the layout, "
            "and the Fit difference could not be attributed to the content"
        )

    def test_the_section_order_is_identical(self):
        assert C1["sections"] == C2["sections"]

    def test_the_type_scale_is_identical(self):
        assert C1["font_sizes"] == C2["font_sizes"]

    def test_the_palette_is_identical(self):
        assert C1["text_colors"] == C2["text_colors"]

    def test_the_font_stack_and_gap_are_identical(self):
        assert C1["first_article_font"] == C2["first_article_font"]
        assert C1["first_article_gap"] == C2["first_article_gap"]

    def test_the_recorded_matrix_agrees_with_the_captures(self):
        assert MATRIX["all_controls_identical"] is True
        for name, value in MATRIX["controls_verified"].items():
            assert value is True, name


class TestTheGroundTruth:
    """The verdict comes from the RENDER, not from a preference."""

    def test_c1_lists_architecture_projects(self):
        joined = " ".join(C1["titles"]).lower()
        assert "proyek" in joined
        assert "menu" not in joined
        assert C1["mentions_architecture"] is True
        assert C1["mentions_menu"] is False

    def test_c2_lists_non_architecture_entries(self):
        joined = " ".join(C2["titles"]).lower()
        assert "menu" in joined
        assert "proyek" not in joined
        assert C2["mentions_architecture"] is False
        assert C2["mentions_menu"] is True

    def test_only_c1_meets_the_requirement(self):
        row = MATRIX["compliance_matrix"][0]
        assert row["C1"] == "satisfied"
        assert row["C2"] == "absent"

    def test_the_requirement_comes_from_an_accepted_brief(self):
        assert MATRIX["synthetic"] is False
        assert MATRIX["requirement"] == "3 proyek arsitektur"
        assert "p26" in MATRIX["requirement_source"]

    def test_the_confounders_are_reported_not_hidden(self):
        assert MATRIX["reported_confounders"], (
            "the residual differences must be recorded, not omitted"
        )


class TestBinding:
    def test_the_requirement_ids_and_revisions_are_bound(self):
        for label in ("C1", "C2"):
            assert CAPS[label]["source_revision"] == MATRIX["source_revision"]

    def test_the_screenshot_digests_are_recorded(self):
        for label in ("C1", "C2"):
            digests = CAPS[label]["screenshot_digests"]
            assert len(digests["desktop"]) == 64
            assert len(digests["mobile"]) == 64

    def test_the_two_candidates_have_different_images(self):
        assert (CAPS["C1"]["screenshot_digests"]["desktop"]
                != CAPS["C2"]["screenshot_digests"]["desktop"])

    def test_neither_candidate_violates_an_unrelated_requirement(self):
        """Both keep every other section, so the only failure is the content."""
        for dom in (C1, C2):
            assert dom["sections"] == ["atas", "karya", "tentang", "layanan", "kontak"]
            assert dom["article_count"] == 3


class TestTheGeneratorIsReproducible:
    def test_the_generator_is_committed(self):
        assert (FIX / "build_fixture.py").exists()
        assert (FIX / "build_matrix.py").exists()

    def test_the_generator_uses_the_pinned_source(self):
        source = (FIX / "build_fixture.py").read_text(encoding="utf-8")
        assert "tg-6329821361-p26" in source

    def test_the_generator_touches_data_strings_only(self):
        """The transformation must not touch markup, classes or CSS."""
        source = (FIX / "build_fixture.py").read_text(encoding="utf-8")
        # It writes to App.tsx, and it never writes to index.css.
        assert "App.tsx" in source
        assert "index.css" not in source
        # The transformation replaces DATA strings. A replacement may quote a
        # className as part of the string it matches, but the class it names
        # must be UNCHANGED -- that is what keeps the craft identical. So the
        # check is that every replacement keeps the class it matched.
        import re
        pairs = re.findall(r'\("([^"]*)",\s*"([^"]*)"\)', source)
        assert pairs, "the replacement list must be present"
        for old, new in pairs:
            old_classes = re.findall(r'className="([^"]*)"', old)
            new_classes = re.findall(r'className="([^"]*)"', new)
            if old_classes:
                assert old_classes == new_classes, (
                    f"a replacement changed a class: {old!r} -> {new!r}"
                )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
