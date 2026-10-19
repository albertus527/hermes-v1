"""Six-dimensional design scoring with GPT-6 Luna Decisions (N8 B7).

WHAT THIS DOES
--------------
Scores a pool of REAL, evidence-backed design candidates on six independent
Design Quality dimensions plus a separate Requirement Fit judgement, using
direct multimodal Decisions requests. Images are the evidence; a screenshot is
what makes a visual judgement possible at all.

WHAT IT REFUSES TO DO
---------------------
* Score a candidate that has no rendered evidence (E2+). A style label, a
  palette or a prose descriptor is a CLAIM, and a claim is never a quality
  score.
* Score part of a pool. A partial response is a FAILED response, never a
  partially-ranked success.
* Turn an intentional abstention into a selection, or a provider failure into a
  quiet fallback that looks like a choice.
* Let model confidence influence eligibility or ranking.
* Claim that character sanitisation prevents semantic prompt injection. It does
  not; the architectural defence is that an answer is only a score or an id from
  a fixed offered set.

THE FORMULA
-----------
    Q = 0.25*hierarchy + 0.20*typography + 0.20*layout
      + 0.15*color     + 0.15*usability  + 0.05*consistency
    S = Q**0.45 * F**0.55
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from app.core.decision_provider import DesignCandidate
from app.core.design_reference import (
    DIMENSION_COLOR,
    DIMENSION_CONSISTENCY,
    DIMENSION_HIERARCHY,
    DIMENSION_LAYOUT,
    DIMENSION_TYPOGRAPHY,
    DIMENSION_USABILITY,
    EVIDENCE_RANK,
    EVIDENCE_SCORABLE_FROM,
)
from app.core.design_reference_safety import one_line

#: Identifier of this scoring contract. Bumped whenever the formula, the label
#: set or the gates change, so the engine refuses to compare values produced
#: under different rules.
SUITABILITY_CONTRACT = "n8-design-v1"

#: Rubric weights. They sum to 1.0 by construction.
WEIGHTS: Mapping[str, float] = {
    DIMENSION_HIERARCHY: 0.25,
    DIMENSION_TYPOGRAPHY: 0.20,
    DIMENSION_LAYOUT: 0.20,
    DIMENSION_COLOR: 0.15,
    DIMENSION_USABILITY: 0.15,
    DIMENSION_CONSISTENCY: 0.05,
}

#: Fixed dimension order, so question keys are deterministic.
DIMENSIONS: Tuple[str, ...] = (
    DIMENSION_HIERARCHY,
    DIMENSION_TYPOGRAPHY,
    DIMENSION_LAYOUT,
    DIMENSION_COLOR,
    DIMENSION_USABILITY,
    DIMENSION_CONSISTENCY,
)

#: Five-point rubric. ``criteria`` MUST be an array; the Decisions API rejects a
#: mapping here with HTTP 400 invalid_type.
LEVELS: Tuple[str, ...] = ("poor", "weak", "adequate", "strong", "excellent")

#: Highest index of the rubric (4 for a 5-level scale).
MAX_LEVEL_INDEX = len(LEVELS) - 1

#: Suitability exponents.
W_QUALITY = 0.45
W_FIT = 0.55

#: Provisional quality standards. Configurable; NOT calibrated probabilities.
DEFAULT_MIN_QUALITY = 0.70
DEFAULT_MIN_FIT = 0.70
DEFAULT_MIN_SUITABILITY = 0.75

#: Per-dimension floors. A severe failure cannot hide inside a weighted mean:
#: Visual Hierarchy poor with everything else excellent still yields Q=0.75,
#: which would pass the aggregate gate. The floors make it fatal instead.
DEFAULT_DIMENSION_FLOORS: Mapping[str, float] = {
    DIMENSION_HIERARCHY: 0.50,
    DIMENSION_TYPOGRAPHY: 0.50,
    DIMENSION_LAYOUT: 0.50,
    DIMENSION_COLOR: 0.50,
    DIMENSION_USABILITY: 0.25,
    DIMENSION_CONSISTENCY: 0.25,
}

#: Outcome codes. A closed set.
STATUS_SCORED = "scored"
STATUS_ABSTAINED = "abstained"
STATUS_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATUS_INSUFFICIENT_BRIEF = "insufficient_brief"
#: N8 B7 calibration: the brief carried no requirement set a Fit judgement could
#: be defended against. This is an INTENTIONAL ABSTAIN, never a low score. A
#: missing requirement is missing DATA, not a design-quality failure, and scoring
#: it as "poor" would silently convert an absence into a negative verdict.
STATUS_INSUFFICIENT_REQUIREMENTS = "insufficient_requirements"
STATUS_MALFORMED = "malformed_scores"
STATUS_INCOMPLETE = "incomplete_scores"
STATUS_STALE_EVIDENCE = "stale_evidence"
STATUS_TOO_MANY = "too_many_candidates"

#: A requirement set is only usable for Fit when it has at least this many
#: independently checkable entries. One entry is a slogan; two is a comparison;
#: three is the smallest set where "how many are satisfied" is a meaningful
#: question rather than a coin flip.
MIN_FIT_REQUIREMENTS = 3

#: Markers the native PRD compiler emits where FAST supplied nothing. A
#: requirement that is itself unresolved cannot be assessed, so it is excluded
#: from the countable set rather than counted as unmet.
UNRESOLVED_REQUIREMENT_MARKERS: Tuple[str, ...] = ("UNRESOLVED",)


def usable_requirements(requirements: Sequence[str]) -> Tuple[str, ...]:
    """Filter a requirement set down to the entries Fit can actually be judged on.

    Drops blanks, duplicates (case-insensitive), and anything carrying an
    UNRESOLVED marker. The last rule matters: the native PRD compiler writes
    ``UNRESOLVED: FAST supplied no what/why`` when the brief was empty, and
    counting that as an unmet requirement would blame the design for a data gap.
    """
    seen: set = set()
    kept: list = []
    for raw in requirements or ():
        text = one_line(raw, 300)
        if not text:
            continue
        if any(marker in text.upper() for marker in UNRESOLVED_REQUIREMENT_MARKERS):
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(text)
    return tuple(kept)


def requirements_are_sufficient(requirements: Sequence[str]) -> bool:
    """Whether a requirement set can support a defensible Fit judgement."""
    return len(usable_requirements(requirements)) >= MIN_FIT_REQUIREMENTS


class ScoringError(RuntimeError):
    """A scoring attempt failed, with a stable status code."""

    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}" if detail else status)


@dataclass(frozen=True)
class DimensionScore:
    """One dimension's raw level index and its normalised value."""

    dimension: str
    level_index: float
    value: float
    confidence: Optional[float] = None


