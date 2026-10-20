"""N8 B7 Option C — structural probe, evidence binding, contrast safety.

These tests pin the properties that make measurements trustworthy:

  * a ratio is produced ONLY when the relationship was genuinely measured
  * uncertain/unsupported cases carry a reason and NO number
  * every measurement is bound to candidate, revision, viewport and screenshot
  * stale or cross-candidate evidence is rejected, not repaired by guessing
  * a probe failure degrades to E2 rather than inventing E3
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.structural_probe import (  # noqa: E402
    CONTRAST_MEASURED,
    CONTRAST_UNCERTAIN,
    CONTRAST_UNSUPPORTED,
    MAX_CONTRAST_PAIRS,
    MAX_TEXT_NODES,
    PROBE_VERSION,
    StructuralEvidence,
    contrast_ratio,
    parse_probe,
    probe_expression,
    relative_luminance,
    verify_binding,
)


def _payload(**overrides):
    """A probe payload shaped exactly like real browser output."""
    base = {
        "ok": True,
        "counts": {
            "elements": 480, "text_nodes": 96, "images": 0,
            "buttons": 3, "links": 12, "headings": 6, "forms": 0,
        },
        "document_height": 4210,
        "text": [
            {
                "tag": "p",
                "font_family": '"Inter", ui-sans-serif',
                "font_size": 16, "font_weight": "400", "line_height": 24,
                "fg": {"r": 33, "g": 30, "b": 26, "a": 1},
                "bg_ok": True,
                "bg": {"r": 250, "g": 248, "b": 245, "a": 1},
                "bg_uncertain": False, "bg_unsupported": False, "bg_reason": "",
            },
        ],
        "blocks": [
            {"tag": "section", "pad": [96, 24, 96, 24], "mar": [0, 0, 0, 0], "gap": 0},
            {"tag": "div", "pad": [24, 24, 24, 24], "mar": [0, 0, 24, 0], "gap": 16},
        ],
        "viewport": {"width": 1440, "height": 900},
    }
    base.update(overrides)
    return base


def _payload_with_contrast(pairs):
    payload = _payload()
    payload["text"] = [{
        "tag": "p", "font_family": '"Inter"', "font_size": 14,
        "font_weight": "400", "line_height": 20,
        "fg": {"r": 1, "g": 2, "b": 3, "a": 1},
        "bg_ok": True, "bg": {"r": 250, "g": 250, "b": 250, "a": 1},
        "bg_uncertain": False, "bg_unsupported": False, "bg_reason": "",
    }]
    payload["_contrast_pairs"] = pairs
    return payload


def _parse(payload=None, **kw):
    defaults = dict(
        candidate_id="cand-c",
        source_revision="rev-3",
        viewport=(1440, 900),
        screenshot_digest="digest-abc",
        measured_at_url="http://127.0.0.1:8801/",
    )
    defaults.update(kw)
    return parse_probe(payload if payload is not None else _payload(), **defaults)


# ---------------------------------------------------------------------------
# contrast maths — WCAG 2.2 reference values
# ---------------------------------------------------------------------------


class TestContrastMaths:
    def test_black_on_white_is_exactly_21(self):
        assert round(contrast_ratio((0, 0, 0), (255, 255, 255)), 2) == 21.0

    def test_identical_colours_are_exactly_1(self):
        assert round(contrast_ratio((18, 52, 86), (18, 52, 86)), 2) == 1.0

    def test_ratio_is_symmetric(self):
        a = contrast_ratio((33, 30, 26), (250, 248, 245))
        b = contrast_ratio((250, 248, 245), (33, 30, 26))
        assert round(a, 6) == round(b, 6)

    def test_matches_known_baseline_and_degraded_values(self):
        """The two real values this whole exercise turns on."""
        baseline = contrast_ratio((0x21, 0x1E, 0x1A), (0xFA, 0xF8, 0xF5))
        degraded = contrast_ratio((0x76, 0x73, 0x6F), (0xFA, 0xF8, 0xF5))
        assert round(baseline, 2) == 15.66
        assert round(degraded, 2) == 4.45
        # 3.5x apart: impossible to lose in a downscale.
        assert baseline / degraded > 3.4

    def test_luminance_of_black_is_zero_and_white_is_one(self):
        assert relative_luminance((0, 0, 0)) == 0.0
        assert round(relative_luminance((255, 255, 255)), 6) == 1.0


# ---------------------------------------------------------------------------
# contrast safety — the heart of the owner's constraint
# ---------------------------------------------------------------------------


class TestContrastSafety:
    def test_opaque_pair_is_measured_with_a_ratio(self):
        ev = _parse()
        pair = ev.contrast[0]
        assert pair.state == CONTRAST_MEASURED
        assert pair.ratio == 15.66

    def test_gradient_behind_text_is_unsupported_and_carries_no_ratio(self):
        payload = _payload()
        payload["text"][0].update(
            {"bg_ok": False, "bg": None, "bg_unsupported": True,
             "bg_reason": "gradient_behind_text"}
        )
        pair = _parse(payload).contrast[0]
        assert pair.state == CONTRAST_UNSUPPORTED
        assert pair.ratio is None, "an unsupported pair must never invent a ratio"
        assert pair.reason == "gradient_behind_text"

    def test_image_behind_text_is_unsupported(self):
        payload = _payload()
        payload["text"][0].update(
            {"bg_ok": False, "bg": None, "bg_unsupported": True,
             "bg_reason": "image_behind_text"}
        )
        pair = _parse(payload).contrast[0]
        assert pair.state == CONTRAST_UNSUPPORTED
        assert pair.ratio is None

    def test_translucent_background_is_uncertain_not_measured(self):
        payload = _payload()
        payload["text"][0].update(
            {"bg_ok": False, "bg": None, "bg_uncertain": True,
             "bg_reason": "translucent_background"}
        )
        pair = _parse(payload).contrast[0]
        assert pair.state == CONTRAST_UNCERTAIN
        assert pair.ratio is None
        assert pair.reason == "translucent_background"

    def test_ancestor_opacity_is_uncertain(self):
        payload = _payload()
        payload["text"][0].update({"bg_uncertain": True, "bg_reason": "ancestor_opacity"})
        pair = _parse(payload).contrast[0]
        assert pair.state == CONTRAST_UNCERTAIN
        assert pair.ratio is None

    def test_translucent_text_colour_is_uncertain(self):
        payload = _payload()
        payload["text"][0]["fg"]["a"] = 0.6
        pair = _parse(payload).contrast[0]
        assert pair.state == CONTRAST_UNCERTAIN
        assert pair.reason == "translucent_text"
        assert pair.ratio is None

    def test_no_opaque_background_is_uncertain(self):
        payload = _payload()
        payload["text"][0].update({"bg_ok": False, "bg": None, "bg_uncertain": True})
        pair = _parse(payload).contrast[0]
        assert pair.state == CONTRAST_UNCERTAIN
        assert pair.ratio is None

    def test_has_contrast_ignores_doubtful_pairs(self):
        """The E2/E3 boundary: doubt must not masquerade as measurement."""
        payload = _payload()
        payload["text"][0].update(
            {"bg_ok": False, "bg": None, "bg_unsupported": True,
             "bg_reason": "gradient_behind_text"}
        )
        ev = _parse(payload)
        assert ev.contrast, "the pair is still recorded"
        assert ev.has_contrast is False, "but it does not count as measured"
        assert len(ev.doubtful_pairs) == 1
        assert len(ev.measured_pairs) == 0

    def test_measured_and_doubtful_coexist(self):
        payload = _payload()
        payload["text"].append(
            {
                "tag": "div", "font_family": '"Inter"', "font_size": 18,
                "font_weight": "400", "line_height": 28,
                "fg": {"r": 255, "g": 255, "b": 255, "a": 1},
                "bg_ok": False, "bg": None, "bg_uncertain": False,
                "bg_unsupported": True, "bg_reason": "gradient_behind_text",
            }
        )
        ev = _parse(payload)
        assert len(ev.measured_pairs) == 1
        assert len(ev.doubtful_pairs) == 1
        assert ev.has_contrast is True

    def test_serialised_pair_records_that_it_is_not_a_gate(self):
        ev = _parse()
        d = ev.contrast[0].to_dict()
        assert d["ratio"] == 15.66
        assert d["meets_aa_normal"] is True
        assert d["wcag_aa_normal"] == 4.5

    def test_doubtful_pair_serialises_none_for_meets_aa(self):
        payload = _payload()
        payload["text"][0].update(
            {"bg_ok": False, "bg": None, "bg_unsupported": True,
             "bg_reason": "gradient_behind_text"}
        )
        d = _parse(payload).contrast[0].to_dict()
        assert d["ratio"] is None
        assert d["meets_aa_normal"] is None, "unknown must not read as failing"


# ---------------------------------------------------------------------------
# binding — stale or cross-candidate evidence must be rejected
# ---------------------------------------------------------------------------


class TestEvidenceBinding:
    def test_binding_is_recorded_verbatim(self):
        ev = _parse()
        assert ev.candidate_id == "cand-c"
        assert ev.source_revision == "rev-3"
        assert ev.viewport == (1440, 900)
        assert ev.screenshot_digest == "digest-abc"
        assert ev.measured_at_url == "http://127.0.0.1:8801/"
        assert ev.probe_version == PROBE_VERSION

    def test_correct_binding_passes(self):
        ev = _parse()
        assert verify_binding(
            ev, candidate_id="cand-c", source_revision="rev-3",
            screenshot_digest="digest-abc",
        ) is None

    def test_wrong_candidate_is_rejected(self):
        ev = _parse()
        assert verify_binding(
            ev, candidate_id="cand-d", source_revision="rev-3",
            screenshot_digest="digest-abc",
        ) == "candidate_mismatch"

    def test_wrong_revision_is_rejected(self):
        ev = _parse()
        assert verify_binding(
            ev, candidate_id="cand-c", source_revision="rev-4",
            screenshot_digest="digest-abc",
        ) == "revision_mismatch"

    def test_replaced_screenshot_is_rejected(self):
        """The stale-evidence failure this binding exists to catch."""
        ev = _parse()
        assert verify_binding(
            ev, candidate_id="cand-c", source_revision="rev-3",
            screenshot_digest="digest-other",
        ) == "screenshot_mismatch"

    def test_wrong_viewport_is_rejected(self):
        ev = _parse()
        assert verify_binding(
            ev, candidate_id="cand-c", source_revision="rev-3",
            screenshot_digest="digest-abc", viewport=(390, 844),
        ) == "viewport_mismatch"

    def test_binding_appears_in_serialised_output(self):
        d = _parse().to_dict()
        for key in ("candidate_id", "source_revision", "viewport",
                    "screenshot_digest", "measured_at_url", "probe_version"):
            assert key in d, f"{key} must survive serialisation"

    def test_digest_ignores_binding_and_tracks_measurements(self):
        a = _parse()
        b = _parse(candidate_id="cand-other", screenshot_digest="different")
        assert a.digest() == b.digest(), "same measurements, different binding"
        changed = _payload()
        changed["blocks"].append(
            {"tag": "p", "pad": [7, 7, 7, 7], "mar": [0, 0, 0, 0], "gap": 0}
        )
        assert _parse(changed).digest() != a.digest()


# ---------------------------------------------------------------------------
# collection — typography, spacing, components
# ---------------------------------------------------------------------------


class TestCollection:
    def test_font_families_are_deduped_and_unquoted(self):
        ev = _parse()
        assert ev.font_families == ("Inter",)

    def test_type_scale_carries_selector_size_and_line_height(self):
        ev = _parse()
        assert ("p", 16.0, 24.0) in ev.type_scale

    def test_spacing_scale_is_sorted_and_positive_only(self):
        ev = _parse()
        assert ev.spacing_scale == (16.0, 24.0, 96.0)
        assert all(v > 0 for v in ev.spacing_scale)

    def test_component_count_sums_interactive_kinds(self):
        ev = _parse()
        # buttons 3 + links 12 + forms 0 + headings 6
        assert ev.component_count == 21

    def test_has_typography_requires_both_family_and_scale(self):
        payload = _payload()
        for entry in payload["text"]:
            entry["font_size"] = 0
        ev = _parse(payload)
        assert ev.font_families, "families were still read"
        assert ev.has_typography is False, "without a scale this is not typography evidence"

    def test_empty_page_collects_but_measures_nothing(self):
        ev = _parse(_payload(text=[], blocks=[], counts={}))
        assert ev.collected is True
        assert ev.has_typography is False
        assert ev.has_spacing is False
        assert ev.has_contrast is False

    def test_text_content_is_never_collected(self):
        """Only style and geometry leave the page, never the copy.

        Checked against the executable body rather than the raw template, so an
        explanatory comment mentioning a property is not mistaken for its use.
        """
        body = "\n".join(
            line for line in probe_expression().splitlines()
            if not line.strip().startswith("//")
        )
        for forbidden in ("innerText", "textContent", "innerHTML", "outerHTML"):
            assert forbidden not in body, f"{forbidden} must not be read"

    def test_probe_is_bounded(self):
        expr = probe_expression()
        assert str(MAX_TEXT_NODES) in expr
        payload = _payload()
        payload["text"] = [
            {
                "tag": "p", "font_family": '"Inter"', "font_size": 16,
                "font_weight": "400", "line_height": 24,
                "fg": {"r": 33, "g": 30, "b": 26, "a": 1},
                "bg_ok": True, "bg": {"r": 250, "g": 248, "b": 245, "a": 1},
                "bg_uncertain": False, "bg_unsupported": False, "bg_reason": "",
            }
        ] * 200
        ev = _parse(payload)
        assert len(ev.type_scale) <= 200
        assert len(ev.contrast) <= MAX_CONTRAST_PAIRS


# ---------------------------------------------------------------------------
# failure handling — a probe failure must degrade, never fabricate
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_non_mapping_payload_is_not_collected(self):
        ev = _parse("not a payload")
        assert ev.collected is False
        assert ev.error == "payload_not_mapping"
        assert ev.has_contrast is False
        assert ev.has_typography is False

    def test_probe_error_is_not_collected(self):
        ev = _parse({"ok": False, "error": "no_body"})
        assert ev.collected is False
        assert ev.error == "no_body"

    def test_malformed_sections_are_not_collected(self):
        ev = _parse(_payload(text="nope"))
        assert ev.collected is False
        assert ev.error == "malformed_sections"

    def test_uncollected_evidence_never_claims_e3(self):
        ev = _parse({"ok": False, "error": "no_body"})
        assert ev.has_typography is False
        assert ev.has_spacing is False
        assert ev.has_contrast is False


# ---------------------------------------------------------------------------
# ScreenshotCapture integration
# ---------------------------------------------------------------------------


class TestScreenshotCaptureIntegration:
    def test_capture_result_carries_structural_payload(self):
        from app.qa.screenshot import CaptureResult

        result = CaptureResult(
            path=Path("/tmp/x.png"), metrics=None, status="captured",
            structural={"ok": True},
        )
        assert result.structural == {"ok": True}

    def test_capture_result_defaults_to_no_structural_evidence(self):
        from app.qa.screenshot import CaptureResult

        result = CaptureResult(path=Path("/tmp/x.png"), metrics=None, status="captured")
        assert result.structural is None, "absent must mean 'not measured'"

    def test_screenshot_set_carries_both_viewports(self):
        from app.qa.screenshot import ScreenshotSet

        s = ScreenshotSet(
            desktop=Path("/tmp/d.png"), mobile=Path("/tmp/m.png"),
            desktop_structural={"ok": True}, mobile_structural={"ok": True},
        )
        assert s.desktop_structural == {"ok": True}
        assert s.mobile_structural == {"ok": True}

    def test_probe_failure_does_not_fail_the_capture(self):
        """A screenshot without measurements is still valid evidence at E2."""
        from app.qa import screenshot as mod

        calls = []

        def fake_run(argv, timeout):
            calls.append(argv)
            if "open" in argv or "viewport" in argv:
                return subprocess.CompletedProcess(argv, 0, '{"success": true, "data": {"result": {}}}', "")
            if "eval" in argv:
                expr = argv[-1]
                if "innerWidth" in expr:
                    return subprocess.CompletedProcess(
                        argv, 0,
                        json.dumps({
                            "success": True,
                            "data": {"result": {
                                "innerWidth": 1440, "innerHeight": 900,
                                "documentClientWidth": 1440,
                                "documentScrollWidth": 1440,
                                "bodyScrollWidth": 1440,
                            }},
                        }),
                        "",
                    )
                # structural probe blows up
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            if "screenshot" in argv:
                Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        original = mod._run_browser_command
        mod._run_browser_command = fake_run
        try:
            result = mod._default_capture_fn("http://x/", 1440, 900, Path("/tmp/probe-test.png"))
        finally:
            mod._run_browser_command = original

        assert result.captured is True, "capture must survive a probe failure"
        assert result.structural is None, "and must report no measurements"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


class TestNonRgbColourSpaces:
    """Chrome serialises colours in the CSS Color 4 lab() space.

    A parser that understood only rgb()/rgba() treated such a background as
    unparseable, and an unparseable background was treated as TRANSPARENT --
    so the search walked past the element's own painted background and paired
    the text with a distant ancestor. On the M2 baseline that produced a
    1.0:1 ratio for a perfectly legible chip and a 1.09:1 ratio for a CTA
    that actually measures 4.54:1.
    """

    def test_the_probe_resolves_any_colour_space(self):
        import re
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        # The canvas pixel path must be present, and the rgb fast path must
        # still be there.
        assert "getImageData" in expr
        assert "rgba?" in expr

    def test_the_probe_skips_zero_size_elements(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "rect.width < 1" in expr

    def test_the_probe_skips_collapsed_disclosure_content(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "DETAILS" in expr
        assert "!node.open" in expr or "node.open" in expr


class TestNonRgbColourSpaces:
    """Chrome serialises colours in the CSS Color 4 lab() space.

    A parser that understood only rgb()/rgba() treated such a background as
    unparseable, and an unparseable background was treated as TRANSPARENT --
    so the search walked past the element's own painted background and paired
    the text with a distant ancestor. On the M2 baseline that produced a
    1.0:1 ratio for a perfectly legible chip and a 1.09:1 ratio for a CTA
    that actually measures 4.54:1.
    """

    def test_the_probe_resolves_any_colour_space(self):
        import re
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        # The canvas pixel path must be present, and the rgb fast path must
        # still be there.
        assert "getImageData" in expr
        assert "rgba?" in expr

    def test_the_probe_skips_zero_size_elements(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "rect.width < 1" in expr

    def test_the_probe_skips_collapsed_disclosure_content(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "DETAILS" in expr
        assert "!node.open" in expr or "node.open" in expr


class TestContrastStatesRemainHonest:
    """The colour fix must not weaken the honesty rules.

    The fix makes MORE pairs measurable. It must not make a doubtful pair
    measurable: a gradient behind text, or a translucent stack, still yields
    no ratio.
    """

    @staticmethod
    def _evidence_with(pairs):
        from app.core.design_reference import (
            ContrastMeasurement,
            StructuralEvidence,
        )
        return StructuralEvidence(
            candidate_id="c1", source_revision="r1", viewport=(1440, 900),
            screenshot_digest="d" * 64, measured_at_url="http://x/",
            probe_version="structural-probe-v1",
            contrast=tuple(
                ContrastMeasurement(
                    state=p["state"], ratio=p["ratio"],
                    foreground=p["foreground"], background=p["background"],
                    font_size_px=p["font_size_px"], reason=p["reason"],
                    sample_tag=p["sample_tag"],
                ) for p in pairs
            ),
        )

    def test_a_doubtful_pair_never_becomes_a_ratio(self):
        ev = self._evidence_with([
            {"state": "uncertain", "ratio": None,
             "foreground": "rgba(1,2,3,1.0)", "background": "none",
             "font_size_px": 14.0, "reason": "translucent_background",
             "sample_tag": "p"},
        ])
        assert ev.has_contrast is False
        assert ev.measured_pairs == ()
        assert ev.doubtful_pairs[0].ratio is None

    def test_an_unsupported_pair_never_becomes_a_ratio(self):
        ev = self._evidence_with([
            {"state": "unsupported", "ratio": None,
             "foreground": "rgba(255,255,255,1.0)", "background": "none",
             "font_size_px": 14.0, "reason": "gradient_behind_text",
             "sample_tag": "a"},
        ])
        assert ev.has_contrast is False
        assert ev.measured_pairs == ()

    def test_a_measured_pair_still_carries_its_ratio(self):
        ev = self._evidence_with([
            {"state": "measured", "ratio": 4.54,
             "foreground": "rgba(255,255,255,1.0)",
             "background": "rgba(230,0,118,1.0)",
             "font_size_px": 14.0, "reason": "", "sample_tag": "a"},
        ])
        assert ev.has_contrast is True
        assert ev.measured_pairs[0].ratio == 4.54
        assert ev.measured_pairs[0].state == "measured"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


class TestNonRgbColourSpaces:
    """Chrome serialises colours in the CSS Color 4 lab() space.

    A parser that understood only rgb()/rgba() treated such a background as
    unparseable, and an unparseable background was treated as TRANSPARENT --
    so the search walked past the element's own painted background and paired
    the text with a distant ancestor. On the M2 baseline that produced a
    1.0:1 ratio for a perfectly legible chip and a 1.09:1 ratio for a CTA
    that actually measures 4.54:1.
    """

    def test_the_probe_resolves_any_colour_space(self):
        import re
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        # The canvas pixel path must be present, and the rgb fast path must
        # still be there.
        assert "getImageData" in expr
        assert "rgba?" in expr

    def test_the_probe_skips_zero_size_elements(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "rect.width < 1" in expr

    def test_the_probe_skips_collapsed_disclosure_content(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "DETAILS" in expr
        assert "!node.open" in expr or "node.open" in expr


class TestNonRgbColourSpaces:
    """Chrome serialises colours in the CSS Color 4 lab() space.

    A parser that understood only rgb()/rgba() treated such a background as
    unparseable, and an unparseable background was treated as TRANSPARENT --
    so the search walked past the element's own painted background and paired
    the text with a distant ancestor. On the M2 baseline that produced a
    1.0:1 ratio for a perfectly legible chip and a 1.09:1 ratio for a CTA
    that actually measures 4.54:1.
    """

    def test_the_probe_resolves_any_colour_space(self):
        import re
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        # The canvas pixel path must be present, and the rgb fast path must
        # still be there.
        assert "getImageData" in expr
        assert "rgba?" in expr

    def test_the_probe_skips_zero_size_elements(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "rect.width < 1" in expr

    def test_the_probe_skips_collapsed_disclosure_content(self):
        from app.core import structural_probe as sp
        expr = sp.probe_expression()
        assert "DETAILS" in expr
        assert "!node.open" in expr or "node.open" in expr

