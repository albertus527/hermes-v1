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
import re
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from app.core.asset_strategy import plan_assets
from app.core.design_fidelity import (
    DIM_VISUAL_EXECUTION,
    SEVERITY_HARD,
    FidelityReport,
    evaluate_design_fidelity,
)
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


# ---------------------------------------------------------------------------
# V4-B2: VISION-vs-fidelity CONSISTENCY classification (calibration)
# ---------------------------------------------------------------------------
#
# A VISION "blocking" verdict and the fidelity gate's own evidence-based
# detectors are INDEPENDENT sources. Neither is allowed to override the other
# here:
#
#   * QA stays AUTHORITATIVE — a VISION blocking finding still blocks
#     acceptance (no tested snapshot, no preview). Nothing in this section
#     downgrades or bypasses it.
#   * The fidelity gate never manufactures a defect from a bare
#     ``vision.pass_ is False``. A blocking finding is only ever treated as an
#     independently CONFIRMED defect when the gate's OWN evidence-based
#     detectors found a matching HARD visual finding.
#
# What this adds is an honest, recorded classification so an operator can tell
# a corroborated defect from an unconfirmed (possibly model-nondeterministic)
# one, and can see when identical evidence received contradictory verdicts.

#: Classification vocabulary for a single VISION blocking finding.
VISION_BLOCKING_REPORTED = "VISION_BLOCKING_REPORTED"
FIDELITY_CONFIRMED_DEFECT = "FIDELITY_CONFIRMED_DEFECT"
INCONSISTENT_OR_UNVERIFIED_FINDING = "INCONSISTENT_OR_UNVERIFIED_FINDING"

VISION_CONSISTENCY_CLASSES: Tuple[str, ...] = (
    VISION_BLOCKING_REPORTED,
    FIDELITY_CONFIRMED_DEFECT,
    INCONSISTENT_OR_UNVERIFIED_FINDING,
)

#: Bounded region vocabulary. A ``location`` is ONLY ever derived from words the
#: finding ITSELF contains — never invented. No match -> "unspecified".
_REGION_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("navigation", ("navigation", "navbar", "nav", "menu")),
    ("header", ("header",)),
    ("hero", ("hero",)),
    ("showcase", ("showcase", "karya", "portfolio", "project")),
    ("services", ("service", "services", "layanan")),
    ("contact", ("contact", "kontak")),
    ("footer", ("footer",)),
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "have", "has",
    "are", "was", "were", "not", "but", "its", "into", "onto", "than",
    "then", "there", "their", "text", "page", "site", "which", "when",
    "where", "what", "does", "make", "makes", "item", "items", "lacks",
})


def _tokens(text: Any) -> frozenset:
    """Significant lowercase word tokens (>=4 chars, stopwords removed)."""
    return frozenset(
        t for t in _WORD_RE.findall(str(text or "").lower())
        if len(t) >= 4 and t not in _STOPWORDS
    )


def _region_of(text: Any) -> str:
    low = str(text or "").lower()
    for region, keys in _REGION_KEYWORDS:
        if any(k in low for k in keys):
            return region
    return "unspecified"


def _shot_pair(entry: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    shots = (entry or {}).get("screenshots_sha") or {}
    return shots.get("desktop"), shots.get("mobile")


def _same_evidence(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """True only when BOTH screenshots are present and byte-identical."""
    da, ma = _shot_pair(a)
    db, mb = _shot_pair(b)
    return bool(da and db and da == db and ma and ma == mb)


def _finding_field(f: Any, key: str, default: Any = "") -> Any:
    """Read a field from a FidelityFinding object OR its ``to_dict()`` form."""
    if isinstance(f, Mapping):
        return f.get(key, default)
    return getattr(f, key, default)


def build_vision_consistency(
    entries: Optional[Sequence[Mapping[str, Any]]],
    report: Any,
) -> Dict[str, Any]:
    """Classify VISION blocking findings. Pure, deterministic, no I/O.

    ``entries`` is an ordered list (oldest first) of per-attempt dicts::

        {"attempt": int, "pass": bool|None, "blocking": [str],
         "observations": [str],
         "screenshots_sha": {"desktop": str|None, "mobile": str|None}}

    ``report`` is a :class:`FidelityReport` (or its ``to_dict()`` form). The
    classification is:

    * ``FIDELITY_CONFIRMED_DEFECT`` — the gate's OWN detectors produced a HARD
      ``VISUAL_EXECUTION`` finding that shares >=2 significant tokens with the
      VISION finding (independent corroboration).
    * ``INCONSISTENT_OR_UNVERIFIED_FINDING`` — no corroboration, AND an earlier
      attempt on byte-identical screenshots returned a PASS (the same pixels
      got contradictory verdicts).
    * ``VISION_BLOCKING_REPORTED`` — no corroboration and no contradiction:
      recorded as reported-but-unconfirmed. Never silently accepted, never
      silently dismissed.
    """
    entries = list(entries or [])
    final: Mapping[str, Any] = entries[-1] if entries else {}
    blocking = [str(b) for b in (final.get("blocking") or []) if str(b).strip()]

    if isinstance(report, Mapping):
        findings = report.get("findings") or ()
    else:
        findings = getattr(report, "findings", ()) or ()

    hard_visual = [
        f for f in findings
        if _finding_field(f, "severity", None) == SEVERITY_HARD
        and _finding_field(f, "dimension", None) == DIM_VISUAL_EXECUTION
    ]
    hard_tokens = [
        (
            _finding_field(f, "rule_id", "?"),
            _tokens(_finding_field(f, "finding", ""))
            | _tokens(_finding_field(f, "evidence", "")),
        )
        for f in hard_visual
    ]

    contradictions = []
    for entry in entries[:-1]:
        if (entry or {}).get("pass") is True and _same_evidence(entry, final):
            contradictions.append({
                "earlier_attempt": (entry or {}).get("attempt"),
                "final_attempt": (final or {}).get("attempt"),
                "reason": "identical_screenshots_contradictory_verdicts",
            })

    classes = []
    for text in blocking:
        toks = _tokens(text)
        matched = [rid for rid, htoks in hard_tokens if len(toks & htoks) >= 2]
        if matched:
            klass = FIDELITY_CONFIRMED_DEFECT
        elif contradictions:
            klass = INCONSISTENT_OR_UNVERIFIED_FINDING
        else:
            klass = VISION_BLOCKING_REPORTED
        classes.append({
            "finding": text,
            "severity": "blocking",
            "location": _region_of(text),
            "location_source": "vision_text",
            "evidence": text,
            "classification": klass,
            "corroborating_fidelity_rule_ids": matched,
        })

    notes = []
    if blocking and not hard_visual:
        notes.append(
            "no independent fidelity finding corroborates the VISION blocking "
            "finding(s); recorded as reported-but-unconfirmed"
        )
    if contradictions:
        notes.append(
            "identical screenshots received contradictory VISION verdicts; "
            "recorded as a consistency issue, not silently accepted"
        )
    return {
        "classes": classes,
        "contradiction_detected": bool(contradictions),
        "contradictions": contradictions,
        "notes": notes,
    }


__all__ = [
    "vision_to_screenshot_evidence",
    "build_fidelity_report",
    "make_fidelity_evaluate",
    "build_vision_consistency",
    "VISION_BLOCKING_REPORTED",
    "FIDELITY_CONFIRMED_DEFECT",
    "INCONSISTENT_OR_UNVERIFIED_FINDING",
    "VISION_CONSISTENCY_CLASSES",
]