@dataclass(frozen=True)
class CandidateScore:
    """The full scoring outcome for one candidate."""

    candidate_id: str
    quality: float
    fit: float
    suitability: float
    dimensions: Tuple[DimensionScore, ...]
    contract: str = SUITABILITY_CONTRACT

    def detail(self) -> Tuple[Tuple[str, float], ...]:
        """Per-dimension breakdown plus the two composites, for audit."""
        return tuple((d.dimension, d.value) for d in self.dimensions) + (
            ("design_quality", self.quality),
            ("requirement_fit", self.fit),
            ("suitability", self.suitability),
        )


@dataclass(frozen=True)
class ScoringOutcome:
    """The result of scoring a whole pool."""

    status: str
    scored: Tuple[CandidateScore, ...] = ()
    eligible: Tuple[CandidateScore, ...] = ()
    rejected: Tuple[Tuple[str, str], ...] = ()
    cost: Optional[float] = None
    latency_ms: float = 0.0
    degraded: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SCORED, STATUS_ABSTAINED)

    @property
    def abstained(self) -> bool:
        return self.status == STATUS_ABSTAINED


# ---------------------------------------------------------------------------
# Pure scoring maths
# ---------------------------------------------------------------------------


def normalise_level(raw: Any) -> Optional[float]:
    """Turn a raw rubric score into ``[0, 1]``, or ``None`` when unusable.

    The API returns ``score`` as a continuous value in
    ``[0, len(criteria)-1]``. Anything outside that range, NaN, or
    non-numeric is untrusted and yields ``None`` -- never a clamped guess.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    if value < 0.0 or value > float(MAX_LEVEL_INDEX):
        return None
    return value / float(MAX_LEVEL_INDEX)


def compute_quality(dimension_values: Mapping[str, float]) -> float:
    """Weighted composite of the six dimensions, clamped to ``[0, 1]``."""
    total = 0.0
    for dimension, weight in WEIGHTS.items():
        total += weight * float(dimension_values[dimension])
    return min(max(total, 0.0), 1.0)


def compute_suitability(quality: float, fit: float) -> float:
    """``S = Q**0.45 * F**0.55``, clamped to ``[0, 1]``.

    The geometric mean is deliberate: a strong dimension cannot carry a weak
    one. ``Q=1.0, F=0.25`` gives 0.55, not the 0.63 an arithmetic mean would.
    """
    q = min(max(float(quality), 0.0), 1.0)
    f = min(max(float(fit), 0.0), 1.0)
    return min(max((q ** W_QUALITY) * (f ** W_FIT), 0.0), 1.0)


def failed_dimensions(
    dimension_values: Mapping[str, float],
    floors: Mapping[str, float] = DEFAULT_DIMENSION_FLOORS,
) -> Tuple[str, ...]:
    """Dimensions below their critical floor, in fixed order."""
    return tuple(
        d for d in DIMENSIONS if float(dimension_values[d]) < float(floors[d])
    )


# ---------------------------------------------------------------------------
# Evidence sufficiency
# ---------------------------------------------------------------------------


def evidence_sufficient(candidate: DesignCandidate) -> bool:
    """True when the candidate carries rendered evidence for EVERY dimension.

    A candidate whose evidence class is E0 or E1 for any scored dimension cannot
    be judged on that dimension, so it is not scored at all. Declared metadata
    is never enough.
    """
    if not candidate.has_provenance:
        return False
    if not candidate.assets:
        return False
    threshold = EVIDENCE_RANK[EVIDENCE_SCORABLE_FROM]
    for dimension in DIMENSIONS:
        level = candidate.evidence_for(dimension)
        if EVIDENCE_RANK.get(level, 0) < threshold:
            return False
    return True


def evidence_is_current(candidate: DesignCandidate) -> bool:
    """True when the attached assets were bound to the candidate's revision."""
    if not candidate.assets:
        return False
    if candidate.evidence_revision != candidate.source_revision:
        return False
    for asset in candidate.assets:
        revision = str(getattr(asset, "revision", "") or "").strip()
        if revision != candidate.source_revision:
            return False
        if not str(getattr(asset, "digest", "") or "").strip():
            return False
    return True


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


