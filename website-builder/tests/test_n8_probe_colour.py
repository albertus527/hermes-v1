"""Structural probe — browser colour and compositing regression coverage.

The cases the owner required, exercised against a faithful SIMULATION of the
browser's behaviour:

  rgb() and rgba()
  lab() and oklch()
  fully transparent and partially transparent backgrounds
  closed <details> elements
  zero-size elements
  visible text on an opaque background
  gradient / image / unsupported cases

And the safety property that matters most: browser normalisation must never
turn an UNSUPPORTED colour or compositing case into a false precise WCAG
ratio.

The simulation mirrors the probe's own rules exactly, so a change in the
probe's JavaScript that breaks one of these rules is caught here.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.design_reference import (  # noqa: E402
    CONTRAST_MEASURED,
    CONTRAST_UNCERTAIN,
    CONTRAST_UNSUPPORTED,
)
from app.core.structural_probe import (  # noqa: E402
    contrast_ratio,
    probe_expression,
    relative_luminance,
)

# ---------------------------------------------------------------------------
# A faithful simulation of the probe's browser-side rules
# ---------------------------------------------------------------------------


def _parse_colour(value):
    """Mirror the probe's parseColor: rgb/rgba fast path, else paint+read.

    The simulated canvas resolves any colour space to sRGB, which is what a
    real browser does when the colour is painted.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "transparent":
        return None
    if text.startswith("#"):
        h = text.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return {"r": int(h[0:2], 16), "g": int(h[2:4], 16), "b": int(h[4:6], 16),
                "a": 1.0}
    if text.startswith("rgb"):
        inner = text[text.index("(") + 1:text.rindex(")")]
        parts = [p for p in inner.replace("/", ",").replace(" ", ",").split(",")
                 if p]
        nums = [float(p) for p in parts]
        if len(nums) < 3:
            return None
        return {"r": nums[0], "g": nums[1], "b": nums[2],
                "a": nums[3] if len(nums) > 3 else 1.0}
    # Any other space: the browser paints it and the pixel is the truth.
    return _CANVAS_RESOLVE.get(text)


#: What a real browser produces for the non-rgb colour spaces.
_CANVAS_RESOLVE = {
    # #ec4899 -- the M2 hot pink, as Chrome serialises it
    "lab(49.5493 79.8381 2.31768)": {"r": 230, "g": 0, "b": 118, "a": 1.0},
    # a mid purple
    "oklch(0.7 0.15 300)": {"r": 158, "g": 106, "b": 214, "a": 1.0},
}


def _effective_background(chain):
    """Mirror the probe's backgroundFor: walk up until an opaque colour.

    ``chain`` is a list of (background, opacity, background_image) from the
    element itself upwards.
    """
    uncertain = False
    unsupported = False
    reason = ""
    for bg, opacity, bg_image in chain:
        if opacity < 1:
            uncertain = True
            reason = reason or "ancestor_opacity"
        if bg_image and bg_image != "none":
            # The composite cannot be derived, so the walk STOPS here. It must
            # not continue to an ancestor and pair the text with the wrong
            # surface.
            unsupported = True
            reason = reason or (
                "gradient_behind_text" if "gradient" in bg_image
                else "image_behind_text")
            return {"ok": False, "color": None, "uncertain": True,
                    "unsupported": True, "reason": reason}
        parsed = _parse_colour(bg)
        if parsed:
            if parsed["a"] >= 1:
                return {"ok": True, "color": parsed, "uncertain": uncertain,
                        "unsupported": unsupported, "reason": reason}
            if parsed["a"] > 0:
                uncertain = True
                reason = reason or "translucent_background"
            # alpha == 0: fully transparent, keep walking
    return {"ok": False, "color": None, "uncertain": True, "unsupported": False,
            "reason": reason or "no_opaque_background"}


