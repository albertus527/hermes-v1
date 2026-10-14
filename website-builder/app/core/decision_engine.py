"""Decision Engine for Website Builder R2 (N3).

The engine wraps a replaceable :class:`~app.core.decision_provider.DecisionProvider`
and enforces the N3 invariants *structurally*, so a misbehaving or hostile
provider cannot violate them:

* **Provider outage never breaks the safe FAST fallback.** Every provider call is
  wrapped. Any raised error — or an invalid response — becomes a labelled
  unavailability and the engine retries once with the deterministic provider.
  The engine therefore always returns a decision.
* **Invalid or untrusted design candidates never override requirements.**
  Candidates without complete provenance are dropped. Scores are validated
  (finite, in range); an unvalidatable score is treated as zero and the candidate
  is marked low-confidence rather than trusted.
* **No forced selection when no suitable design exists.** When no candidate
  survives validation and the score floor, the engine **abstains** — it returns
  no selection, never a fabricated one.
* **Bounded retrieval.** The candidate limit is a hard cap enforced by the
  engine, independent of what a provider returns.

The engine never mutates project state and never touches the publication path.
It is deterministic given a deterministic provider, and every field it emits is
explicit so a caller never has to guess whether "empty" meant "none" or "failed".
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

from app.core.decision_provider import (
    DETERMINISTIC_PROVIDER_NAME,
    INTAKE_UNKNOWN,
    PROVIDER_INVALID,
    PROVIDER_OK,
    PROVIDER_UNAVAILABLE,
    DecisionProvider,
    DecisionProviderError,
    DecisionProviderRegistry,
    DesignCandidate,
    IntakeClassification,
)

logger = logging.getLogger(__name__)

#: Hard cap on candidates the engine will ever consider, regardless of what a
#: provider returns or a caller requests.
MAX_CANDIDATES = 8

#: A candidate scoring below this is not suitable and must not be selected.
DEFAULT_SCORE_FLOOR = 0.5

#: Reasons a decision abstained. A closed set.
ABSTAIN_NO_CANDIDATES = "no_candidates"
ABSTAIN_BELOW_FLOOR = "below_score_floor"
ABSTAIN_NO_PROVENANCE = "no_provenance"

#: Sources the engine records for how a decision was reached.
SOURCE_PROVIDER = "provider"
SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True)
class CandidateValidation:
    """Per-candidate validation outcome, retained for observability."""

    candidate_id: str
    accepted: bool
    reason: str


@dataclass(frozen=True)
class IntakeDecision:
    """The engine's intake decision. Always present (never abstains)."""

    classification: IntakeClassification
    source: str
    provider: str
    degraded: bool = False
    error_code: str = ""

    @property
    def kind(self) -> str:
        return self.classification.kind

    @property
    def has_reference(self) -> bool:
        return self.classification.has_reference


@dataclass(frozen=True)
class DesignDecision:
    """The engine's design decision. May abstain (``selected`` is ``None``)."""

    selected: Optional[DesignCandidate]
    ranked: Tuple[DesignCandidate, ...]
    abstained: bool
    abstain_reason: str
    source: str
    provider: str
    dropped: Tuple[CandidateValidation, ...] = ()
    degraded: bool = False
    error_code: str = ""

    def to_dict(self) -> dict:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "ranked": [c.to_dict() for c in self.ranked],
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
            "source": self.source,
            "provider": self.provider,
            "dropped": [
                {"candidate_id": d.candidate_id, "reason": d.reason}
                for d in self.dropped
            ],
            "degraded": self.degraded,
            "error_code": self.error_code,
        }