#: Per-dimension scoring guidance (N8 B7 calibration).
#:
#: Each entry has three parts, and all three are required for the dimension to be
#: separable from its neighbours:
#:
#:   ``only``  what this dimension IS responsible for
#:   ``not``   what belongs to a DIFFERENT dimension and must be ignored here
#:   ``why``   the reasoning that makes the boundary concrete
#:
#: Before this calibration every instruction was a bare "Rate the X ... judge this
#: dimension in isolation" (105-108 chars) with no definition and no exclusion
#: clause. Measured against the V2 controlled pool that produced r(hierarchy,
#: layout) = 0.956 -- effectively one question asked twice. The one dimension that
#: already carried a dimension-specific clause (color) was also the only one that
#: isolated cleanly (D1 -0.025, D2 -0.568, D3 +0.055).
#:
#: Weights, thresholds, floors, the Q/F/S formula and the evidence gates are NOT
#: touched by this text: it changes what the model is asked to look at, never how
#: the answer is scored.
DIMENSION_GUIDANCE: Mapping[str, Mapping[str, str]] = {
    DIMENSION_HIERARCHY: {
        "only": (
            "information priority and attention order: whether there is a single "
            "clear entry point, whether heading levels are visually "
            "distinguishable from body text, whether the eye has an obvious path "
            "through the page, and whether secondary elements recede"
        ),
        "not": "spacing, colour, or contrast",
        "why": "",
    },
    DIMENSION_LAYOUT: {
        "only": (
            "spacing rhythm and spacing consistency: whether the gaps between "
            "sections repeat a recognisable scale, whether margins and padding "
            "are consistent between comparable blocks, whether elements align to "
            "a coherent grid, and whether whitespace looks deliberate"
        ),
        "not": "typography or colour",
        "why": "",
        "secondary": "grid structure and alignment",
    },
    DIMENSION_TYPOGRAPHY: {
        "only": (
            "typeface choice and suitability, size scale, weight variation, "
            "line-height, line length, and legibility of the text itself"
        ),
        "not": "page-level alignment, margins, spacing rhythm, or colour",
        "why": "",
    },
    DIMENSION_COLOR: {
        "only": (
            "foreground/background readability: whether body text, headings, "
            "labels and button text are comfortably legible against the surface "
            "behind them, and whether any text visibly washes out or disappears"
        ),
        "not": "layout, spacing, or type size",
        "why": "",
        # N8 B7 calibration v2: readability is PRIMARY and outweighs palette
        # harmony. The previous wording made palette coherence a co-equal
        # conjunct, which let it average a legibility failure away: the D2
        # contrast fixture kept an identical palette and moved only the ink
        # lightness, so "coherent palette" scored it well while "readable text"
        # scored it badly, and the two cancelled. Sensitivity fell from -0.568
        # to -0.023. Palette harmony stays, but it can never rescue a legibility
        # failure.
        "secondary": (
            "palette harmony, which cannot compensate for illegible text"
        ),
    },
    DIMENSION_USABILITY: {
        "only": (
            "whether interactive elements are visually identifiable as "
            "interactive, whether labels are clear, and whether the next action "
            "is obvious"
        ),
        "not": "aesthetic quality, colour harmony, spacing, or typeface choice",
        "why": "",
    },
    DIMENSION_CONSISTENCY: {
        "only": (
            "whether repeated elements share the same treatment across the page "
            "and between the attached viewports, and whether visible style "
            "inconsistencies exist"
        ),
        "not": "whether the design is attractive, or whether a style choice is good",
        "why": "",
    },
}