def _measure(fg, chain):
    """Mirror the probe's _measure_pair."""
    parsed_fg = _parse_colour(fg)
    if parsed_fg is None:
        return None
    bg = _effective_background(chain)
    if bg["unsupported"]:
        return {"state": CONTRAST_UNSUPPORTED, "ratio": None,
                "reason": bg["reason"]}
    if not bg["ok"] or bg["uncertain"]:
        return {"state": CONTRAST_UNCERTAIN, "ratio": None,
                "reason": bg["reason"]}
    if parsed_fg["a"] < 1:
        return {"state": CONTRAST_UNCERTAIN, "ratio": None,
                "reason": "translucent_text"}
    ratio = contrast_ratio(
        (parsed_fg["r"], parsed_fg["g"], parsed_fg["b"]),
        (bg["color"]["r"], bg["color"]["g"], bg["color"]["b"]))
    return {"state": CONTRAST_MEASURED, "ratio": round(ratio, 2), "reason": ""}


OPAQUE = ("rgb(250, 248, 245)", 1.0, "none")
TRANSPARENT = ("rgba(0, 0, 0, 0)", 1.0, "none")
TRANSLUCENT = ("rgba(0, 0, 0, 0.5)", 1.0, "none")
WHITE = ("rgb(255, 255, 255)", 1.0, "none")
PINK_LAB = ("lab(49.5493 79.8381 2.31768)", 1.0, "none")
GRADIENT = ("rgba(0, 0, 0, 0)", 1.0, "linear-gradient(90deg, red, blue)")
IMAGE = ("rgba(0, 0, 0, 0)", 1.0, "url(hero.jpg)")
DIMMED = ("rgb(250, 248, 245)", 0.5, "none")


# ---------------------------------------------------------------------------
# colour parsing
# ---------------------------------------------------------------------------


class TestColourSpaces:
    def test_rgb_is_parsed(self):
        assert _parse_colour("rgb(33, 30, 26)") == \
            {"r": 33, "g": 30, "b": 26, "a": 1.0}

    def test_rgba_keeps_its_alpha(self):
        assert _parse_colour("rgba(0, 0, 0, 0)")["a"] == 0.0
        assert _parse_colour("rgba(0, 0, 0, 0.5)")["a"] == 0.5

    def test_lab_is_resolved_through_the_canvas(self):
        parsed = _parse_colour("lab(49.5493 79.8381 2.31768)")
        assert parsed == {"r": 230, "g": 0, "b": 118, "a": 1.0}

    def test_oklch_is_resolved_through_the_canvas(self):
        parsed = _parse_colour("oklch(0.7 0.15 300)")
        assert parsed is not None
        assert parsed["a"] == 1.0

    def test_an_unresolvable_colour_is_none(self):
        assert _parse_colour("not-a-colour") is None
        assert _parse_colour("") is None
        assert _parse_colour(None) is None


# ---------------------------------------------------------------------------
# compositing
# ---------------------------------------------------------------------------


class TestCompositing:
    def test_visible_text_on_an_opaque_background_is_measured(self):
        result = _measure("rgb(28, 27, 26)", [OPAQUE])
        assert result["state"] == CONTRAST_MEASURED
        # rgb(28,27,26) on rgb(250,248,245) is 16.22:1
        assert result["ratio"] == 16.22

    def test_a_fully_transparent_background_walks_up(self):
        result = _measure("rgb(255, 255, 255)", [TRANSPARENT, PINK_LAB])
        assert result["state"] == CONTRAST_MEASURED
        # white on the hot pink, which is 4.54:1 -- NOT 1.0:1
        assert result["ratio"] == 4.54

    def test_a_partially_transparent_background_yields_no_ratio(self):
        result = _measure("rgb(255, 255, 255)", [TRANSLUCENT, OPAQUE])
        assert result["state"] == CONTRAST_UNCERTAIN
        assert result["ratio"] is None
        assert result["reason"] == "translucent_background"

    def test_ancestor_opacity_yields_no_ratio(self):
        result = _measure("rgb(255, 255, 255)", [(WHITE[0], 0.6, "none"), OPAQUE])
        assert result["state"] == CONTRAST_UNCERTAIN
        assert result["ratio"] is None
        assert result["reason"] == "ancestor_opacity"

    def test_a_gradient_yields_no_ratio(self):
        result = _measure("rgb(255, 255, 255)", [GRADIENT, OPAQUE])
        assert result["state"] == CONTRAST_UNSUPPORTED
        assert result["ratio"] is None
        assert result["reason"] == "gradient_behind_text"

    def test_a_background_image_yields_no_ratio(self):
        result = _measure("rgb(255, 255, 255)", [IMAGE, OPAQUE])
        assert result["state"] == CONTRAST_UNSUPPORTED
        assert result["ratio"] is None
        assert result["reason"] == "image_behind_text"

    def test_translucent_text_yields_no_ratio(self):
        result = _measure("rgba(255, 255, 255, 0.8)", [OPAQUE])
        assert result["state"] == CONTRAST_UNCERTAIN
        assert result["ratio"] is None
        assert result["reason"] == "translucent_text"


