"""Coarse liveness ("progress") events for supervised agent runs.

Why this exists
---------------
A supervisor that kills a child on elapsed wall-clock time is wrong for
long-running work: a healthy run can be mid-model-request, mid-tool-call, or
mid-stream and still look "slow".  The supervisor instead needs a *liveness*
signal, and the agent already maintains one internally: the activity clock
``AIAgent._touch_activity`` (``run_agent.py``), which fires at every real
progress point — model request start/complete, tool execution start/complete,
streaming responses, terminal activity, retry backoff, and long-operation
keep-alives.

This module owns the vocabulary that turns that internal clock into a small,
transport-agnostic event stream.  ``AIAgent.progress_callback`` receives these
events; a transport (oneshot JSONL file, gateway bus, a test fake) decides
where they go.

Design rules
------------
* **No content.**  Payloads carry a kind, a phase, and the already-bounded
  activity description (clamped to ``ACTIVITY_DESCRIPTION_MAX``).  No prompts,
  no tool arguments, no source, no model output, no credentials.  A consumer
  that logs these events can never leak a secret.
* **UNKNOWN is safe.**  An unrecognised description classifies as
  ``UNKNOWN``/``active``: it refreshes liveness but neither claims nor clears
  an in-flight operation.  A description this table does not recognise can
  therefore never cause a premature termination.
* **Boundary events are not coalesced here.**  Transport-level rate limiting
  may coalesce ``active`` events, but must never drop ``started``/``completed``
  — those are what let a supervisor distinguish "request in flight" from
  "hung".

This is deliberately *not* ``AIAgent.event_callback``: that channel carries
low-frequency lifecycle milestones and is fanned out to the gateway's async
hook bus, which must not receive per-token-rate traffic.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

__all__ = [
    "CHANNEL_READY_EVENT",
    "KIND_MODEL",
    "KIND_STREAM",
    "KIND_TOOL",
    "KIND_UNKNOWN",
    "PHASE_ACTIVE",
    "PHASE_COMPLETED",
    "PHASE_STARTED",
    "classify_progress_event",
    "make_progress_payload",
]

# Emitted once by the transport when it has installed itself, before any agent
# activity. A supervisor waits a short grace for this line: without it, the
# child may simply be running an old runtime that has no progress channel, and
# the supervisor must fall back rather than assume a broken build is idle.
CHANNEL_READY_EVENT = "channel_ready"

KIND_MODEL = "MODEL"
KIND_STREAM = "STREAM"
KIND_TOOL = "TOOL"
#: Unclassified activity. Still proves the child is alive (see module docstring).
KIND_UNKNOWN = "UNKNOWN"

PHASE_STARTED = "started"
PHASE_COMPLETED = "completed"
PHASE_ACTIVE = "active"

# Ordered, first-match-wins. ``_PREFIX_RULES`` entries are (prefix, kind,
# phase). Keep this table aligned with the ``_touch_activity`` call sites; an
# unmatched description degrades to UNKNOWN/active, which is safe.
_PREFIX_RULES: Tuple[Tuple[str, str, str], ...] = (
    # Model request lifecycle.
    ("starting API call #", KIND_MODEL, PHASE_STARTED),
    ("waiting for provider response", KIND_MODEL, PHASE_STARTED),
    ("waiting for non-streaming API response", KIND_MODEL, PHASE_STARTED),
    ("waiting for stream response", KIND_MODEL, PHASE_STARTED),
    # Streaming response body. Keep-alive for a slow provider turn.
    ("receiving stream response", KIND_STREAM, PHASE_ACTIVE),
    # Tool lifecycle.
    ("executing tool: ", KIND_TOOL, PHASE_STARTED),
    ("executing ", KIND_TOOL, PHASE_STARTED),  # concurrent tool batch
    ("tool completed: ", KIND_TOOL, PHASE_COMPLETED),
)

# ``API call #N completed`` is the model-completion boundary. Matched as a
# suffix because the counter in the middle makes prefix matching ambiguous with
# the "starting API call #" family.
_SUFFIX_RULES: Tuple[Tuple[str, str, str], ...] = (
    (" completed", KIND_MODEL, PHASE_COMPLETED),
)


def classify_progress_event(description: Optional[str]) -> Tuple[str, str]:
    """Map an activity description to a ``(kind, phase)`` pair.

    Never raises. An empty, missing, or unrecognised description yields
    ``(KIND_UNKNOWN, PHASE_ACTIVE)``.
    """
    text = (description or "").strip()
    if not text:
        return (KIND_UNKNOWN, PHASE_ACTIVE)
    for prefix, kind, phase in _PREFIX_RULES:
        if text.startswith(prefix):
            return (kind, phase)
    for suffix, kind, phase in _SUFFIX_RULES:
        if text.endswith(suffix):
            return (kind, phase)
    return (KIND_UNKNOWN, PHASE_ACTIVE)


def make_progress_payload(
    description: Optional[str],
    *,
    kind: Optional[str] = None,
    phase: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the bounded payload for one progress event.

    ``kind``/``phase`` may be supplied to bypass classification (used by the
    ``channel_ready`` handshake and by tests).
    """
    from agent.session_activity import bound_activity_description

    if kind is None or phase is None:
        auto_kind, auto_phase = classify_progress_event(description)
        kind = kind or auto_kind
        phase = phase or auto_phase
    return {
        "kind": kind,
        "phase": phase,
        # Already clamped to ACTIVITY_DESCRIPTION_MAX by the shared helper, so
        # the description can never grow into a payload-sized leak.
        "desc": bound_activity_description(description),
    }