#: Dimension-specific honesty limits. These are not scoring hints; they stop the
#: model from claiming a measurement a screenshot cannot support.
DIMENSION_LIMITS: Mapping[str, str] = {
    DIMENSION_COLOR: (
        "A screenshot cannot prove a contrast RATIO: rate only what is visible, "
        "and do not claim a measured ratio."
    ),
    DIMENSION_USABILITY: (
        "A screenshot shows affordances, not behaviour: do not claim "
        "accessibility compliance or interaction success."
    ),
    DIMENSION_CONSISTENCY: (
        "If only one viewport is attached, judge internal consistency only."
    ),
}


def _dimension_instruction(dimension: str) -> str:
    """The per-dimension instruction: primary criterion, exclusions, honesty limit.

    Ordering is deliberate. The primary clause comes first so the model has a
    positive target. The exclusion follows immediately, stated briefly and with
    NO worked example: an exclusion that describes the confounder in detail hands
    the model a concrete thing to notice, which is the opposite of suppressing it.
    Measured on the L1 run, the layout instruction's worked example ("a page with
    flat type can still have perfect spacing") coincided with layout scoring the
    flat-type fixture lowest of all -- the example primed the very confound it was
    meant to exclude.

    The honesty limit comes last so it qualifies the answer rather than the task.
    """
    guidance = DIMENSION_GUIDANCE.get(dimension)
    label = dimension.upper()
    if dimension == DIMENSION_LAYOUT:
        label = "LAYOUT AND SPACING"
    elif dimension == DIMENSION_COLOR:
        label = "COLOR AND CONTRAST"
    elif dimension == DIMENSION_USABILITY:
        label = "USABILITY AND CLARITY"
    elif dimension == DIMENSION_CONSISTENCY:
        label = "CONSISTENCY AND POLISH"
    elif dimension == DIMENSION_HIERARCHY:
        label = "VISUAL HIERARCHY"

    if not guidance:
        return (
            f"Rate the {label} of candidate {{{{cid}}}} from the attached "
            f"screenshot(s) only."
        )

    parts = [
        f"Rate the {label} of candidate {{{{cid}}}} from the attached "
        f"screenshot(s) only.",
        f"Judge PRIMARILY: {guidance['only']}.",
    ]
    secondary = guidance.get("secondary")
    if secondary:
        parts.append(f"Judge SECONDARILY: {secondary}.")
    exclusion = f"Do NOT judge: {guidance['not']}."
    why = guidance.get("why")
    if why:
        exclusion = f"Do NOT judge: {guidance['not']} -- {why}."
    parts.append(exclusion)

    limit = DIMENSION_LIMITS.get(dimension)
    if limit:
        parts.append(limit)
    return "\n".join(parts)