def _valid_score(value: Any) -> Optional[float]:
    """Return a finite float in ``[0, 1]``, or ``None`` when unvalidatable."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    if score < 0.0 or score > 1.0:
        return None
    return score


def _normalize_candidate(raw: Any) -> Optional[DesignCandidate]:
    """Coerce a provider's raw output into a validated candidate, or ``None``.

    A provider may return a mapping or a :class:`DesignCandidate`. Anything that
    is neither — or that lacks the required string fields — is rejected. This is
    the boundary where an untrusted provider object stops being trusted.
    """
    if isinstance(raw, DesignCandidate):
        candidate = raw
    elif isinstance(raw, Mapping):
        try:
            candidate = DesignCandidate(
                candidate_id=str(raw.get("candidate_id", "")).strip(),
                label=str(raw.get("label", "")).strip(),
                descriptor=str(raw.get("descriptor", "")).strip(),
                palette=tuple(str(p) for p in (raw.get("palette") or ())),
                source_uri=str(raw.get("source_uri", "")).strip(),
                source_revision=str(raw.get("source_revision", "")).strip(),
                score=raw.get("score", 0.0),
                uncertainty=str(raw.get("uncertainty", "")),
            )
        except (DecisionProviderError, TypeError, ValueError):
            return None
    else:
        return None

    if not candidate.candidate_id:
        return None
    score = _valid_score(candidate.score)
    if score is None:
        # An unvalidatable score is not trusted: pin it to zero so the candidate
        # can never pass the floor on a bogus value.
        candidate = DesignCandidate(
            candidate_id=candidate.candidate_id,
            label=candidate.label,
            descriptor=candidate.descriptor,
            palette=candidate.palette,
            source_uri=candidate.source_uri,
            source_revision=candidate.source_revision,
            score=0.0,
            uncertainty=candidate.uncertainty or "score_unvalidatable",
        )
    return candidate


class DecisionEngine:
    """Enforces the N3 invariants around a replaceable provider."""

    def __init__(
        self,
        registry: Optional[DecisionProviderRegistry] = None,
        *,
        provider_name: Optional[str] = None,
        score_floor: float = DEFAULT_SCORE_FLOOR,
        max_candidates: int = MAX_CANDIDATES,
    ) -> None:
        self.registry = registry or DecisionProviderRegistry()
        self.provider_name = provider_name
        self.score_floor = score_floor
        self.max_candidates = max(0, min(int(max_candidates), MAX_CANDIDATES))

    @property
    def provider(self) -> DecisionProvider:
        return self.registry.resolve(self.provider_name)

    # -- intake -----------------------------------------------------------

    def decide_intake(
        self, request_text: str, context: Optional[Mapping[str, Any]] = None
    ) -> IntakeDecision:
        """Classify intake, falling back safely on any provider failure."""
        ctx = context or {}
        provider = self.provider
        if bool(self.provider_name) and provider.name != self.provider_name:
            # The named provider is not available (disabled/unregistered), so the
            # registry resolved the safe fallback. Report that honestly as a
            # degraded fallback rather than as a clean provider result.
            return self._fallback_intake(
                request_text,
                ctx,
                DecisionProviderError(
                    PROVIDER_UNAVAILABLE, "requested provider unavailable"
                ),
            )
        try:
            classification = provider.classify_intake(request_text, ctx)
            if not isinstance(classification, IntakeClassification):
                raise DecisionProviderError(
                    PROVIDER_INVALID, "provider returned a non-classification"
                )
            return IntakeDecision(
                classification=classification,
                source=SOURCE_PROVIDER,
                provider=provider.name,
            )
        except Exception as exc:  # noqa: BLE001 - a provider must never break intake
            logger.warning(
                "Decision intake provider %r failed (%s); using deterministic fallback",
                provider.name,
                type(exc).__name__,
            )
            return self._fallback_intake(request_text, ctx, exc)

    def _fallback_intake(
        self, request_text: str, context: Mapping[str, Any], exc: Exception
    ) -> IntakeDecision:
        fallback = self.registry.fallback
        classification = fallback.classify_intake(request_text, context)
        return IntakeDecision(
            classification=classification,
            source=SOURCE_FALLBACK,
            provider=fallback.name,
            degraded=True,
            error_code=getattr(exc, "code", type(exc).__name__),
        )

    # -- design -----------------------------------------------------------

    def decide_design(
        self,
        context: Optional[Mapping[str, Any]] = None,
        *,
        limit: Optional[int] = None,
    ) -> DesignDecision:
        """Retrieve and rank design candidates, or abstain.

        Never raises, never forces a selection, and never trusts an unprovenanced
        candidate. On any provider failure it retries once with the deterministic
        provider.
        """
        ctx = context or {}
        cap = self.max_candidates if limit is None else max(0, min(int(limit), self.max_candidates))
        if cap == 0:
            return DesignDecision(
                selected=None,
                ranked=(),
                abstained=True,
                abstain_reason=ABSTAIN_NO_CANDIDATES,
                source=SOURCE_PROVIDER,
                provider=self.provider.name,
            )

        provider = self.provider
        raw_candidates, source, provider_name, degraded, error_code = self._gather(
            provider, ctx, cap
        )
        return self._rank(
            raw_candidates,
            source=source,
            provider_name=provider_name,
            degraded=degraded,
            error_code=error_code,
        )

    def _gather(
        self, provider: DecisionProvider, ctx: Mapping[str, Any], cap: int
    ) -> Tuple[Sequence[Any], str, str, bool, str]:
        # If a provider was explicitly configured but the registry could not
        # supply it (disabled, unregistered, or absent), it resolved the safe
        # fallback. That is a DEGRADED outcome, and must be reported as such —
        # never as a clean provider result — so an unavailable OpenViking can
        # never look like a successful selection.
        if bool(self.provider_name) and provider.name != self.provider_name:
            logger.warning(
                "Decision design provider %r unavailable; using deterministic fallback",
                self.provider_name,
            )
            fallback = self.registry.fallback
            raw = fallback.propose_design_candidates(ctx, limit=cap)
            return (raw, SOURCE_FALLBACK, fallback.name, True, PROVIDER_UNAVAILABLE)
        try:
            raw = provider.propose_design_candidates(ctx, limit=cap)
            return raw, SOURCE_PROVIDER, provider.name, False, ""
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Decision design provider %r failed (%s); using deterministic fallback",
                provider.name,
                type(exc).__name__,
            )
            fallback = self.registry.fallback
            raw = fallback.propose_design_candidates(ctx, limit=cap)
            return (
                raw,
                SOURCE_FALLBACK,
                fallback.name,
                True,
                getattr(exc, "code", type(exc).__name__),
            )

    def _rank(
        self,
        raw_candidates: Sequence[Any],
        *,
        source: str,
        provider_name: str,
        degraded: bool,
        error_code: str,
    ) -> DesignDecision:
        dropped: list = []
        accepted: list = []
        seen_ids = set()

        # Deterministic iteration: sort by a stable key derived from the raw
        # order index is unnecessary because we preserve input order and dedupe.
        for index, raw in enumerate(raw_candidates):
            if len(accepted) >= self.max_candidates:
                break
            candidate = _normalize_candidate(raw)
            if candidate is None:
                dropped.append(CandidateValidation(f"<item-{index}>", False, PROVIDER_INVALID))
                continue
            if candidate.candidate_id in seen_ids:
                dropped.append(
                    CandidateValidation(candidate.candidate_id, False, "duplicate")
                )
                continue
            seen_ids.add(candidate.candidate_id)
            if not candidate.has_provenance:
                dropped.append(
                    CandidateValidation(candidate.candidate_id, False, ABSTAIN_NO_PROVENANCE)
                )
                continue
            score = _valid_score(candidate.score)
            if score is None or score < self.score_floor:
                dropped.append(
                    CandidateValidation(candidate.candidate_id, False, ABSTAIN_BELOW_FLOOR)
                )
                continue
            accepted.append(candidate)

        if not accepted:
            reason = (
                ABSTAIN_NO_PROVENANCE
                if dropped and all(d.reason == ABSTAIN_NO_PROVENANCE for d in dropped)
                else (ABSTAIN_BELOW_FLOOR if dropped else ABSTAIN_NO_CANDIDATES)
            )
            return DesignDecision(
                selected=None,
                ranked=(),
                abstained=True,
                abstain_reason=reason,
                source=source,
                provider=provider_name,
                dropped=tuple(dropped),
                degraded=degraded,
                error_code=error_code,
            )

        # Rank deterministically: score descending, then candidate_id ascending,
        # so equal scores never depend on provider iteration order.
        ranked = tuple(sorted(accepted, key=lambda c: (-c.score, c.candidate_id)))
        return DesignDecision(
            selected=ranked[0],
            ranked=ranked,
            abstained=False,
            abstain_reason="",
            source=source,
            provider=provider_name,
            dropped=tuple(dropped),
            degraded=degraded,
            error_code=error_code,
        )


__all__ = [
    "MAX_CANDIDATES",
    "DEFAULT_SCORE_FLOOR",
    "ABSTAIN_NO_CANDIDATES",
    "ABSTAIN_BELOW_FLOOR",
    "ABSTAIN_NO_PROVENANCE",
    "SOURCE_PROVIDER",
    "SOURCE_FALLBACK",
    "CandidateValidation",
    "IntakeDecision",
    "DesignDecision",
    "DecisionEngine",
]
