"""Readiness gating for the native browser capture and structural probe.

The CF-1 v2 benchmark capture measured a page before React had mounted, and
read a document with ZERO elements. The native Hermes capture path
(app/qa/screenshot.py) has the same defect, and worse: an empty document
parses into a WELL-FORMED, BOUND evidence record with collected=True and
no error, which reads as "we measured, and there was nothing to measure".

That is an evidence-integrity defect, so it is fixed at the source: a
capture that is not ready is reported as CAPTURE_NOT_READY, and a
measurement that cannot be taken is reported as MEASUREMENT_UNAVAILABLE.
Neither may ever be presented as a measurement of zero problems.

This module is the readiness gate. It is deliberately separate from the
capture so it can be tested on its own, and so the capture can keep its
"a probe failure must not fail a capture" behaviour: the READINESS failure
and the MEASUREMENT failure are reported separately from the CAPTURE
failure.

The checks are bounded and application-appropriate. A bare
``document.readyState`` is NOT enough: it says the document parsed, not
that the application rendered. The gate therefore requires:

  * the app root element to exist;
  * the root to have rendered children;
  * the document to have a non-trivial height;
  * fonts to have SETTLED, or to have failed to load (both are terminal
    states -- an unsettled font is not a terminal state);
  * every image to be complete or broken (also both terminal);
  * the layout to be STABLE across two consecutive samples.

It has a deadline and a bounded number of attempts. When the deadline
passes, the outcome is CAPTURE_NOT_READY, never a fabricated "the page is
empty".
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: The status the capture reports when the page never became ready.
CAPTURE_NOT_READY = "CAPTURE_NOT_READY"

#: The status the measurement reports when it could not be taken.
MEASUREMENT_UNAVAILABLE = "MEASUREMENT_UNAVAILABLE"

#: The status of a ready page with a measurement that WAS taken.
MEASUREMENT_AVAILABLE = "MEASUREMENT_AVAILABLE"

#: The default deadline for the readiness gate, in seconds.
DEFAULT_READY_TIMEOUT_SECONDS = 20.0

#: The deadline the capture uses, in seconds.
READY_TIMEOUT_SECONDS = DEFAULT_READY_TIMEOUT_SECONDS

#: How long to wait between readiness samples, in seconds.
SAMPLE_INTERVAL_SECONDS = 0.25

#: How many identical layout samples are required to call the layout stable.
STABLE_SAMPLES_REQUIRED = 2

#: The JavaScript that reports readiness. It is written to be cheap and to
#: never block: every branch returns a plain object.
READINESS_EXPRESSION = r"""
(() => {
  const root = document.querySelector('#root') || document.body;
  const rootChildren = root ? root.children.length : 0;
  const imgs = Array.from(document.images || []);
  const imgsSettled = imgs.filter(i => i.complete).length;
  const fontsState = (document.fonts && document.fonts.status)
    ? document.fonts.status : 'unsupported';
  const fontsReady = fontsState === 'loaded' || fontsState === 'unsupported';
  return JSON.stringify({
    readyState: document.readyState,
    hasRoot: !!root,
    rootChildren: rootChildren,
    elements: document.querySelectorAll('*').length,
    height: document.documentElement.scrollHeight,
    width: document.documentElement.scrollWidth,
    fontsState: fontsState,
    fontsReady: fontsReady,
    images: imgs.length,
    imagesSettled: imgsSettled,
    imagesReady: imgsSettled === imgs.length,
    textLength: (document.body && document.body.innerText || '').length,
  });
})()
"""


@dataclass(frozen=True)
class Readiness:
    """One readiness sample."""

    ready_state: str = ""
    has_root: bool = False
    root_children: int = 0
    elements: int = 0
    height: int = 0
    width: int = 0
    fonts_state: str = ""
    fonts_ready: bool = False
    images: int = 0
    images_settled: int = 0
    images_ready: bool = True
    text_length: int = 0

    @property
    def layout_key(self) -> Tuple[int, int, int]:
        """The key that decides whether the layout stopped moving."""
        return (self.height, self.width, self.root_children)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ready_state": self.ready_state,
            "has_root": self.has_root,
            "root_children": self.root_children,
            "elements": self.elements,
            "height": self.height,
            "width": self.width,
            "fonts_state": self.fonts_state,
            "fonts_ready": self.fonts_ready,
            "images": self.images,
            "images_settled": self.images_settled,
            "images_ready": self.images_ready,
            "text_length": self.text_length,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "Readiness":
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return cls(
            ready_state=str(payload.get("readyState") or ""),
            has_root=bool(payload.get("hasRoot")),
            root_children=int(payload.get("rootChildren") or 0),
            elements=int(payload.get("elements") or 0),
            height=int(payload.get("height") or 0),
            width=int(payload.get("width") or 0),
            fonts_state=str(payload.get("fontsState") or ""),
            fonts_ready=bool(payload.get("fontsReady")),
            images=int(payload.get("images") or 0),
            images_settled=int(payload.get("imagesSettled") or 0),
            images_ready=bool(payload.get("imagesReady", True)),
            text_length=int(payload.get("textLength") or 0),
        )


@dataclass(frozen=True)
class ReadinessOutcome:
    """The result of the readiness gate."""

    ready: bool
    status: str
    sample: Optional[Readiness] = None
    #: Why the gate gave up, in a stable word.
    reason: str = ""
    #: How many samples were taken.
    attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "status": self.status,
            "reason": self.reason,
            "attempts": self.attempts,
            "sample": self.sample.to_dict() if self.sample else None,
        }


def is_ready(sample: Readiness) -> bool:
    """Whether a single sample shows a rendered application.

    The checks are deliberately strict about the thing that actually
    matters -- the application rendered CONTENT -- and tolerant about the
    things that legitimately vary (fonts failing to load, images failing to
    load, a page with no images at all).
    """
    if not sample.has_root:
        return False
    if sample.root_children <= 0:
        return False
    if sample.elements <= 0:
        return False
    if sample.height <= 0:
        return False
    if not sample.fonts_ready:
        return False
    if not sample.images_ready:
        return False
    return True


def wait_for_ready(
    evaluate: Any,
    *,
    timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
    interval_seconds: float = SAMPLE_INTERVAL_SECONDS,
    sleep: Any = None,
    clock: Any = None,
) -> ReadinessOutcome:
    """Wait until the page is ready, or give up with a bounded reason.

    ``evaluate`` is called with no arguments and must return the payload of
    :data:`READINESS_EXPRESSION` (a JSON string or a mapping).

    The layout must be STABLE across two consecutive samples, so a page
    that is still animating or still streaming content does not pass.
    """
    import time as _time

    sleeper = sleep or _time.sleep
    timer = clock or _time.monotonic
    started = timer()
    attempts = 0
    last: Optional[Readiness] = None
    stable = 0
    previous_key: Optional[Tuple[int, int, int]] = None

    while True:
        attempts += 1
        raw = evaluate()
        sample = Readiness.from_payload(raw)
        last = sample
        if is_ready(sample):
            key = sample.layout_key
            if previous_key is not None and key == previous_key:
                stable += 1
                if stable >= STABLE_SAMPLES_REQUIRED:
                    return ReadinessOutcome(
                        ready=True, status="ready", sample=sample,
                        attempts=attempts,
                    )
            else:
                stable = 0
            previous_key = key
        else:
            stable = 0
            previous_key = None

        if timer() - started >= timeout_seconds:
            return ReadinessOutcome(
                ready=False, status=CAPTURE_NOT_READY, sample=last,
                reason=_reason_for(last), attempts=attempts,
            )
        sleeper(interval_seconds)


def _reason_for(sample: Optional[Readiness]) -> str:
    """A stable, specific reason the page was not ready."""
    if sample is None:
        return "no_sample"
    if not sample.has_root:
        return "no_app_root"
    if sample.root_children <= 0:
        return "app_root_empty"
    if sample.elements <= 0:
        return "no_elements"
    if sample.height <= 0:
        return "zero_height"
    if not sample.fonts_ready:
        return "fonts_unsettled"
    if not sample.images_ready:
        return "images_unsettled"
    return "layout_unstable"
