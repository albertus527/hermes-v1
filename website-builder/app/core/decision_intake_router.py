"""Bridges the Decisions intake provider to the routing seam.

WHY THIS EXISTS
---------------
``_classify_intent`` must stay thin. This module owns three concerns that would
otherwise clutter the runtime:

1. **Six-intent -> ``ConversationIntent`` mapping.** The provider speaks wire
   strings; the dispatcher speaks the enum. The bridge is the only place the two
   meet.
2. **Conversation-scoped degradation.** Three consecutive transport failures
   degrade Decision Intake for the rest of that conversation.
3. **Mode handling.** ``disabled`` / ``shadow`` / ``active`` are resolved here so
   the runtime branch stays a single ``if``.

OWNER CORRECTION A -- degradation recovery
------------------------------------------
"Three consecutive transport failures degrade Decision Intake for the rest of
that conversation. Use FAST fallback thereafter. Reset on a new conversation or
process restart. Do not claim a successful Decision request can reset degraded
state if Decision requests are disabled."

Therefore: once degraded, this bridge **stops calling the provider**. It does not
probe, and it does not observe a "successful request" -- because it makes no
request. The only reset paths are a new conversation or a process restart. This
is deliberate: an automatic recovery probe would be a hidden retry, which the
owner forbade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: Modes, mirroring the existing DecisionEngineMode vocabulary.
MODE_DISABLED = "disabled"
MODE_SHADOW = "shadow"
MODE_ACTIVE = "active"

#: Consecutive transport failures before a conversation is degraded.
DEGRADE_AFTER = 3

#: Error classes that count as TRANSPORT failures.
#:
#: A lifecycle-illegal label is deliberately NOT here: the model answered
#: correctly and the answer was legally unavailable. That is a routing
#: rejection, not a transport fault, so it must not degrade the conversation.
TRANSPORT_ERROR_CLASSES = frozenset(
    {
        "deadline_exceeded",
        "transport",
        "http_429",
        "http_529",
        "http_5xx",
        "http_4xx",
        "no_status",
        "unparseable_body",
        "non_object_body",
        "missing_credential",
        "invalid_model",
    }
)


@dataclass
class _ConversationState:
    """Per-conversation degradation bookkeeping."""

    consecutive_failures: int = 0
    degraded: bool = False


@dataclass
class IntakeRouter:
    """Resolves a turn to a routing intent, or ``None`` to defer to FAST.

    ``enabled`` reflects configuration. When the router is disabled the runtime
    branch never runs, so behaviour is byte-identical to the pre-N8 path.
    """

    provider: object
    mode: str = MODE_DISABLED
    _states: dict = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return (
            self.mode in (MODE_SHADOW, MODE_ACTIVE)
            and self.provider is not None
            and bool(getattr(self.provider, "enabled", False))
        )

    @property
    def active(self) -> bool:
        """True only when the answer may influence routing."""
        return self.mode == MODE_ACTIVE

    # -- degradation -------------------------------------------------------

    def _state_for(self, conversation_key: str) -> _ConversationState:
        state = self._states.get(conversation_key)
        if state is None:
            state = _ConversationState()
            self._states[conversation_key] = state
        return state

    def is_degraded(self, conversation_key: str) -> bool:
        return self._state_for(conversation_key).degraded

    def reset_conversation(self, conversation_key: str) -> None:
        """Drop per-conversation state. Called when a conversation ends."""
        self._states.pop(conversation_key, None)

    def _record_failure(self, conversation_key: str, error_class: str) -> None:
        state = self._state_for(conversation_key)
        if error_class not in TRANSPORT_ERROR_CLASSES:
            return
        state.consecutive_failures += 1
        if state.consecutive_failures >= DEGRADE_AFTER and not state.degraded:
            state.degraded = True
            logger.warning(
                "decision intake degraded for this conversation",
                extra={
                    "n8_role": "intake",
                    "n8_consecutive_failures": state.consecutive_failures,
                },
            )

    def _record_success(self, conversation_key: str) -> None:
        state = self._state_for(conversation_key)
        state.consecutive_failures = 0

    # -- classification ----------------------------------------------------

    def classify(
        self,
        *,
        text: str,
        lifecycle: str,
        valid_intents: Iterable[str],
        conversation_key: str = "",
        context: str = "",
        intent_enum=None,
    ) -> Optional[object]:
        """Return a ``ConversationIntent`` member, or ``None`` to defer to FAST.

        ``None`` is the answer for: router disabled, shadow mode, degraded
        conversation, provider failure, and any label outside the offered set.

        In ``shadow`` mode the result is observed and logged but ``None`` is
        still returned, so routing is provably unaffected.
        """
        if not self.enabled:
            return None

        allowed = [str(i) for i in valid_intents]
        if not allowed:
            return None

        # Degraded: do NOT call the provider. No probes, no hidden retries.
        if self.is_degraded(conversation_key):
            return None

        try:
            routing = self.provider.classify(
                text=text,
                lifecycle=lifecycle,
                valid_intents=allowed,
                context=context,
            )
        except Exception as exc:  # noqa: BLE001 - a provider must never break a turn
            logger.info(
                "decision intake provider raised",
                extra={"n8_role": "intake",
                       "n8_error_class": type(exc).__name__},
            )
            self._record_failure(conversation_key, "transport")
            return None

        if routing is None:
            # Provider returned no usable proposal. Distinguish transport faults
            # from a lifecycle rejection so degradation stays accurate.
            error_class = getattr(self.provider, "last_error_code", "") or ""
            if not error_class:
                error_class = "invalid_label"
            self._record_failure(conversation_key, error_class)
            return None

        label = getattr(routing, "label", None)
        if label not in allowed:
            self._record_failure(conversation_key, "invalid_label")
            return None

        self._record_success(conversation_key)

        if self.mode == MODE_SHADOW:
            logger.info(
                "decision intake shadow result",
                extra={"n8_role": "intake", "n8_shadow_label": label,
                       "n8_model": getattr(routing, "resolved_model", "")},
            )
            return None

        if intent_enum is None:
            return label
        try:
            return intent_enum(label)
        except Exception:  # noqa: BLE001 - unknown enum member is a rejection
            logger.info(
                "decision intake label not in ConversationIntent",
                extra={"n8_role": "intake", "n8_error_class": "invalid_label"},
            )
            return None


__all__ = [
    "MODE_DISABLED",
    "MODE_SHADOW",
    "MODE_ACTIVE",
    "DEGRADE_AFTER",
    "TRANSPORT_ERROR_CLASSES",
    "IntakeRouter",
]