def build_scoring_questions(
    candidates: Sequence[DesignCandidate],
    requirements: Sequence[str] = (),
) -> dict:
    """One score question per dimension plus one for fit, per candidate.

    Keys are ``q<i>_<dimension>`` and ``f<i>`` so an untrusted candidate id never
    enters the question-key namespace. The id appears only inside the sanitised
    instruction text and in the state.

    ``requirements`` is the accepted requirement set from the native PRD. When it
    is not sufficient for a defensible judgement the Fit question is NOT emitted
    at all -- asking it and then discarding the answer would waste a question
    slot and, worse, invite a number that looks like an assessment. The absence
    of ``f<i>`` is what makes ``parse_pool_scores`` abstain with
    ``insufficient_requirements`` rather than invent an F.
    """
    questions: dict = {}
    fit_usable = requirements_are_sufficient(requirements)
    usable = usable_requirements(requirements)

    for index, candidate in enumerate(candidates):
        cid = one_line(candidate.candidate_id, 80)
        for dimension in DIMENSIONS:
            questions[f"q{index}_{dimension}"] = {
                "type": "score",
                "instructions": _dimension_instruction(dimension).replace(
                    "{{cid}}", f"'{cid}'"
                ),
                "criteria": list(LEVELS),
            }
        if not fit_usable:
            continue
        questions[f"f{index}"] = {
            "type": "score",
            "instructions": (
                f"Rate how well candidate '{cid}' SATISFIES the explicit "
                f"requirements listed under 'Requirements' in the brief.\n"
                f"For EACH numbered requirement, decide whether the attached "
                f"screenshot(s) show it satisfied, partially satisfied, or "
                f"absent. Base the score on how many requirements are satisfied "
                f"and how completely.\n"
                f"If a requirement cannot be judged from a screenshot, say so "
                f"and do not count it as satisfied.\n"
                f"Do NOT consider general visual attractiveness, polish, or "
                f"design quality. A beautiful page that omits a required "
                f"element must score LOW here.\n"
                f"There are {len(usable)} requirements; judge every one."
            ),
            "criteria": list(LEVELS),
        }
    return questions


