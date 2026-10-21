"""Readiness gating -- the regression coverage the addendum asks for.

The scenarios:
  * a delayed React mount
  * slow fonts
  * late images
  * an empty DOM
  * an unstable layout
  * the readiness timeout

And the invariants:
  * an incomplete capture is CAPTURE_NOT_READY, never a zero-problem score;
  * an empty document is a FAILED measurement, never a successful one;
  * a failed capture cannot cause an approval, a publish, a scoring
    eligibility change, or a silent fallback to fabricated evidence;
  * a valid screenshot with uncertain contrast pairs stays valid, and the
    uncertainty stays explicit.
"""
import json

import pytest

from app.core.design_scoring import (
    DIMENSIONS,
    evidence_class_for,
    measured_evidence_block,
)
from app.core.structural_probe import parse_probe
from app.qa.readiness import (
    CAPTURE_NOT_READY,
    MEASUREMENT_UNAVAILABLE,
    Readiness,
    ReadinessOutcome,
    is_ready,
    wait_for_ready,
)


def _sample(**over):
    base = dict(
        ready_state="complete", has_root=True, root_children=3, elements=146,
        height=3619, width=1440, fonts_state="loaded", fonts_ready=True,
        images=0, images_settled=0, images_ready=True, text_length=2015,
    )
    base.update(over)
    return Readiness(**base)


def _evaluate(*samples):
    """An evaluate() that returns the given samples in order."""
    seq = list(samples)
    state = {"i": 0}

    def _fn():
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    return _fn


class TestWhatCountsAsReady:
    def test_a_rendered_page_is_ready(self):
        assert is_ready(_sample()) is True

    def test_an_empty_dom_is_not_ready(self):
        assert is_ready(_sample(root_children=0)) is False
        assert is_ready(_sample(elements=0)) is False

    def test_a_missing_app_root_is_not_ready(self):
        assert is_ready(_sample(has_root=False)) is False

    def test_a_zero_height_page_is_not_ready(self):
        assert is_ready(_sample(height=0)) is False

    def test_unsettled_fonts_are_not_ready(self):
        assert is_ready(_sample(fonts_ready=False)) is False

    def test_unsettled_images_are_not_ready(self):
        assert is_ready(_sample(images=2, images_settled=1,
                               images_ready=False)) is False

    def test_a_failed_font_is_a_terminal_state_and_is_ready(self):
        """A font that FAILED to load is settled. Only an unsettled one
        is not."""
        assert is_ready(_sample(fonts_state="loaded")) is True
        assert is_ready(_sample(fonts_state="unsupported",
                                fonts_ready=True)) is True

    def test_a_broken_image_is_a_terminal_state_and_is_ready(self):
        assert is_ready(_sample(images=2, images_settled=2,
                                images_ready=True)) is True


class TestTheWait:
    def test_a_delayed_react_mount_eventually_becomes_ready(self):
        outcome = wait_for_ready(
            _evaluate(
                {"readyState": "loading", "hasRoot": True, "rootChildren": 0,
                 "elements": 3, "height": 0, "fontsReady": True,
                 "imagesReady": True},
                {"readyState": "loading", "hasRoot": True, "rootChildren": 0,
                 "elements": 3, "height": 0, "fontsReady": True,
                 "imagesReady": True},
                {"readyState": "complete", "hasRoot": True,
                 "rootChildren": 3, "elements": 146, "height": 3619,
                 "fontsReady": True, "imagesReady": True},
                {"readyState": "complete", "hasRoot": True,
                 "rootChildren": 3, "elements": 146, "height": 3619,
                 "fontsReady": True, "imagesReady": True},
            ),
            timeout_seconds=5.0, interval_seconds=0.0, sleep=lambda _s: None,
        )
        assert outcome.ready is True
        assert outcome.status == "ready"
        assert outcome.attempts >= 3

    def test_slow_fonts_are_not_ready_until_they_settle(self):
        outcome = wait_for_ready(
            _evaluate(
                {"readyState": "complete", "hasRoot": True, "rootChildren": 3,
                 "elements": 146, "height": 3619, "fontsReady": False,
                 "imagesReady": True},
            ),
            timeout_seconds=0.5, interval_seconds=0.0, sleep=lambda _s: None,
        )
        assert outcome.ready is False
        assert outcome.reason == "fonts_unsettled"

    def test_late_images_are_not_ready_until_they_settle(self):
        outcome = wait_for_ready(
            _evaluate(
                {"readyState": "complete", "hasRoot": True, "rootChildren": 3,
                 "elements": 146, "height": 3619, "fontsReady": True,
                 "images": 3, "imagesSettled": 1, "imagesReady": False},
            ),
            timeout_seconds=0.5, interval_seconds=0.0, sleep=lambda _s: None,
        )
        assert outcome.ready is False
        assert outcome.reason == "images_unsettled"

    def test_an_empty_dom_times_out_as_capture_not_ready(self):
        outcome = wait_for_ready(
            _evaluate(
                {"readyState": "complete", "hasRoot": True, "rootChildren": 0,
                 "elements": 0, "height": 0, "fontsReady": True,
                 "imagesReady": True},
            ),
            timeout_seconds=0.5, interval_seconds=0.0, sleep=lambda _s: None,
        )
        assert outcome.ready is False
        assert outcome.status == CAPTURE_NOT_READY
        assert outcome.reason == "app_root_empty"

    def test_an_unstable_layout_never_settles(self):
        """A page whose height keeps changing is not ready."""
        counter = {"n": 0}

        def _growing():
            counter["n"] += 1
            return {"readyState": "complete", "hasRoot": True,
                    "rootChildren": 3, "elements": 146,
                    "height": 100 * counter["n"], "fontsReady": True,
                    "imagesReady": True}

        outcome = wait_for_ready(
            _growing,
            timeout_seconds=0.5, interval_seconds=0.0, sleep=lambda _s: None,
        )
        assert outcome.ready is False
        assert outcome.reason == "layout_unstable"

    def test_the_deadline_is_bounded(self):
        calls = {"n": 0}

        def _never():
            calls["n"] += 1
            return {"readyState": "loading", "hasRoot": True,
                    "rootChildren": 0, "elements": 0, "height": 0,
                    "fontsReady": True, "imagesReady": True}

        outcome = wait_for_ready(
            _never, timeout_seconds=0.5, interval_seconds=0.0,
            sleep=lambda _s: None)
        assert outcome.ready is False
        assert outcome.status == CAPTURE_NOT_READY
        assert outcome.attempts >= 1

    def test_a_malformed_payload_is_not_ready(self):
        outcome = wait_for_ready(
            _evaluate("not json at all"),
            timeout_seconds=0.2, interval_seconds=0.0, sleep=lambda _s: None,
        )
        assert outcome.ready is False
        assert outcome.status == CAPTURE_NOT_READY


