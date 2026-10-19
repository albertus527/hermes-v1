"""Decision Design: selects among REAL candidate ids supplied by the evidence pipeline.

OWNER CORRECTION B
------------------
"GPT-6 Luna Decisions must select or abstain among actual candidate IDs supplied
by the existing evidence pipeline. Never generate fictitious source_uri,
source_revision, or provenance."

This provider therefore does exactly one thing: it is handed a set of candidate
ids that the retrieval layer already produced, asks the model which id is best
grounded, and maps the answer back onto the *original* candidate object. It
never constructs a candidate, never invents provenance, and never fabricates an
id.

It implements the existing :class:`app.core.decision_provider.DecisionProvider`
protocol so the existing ``DecisionEngine`` / ``DecisionProviderRegistry``
machinery consumes it unchanged. ``classify_intake`` raises ``NOT_IMPLEMENTED``
because intake is a different role -- exactly the shape
``OpenVikingDecisionProvider`` already uses.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

from app.core.decision_provider import (
    DecisionProviderError,
    DesignCandidate,
)
from app.core.decisions_client import (
    extract_choice,
    post_decision,
    resolved_model_id,
)

logger = logging.getLogger(__name__)

#: Provider name registered with the DecisionProviderRegistry.
DECISIONS_DESIGN_PROVIDER_NAME = "decisions-design"

#: Question key used in the request/response envelope.
QUESTION_NAME = "choice"

#: Option offered when nothing is adequately grounded. The model must be able to
#: decline; forcing a pick is what produced the Stage 2B abstention failures.
ABSTAIN_OPTION = "ABSTAIN"
ABSTAIN_CRITERION = (
    "No candidate is adequately grounded by the brief and provenance."
)

#: Scope rule injected verbatim into the prompt. This is the D11 fix: the
#: website-only constraint must be VISIBLE to the model whenever it determines
#: the correct answer, never implicit.
SCOPE_RULE = (
    "Scope rule: this pipeline builds WEBSITE surfaces only. It cannot produce "
    "or validate logos, mascots, print collateral, or brand identity systems. "
    "A candidate that requires non-website output must not be chosen."
)


def build_design_state(
    *,
    brief: Mapping[str, Any],
    candidates: Sequence[DesignCandidate],
    include_scope_rule: bool = True,
    limit: int = 1400,
) -> str:
    """Render the ``state`` for a Design decision.

    Every candidate line carries the provenance fields the engine already
    holds, so the model chooses among real, cited options only.
    """
    lines = []
    brief_what = str(brief.get("what") or "").strip()
    brief_why = str(brief.get("why") or "").strip()
    brief_name = str(brief.get("name") or "").strip()
    if brief_name or brief_what or brief_why:
        lines.append(f"Brief: {brief_name} {brief_what} {brief_why}".strip())
    intent = str(brief.get("design_intent") or "").strip()
    if intent:
        lines.append(f"Owner design intent (verbatim): {intent}")
    if include_scope_rule:
        lines.append(SCOPE_RULE)
    lines.append("")
    lines.append("Candidate references:")
    for candidate in candidates:
        lines.append(
            f"- {candidate.candidate_id}: {candidate.label} | "
            f"provenance: {candidate.source_uri} @ {candidate.source_revision} | "
            f"descriptor: {candidate.descriptor}"
        )
    lines.append("")
    lines.append(
        "Choose the single best-grounded candidate id, or ABSTAIN if none is "
        "adequately grounded by the brief and provenance."
    )
    return "\n".join(lines)[:limit]


def build_design_questions(candidates: Sequence[DesignCandidate]) -> dict:
    """Build the ``questions`` payload from the REAL candidate set.

    Only ids that actually exist are offered, plus ``ABSTAIN``. The model cannot
    name anything else because it is not an option.
    """
    criteria: dict = {}
    for candidate in candidates:
        criteria[candidate.candidate_id] = (
            f"{candidate.label}. Provenance: {candidate.source_uri} @ "
            f"{candidate.source_revision}. Descriptor: {candidate.descriptor}"
        )
    criteria[ABSTAIN_OPTION] = ABSTAIN_CRITERION
    return {
        QUESTION_NAME: {
            "type": "choice",
            "instructions": (
                "Which candidate is best grounded in the brief, subject to the "
                "stated scope rule? Answer ABSTAIN if none is adequately grounded."
            ),
            "criteria": criteria,
        }
    }


class DecisionsDesignProvider:
    """Proposes a grounded design selection using the Decisions API.

    Implements the existing ``DecisionProvider`` protocol's design half. The
    intake half raises ``NOT_IMPLEMENTED`` so the engine falls back
    deterministically for intake, exactly as ``OpenVikingDecisionProvider`` does.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        deadline_seconds: float = 20.0,
        include_scope_rule: bool = True,
    ) -> None:
        self.model = model
        #: Name-mangled and excluded from repr(), so logging the provider can
        #: never leak the credential.
        self.__api_key = api_key
        self.base_url = base_url
        self.deadline_seconds = deadline_seconds
        self.include_scope_rule = include_scope_rule
        #: Set by the last call so telemetry can distinguish abstain vs failure.
        self.last_error_code = ""
        self.last_degraded = False

    @property
    def _credential(self) -> str:
        return self.__api_key

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, "
            f"base_url={self.base_url!r})"
        )

    @property
    def name(self) -> str:
        return DECISIONS_DESIGN_PROVIDER_NAME

    @property
    def enabled(self) -> bool:
        return bool(self.model) and bool(self._credential)

    # -- intake half of the protocol: deliberately unimplemented -------------

    def classify_intake(self, request_text: str, context: Mapping[str, Any]) -> Any:
        """Not this provider's job -- a design selector has no intake authority."""
        raise DecisionProviderError(
            "NOT_IMPLEMENTED", "decisions-design provides design selection only"
        )

    # -- design half --------------------------------------------------------

    def propose_design_candidates(
        self,
        context: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[DesignCandidate]:
        """Ask the model to pick among the supplied candidates.

        ``context`` must carry ``candidates``: the real, already-provenanced
        options produced by the evidence pipeline. If it does not, this provider
        returns ``()`` -- it will never invent one.
        """
        self.last_error_code = ""
        self.last_degraded = False

        if not self.enabled or limit <= 0:
            return ()

        supplied = context.get("candidates") if isinstance(context, Mapping) else None
        if not supplied:
            # No real candidates => nothing to choose among. Abstain honestly.
            self.last_error_code = "no_candidates"
            return ()

        real: list = []
        for item in supplied:
            if isinstance(item, DesignCandidate):
                real.append(item)
            elif isinstance(item, Mapping):
                candidate = _coerce_candidate(item)
                if candidate is not None:
                    real.append(candidate)
        # Only candidates carrying provenance may reach the model at all.
        real = [c for c in real if c.has_provenance][:limit]
        if not real:
            self.last_error_code = "no_provenanced_candidates"
            return ()

        brief = context.get("brief") if isinstance(context, Mapping) else None
        brief = brief if isinstance(brief, Mapping) else {}

        # N8 B5: the requirement revision this fit judgement is tied to. Every
        # candidate in the pool carries the same value, which is what makes their
        # suitability scores comparable in the engine.
        requirement_version = ""
        if isinstance(context, Mapping):
            requirement_version = str(
                context.get("requirement_version") or ""
            ).strip()

        state = build_design_state(
            brief=brief,
            candidates=real,
            include_scope_rule=self.include_scope_rule,
        )
        questions = build_design_questions(real)

        result = post_decision(
            model=self.model,
            state=state,
            questions=questions,
            api_key=self._credential,
            base_url=self.base_url,
            deadline_seconds=self.deadline_seconds,
        )

        if not result.ok:
            self.last_error_code = result.error_code or "transport"
            self.last_degraded = True
            logger.info(
                "decisions design call failed",
                extra={
                    "n8_role": "design",
                    "n8_model": self.model,
                    "n8_error_class": result.error_code,
                    "n8_latency_ms": result.elapsed_ms,
                },
            )
            return ()

        payload = result.payload or {}
        choice, confidence, _cost = extract_choice(payload, QUESTION_NAME)

        if choice is None:
            self.last_error_code = "malformed"
            self.last_degraded = True
            return ()

        if choice == ABSTAIN_OPTION:
            # Intentional abstention. NOT a failure: the model declined.
            self.last_error_code = "abstained"
            return ()

        # Map the model's id back onto the ORIGINAL candidate object. Provenance
        # therefore comes from the evidence pipeline, never from the model.
        by_id = {c.candidate_id: c for c in real}
        selected = by_id.get(choice)
        if selected is None:
            # The model named an id that was not offered. Reject outright.
            self.last_error_code = "unknown_candidate"
            self.last_degraded = True
            logger.info(
                "decisions design returned an unoffered candidate id",
                extra={"n8_role": "design", "n8_model": self.model,
                       "n8_error_class": "unknown_candidate"},
            )
            return ()

        # N8 B6: the artificial score promotion is GONE.
        #
        # The old code rewrote the chosen candidate's score to 1.0 so the
        # engine's ``(-score, candidate_id)`` sort would pick it. That destroyed
        # the evidence pipeline's own ranking signal and encoded "the model named
        # this" as if it were a quality score.
        #
        # The model's preference is now expressed the only honest way available:
        # as an ORDER, with every candidate's retrieval score preserved exactly
        # as retrieved. Ranking by suitability is B7's job; until a suitability
        # score exists, the engine's own score-floor and provenance gates decide.
        ranked = [
            DesignCandidate(
                candidate_id=candidate.candidate_id,
                label=candidate.label,
                descriptor=candidate.descriptor,
                palette=candidate.palette,
                source_uri=candidate.source_uri,
                source_revision=candidate.source_revision,
                # Verbatim: never rewritten.
                score=candidate.score,
                uncertainty=candidate.uncertainty,
                suitability=candidate.suitability,
                suitability_detail=candidate.suitability_detail,
                suitability_contract=candidate.suitability_contract,
                evidence=candidate.evidence,
                evidence_revision=candidate.evidence_revision,
                assets=candidate.assets,
                requirement_version=requirement_version or candidate.requirement_version,
            )
            for candidate in real
        ]

        # Put the model's pick first so a stable sort preserves the preference
        # when scores tie. This changes ORDER only, never a score.
        ranked.sort(key=lambda c: c.candidate_id != selected.candidate_id)

        logger.info(
            "decisions design selection",
            extra={
                "n8_role": "design",
                "n8_model": self.model,
                "n8_resolved_model": resolved_model_id(payload),
                "n8_confidence": confidence,
                "n8_latency_ms": result.elapsed_ms,
            },
        )
        return tuple(ranked)


def _coerce_candidate(item: Mapping[str, Any]) -> Optional[DesignCandidate]:
    """Best-effort coercion of a mapping into a DesignCandidate.

    Returns ``None`` when required fields are missing, so a malformed input can
    never become a fabricated candidate.
    """
    try:
        candidate_id = str(item.get("candidate_id") or "").strip()
        label = str(item.get("label") or "").strip()
        descriptor = str(item.get("descriptor") or "").strip()
        if not candidate_id or not label:
            return None
        palette = item.get("palette") or ()
        if isinstance(palette, str):
            palette = (palette,)
        return DesignCandidate(
            candidate_id=candidate_id,
            label=label,
            descriptor=descriptor,
            palette=tuple(str(p) for p in palette),
            source_uri=str(item.get("source_uri") or "").strip(),
            source_revision=str(item.get("source_revision") or "").strip(),
            score=float(item.get("score") or 0.0),
            uncertainty=str(item.get("uncertainty") or ""),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "DECISIONS_DESIGN_PROVIDER_NAME",
    "QUESTION_NAME",
    "ABSTAIN_OPTION",
    "SCOPE_RULE",
    "DecisionsDesignProvider",
    "build_design_state",
    "build_design_questions",
]