def build_scoring_state(
    *,
    brief: Mapping[str, Any],
    candidates: Sequence[DesignCandidate],
    include_scope_rule: bool = True,
    scope_rule: str = "",
    requirements: Sequence[str] = (),
) -> list:
    """Build the multimodal ``state`` array.

    Text items are plain strings and image items are ``image_url`` parts, per the
    documented Decisions contract. EVERY interpolated value passes through
    ``one_line()`` so untrusted reference text cannot fabricate prompt structure.

    ``requirements`` are rendered as a NUMBERED list under a ``Requirements:``
    heading. Numbering is what makes the Fit question answerable
    requirement-by-requirement; an unnumbered blob of prose is not. Each entry is
    sanitised individually, so a requirement cannot inject extra numbered items
    or a new section.
    """
    parts: list = []

    header: list = ["Owner brief:"]
    for key, label in (("name", "name"), ("what", "what"), ("why", "why")):
        value = one_line(brief.get(key) or "", 400)
        if value:
            header.append(f"  {label}: {value}")
    intent = one_line(brief.get("design_intent") or "", 600)
    if intent:
        header.append(f"  design intent (verbatim): {intent}")
    if include_scope_rule and scope_rule:
        header.append("  " + one_line(scope_rule, 600))
    parts.append("\n".join(header))

    usable = usable_requirements(requirements)
    if usable:
        numbered = [f"Requirements:"]
        for position, text in enumerate(usable, start=1):
            numbered.append(f"  {position}. {text}")
        parts.append("\n".join(numbered))

    for index, candidate in enumerate(candidates):
        parts.append(
            "\n".join(
                [
                    f"Candidate {index}:",
                    f"  id: {one_line(candidate.candidate_id, 80)}",
                    f"  label: {one_line(candidate.label, 160)}",
                    f"  descriptor: {one_line(candidate.descriptor, 600)}",
                    f"  provenance: {one_line(candidate.source_uri, 300)} @ "
                    f"{one_line(candidate.source_revision, 64)}",
                ]
            )
        )
        for asset in candidate.assets:
            parts.append(_image_part(asset))

    return parts


def _image_part(asset: Any) -> dict:
    """Build one ``image_url`` part from an asset carrying base64 bytes."""
    encoded = str(getattr(asset, "base64_data", "") or "")
    mime = str(getattr(asset, "mime", "") or "image/png")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _score_of(answer: Any) -> Optional[Tuple[float, Optional[float]]]:
    """``(normalised value, confidence)`` from one score answer, or ``None``."""
    if not isinstance(answer, Mapping):
        return None
    if str(answer.get("type", "score")) != "score":
        return None
    value = normalise_level(answer.get("score"))
    if value is None:
        return None
    confidence = answer.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return value, confidence


def parse_pool_scores(
    payload: Mapping[str, Any],
    candidates: Sequence[DesignCandidate],
    *,
    expect_fit: bool = True,
) -> Tuple[CandidateScore, ...]:
    """Parse a full-pool response.

    ALL-OR-NOTHING: if any candidate is missing any answer, the whole response is
    rejected. A partially scored pool would let the engine rank a candidate whose
    evidence was never assessed against one that was.

    ``expect_fit=False`` is the intentional-abstain path. When the requirement set
    could not support a Fit judgement, no ``f<i>`` question was asked, so no Fit
    answer exists. Returning a score here would mean inventing F, and returning a
    plain ``incomplete_scores`` would report missing DATA as a provider failure.
    Neither is honest, so this raises ``insufficient_requirements`` -- a distinct
    status the provider maps to intentional abstention rather than degradation.
    """
    if not expect_fit:
        raise ScoringError(
            STATUS_INSUFFICIENT_REQUIREMENTS,
            "no requirement set sufficient for a Fit judgement; abstaining "
            "rather than inventing an F score",
        )

    answers = payload.get("answers") if isinstance(payload, Mapping) else None
    if not isinstance(answers, Mapping):
        raise ScoringError(STATUS_MALFORMED, "no answers object")

    results: list = []
    missing: list = []

    for index, candidate in enumerate(candidates):
        dimension_scores: list = []
        values: dict = {}
        for dimension in DIMENSIONS:
            parsed = _score_of(answers.get(f"q{index}_{dimension}"))
            if parsed is None:
                missing.append(f"q{index}_{dimension}")
                continue
            value, confidence = parsed
            values[dimension] = value
            dimension_scores.append(
                DimensionScore(dimension, value * MAX_LEVEL_INDEX, value, confidence)
            )

        fit_parsed = _score_of(answers.get(f"f{index}"))
        if fit_parsed is None:
            missing.append(f"f{index}")
            continue

        if len(values) != len(DIMENSIONS):
            continue

        quality = compute_quality(values)
        fit = fit_parsed[0]
        results.append(
            CandidateScore(
                candidate_id=candidate.candidate_id,
                quality=quality,
                fit=fit,
                suitability=compute_suitability(quality, fit),
                dimensions=tuple(dimension_scores),
            )
        )

    if missing:
        raise ScoringError(
            STATUS_INCOMPLETE,
            f"{len(missing)} missing answer(s): {', '.join(sorted(missing)[:6])}",
        )
    if len(results) != len(candidates):
        raise ScoringError(
            STATUS_INCOMPLETE,
            f"scored {len(results)} of {len(candidates)} candidates",
        )
    return tuple(results)