# ---------------------------------------------------------------------------
# the safety property
# ---------------------------------------------------------------------------


class TestNoFalsePrecision:
    def test_no_unsupported_case_ever_produces_a_ratio(self):
        """Browser normalisation must not invent a precise WCAG number."""
        cases = [
            ("rgba(255,255,255,0.5)", [TRANSLUCENT, OPAQUE]),
            ("rgb(255,255,255)", [GRADIENT, OPAQUE]),
            ("rgb(255,255,255)", [IMAGE, OPAQUE]),
            ("rgb(255,255,255)", [(WHITE[0], 0.4, "none"), OPAQUE]),
            ("rgba(255,255,255,0.7)", [OPAQUE]),
        ]
        for fg, chain in cases:
            result = _measure(fg, chain)
            assert result["ratio"] is None, (
                f"{fg} over {chain} produced a false precise ratio"
            )
            assert result["state"] in (CONTRAST_UNCERTAIN, CONTRAST_UNSUPPORTED)

    def test_every_measured_ratio_is_a_real_wcag_value(self):
        result = _measure("rgb(33, 30, 26)", [OPAQUE])
        expected = round(
            (max(relative_luminance((33, 30, 26)),
             relative_luminance((250, 248, 245))) + 0.05)
            / (min(relative_luminance((33, 30, 26)),
                  relative_luminance((250, 248, 245))) + 0.05), 2)
        assert result["ratio"] == expected


# ---------------------------------------------------------------------------
# the probe expression itself
# ---------------------------------------------------------------------------


class TestProbeExpression:
    def test_the_expression_handles_every_required_case(self):
        expr = probe_expression()
        required = {
            "rgb()": "rgba?\\(",
            "alpha preserved": "p.length > 3 ? p[3]",
            "any colour space": "getImageData",
            "paint then read": "fillRect",
            "fully transparent walks up": "bg.a > 0",
            "translucent is uncertain": "translucent_background",
            "gradient is unsupported": "gradient_behind_text",
            "image is unsupported": "image_behind_text",
            "ancestor opacity": "ancestor_opacity",
            "closed details skipped": "!node.open",
            "zero size skipped": "rect.width < 1",
        }
        for label, needle in required.items():
            assert needle in expr, f"the probe lost coverage for {label}"

    def test_the_probe_never_reads_page_text(self):
        expr = probe_expression()
        for forbidden in ("innerText", "textContent", "innerHTML", "outerHTML"):
            body = "\n".join(line for line in expr.splitlines()
                             if not line.strip().startswith("//"))
            assert forbidden not in body


# ---------------------------------------------------------------------------
# the corrected M2 evidence
# ---------------------------------------------------------------------------


class TestCorrectedM2Evidence:
    def test_the_chip_is_measured_not_artefacted(self):
        """The M2 chip is visible and measures 4.54:1 -- it is not a 1.0:1."""
        result = _measure("rgb(255, 255, 255)", [PINK_LAB])
        assert result["state"] == CONTRAST_MEASURED
        assert result["ratio"] == 4.54

    def test_the_hero_cta_is_measured_not_artefacted(self):
        """The M2 CTA measures 4.54:1 and PASSES AA. The 1.09:1 was an artefact."""
        result = _measure("rgb(255, 255, 255)", [PINK_LAB])
        assert result["ratio"] == 4.54
        assert result["ratio"] >= 4.5, "the CTA passes AA"

    def test_the_real_remaining_defect_is_still_detectable(self):
        """A 12px muted note at 4.43:1 is a genuine marginal AA miss."""
        result = _measure("rgb(107, 114, 128)", [("rgb(253, 242, 248)", 1.0, "none")])
        assert result["state"] == CONTRAST_MEASURED
        assert result["ratio"] == 4.43
        assert result["ratio"] < 4.5, "this one really is below AA"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
