"""V3-C: the bounded, evidence-based Design Fidelity Gate.

V2-A/B preserved owner intent; V3-A/B turned it into actionable guidance and an
honest asset strategy. V3-C closes the loop: it evaluates the RENDERED website
against that intent and produces an honest, dimension-separated verdict plus
concrete, evidence-backed findings — without turning QA into a rigid aesthetic
checklist.

This module is **pure policy + evaluation**: no subprocess, no filesystem, no
network, no model call. It does not replace VISION (which judges pixels) or the
Impeccable critic (an anti-pattern code scanner); it CONSUMES their evidence and
the deterministic evidence, and keeps four verdicts strictly separate:

* ``TECHNICAL_PASS``       — technical correctness. The existing deterministic
  QA is authoritative; this gate never overrides it.
* ``REQUIREMENT_FIDELITY`` — does the render match the owner's explicit
  ``design_intent``, the PRD, and the DesignGuidance?
* ``VISUAL_EXECUTION``     — concrete visual weaknesses DEMONSTRABLE from
  evidence (repetition, weak hierarchy) — never a subjective score.
* ``ASSET_HONESTY``        — are conceptual visuals disclosed, and is nothing
  misrepresented as a real completed project?

A finding may only trigger a repair when it is HIGH-confidence AND actionable
(a contradiction or a concrete defect). Everything else is surfaced, never
acted on. The repair loop is bounded to ONE pass, and a repair is never marked
PASS without verifying the actual repaired result.

Intentional minimalism is never penalised: the gate has no fixed image counts,
no hardcoded colour ratios, no mandatory hero photo, and no arbitrary score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Verdict dimensions (A2)
# ---------------------------------------------------------------------------

DIM_TECHNICAL = "TECHNICAL_PASS"
DIM_REQUIREMENT_FIDELITY = "REQUIREMENT_FIDELITY"
DIM_VISUAL_EXECUTION = "VISUAL_EXECUTION"
DIM_ASSET_HONESTY = "ASSET_HONESTY"

DIMENSIONS: Tuple[str, ...] = (
    DIM_TECHNICAL,
    DIM_REQUIREMENT_FIDELITY,
    DIM_VISUAL_EXECUTION,
    DIM_ASSET_HONESTY,
)

#: Per-dimension verdicts. ``UNKNOWN`` is the honest answer when there is not
#: enough evidence — it is never coerced into PASS or FAIL.
VERDICT_PASS = "PASS"
VERDICT_WARN = "WARN"
VERDICT_FAIL = "FAIL"
VERDICT_UNKNOWN = "UNKNOWN"

VERDICTS: Tuple[str, ...] = (VERDICT_PASS, VERDICT_WARN, VERDICT_FAIL, VERDICT_UNKNOWN)

#: Finding severity classes. HARD == a technical/representation failure the
#: pipeline must not ship silently. SOFT == a visual-quality warning that never
#: blocks on its own (B7).
SEVERITY_HARD = "hard"
SEVERITY_SOFT = "soft"
SEVERITIES: Tuple[str, ...] = (SEVERITY_HARD, SEVERITY_SOFT)

#: Confidence. Only HIGH-confidence, actionable findings may trigger a repair
#: (B3). LOW-confidence findings are surfaced, never acted on.
CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

#: Maximum fidelity-driven repair attempts per build (B4): ONE.
MAX_FIDELITY_REPAIR_ATTEMPTS = 1

#: Bounded set of stable reason codes. A caller switches on these.
FIDELITY_REPAIR_REASONS: Tuple[str, ...] = (
    "no_high_confidence_findings",
    "technical_failure_is_not_visually_repairable",
    "repair_budget_exhausted",
    "repair_allowed",
    "repair_verified",
    "repair_not_verified",
    "repair_stalled",
    "insufficient_evidence",
)

#: Bound on echoed evidence, so a finding can never smuggle an unbounded blob.
MAX_EVIDENCE_CHARS = 400

# ---------------------------------------------------------------------------
# Detector vocabulary (bounded, evidence-based)
# ---------------------------------------------------------------------------

#: Quoted DISPLAY strings the repetition detector compares. Two guards keep it
#: on visible text only: a negative lookbehind rejects an opening quote preceded
#: by ``=`` or a word character (``x2="140"``, ``="…"``), and the content class
#: excludes ``=`` so a stray ``x2=`` fragment can never be captured.
_STRING_LITERAL = re.compile(r"""(?<![=\w])['"`]([^'"`=\n]{3,80})['"`]""")
#: A trailing index ("Contoh Proyek 01") is normalised away so a numbered family
#: of otherwise-identical labels reads as ONE repeated label.
_TRAILING_INDEX = re.compile(r"\b0?[1-9]\d?\b")
#: A label must contain at least one letter to be a display label (rejects pure
#: numeric attribute noise).
_HAS_LETTER = re.compile(r"[A-Za-z]")
#: A repeated label must ALSO look human-facing: a multi-word phrase or a
#: capitalised label. This rejects single lower-case code tokens (e.g. an SVG
#: motif enum value ``'arch'``) that are repeated in source but are not visible
#: card text. Deliberately conservative: it may miss an unusual single
#: lower-case word used as a repeated card title, which is an acceptable miss.
_DISPLAY_LIKE = re.compile(r"(?:\s)|[A-Z]")
#: Placeholder tokens that are launch-critical when they reach a primary CTA.
_CTA_PLACEHOLDER = re.compile(
    r"\b(lorem|ipsum|dolor|todo|tbd|fixme|xxx+|placeholder|example\.com|"
    r"your[- ]?domain|coming soon|segera hadir)\b",
    re.IGNORECASE,
)
#: A primary action / CTA region in generated JSX.
_CTA_REGION = re.compile(
    r"(?:<a\b[^>]*>|<button\b[^>]*>|href\s*=|onClick\s*=)([^<>{]{0,120})",
    re.IGNORECASE,
)
#: Disclosure terms that make a conceptual visual honest.
_DISCLOSURE_TERMS: Tuple[str, ...] = (
    "placeholder", "konsep", "conceptual", "ilustrasi", "illustration",
    "contoh", "belum tersedia", "akan ditambahkan", "draft", "mockup",
    "preview", "pratinjau",
)
#: Terms that CLAIM the visuals are the studio's own real, completed work.
_REAL_WORK_CLAIM: Tuple[str, ...] = (
    "karya kami", "our work", "our projects", "proyek kami", "portfolio kami",
    "our portfolio", "completed projects", "proyek selesai", "klien kami",
    "our clients", "studi kasus kami", "our case studies",
)
#: Colour words the owner may name, mapped to CSS-ish tokens that count as the
#: same colour family. Used ONLY to detect a CONTRADICTION, never to impose a
#: value (no hex is required or emitted).
_COLOR_FAMILY: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("terracotta", ("terracotta", "#a8674a", "#c16a4a", "#b5651d", "#cc6b49")),
    ("off-white", ("off-white", "off white", "#faf9f7", "#f7f5f0", "#faf8f5", "#fdfcf9")),
    ("charcoal", ("charcoal", "#1c1b1a", "#222", "#2b2b2b", "#333")),
)
#: Typography families the owner may name.
_TYPO_FAMILY: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("serif", ("serif", "georgia", "times", "garamond", "playfair", "fraunces")),
    ("sans", ("sans", "system-ui", "inter", "roboto", "helvetica", "arial")),
)


def _bounded(value: Any, limit: int = MAX_EVIDENCE_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _normalise_label(text: str) -> str:
    """Lower-case, collapse whitespace, drop a trailing index."""
    lowered = " ".join(_bounded(text, 120).lower().split())
    return _TRAILING_INDEX.sub("#", lowered).strip()


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FidelityFinding:
    """One evidence-backed finding, kept in a closed vocabulary.

    ``repairable`` is the ONLY field the repair loop gates on, and it is True
    only for a HIGH-confidence, actionable finding (B3). The application owns
    this decision; a detector never authorizes its own repair.
    """

    rule_id: str
    dimension: str
    severity: str
    confidence: str
    location: str
    finding: str
    evidence: str
    suggested_action: str

    @property
    def repairable(self) -> bool:
        return self.severity == SEVERITY_HARD and self.confidence == CONFIDENCE_HIGH

    @property
    def identity(self) -> str:
        return f"{self.rule_id.strip().lower()}::{' '.join(self.finding.lower().split())}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "dimension": self.dimension,
            "severity": self.severity,
            "confidence": self.confidence,
            "location": self.location,
            "finding": self.finding,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
            "repairable": self.repairable,
        }


@dataclass(frozen=True)
class FidelityReport:
    """The gate's verdict, one value per dimension, plus its findings."""

    dimensions: Mapping[str, str]
    findings: Tuple[FidelityFinding, ...] = ()
    evidence_sufficient: bool = True
    notes: Tuple[str, ...] = ()

    @property
    def overall(self) -> str:
        values = tuple(self.dimensions.get(d, VERDICT_UNKNOWN) for d in DIMENSIONS)
        if VERDICT_FAIL in values:
            return VERDICT_FAIL
        if VERDICT_WARN in values:
            return VERDICT_WARN
        if VERDICT_PASS in values:
            return VERDICT_PASS
        return VERDICT_UNKNOWN

    def hard_findings(self) -> Tuple[FidelityFinding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_HARD)

    def repairable_findings(self) -> Tuple[FidelityFinding, ...]:
        return tuple(f for f in self.findings if f.repairable)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimensions": dict(self.dimensions),
            "overall": self.overall,
            "evidence_sufficient": self.evidence_sufficient,
            "notes": list(self.notes),
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Detectors (A3) — each returns zero or more findings, evidence-based
# ---------------------------------------------------------------------------


def _display_labels(source_text: str):
    """Yield quoted DISPLAY labels from ``source_text``, skipping markup.

    The ``_STRING_LITERAL`` pattern already excludes attribute values (its
    lookbehind rejects a quote preceded by ``=`` or a word char, and its content
    class excludes ``=``); this helper additionally requires a letter so pure
    numeric noise can never be treated as a label.
    """
    for match in _STRING_LITERAL.finditer(source_text or ""):
        value = match.group(1)
        if _HAS_LETTER.search(value) and _DISPLAY_LIKE.search(value):
            yield value


def _detect_repeated_content(source_text: str) -> Tuple[FidelityFinding, ...]:
    """Identical/near-identical repeated card labels (A3: unintended repetition).

    Only fires when a normalised label recurs >= 3 times, which is the pattern
    the V1 audit found ("Contoh Proyek 01/02/03", "Layanan Studio" x3). Two
    similar items is a design choice, not a defect. Only DISPLAY text is
    considered — code attributes are ignored.
    """
    counts: Dict[str, int] = {}
    sample: Dict[str, str] = {}
    for raw in _display_labels(source_text):
        label = _normalise_label(raw)
        if len(label) < 3:
            continue
        counts[label] = counts.get(label, 0) + 1
        sample.setdefault(label, _bounded(raw, 80))
    repeated = sorted(
        ((label, n) for label, n in counts.items() if n >= 3),
        key=lambda kv: (-kv[1], kv[0]),
    )
    findings = []
    for label, n in repeated[:3]:
        findings.append(
            FidelityFinding(
                rule_id="repeated-card-content",
                dimension=DIM_VISUAL_EXECUTION,
                severity=SEVERITY_HARD,
                confidence=CONFIDENCE_HIGH,
                location="repeated cards / list items",
                finding=(
                    f"{n} cards share the same normalised label "
                    f"'{label}' (e.g. \"{sample[label]}\")"
                ),
                evidence=f"label '{label}' appears {n} times in the generated source",
                suggested_action=(
                    "Give each card distinct, honest content, or reduce to the "
                    "number of real items; if the items are examples, label them "
                    "as such rather than repeating one template."
                ),
            )
        )
    return tuple(findings)


def _detect_unlabeled_conceptual(
    source_text: str,
    asset_plan: Optional[Mapping[str, Any]],
) -> Tuple[FidelityFinding, ...]:
    """Conceptual visuals presented as real work, or left undisclosed (A3/C3).

    Fires only when the asset strategy says the showcase is CONCEPTUAL and the
    source neither discloses that nor (worse) claims the visuals are the
    studio's own completed work. A clearly-labelled conceptual preview is
    ALLOWED and produces no finding.
    """
    if not isinstance(asset_plan, Mapping):
        return ()
    state = (asset_plan.get("states") or {}).get("showcase")
    if state != "CONCEPTUAL":
        return ()
    haystack = (source_text or "").lower()
    if not haystack:
        return ()
    disclosed = any(term in haystack for term in _DISCLOSURE_TERMS)
    claims_real = [term for term in _REAL_WORK_CLAIM if term in haystack]
    if claims_real:
        return (
            FidelityFinding(
                rule_id="conceptual-presented-as-real",
                dimension=DIM_ASSET_HONESTY,
                severity=SEVERITY_HARD,
                confidence=CONFIDENCE_HIGH,
                location="showcase / portfolio section",
                finding=(
                    "conceptual visuals are presented as the studio's own real "
                    "work (" + ", ".join(sorted(claims_real)) + ")"
                ),
                evidence="source claims real work while the asset strategy state is CONCEPTUAL",
                suggested_action=(
                    "Remove the claim of real completed work, or replace the "
                    "visuals with owner-supplied assets."
                ),
            ),
        )
    if not disclosed:
        return (
            FidelityFinding(
                rule_id="conceptual-not-disclosed",
                dimension=DIM_ASSET_HONESTY,
                severity=SEVERITY_HARD,
                confidence=CONFIDENCE_HIGH,
                location="showcase / portfolio section",
                finding="conceptual visuals are not disclosed as placeholders",
                evidence="asset strategy state is CONCEPTUAL and no disclosure term is present",
                suggested_action=(
                    "Label each conceptual visual clearly (e.g. a 'placeholder' "
                    "or 'concept' note) so it is never mistaken for real work."
                ),
            ),
        )
    return ()


def _detect_launch_critical_placeholder(
    source_text: str,
    guidance_text: str,
) -> Tuple[FidelityFinding, ...]:
    """Placeholder/debug text inside a primary CTA or production-critical copy."""
    findings = []
    for match in _CTA_REGION.finditer(source_text or ""):
        region = match.group(1) or ""
        hit = _CTA_PLACEHOLDER.search(region)
        if hit:
            findings.append(
                FidelityFinding(
                    rule_id="launch-critical-placeholder",
                    dimension=DIM_REQUIREMENT_FIDELITY,
                    severity=SEVERITY_HARD,
                    confidence=CONFIDENCE_HIGH,
                    location="primary call-to-action",
                    finding=f"placeholder text '{hit.group(0)}' in a primary CTA",
                    evidence=_bounded(region, 120),
                    suggested_action=(
                        "Replace the placeholder with a real label or an honest "
                        "neutral label; never ship a placeholder in the primary action."
                    ),
                )
            )
            break
    return tuple(findings)


def _detect_intent_contradiction(
    brief: Mapping[str, Any],
    source_text: str,
    css_text: str,
) -> Tuple[FidelityFinding, ...]:
    """A MAJOR contradiction to an explicit owner colour/typography requirement.

    Conservative and evidence-based: it fires only when the owner named a colour
    or type family AND the render declares design values that exclude it. If the
    render carries no such evidence, the dimension is UNKNOWN — never a FAIL
    invented from absence.
    """
    intent = _bounded((brief or {}).get("design_intent"), 600).lower()
    if not intent:
        return ()
    render = f"{source_text or ''}\n{css_text or ''}".lower()
    if not render.strip():
        return ()

    findings = []
    for colour, tokens in _COLOR_FAMILY:
        if colour not in intent:
            continue
        if not any(tok in render for tok in tokens):
            findings.append(
                FidelityFinding(
                    rule_id=f"intent-colour-contradiction:{colour}",
                    dimension=DIM_REQUIREMENT_FIDELITY,
                    severity=SEVERITY_HARD,
                    confidence=CONFIDENCE_HIGH,
                    location="palette",
                    finding=(
                        f"owner required '{colour}' but no '{colour}' value "
                        "appears in the render"
                    ),
                    evidence=f"owner intent names '{colour}'; render declares no matching value",
                    suggested_action=(
                        f"Apply the owner's '{colour}' as specified, or record why "
                        "it is intentionally omitted."
                    ),
                )
            )
    for family, tokens in _TYPO_FAMILY:
        if family not in intent:
            continue
        if not any(tok in render for tok in tokens):
            findings.append(
                FidelityFinding(
                    rule_id=f"intent-typography-contradiction:{family}",
                    dimension=DIM_REQUIREMENT_FIDELITY,
                    severity=SEVERITY_HARD,
                    confidence=CONFIDENCE_HIGH,
                    location="typography",
                    finding=(
                        f"owner required a {family} typeface but none appears in the render"
                    ),
                    evidence=f"owner intent names '{family}'; render declares no matching family",
                    suggested_action=(
                        f"Use a {family} typeface as the owner specified, or record "
                        "why it is intentionally omitted."
                    ),
                )
            )
    return tuple(findings)


def _detect_weak_hierarchy(
    screenshot_evidence: Optional[Sequence[str]],
) -> Tuple[FidelityFinding, ...]:
    """Weak/repetitive hierarchy, ONLY when demonstrable from evidence (A3).

    Without screenshot/VISION evidence this returns nothing and the dimension is
    reported as UNKNOWN rather than invented. It is a SOFT warning: weak
    hierarchy never blocks on its own.
    """
    if not screenshot_evidence:
        return ()
    joined = " ".join(_bounded(e, 200).lower() for e in screenshot_evidence)
    signals = (
        "same size", "identical heading", "no visual hierarchy",
        "flat hierarchy", "repetitive composition", "uniform",
    )
    hit = next((s for s in signals if s in joined), "")
    if not hit:
        return ()
    return (
        FidelityFinding(
            rule_id="weak-hierarchy",
            dimension=DIM_VISUAL_EXECUTION,
            severity=SEVERITY_SOFT,
            confidence=CONFIDENCE_HIGH,
            location="page hierarchy",
            finding=f"weak or repetitive hierarchy reported by visual evidence ({hit})",
            evidence=_bounded(joined, 200),
            suggested_action=(
                "Increase contrast between levels (size, weight, spacing) so the "
                "page has a clear reading order."
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Evaluation (A1) — pure function over evidence
# ---------------------------------------------------------------------------


def evaluate_design_fidelity(
    *,
    brief: Optional[Mapping[str, Any]] = None,
    prd_text: Optional[str] = None,
    guidance_text: Optional[str] = None,
    asset_plan: Optional[Mapping[str, Any]] = None,
    source_text: Optional[str] = None,
    css_text: Optional[str] = None,
    screenshot_evidence: Optional[Sequence[str]] = None,
    technical_ok: bool = True,
) -> FidelityReport:
    """Evaluate the rendered website and return a dimension-separated verdict.

    ``technical_ok`` is the AUTHORITATIVE deterministic result (render, build,
    typecheck, screenshots, overflow). When it is False the gate reports
    ``TECHNICAL_PASS = FAIL`` and marks technical failure as NOT visually
    repairable, so a hard technical failure is never laundered into a cosmetic
    repair (B7).

    Every detector is evidence-based and conservative: absent evidence yields
    UNKNOWN, never an invented FAIL. Intentional minimalism produces no finding.
    """
    brief = brief if isinstance(brief, Mapping) else {}
    findings: list = []
    notes: list = []

    # --- ASSET_HONESTY -----------------------------------------------------
    findings.extend(_detect_unlabeled_conceptual(source_text or "", asset_plan))

    # --- VISUAL_EXECUTION --------------------------------------------------
    findings.extend(_detect_repeated_content(source_text or ""))
    findings.extend(_detect_weak_hierarchy(screenshot_evidence))

    # --- REQUIREMENT_FIDELITY ---------------------------------------------
    findings.extend(_detect_launch_critical_placeholder(source_text or "", guidance_text or ""))
    findings.extend(_detect_intent_contradiction(brief, source_text or "", css_text or ""))

    # --- verdicts ----------------------------------------------------------
    dims: Dict[str, str] = {}

    dims[DIM_TECHNICAL] = VERDICT_PASS if technical_ok else VERDICT_FAIL
    if not technical_ok:
        notes.append("technical QA failed; this is not a visually repairable defect")

    def _dimension_verdict(dim: str, *, evidence_present: bool) -> str:
        related = [f for f in findings if f.dimension == dim]
        if any(f.severity == SEVERITY_HARD for f in related):
            return VERDICT_FAIL
        if any(f.severity == SEVERITY_SOFT for f in related):
            return VERDICT_WARN
        return VERDICT_PASS if evidence_present else VERDICT_UNKNOWN

    has_intent = bool(_bounded((brief or {}).get("design_intent"), 600))
    has_render = bool((source_text or "").strip() or (css_text or "").strip())
    dims[DIM_REQUIREMENT_FIDELITY] = _dimension_verdict(
        DIM_REQUIREMENT_FIDELITY, evidence_present=has_intent and has_render
    )
    dims[DIM_VISUAL_EXECUTION] = _dimension_verdict(
        DIM_VISUAL_EXECUTION, evidence_present=has_render
    )
    dims[DIM_ASSET_HONESTY] = _dimension_verdict(
        DIM_ASSET_HONESTY, evidence_present=has_render
    )

    # The gate is "evidence-sufficient" when it actually saw the render. When it
    # did not, the non-technical dimensions are honestly UNKNOWN rather than
    # invented PASS/FAIL.
    evidence_sufficient = has_render
    if not has_render:
        notes.append("no rendered source/CSS was supplied; visual dimensions are UNKNOWN")
    return FidelityReport(
        dimensions=dims,
        findings=tuple(findings),
        evidence_sufficient=evidence_sufficient,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Bounded repair policy (B4/B5/B6/B8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FidelityRepairBudget:
    """Remaining bounded budget for a fidelity-driven repair (ONE pass)."""

    max_attempts: int = MAX_FIDELITY_REPAIR_ATTEMPTS
    attempts_used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts_used)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def consume(self) -> "FidelityRepairBudget":
        return FidelityRepairBudget(self.max_attempts, self.attempts_used + 1)


def repair_allowed(
    report: FidelityReport,
    *,
    budget: Optional[FidelityRepairBudget] = None,
) -> Tuple[bool, str]:
    """Whether ONE bounded repair may run for ``report``.

    A repair is allowed only when (a) the budget is not exhausted, and (b) there
    is at least one HIGH-confidence actionable finding. A technical failure is
    NOT visually repairable and returns that explicit reason (B7).
    """
    budget = budget or FidelityRepairBudget()
    if report.dimensions.get(DIM_TECHNICAL) == VERDICT_FAIL:
        return False, "technical_failure_is_not_visually_repairable"
    if not report.repairable_findings():
        return False, "no_high_confidence_findings"
    if budget.exhausted:
        return False, "repair_budget_exhausted"
    return True, "repair_allowed"


def verify_repair(
    before: FidelityReport,
    after: FidelityReport,
    *,
    technical_ok: bool = True,
) -> Tuple[bool, str]:
    """Post-repair verification (B5/B6).

    A repair is verified only when the SAME findings no longer appear, no new
    HARD finding was introduced, and the technical gates still hold. A repair is
    NEVER marked PASS merely because a repair ran.
    """
    if not technical_ok or after.dimensions.get(DIM_TECHNICAL) == VERDICT_FAIL:
        return False, "repair_not_verified"
    before_ids = {f.identity for f in before.findings if f.severity == SEVERITY_HARD}
    after_ids = {f.identity for f in after.findings if f.severity == SEVERITY_HARD}
    if before_ids & after_ids:
        return False, "repair_stalled"
    if after_ids - before_ids:
        return False, "repair_not_verified"
    return True, "repair_verified"


def overall_terminal_outcome(
    report: FidelityReport,
    *,
    repair_used: bool,
    repair_verified: bool,
) -> str:
    """The terminal fidelity outcome for a build.

    Clean, closed set so a caller cannot invent one by spelling a new string.
    """
    if report.overall == VERDICT_FAIL and report.dimensions.get(DIM_TECHNICAL) == VERDICT_FAIL:
        return "TECHNICAL_FAILED"
    if report.overall == VERDICT_PASS:
        return "FIDELITY_PASS"
    if report.overall == VERDICT_WARN:
        return "FIDELITY_WARN"          # soft warnings never block (B7)
    if report.overall == VERDICT_UNKNOWN:
        return "FIDELITY_UNKNOWN"
    # overall == FAIL, not technical
    if repair_used and repair_verified:
        return "FIDELITY_REPAIRED"
    if repair_used and not repair_verified:
        return "FIDELITY_REPAIR_FAILED"
    return "FIDELITY_FAILED"


TERMINAL_OUTCOMES: Tuple[str, ...] = (
    "FIDELITY_PASS",
    "FIDELITY_WARN",
    "FIDELITY_UNKNOWN",
    "FIDELITY_REPAIRED",
    "FIDELITY_REPAIR_FAILED",
    "FIDELITY_FAILED",
    "TECHNICAL_FAILED",
)


def plan_repair(
    report: FidelityReport,
    *,
    budget: Optional[FidelityRepairBudget] = None,
) -> Dict[str, Any]:
    """The bounded repair plan: the allowed findings plus the consumed budget.

    Returns ``{"allowed": bool, "reason": str, "findings": [...], "budget": ...}``.
    Only HIGH-confidence actionable findings are included, and they are sorted
    deterministically. It never authorizes more than the budget permits.
    """
    budget = budget or FidelityRepairBudget()
    allowed, reason = repair_allowed(report, budget=budget)
    if not allowed:
        return {
            "allowed": False,
            "reason": reason,
            "findings": [],
            "budget": {"max_attempts": budget.max_attempts,
                       "attempts_used": budget.attempts_used},
        }
    ordered = sorted(report.repairable_findings(), key=lambda f: f.identity)
    return {
        "allowed": True,
        "reason": reason,
        "findings": [f.to_dict() for f in ordered],
        "budget": {
            "max_attempts": budget.max_attempts,
            "attempts_used": budget.consume().attempts_used,
        },
    }


# ---------------------------------------------------------------------------
# Bounded repair driver (B4/B5/B6/B8/B9)
# ---------------------------------------------------------------------------


@dataclass
class FidelityGateResult:
    """Outcome of the bounded fidelity gate for one build.

    ``failed`` True means the caller must NOT treat the build as passing (a hard
    finding remains and no verified repair resolved it). Soft warnings never set
    ``failed`` (B7).
    """

    outcome: str
    report: FidelityReport
    attempts_used: int = 0
    repair_used: bool = False
    repair_verified: bool = False
    reason: str = ""
    history: Tuple[Dict[str, Any], ...] = ()
    failed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "attempts_used": self.attempts_used,
            "repair_used": self.repair_used,
            "repair_verified": self.repair_verified,
            "reason": self.reason,
            "failed": self.failed,
            "history": list(self.history),
            "report": self.report.to_dict(),
        }


def run_bounded_fidelity_gate(
    *,
    evaluate,
    repair=None,
    budget: Optional[FidelityRepairBudget] = None,
    technical_ok: bool = True,
) -> FidelityGateResult:
    """Run the bounded fidelity gate with AT MOST ONE repair pass.

    ``evaluate`` is a zero-arg callable returning a :class:`FidelityReport` (it
    is re-invoked after a repair, so the caller re-reads the ACTUAL repaired
    artifacts — B5/B6). ``repair`` is an optional callable taking the bounded
    finding list and returning ``True`` when a repair actually ran.

    Semantics:

    * No high-confidence actionable finding -> PASS/WARN/UNKNOWN, no repair.
    * A technical failure -> never visually repaired (B7).
    * Otherwise ONE repair runs, then the gate is RE-EVALUATED and the result
      VERIFIED. A repair is never marked PASS without that verification (B6).
    * A failed/unverified repair terminates cleanly with diagnostics preserved —
      there is no loop (B8), and no second model call (B9).
    """
    budget = budget or FidelityRepairBudget()
    report = evaluate()

    if report.dimensions.get(DIM_TECHNICAL) == VERDICT_FAIL or not technical_ok:
        return FidelityGateResult(
            outcome="TECHNICAL_FAILED", report=report, attempts_used=budget.attempts_used,
            reason="technical_failure_is_not_visually_repairable", failed=True,
        )

    plan = plan_repair(report, budget=budget)
    if not plan["allowed"]:
        outcome = overall_terminal_outcome(report, repair_used=False, repair_verified=False)
        # Only a HARD finding makes the build fail; a WARN never does (B7).
        failed = report.overall == VERDICT_FAIL
        return FidelityGateResult(
            outcome=outcome, report=report, attempts_used=budget.attempts_used,
            reason=plan["reason"], failed=failed,
        )

    # --- exactly ONE bounded repair attempt -----------------------------
    budget = budget.consume()
    history: list = []
    ran = True
    if repair is not None:
        try:
            ran = bool(repair(plan["findings"]))
        except Exception:  # noqa: BLE001 - a repair failure terminates cleanly
            ran = False
    history.append({
        "attempt": budget.attempts_used,
        "max_attempts": budget.max_attempts,
        "phase": "repair_started",
        "findings": [f["rule_id"] for f in plan["findings"]],
    })

    if not ran:
        history.append({"attempt": budget.attempts_used, "phase": "repair_not_run"})
        return FidelityGateResult(
            outcome="FIDELITY_REPAIR_FAILED", report=report,
            attempts_used=budget.attempts_used, repair_used=True,
            repair_verified=False, reason="repair_execution_failed",
            history=tuple(history), failed=True,
        )

    # --- POST-REPAIR VERIFICATION (B5/B6) -------------------------------
    after = evaluate()
    verified, reason = verify_repair(report, after, technical_ok=technical_ok)
    history.append({
        "attempt": budget.attempts_used,
        "phase": "repair_verified" if verified else "repair_unverified",
        "reason": reason,
        "before_overall": report.overall,
        "after_overall": after.overall,
    })
    outcome = overall_terminal_outcome(report, repair_used=True, repair_verified=verified)
    return FidelityGateResult(
        outcome=outcome, report=after, attempts_used=budget.attempts_used,
        repair_used=True, repair_verified=verified, reason=reason,
        history=tuple(history), failed=not verified,
    )


__all__ = [
    "DIMENSIONS",
    "DIM_TECHNICAL",
    "DIM_REQUIREMENT_FIDELITY",
    "DIM_VISUAL_EXECUTION",
    "DIM_ASSET_HONESTY",
    "VERDICTS",
    "VERDICT_PASS",
    "VERDICT_WARN",
    "VERDICT_FAIL",
    "VERDICT_UNKNOWN",
    "SEVERITIES",
    "SEVERITY_HARD",
    "SEVERITY_SOFT",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "MAX_FIDELITY_REPAIR_ATTEMPTS",
    "MAX_EVIDENCE_CHARS",
    "FIDELITY_REPAIR_REASONS",
    "TERMINAL_OUTCOMES",
    "FidelityFinding",
    "FidelityReport",
    "FidelityRepairBudget",
    "FidelityGateResult",
    "evaluate_design_fidelity",
    "repair_allowed",
    "verify_repair",
    "plan_repair",
    "run_bounded_fidelity_gate",
    "overall_terminal_outcome",
]