# ---------------------------------------------------------------------------
# Gate application
# ---------------------------------------------------------------------------


def apply_gates(
    scores: Sequence[CandidateScore],
    *,
    min_quality: float = DEFAULT_MIN_QUALITY,
    min_fit: float = DEFAULT_MIN_FIT,
    min_suitability: float = DEFAULT_MIN_SUITABILITY,
    floors: Mapping[str, float] = DEFAULT_DIMENSION_FLOORS,
) -> Tuple[Tuple[CandidateScore, ...], Tuple[Tuple[str, str], ...]]:
    """Split scores into eligible and rejected, with a reason per rejection."""
    eligible: list = []
    rejected: list = []
    for score in scores:
        values = {d.dimension: d.value for d in score.dimensions}
        failed = failed_dimensions(values, floors)
        if failed:
            rejected.append((score.candidate_id, f"dimension_floor:{failed[0]}"))
            continue
        if score.quality < min_quality:
            rejected.append((score.candidate_id, "quality_below_min"))
            continue
        if score.fit < min_fit:
            rejected.append((score.candidate_id, "fit_below_min"))
            continue
        if score.suitability < min_suitability:
            rejected.append((score.candidate_id, "suitability_below_min"))
            continue
        eligible.append(score)
    eligible.sort(key=lambda s: (-s.suitability, s.candidate_id))
    return tuple(eligible), tuple(rejected)


__all__ = [
    "SUITABILITY_CONTRACT",
    "WEIGHTS",
    "DIMENSIONS",
    "LEVELS",
    "MAX_LEVEL_INDEX",
    "W_QUALITY",
    "W_FIT",
    "DEFAULT_MIN_QUALITY",
    "DEFAULT_MIN_FIT",
    "DEFAULT_MIN_SUITABILITY",
    "DEFAULT_DIMENSION_FLOORS",
    "STATUS_SCORED",
    "STATUS_ABSTAINED",
    "STATUS_INSUFFICIENT_EVIDENCE",
    "STATUS_INSUFFICIENT_BRIEF",
    "STATUS_MALFORMED",
    "STATUS_INCOMPLETE",
    "STATUS_STALE_EVIDENCE",
    "STATUS_TOO_MANY",
    "ScoringError",
    "DimensionScore",
    "CandidateScore",
    "ScoringOutcome",
    "normalise_level",
    "compute_quality",
    "compute_suitability",
    "failed_dimensions",
    "evidence_sufficient",
    "evidence_is_current",
    "build_scoring_questions",
    "build_scoring_state",
    "parse_pool_scores",
    "apply_gates",
]
