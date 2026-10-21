"""Bounded browser-side structural evidence for design scoring (N8 B7 Option C).

WHY THIS EXISTS
---------------
Three live runs showed that Color & Contrast cannot be judged from a downscaled
screenshot. The D2 fixture measures 4.45:1 against a 15.66:1 baseline -- a 0.05
margin below AA -- and that difference does not survive the resize the model
receives. The question was never badly worded; it was unanswerable from pixels.

This module supplies the missing evidence instead of re-wording the question. It
runs INSIDE the browser session ``ScreenshotCapture`` already opens, reads
``getComputedStyle`` from the real rendered page, and returns a bounded record
that can be bound to a candidate, a revision, a viewport and a screenshot.

WHAT THIS IS NOT
----------------
* Not a scoring formula. It produces measurements, never quality scores.
* Not a replacement for Luna. Perceptual judgement stays with the model.
* Not a quality proxy. Perfect contrast ratios do not make a page well designed,
  and this module never claims otherwise.

CONTRAST SAFETY
---------------
Computed CSS is NOT equivalent to visual contrast. A ``color`` value says
nothing about what is actually behind the text. This module therefore reports
one of three states per pair and refuses to invent a ratio:

    measured     an opaque background was found with no intervening
                 transparency, gradient or image
    uncertain    a translucent layer, ancestor opacity, or a mixed stack means
                 the true composite cannot be derived from computed styles alone
    unsupported  a gradient, background image, or unresolvable stack sits behind
                 the text

Only ``measured`` carries a ratio. ``uncertain`` and ``unsupported`` carry the
reason and no number, because a fabricated WCAG ratio is worse than no ratio.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_reference import (
    CONTRAST_MEASURED,
    CONTRAST_UNCERTAIN,
    CONTRAST_UNSUPPORTED,
    ContrastMeasurement,
    PROBE_VERSION,
    StructuralEvidence,
    WCAG_AA_NORMAL,
)

#: Hard bounds. A page with thousands of nodes must not produce an unbounded
#: payload, and the model must not receive a wall of numbers.
MAX_TEXT_NODES = 40
MAX_BLOCK_NODES = 60
MAX_CONTRAST_PAIRS = 12
MAX_SPACING_VALUES = 40


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

_PROBE_TEMPLATE = r"""
JSON.stringify((() => {
  const MAXT = %(max_text)d, MAXB = %(max_block)d;
  const text = [], blocks = [];
  const counts = {elements: 0, text_nodes: 0, images: 0, buttons: 0,
                  links: 0, headings: 0, forms: 0};

  // Chrome computes some colours in the CSS Color 4 space and serialises them
  // as "lab(L a b)" or "oklch(...)", not as rgb(). A parser that only knows
  // rgb()/rgba() treats such a colour as UNPARSEABLE, and an unparseable
  // background was being treated as TRANSPARENT -- so the search walked past
  // the element's own painted background and paired the text with a white
  // ancestor. That produced a 1.0:1 ratio for a perfectly legible chip.
  //
  // The fix is to convert ANY colour the browser can produce into sRGB using
  // its own canvas, so the arithmetic is done on real channel values.
  const _colourCanvas = document.createElement('canvas');
  const _colourCtx = _colourCanvas.getContext('2d', {willReadFrequently: true});
  function parseColor(value) {
    if (!value) return null;
    const text = String(value).trim();
    if (!text || text === 'transparent') return null;
    // The fast path: the browser already gave us rgb/rgba.
    const m = text.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(/[,\s\/]+/).filter(Boolean).map(s => parseFloat(s));
      if (p.length < 3 || p.some(n => !isFinite(n))) return null;
      return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1};
    }
    // Any other colour space (lab(), oklch(), color-mix(), ...): let the
    // browser PAINT it and read the pixel back. Assigning to fillStyle keeps
    // the string in its own space, so the pixels are the only reliable source
    // of sRGB values.
    try {
      _colourCtx.clearRect(0, 0, 1, 1);
      _colourCtx.fillStyle = text;
      _colourCtx.fillRect(0, 0, 1, 1);
      const d = _colourCtx.getImageData(0, 0, 1, 1).data;
      if (d[3] === 0) return null;   // painted nothing: treat as transparent
      return {r: d[0], g: d[1], b: d[2], a: d[3] / 255};
    } catch (e) {
      return null;
    }
  }

  // Walk ancestors for the first opaque background. Any translucency, gradient,
  // image or opacity below 1 makes the composite underivable from computed
  // styles, so we record the doubt rather than guessing.
  function backgroundFor(el) {
    let node = el;
    let uncertain = false;
    let unsupported = false;
    let reason = '';
    let depth = 0;
    while (node && depth < 40) {
      const cs = getComputedStyle(node);
      if (parseFloat(cs.opacity) < 1) { uncertain = true; reason = reason || 'ancestor_opacity'; }
      const bi = cs.backgroundImage;
      if (bi && bi !== 'none') {
        unsupported = true;
        reason = reason || (bi.indexOf('gradient') >= 0 ? 'gradient_behind_text' : 'image_behind_text');
        break;
      }
      const bg = parseColor(cs.backgroundColor);
      if (bg) {
        if (bg.a >= 1) {
          return {ok: true, color: bg, uncertain: uncertain, unsupported: unsupported, reason: reason};
        }
        // alpha == 0 is FULLY TRANSPARENT, not "translucent": the element simply
        // paints no background and the search must continue to its ancestor.
        // Treating it as uncertainty made 8 of 9 pairs on the baseline page
        // unmeasurable for no reason. Only a PARTIAL alpha (0 < a < 1) means the
        // composite genuinely cannot be derived from computed styles.
        if (bg.a > 0) {
          uncertain = true;
          reason = reason || 'translucent_background';
        }
      }
      node = node.parentElement;
      depth += 1;

      // An UNSUPPORTED composite is not merely uncertain: it is a case where
      // the true background cannot be derived at all. The walk must not
      // continue past it to an ancestor and pair the text with a surface it
      // is not actually painted on.
      if (unsupported) {
        return {ok: false, color: null, uncertain: true, unsupported: true,
                reason: reason || 'unsupported_composite'};
      }
      return {ok: false, color: null, uncertain: true, unsupported: false,
              reason: reason || 'no_opaque_background'};
    }
      return {ok: false, color: null, uncertain: true, unsupported: false,
            reason: reason || 'no_opaque_background'};
      }

  const body = document.body;
  if (!body) return {ok: false, error: 'no_body'};

  const all = body.querySelectorAll('*');
  counts.elements = all.length;

  // A closed <details> renders its content at ZERO size. That content is not
  // visible, so its colour must not be paired with a background: doing so
  // produced a 1.0:1 ratio for a filter chip nobody can see, and a real
  // accessibility finding was buried under the artefact.
  const isCollapsed = (el) => {
    let node = el;
    let depth = 0;
    while (node && depth < 30) {
      if (node.tagName === 'DETAILS' && !node.open) return true;
      node = node.parentElement;
      depth += 1;
    }
    return false;
  };

  for (const el of all) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'img') counts.images += 1;
    if (tag === 'button') counts.buttons += 1;
    if (tag === 'a') counts.links += 1;
    if (tag === 'form') counts.forms += 1;
    if (/^h[1-6]$/.test(tag)) counts.headings += 1;

    const cs = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    // Not rendered: no pixels, so no visible text, so no contrast to measure.
    if (rect.width < 1 || rect.height < 1) continue;
    // Also skip content inside a collapsed disclosure, whose text is not
    // visible even though its box may be measured.
    if (isCollapsed(el)) continue;

    // Blocks: spacing and geometry only. No text content is ever sent.
    if (blocks.length < MAXB) {
      const pad = [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft]
        .map(v => parseFloat(v) || 0);
      const mar = [cs.marginTop, cs.marginRight, cs.marginBottom, cs.marginLeft]
        .map(v => parseFloat(v) || 0);
      const gap = cs.rowGap && cs.rowGap !== 'normal' ? (parseFloat(cs.rowGap) || 0) : 0;
      if (pad.some(v => v > 0) || mar.some(v => v > 0) || gap > 0) {
        blocks.push({tag: tag, pad: pad, mar: mar, gap: gap});
      }
    }

    // Text: only leaf-ish nodes with a non-empty direct text child.
    if (text.length < MAXT) {
      let hasDirectText = false;
      for (const child of el.childNodes) {
        // nodeValue is this text node's own data, never an aggregated
        // descendant read. Only style and geometry may leave the page.
        if (child.nodeType === 3 && (child.nodeValue || "").trim().length > 1) {
          hasDirectText = true; break;
        }
      }
      if (hasDirectText) {
        counts.text_nodes += 1;
        const fg = parseColor(cs.color);
        const bg = backgroundFor(el);
        text.push({
          tag: tag,
          font_family: cs.fontFamily,
          font_size: parseFloat(cs.fontSize) || 0,
          font_weight: cs.fontWeight,
          line_height: cs.lineHeight === 'normal' ? 0 : (parseFloat(cs.lineHeight) || 0),
          letter_spacing: cs.letterSpacing === 'normal' ? 0 : (parseFloat(cs.letterSpacing) || 0),
          fg: fg,
          bg_ok: bg.ok,
          bg: bg.color,
          bg_uncertain: bg.uncertain,
          bg_unsupported: bg.unsupported,
          bg_reason: bg.reason,
          width: Math.round(rect.width),
          height: Math.round(rect.height)
        });
      }
    }
  }

  return {ok: true, text: text, blocks: blocks, counts: counts,
          viewport: {width: window.innerWidth, height: window.innerHeight},
          document_height: document.documentElement.scrollHeight};
})())
"""


def probe_expression() -> str:
    """The single JS expression to pass to ``agent-browser eval``.

    Bounded at both ends: ``MAX_TEXT_NODES`` / ``MAX_BLOCK_NODES`` cap collection
    inside the page, so a pathological DOM cannot produce a huge payload.
    """
    return _PROBE_TEMPLATE % {"max_text": MAX_TEXT_NODES, "max_block": MAX_BLOCK_NODES}


# ---------------------------------------------------------------------------
# contrast maths
# ---------------------------------------------------------------------------


def _srgb_channel(value: float) -> float:
    """Linearise one sRGB channel in 0..255 per WCAG 2.2."""
    c = max(0.0, min(255.0, float(value))) / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: Sequence[float]) -> float:
    r, g, b = (_srgb_channel(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: Sequence[float], background: Sequence[float]) -> float:
    """WCAG contrast ratio, always >= 1.0."""
    lf = relative_luminance(foreground)
    lb = relative_luminance(background)
    lighter, darker = max(lf, lb), min(lf, lb)
    return (lighter + 0.05) / (darker + 0.05)




def _fmt_rgba(color: Optional[Mapping[str, Any]]) -> str:
    if not color:
        return "none"
    return "rgba({},{},{},{})".format(
        int(round(float(color.get("r", 0)))),
        int(round(float(color.get("g", 0)))),
        int(round(float(color.get("b", 0)))),
        round(float(color.get("a", 1)), 3),
    )


def _measure_pair(entry: Mapping[str, Any]) -> Optional[ContrastMeasurement]:
    """Turn one probe text entry into a ContrastMeasurement, honestly.

    Order matters: unsupported beats uncertain beats measured. A pair is only
    ``measured`` when the probe found an opaque background AND no doubt flags.
    """
    fg = entry.get("fg")
    if not isinstance(fg, Mapping):
        return None
    font_size = float(entry.get("font_size") or 0.0)
    tag = str(entry.get("tag") or "")

    if bool(entry.get("bg_unsupported")):
        return ContrastMeasurement(
            state=CONTRAST_UNSUPPORTED,
            ratio=None,
            foreground=_fmt_rgba(fg),
            background=_fmt_rgba(entry.get("bg")),
            font_size_px=font_size,
            reason=str(entry.get("bg_reason") or "background_not_resolvable"),
            sample_tag=tag,
        )

    bg = entry.get("bg")
    if not bool(entry.get("bg_ok")) or not isinstance(bg, Mapping):
        return ContrastMeasurement(
            state=CONTRAST_UNCERTAIN,
            ratio=None,
            foreground=_fmt_rgba(fg),
            background=_fmt_rgba(bg),
            font_size_px=font_size,
            reason=str(entry.get("bg_reason") or "no_opaque_background"),
            sample_tag=tag,
        )

    if bool(entry.get("bg_uncertain")):
        return ContrastMeasurement(
            state=CONTRAST_UNCERTAIN,
            ratio=None,
            foreground=_fmt_rgba(fg),
            background=_fmt_rgba(bg),
            font_size_px=font_size,
            reason=str(entry.get("bg_reason") or "translucent_stack"),
            sample_tag=tag,
        )

    if float(fg.get("a", 1)) < 1.0:
        return ContrastMeasurement(
            state=CONTRAST_UNCERTAIN,
            ratio=None,
            foreground=_fmt_rgba(fg),
            background=_fmt_rgba(bg),
            font_size_px=font_size,
            reason="translucent_text",
            sample_tag=tag,
        )

    ratio = contrast_ratio(
        (float(fg.get("r", 0)), float(fg.get("g", 0)), float(fg.get("b", 0))),
        (float(bg.get("r", 0)), float(bg.get("g", 0)), float(bg.get("b", 0))),
    )
    return ContrastMeasurement(
        state=CONTRAST_MEASURED,
        ratio=round(ratio, 2),
        foreground=_fmt_rgba(fg),
        background=_fmt_rgba(bg),
        font_size_px=font_size,
        reason="",
        sample_tag=tag,
    )


# ---------------------------------------------------------------------------
# bound evidence
# ---------------------------------------------------------------------------




def _normalise_family(raw: str) -> str:
    """First family in a computed font stack, unquoted."""
    first = str(raw or "").split(",")[0].strip()
    return first.strip("'\"").strip()


def parse_probe(
    payload: Any,
    *,
    candidate_id: str,
    source_revision: str,
    viewport: Tuple[int, int],
    screenshot_digest: str,
    measured_at_url: str,
) -> StructuralEvidence:
    """Parse a probe payload into bound evidence.

    A failed or empty probe returns ``collected=False`` with a reason -- never a
    half-populated record that would silently read as E3.
    """
    bind = dict(
        candidate_id=str(candidate_id),
        source_revision=str(source_revision),
        viewport=(int(viewport[0]), int(viewport[1])),
        screenshot_digest=str(screenshot_digest),
        measured_at_url=str(measured_at_url),
    )

    def _evidence(**extra: Any) -> StructuralEvidence:
        """Construct with the binding applied once, so it cannot drift."""
        merged: Dict[str, Any] = {**bind, **extra}
        return StructuralEvidence(**merged)

    if not isinstance(payload, Mapping):
        return _evidence(collected=False, error="payload_not_mapping")
    if not payload.get("ok"):
        return _evidence(collected=False, error=str(payload.get("error") or "probe_failed"))

    raw_text = payload.get("text") or ()
    raw_blocks = payload.get("blocks") or ()
    if not isinstance(raw_text, (list, tuple)) or not isinstance(raw_blocks, (list, tuple)):
        return _evidence(collected=False, error="malformed_sections")

    # An EMPTY document is a failed measurement, not a successful one.
    # The probe ran, but it ran against a page that had not rendered, so
    # there is nothing to report. Recording collected=True here would read
    # as "we measured, and the page had no measurable content" -- which is
    # a fabrication, and the kind that hides a real capture problem.
    if not raw_text and not raw_blocks:
        return _evidence(collected=False, error="empty_document")

    # -- typography -------------------------------------------------------
    families: List[str] = []
    type_scale: List[Tuple[str, float, float]] = []
    for entry in raw_text[:MAX_TEXT_NODES]:
        if not isinstance(entry, Mapping):
            continue
        family = _normalise_family(entry.get("font_family", ""))
        if family and family not in families:
            families.append(family)
        size = float(entry.get("font_size") or 0.0)
        if size > 0:
            tag = str(entry.get("tag") or "")
            line = float(entry.get("line_height") or 0.0)
            triple = (tag, round(size, 2), round(line, 2))
            if triple not in type_scale:
                type_scale.append(triple)

    # -- spacing ----------------------------------------------------------
    steps: List[float] = []
    for entry in raw_blocks[:MAX_BLOCK_NODES]:
        if not isinstance(entry, Mapping):
            continue
        for key in ("pad", "mar"):
            for value in entry.get(key) or ():
                try:
                    v = round(float(value), 2)
                except (TypeError, ValueError):
                    continue
                if v > 0 and v not in steps:
                    steps.append(v)
        try:
            gap = round(float(entry.get("gap") or 0.0), 2)
        except (TypeError, ValueError):
            gap = 0.0
        if gap > 0 and gap not in steps:
            steps.append(gap)
    steps = sorted(steps)[:MAX_SPACING_VALUES]

    # -- contrast ---------------------------------------------------------
    pairs: List[ContrastMeasurement] = []
    seen: set = set()
    for entry in raw_text[:MAX_TEXT_NODES]:
        if not isinstance(entry, Mapping):
            continue
        pair = _measure_pair(entry)
        if pair is None:
            continue
        key = (pair.state, pair.foreground, pair.background, pair.reason)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(pair)
        if len(pairs) >= MAX_CONTRAST_PAIRS:
            break

    counts = payload.get("counts") if isinstance(payload.get("counts"), Mapping) else {}
    component_count = 0
    for key in ("buttons", "links", "forms", "headings"):
        try:
            component_count += int(counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue

    return _evidence(
        font_families=tuple(families),
        type_scale=tuple(type_scale),
        spacing_scale=tuple(steps),
        contrast=tuple(pairs),
        component_count=component_count,
        counts=dict(counts),
        document_height=int(payload.get("document_height") or 0),
        collected=True,
    )


def verify_binding(
    evidence: StructuralEvidence,
    *,
    candidate_id: str,
    source_revision: str,
    screenshot_digest: str,
    viewport: Optional[Tuple[int, int]] = None,
) -> Optional[str]:
    """Return a mismatch reason, or ``None`` when the binding holds.

    Called before evidence is used for scoring. A mismatch is never repaired by
    guessing: the evidence is dropped and the candidate falls back to E2.
    """
    if evidence.candidate_id != candidate_id:
        return "candidate_mismatch"
    if evidence.source_revision != source_revision:
        return "revision_mismatch"
    if evidence.screenshot_digest != screenshot_digest:
        return "screenshot_mismatch"
    if viewport is not None and tuple(evidence.viewport) != (int(viewport[0]), int(viewport[1])):
        return "viewport_mismatch"
    return None