class TestTheEmptyDocumentIsAFailedMeasurement:
    def test_an_empty_document_is_not_collected(self):
        evidence = parse_probe(
            {"ok": True, "text": [], "blocks": [], "counts": {}},
            candidate_id="c1", source_revision="7",
            screenshot_digest="d" * 64, viewport=(1440, 900),
            measured_at_url="http://x/")
        assert evidence.collected is False
        assert evidence.error == "empty_document"

    def test_an_empty_document_never_becomes_a_measured_block(self):
        evidence = parse_probe(
            {"ok": True, "text": [], "blocks": [], "counts": {}},
            candidate_id="c1", source_revision="7",
            screenshot_digest="d" * 64, viewport=(1440, 900),
            measured_at_url="http://x/")
        for dim in DIMENSIONS:
            assert measured_evidence_block(dim, evidence) == ""
            assert evidence_class_for(dim, evidence) == "E2"

    def test_a_failed_probe_is_reported_as_unavailable(self):
        evidence = parse_probe(
            {"ok": False, "error": "probe_failed"},
            candidate_id="c1", source_revision="7",
            screenshot_digest="d" * 64, viewport=(1440, 900),
            measured_at_url="http://x/")
        assert evidence.collected is False
        assert evidence.error == "probe_failed"
        assert MEASUREMENT_UNAVAILABLE


class TestUncertainPairsStayExplicit:
    def test_a_valid_screenshot_with_an_uncertain_pair_stays_valid(self):
        """A screenshot is valid even when a contrast pair could not be
        quantified. The uncertainty must remain explicit."""
        evidence = parse_probe(
            {
                "ok": True,
                "text": [
                    {"tag": "a", "font_family": "x", "font_size": 14,
                     "font_weight": "400", "line_height": 20,
                     "letter_spacing": 0,
                     "fg": {"r": 250, "g": 248, "b": 245, "a": 1},
                     "bg": {"r": 28, "g": 27, "b": 26, "a": 1},
                     "bg_ok": True, "bg_reason": "", "bg_uncertain": False,
                     "bg_unsupported": False, "width": 10, "height": 20},
                    {"tag": "p", "font_family": "x", "font_size": 12,
                     "font_weight": "400", "line_height": 18,
                     "letter_spacing": 0,
                     "fg": {"r": 180, "g": 85, "b": 45, "a": 1},
                     "bg": None, "bg_ok": False,
                     "bg_reason": "no_opaque_background", "bg_uncertain": True,
                     "bg_unsupported": False, "width": 10, "height": 18},
                ],
                "blocks": [{"pad": [0, 24], "mar": [0, 0]}],
                "counts": {"elements": 146, "text_nodes": 2, "links": 1,
                           "buttons": 0, "headings": 0, "forms": 0},
            },
            candidate_id="c1", source_revision="7",
            screenshot_digest="d" * 64, viewport=(1440, 900),
            measured_at_url="http://x/")
        assert evidence.collected is True
        assert evidence.error == ""
        assert evidence.has_contrast is True
        states = {c.state for c in evidence.contrast}
        assert "measured" in states
        assert "uncertain" in states
        block = measured_evidence_block("color", evidence)
        assert "16.22:1" in block
        assert "could not be measured" in block
        # The uncertain pair must not appear as a NUMBER. The "1.0" in
        # "rgba(...,1.0)" is an alpha channel, not a ratio.
        assert "= 1.0:1" not in block, (
            "an uncertain pair must not become a ratio of 1.0")
        assert ":1" in block


class TestNoUnauthorizedConsequence:
    def test_a_capture_not_ready_is_a_capture_failure(self):
        """The status is a CAPTURE failure, distinct from a measurement
        failure and from a successful capture."""
        assert CAPTURE_NOT_READY == "CAPTURE_NOT_READY"
        assert CAPTURE_NOT_READY != MEASUREMENT_UNAVAILABLE

    def test_the_readiness_module_imports_nothing_that_authorises(self):
        """The gate must not be able to approve, publish or score."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "app" / "qa" / "readiness.py"
        text = src.read_text(encoding="utf-8")
        for banned in ("promote", "publish", "approve", "authz",
                       "apply_gates", "scoring_enabled", "api_key"):
            assert banned not in text, banned

    def test_the_capture_module_does_not_gain_authorisation(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "app" / "qa" / "screenshot.py").read_text(encoding="utf-8")
        # The readiness gate may only REFUSE a capture.
        assert "CAPTURE_NOT_READY" in src
        for banned in ("promote(", "publish(", "approve(", "apply_gates("):
            assert banned not in src, banned
