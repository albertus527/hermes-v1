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
* **No content.**  Payloads carry a kind, a phase, an ``advance`` flag, and the
  already-bounded activity description (clamped to ``ACTIVITY_DESCRIPTION_MAX``).
  No prompts, no tool arguments, no source, no model output, no credentials.  A
  consumer that logs these events can never leak a secret.
* **Advance is the liveness-with-progress bit.**  ``advance=True`` means "real
  forward progress happened": a new operation was claimed, an operation
  completed, a tool ran, or a stream event carried model output.  ``advance=False``
  means "the process is alive and we are telling you so, but nothing moved" —
  a retry backoff, a poll heartbeat, a long-operation keep-alive.  A supervisor
  must refresh its *progress* clock only on ``advance`` events while still
  counting every event as observable activity.
* **Fail-safe is forward.**  ``advance`` defaults to ``True`` and an
  unrecognised description advances, because the two failure directions are not
  symmetric: an unrecognised-but-healthy description wrongly counted as
  non-progress would terminate a working run, while one wrongly counted as
  progress merely loses the chance to end a stuck one early.  The hard elapsed
  fuse still bounds the second case.
* **Boundary events are not coalesced here.**  Transport-level rate limiting
  may coalesce ``active`` events, but must never drop ``started``/``completed``
  — those are what let a supervisor distinguish "request in flight" from
  "hung".
* **The optional ``stream_diag`` object is observation, not control.**  A
  STREAM event may carry a small bounded object naming the SHAPE of the frame
  that arrived (built by ``agent/stream_shapes.py``: a closed enum, a frame
  index, an elapsed-seconds scalar, and two identity fingerprints). It exists
  so a supervisor can record *which* frame kept a stream "alive" — it changes
  no verdict, is read by no bound, and is absent on every event that is not a
  receiving-stream frame. Like the rest of the payload it carries no content:
  field PRESENCE booleans only, never a value, a length, or a body.

This is deliberately *not* ``AIAgent.event_callback``: that channel carries
low-frequency lifecycle milestones and is fanned out to the gateway's async
hook bus, which must not receive per-token-rate traffic.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    "description_advances",
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

#: Descriptions that are ALWAYS a keep-alive, checked before ``_PREFIX_RULES``.
#: Only ``advance=False`` (never a kind change) — an unrecognised kind still
#: degrades to UNKNOWN/active and refreshes observable activity exactly as
#: before.
#:
#: Membership rule: an entry belongs here only if its exact text is emitted by a
#: timer/heartbeat and NEVER by a genuine boundary. A description shared between
#: a heartbeat and a real transition (``"waiting for non-streaming API response"``
#: is the genuine request start *and* the 15s direct-API heartbeat) is
#: deliberately absent — it must pass ``advance=False`` at its call site instead,
#: because a description-keyed rule cannot tell the two call sites apart.
_KEEPALIVE_PREFIX_RULES: Tuple[str, ...] = (
    # Streaming poll heartbeat: "chunks are flowing, still waiting" (30s cadence).
    "waiting for stream response (",
    # Tool executor heartbeats while a long tool is still executing.
    "sequential tool running (",
    "concurrent tools running (",
    # Retry / backoff waits (30s cadence per wait).
    "retry backoff (",
    "error retry backoff (",
    "empty response retry backoff (",
    # Stale-stream / stale-call kill-and-reconnect notices.
    "stale stream detected after ",
    "stale non-streaming call killed after ",
    "codex stream killed after ",
    # Bounded stream-retry accounting.
    "stream retry ",
)


def description_advances(description: Optional[str]) -> bool:
    """True unless *description* is a known keep-alive.

    The default for ``advance``. Anything not in
    :data:`_KEEPALIVE_PREFIX_RULES` advances — including an empty or
    unrecognised description — because advancing is the fail-safe direction.
    """
    text = (description or "").strip()
    if not text:
        return True
    return not text.startswith(_KEEPALIVE_PREFIX_RULES)


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
    advance: Optional[bool] = None,
    stream_diag: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the bounded payload for one progress event.

    ``kind``/``phase`` may be supplied to bypass classification (used by the
    ``channel_ready`` handshake and by tests).

    ``advance`` is the forward-progress bit and is tri-state:

    * ``None`` (default) — derive it from :func:`description_advances`.
    * ``True``/``False`` — explicit. Stream emitters MUST pass it explicitly,
      because ``"receiving stream response"`` cannot say whether the item that
      arrived carried model output.

    ``stream_diag`` is an OPTIONAL, purely observational object attached by the
    three receiving-stream emit sites to name the shape of the frame that
    arrived (see ``agent/stream_shapes.py``). The key is absent unless a mapping
    is supplied, so every other call site is byte-identical to before, and a
    supervisor driving an older producer sees no change at all.
    """
    from agent.session_activity import bound_activity_description

    if kind is None or phase is None:
        auto_kind, auto_phase = classify_progress_event(description)
        kind = kind or auto_kind
        phase = phase or auto_phase
    bounded = bound_activity_description(description)
    if advance is None:
        advance = description_advances(description)
    payload: Dict[str, Any] = {
        "kind": kind,
        "phase": phase,
        # Genuine forward progress vs. observable-but-not-progress. Consumers
        # must treat an absent key as True so an older producer keeps working.
        "advance": bool(advance),
        # Already clamped to ACTIVITY_DESCRIPTION_MAX by the shared helper, so
        # the description can never grow into a payload-sized leak.
        "desc": bounded,
    }
    if isinstance(stream_diag, Mapping):
        payload["stream_diag"] = stream_diag
    return payload
