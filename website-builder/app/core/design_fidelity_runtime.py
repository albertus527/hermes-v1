"""V4-A: runtime evidence + integration for the V3-C Design Fidelity Gate.

V3-C built a *pure* gate (:mod:`app.core.design_fidelity`) that consumes
evidence and returns a dimension-separated verdict. V4-A connects it to the
real staging build/QA pipeline by supplying that evidence from the SAME
workspace/revision that is about to be accepted:

* the owner's persisted ``design_intent`` (via the brief),
* ``PRD.md`` and ``DesignGuidance.md`` (incl. the V3-B Asset Strategy section),
* the generated ``src/**`` source and CSS,
* the VISION evidence (blocking findings + subjective observations) as
  ``screenshot_evidence``.

Two hard rules are enforced here:

1. **No fabrication.** Missing evidence is passed through as *absent*; the gate
   then answers ``UNKNOWN`` for a dimension rather than inventing a PASS/FAIL.
   A VISION *infrastructure* failure is NOT converted into a visual finding —
   it is a technical condition the existing QA already blocks on.
2. **No new mandatory model call.** This module only reads files and maps
   already-produced evidence; it never invokes a model, a browser, or the
   network. The single conditional VISION re-run after a fidelity repair is
   owned by the orchestrator (the mandated post-repair re-verification, C4).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from app.core.asset_strategy import plan_assets
from app.core.design_fidelity import FidelityReport, evaluate_design_fidelity
from app.core.document_store import read_design_guidance, read_documents

logger = logging.getLogger(__name__)

#: Generated source / style suffixes the evidence reader concatenates.
_SOURCE_SUFFIXES: Tuple[str, ...] = (".tsx", ".jsx", ".ts", ".js")
_STYLE_SUFFIXES: Tuple[str, ...] = (".css",)

#: Bounded read budgets, so a pathological workspace can never blow up memory.
_MAX_SOURCE_CHARS = 120_000
_MAX_STYLE_CHARS = 40_000
_MAX_DOC_CHARS = 20_000
#: Bound on the number of VISION evidence strings handed to the gate.
_MAX_EVIDENCE_ITEMS = 40


def _read_tree(root: Path, suffixes: Tuple[str, ...], limit: int) -> str:
    """Concatenate matching files under ``root``, bounded to ``limit`` chars.

    Deterministic order (sorted paths) and per-file provenance comments, so the
    evidence is reproducible and a reviewer can see which file a label came
    from. A missing directory or an unreadable file is skipped, never fatal.
    """
    if not root.exists():
        return ""
    parts = []
    total = 0
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunk = f"\n/* {path.name} */\n{text}"
        if total + len(chunk) > limit:
            chunk = chunk[: max(0, limit - total)]
        if not chunk:
            break
        parts.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    return "".join(parts)


def _read_prd(workspace: Path) -> str:
    try:
        prd, _design = read_documents(workspace)
    except Exception:  # noqa: BLE001 - a broken document is simply absent
        return ""
    text = getattr(prd, "text", "") if prd is not None else ""
    return (text or "")[:_MAX_DOC_CHARS]


def _read_guidance(workspace: Path) -> str:
    try:
        doc = read_design_guidance(workspace)
    except Exception:  # noqa: BLE001 - a broken document is simply absent
        return ""
    text = getattr(doc, "text", "") if doc is not None else ""
    return (text or "")[:_MAX_DOC_CHARS]


def vision_to_screenshot_evidence(vision: Any) -> Tuple[str, ...]:
    """Map VISION evidence onto the gate's ``screenshot_evidence`` schema (B1-B3).

    * ``blocking_findings`` become ``[blocking] …`` evidence (concrete defects).
    * ``observations`` become ``[observation] …`` evidence (subjective).
    * ``raw_error`` (VISION could not run) is deliberately NOT mapped: that is a
      technical/infrastructure condition, not a visual defect, and the existing
      QA already fails closed on it. Converting it here would fabricate a
      visual finding.
    * ``None`` (no VISION evidence at all) yields ``()``, so the gate answers
      ``UNKNOWN`` rather than inventing a failed inspection (B3).
    """
    if vision is None:
        return ()
    out = []
    for item in list(getattr(vision, "blocking_findings", []) or []):
        text = str(item).strip()
        if text:
            out.append(f"[blocking] {text}")
    for item in list(getattr(vision, "observations", []) or []):
        text = str(item).strip()
        if text:
            out.append(f"[observation] {text}")
    return tuple(out[:_MAX_EVIDENCE_ITEMS])


def build_fidelity_report(
    workspace: Path,
    brief: Optional[Mapping[str, Any]],
    *,
    vision: Any = None,
    technical_ok: bool = True,
    prd_text: Optional[str] = None,
    guidance_text: Optional[str] = None,
) -> FidelityReport:
    """Read the current revision's evidence and evaluate it (pure, no model).

    Every artifact is read from ``workspace`` at call time, so a caller that
    re-invokes this after a repair evaluates the ACTUAL changed artifacts
    (B5/B6). ``prd_text``/``guidance_text`` may be injected by a test.
    """
    workspace = Path(workspace)
    source_text = _read_tree(workspace / "src", _SOURCE_SUFFIXES, _MAX_SOURCE_CHARS)
    css_text = _read_tree(workspace / "src", _STYLE_SUFFIXES, _MAX_STYLE_CHARS)
    guidance = (
        guidance_text if guidance_text is not None else _read_guidance(workspace)
    )
    prd = prd_text if prd_text is not None else _read_prd(workspace)
    asset_plan = plan_assets(brief if isinstance(brief, Mapping) else {})
    return evaluate_design_fidelity(
        brief=brief,
        prd_text=prd,
        guidance_text=guidance,
        asset_plan=asset_plan,
        source_text=source_text,
        css_text=css_text,
        screenshot_evidence=vision_to_screenshot_evidence(vision),
        technical_ok=technical_ok,
    )


def make_fidelity_evaluate(
    workspace: Path,
    brief: Optional[Mapping[str, Any]],
    *,
    vision_provider: Callable[[], Any],
    technical_ok_provider: Callable[[], bool],
) -> Callable[[], FidelityReport]:
    """Build the zero-arg ``evaluate`` the bounded gate re-invokes.

    The providers are read at EACH call, so the post-repair evaluation reflects
    the fresh attempt's VISION evidence and the fresh rebuild's technical state.
    """

    def _evaluate() -> FidelityReport:
        return build_fidelity_report(
            workspace,
            brief,
            vision=vision_provider(),
            technical_ok=technical_ok_provider(),
        )

    return _evaluate


__all__ = [
    "vision_to_screenshot_evidence",
    "build_fidelity_report",
    "make_fidelity_evaluate",
]
