"""Decision Intake: six-intent routing type + provider.

WHY A SEPARATE TYPE (owner decision #1)
---------------------------------------
``IntakeClassification`` is deliberately three-valued (``new``/``revision``/
``unknown``) and is consumed by ``app/core/intake.py`` for new-vs-revision
routing. The benchmark measured SIX routing intents. Rather than widen a
frozen, load-bearing type, this module defines a separate routing type that
speaks the existing :class:`ConversationIntent` enum directly.

Verified constraint check: ``_classify_intent`` (the routing seam) has no
dependency on ``DecisionEngine`` or ``IntakeClassification``, so a separate type
at that seam needs no protocol change. The only place that hard-requires
``IntakeClassification`` is ``DecisionEngine.decide_intake`` via ``isinstance``,
and this module does not route through it.

DESIGN INVARIANTS
-----------------
* The provider can only return a label that is a member of the *valid intents
  offered for the current lifecycle*. A model answering ``PUBLISH`` at
  ``RUNNING`` is rejected by construction, not by prompt wording.
* Any transport, parsing, or validation failure returns ``None``. The caller
  then uses FAST. There is no path where a failure becomes an acting label.
* No retries, no substitutions, no confidence-based promotion.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.core.decisions_client import (
    DecisionsResult,
    extract_choice,
    post_decision,
    resolved_model_id,
)

logger = logging.getLogger(__name__)

#: The six routing intents, as wire strings. Kept in lock-step with
#: ``app.runtime.ConversationIntent`` by an assertion in the test suite.
INTAKE = "INTAKE"
REVISE = "REVISE"
APPROVE = "APPROVE"
PUBLISH = "PUBLISH"
CLARIFY = "CLARIFY"
OUT_OF_SCOPE = "OUT_OF_SCOPE"

ROUTING_INTENTS: tuple = (INTAKE, REVISE, APPROVE, PUBLISH, CLARIFY, OUT_OF_SCOPE)

#: Human-readable criteria sent to the model. Wording is part of the contract:
#: the benchmark scored against exactly these definitions.
CRITERIA: Mapping[str, str] = {
    INTAKE: "Request to create a NEW website project.",
    REVISE: "Change request to an EXISTING project.",
    APPROVE: "Owner accepts the current preview shown to them.",
    PUBLISH: "Owner asks to publish live.",
    CLARIFY: "Intent genuinely unclear; ask the owner.",
    OUT_OF_SCOPE: "Not a website build or revision request.",
}

#: Question key used in the request/response envelope.
QUESTION_NAME = "intent"


@dataclass(frozen=True)
class IntakeRouting:
    """One six-intent classification proposal.

    Deliberately *not* ``IntakeClassification``: that type is three-valued and
    feeds a different consumer. This one speaks the routing vocabulary.

    ``label`` is always a member of :data:`ROUTING_INTENTS`. ``confidence`` is
    recorded for observability only and must never gate authorization -- the
    selected Intake model saturates at 1.0 on roughly half its responses, so a
    confidence threshold would be meaningless.
    """

    label: str
    confidence: Optional[float] = None
    resolved_model: str = ""
    provider: str = ""
    latency_ms: int = 0
    cost_usd: Optional[float] = None

    def __post_init__(self) -> None:
        if self.label not in ROUTING_INTENTS:
            raise ValueError(f"label={self.label!r} not a routing intent")


def build_state(lifecycle: str, context: str, message: str, *, limit: int = 1400) -> str:
    """Render the bounded ``state`` string sent to the model.

    Mirrors the shape used in the confirmation benchmark so production prompts
    stay comparable to the measured ones.
    """
    text = (
        f"Project lifecycle: {lifecycle}\n"
        f"Context: {context}\n"
        f"Owner message: {message}"
    )
    return text[:limit]


def build_questions(valid_intents: Sequence[str]) -> dict:
    """Build the ``questions`` payload restricted to *valid_intents*.

    Restriction is the whole point: the option set itself encodes lifecycle
    legality, so the model is never offered an intent the lifecycle forbids.
    """
    allowed = [i for i in valid_intents if i in CRITERIA]
    if not allowed:
        allowed = [INTAKE]
    return {
        QUESTION_NAME: {
            "type": "choice",
            "instructions": (
                "Classify the owner's message into exactly one routing intent."
            ),
            "criteria": {name: CRITERIA[name] for name in allowed},
        }
    }


class DecisionsIntakeProvider:
    """Classifies a turn into one of six routing intents via the Decisions API.

    The provider is intentionally thin: it builds a request, sends it once, and
    validates the answer against the offered option set. It never retries, never
    substitutes a model, and never invents a label.
    """

    #: Stable provider name used in telemetry and configuration.
    name = "decisions-intake"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        deadline_seconds: float = 20.0,
        context_builder=None,
    ) -> None:
        self.model = model
        #: Held in a name-mangled attribute and never included in repr()/dict
        #: output, so an accidental log of the provider cannot leak the key.
        self.__api_key = api_key
        self.base_url = base_url
        self.deadline_seconds = deadline_seconds
        self._context_builder = context_builder

    @property
    def _credential(self) -> str:
        """The API key, readable only through an explicit accessor."""
        return self.__api_key

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, "
            f"base_url={self.base_url!r}, deadline_seconds={self.deadline_seconds!r})"
        )

    @property
    def enabled(self) -> bool:
        return bool(self.model) and bool(self._credential)

    def classify(
        self,
        *,
        text: str,
        lifecycle: str,
        valid_intents: Iterable[str],
        context: str = "",
    ) -> Optional[IntakeRouting]:
        """Return a validated routing proposal, or ``None``.

        ``None`` means "use FAST". It is returned for every failure mode:
        transport error, timeout, malformed body, a label outside the offered
        set, or a label outside the routing vocabulary.
        """
        if not self.enabled:
            return None

        allowed = [str(i) for i in valid_intents]
        if not allowed:
            return None

        state = build_state(lifecycle, context, text)
        questions = build_questions(allowed)

        result: DecisionsResult = post_decision(
            model=self.model,
            state=state,
            questions=questions,
            api_key=self._credential,
            base_url=self.base_url,
            deadline_seconds=self.deadline_seconds,
        )

        if not result.ok:
            logger.info(
                "decisions intake call failed",
                extra={
                    "n8_role": "intake",
                    "n8_model": self.model,
                    "n8_error_class": result.error_code,
                    "n8_latency_ms": result.elapsed_ms,
                },
            )
            return None

        payload = result.payload or {}
        choice, confidence, cost = extract_choice(payload, QUESTION_NAME)

        # Validation ladder. Each failure falls through to FAST.
        if choice is None:
            logger.info(
                "decisions intake returned no choice",
                extra={"n8_role": "intake", "n8_model": self.model,
                       "n8_error_class": "malformed"},
            )
            return None
        if choice not in ROUTING_INTENTS:
            logger.info(
                "decisions intake returned an unknown label",
                extra={"n8_role": "intake", "n8_model": self.model,
                       "n8_error_class": "invalid_label"},
            )
            return None
        if choice not in allowed:
            # Correct rejection: the model named an intent the lifecycle forbids.
            # Not a transport failure -- fall back to FAST for this turn only.
            logger.info(
                "decisions intake returned a lifecycle-illegal label",
                extra={"n8_role": "intake", "n8_model": self.model,
                       "n8_error_class": "invalid_label"},
            )
            return None

        return IntakeRouting(
            label=choice,
            confidence=confidence,
            resolved_model=resolved_model_id(payload),
            provider=str(payload.get("provider") or ""),
            latency_ms=result.elapsed_ms,
            cost_usd=cost,
        )


__all__ = [
    "INTAKE",
    "REVISE",
    "APPROVE",
    "PUBLISH",
    "CLARIFY",
    "OUT_OF_SCOPE",
    "ROUTING_INTENTS",
    "CRITERIA",
    "QUESTION_NAME",
    "IntakeRouting",
    "DecisionsIntakeProvider",
    "build_state",
    "build_questions",
]
