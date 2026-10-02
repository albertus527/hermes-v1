"""Frame-shape taxonomy for post-provider-close stream forensics.

Why
---
A streaming run that stops making progress but never goes quiet is the failure
mode a supervisor cannot see. The keep-alive classification that decides which
frames count as forward progress is a **denylist** (see
``agent.chat_completion_helpers._openai_chunk_advances``), so every frame shape
it does not positively recognise emits ``advance=True`` and refreshes the
progress clock forever. That is the correct fail-safe direction — a premature
kill is the only unacceptable outcome — but it also means the *reason* a run
looks alive is invisible after the fact.

This module names the shape of a frame so a supervisor can record WHICH shape
was doing the refreshing. It adds no decision: it is the same question the
existing discriminators already answer, with the answer widened from a boolean
to a bounded enum.

Two rules make it safe to put on the hot path:

* **Presence, never value.** Nothing here calls ``repr()``/``str()`` on a
  payload, hashes one, or measures one. The frame's *fields' presence* is the
  whole signal, so no model output, tool argument, prompt fragment, or
  credential can reach a diagnostic record. ``repr()`` on the streaming hot
  path was measured at 5.5-8.8 us per chunk in ``_estimate_chunk_bytes``; that
  cost is not paid here.
* **Total.** Every public function returns a value for every input, including
  ``None``, a bare ``object()``, a ``Mapping``, a ``MagicMock``, and an object
  whose ``__getattr__`` raises. A frame that cannot be understood is
  :data:`SHAPE_UNKNOWN` — never an exception, because the only call sites are
  inside the agent's streaming loop.

The taxonomy is fixed and closed: :data:`STREAM_SHAPES` is the complete set of
strings that can ever appear on the wire.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Chat Completions frame taxonomy (the 8-way enum).
# ---------------------------------------------------------------------------

#: A choice carried real assistant text.
SHAPE_TEXT_DELTA = "text_delta"
#: A choice carried real reasoning/thinking text.
SHAPE_REASONING_DELTA = "reasoning_delta"
#: A choice carried tool-call payload (delta.tool_calls / delta.function_call).
SHAPE_TOOL_CALL_DELTA = "tool_call_delta"
#: A choice carried only a terminal ``finish_reason`` and no delta payload.
SHAPE_FINISH_ONLY = "finish_only"
#: A choice carried no recognisable output: ``choices=[]``/``choices=None``, or
#: a delta whose every field is absent or empty. **This is the shape the denylist
#: cannot demote** — the provider keep-alive that reads as forward progress.
SHAPE_EMPTY_DELTA = "empty_delta"
#: No choices and a usage payload. The one shape positively known to be terminal.
SHAPE_USAGE_ONLY = "usage_only"
#: An in-stream error frame (``error_type``/``error_message``/``error``, including
#: inside ``model_extra``).
SHAPE_ERROR_SHAPE = "error_shape"
#: Unreadable, or a shape this build does not recognise.
SHAPE_UNKNOWN = "unknown"

#: Every shape string a Chat Completions frame can classify to.
OPENAI_CHUNK_SHAPES = frozenset(
    {
        SHAPE_TEXT_DELTA,
        SHAPE_REASONING_DELTA,
        SHAPE_TOOL_CALL_DELTA,
        SHAPE_FINISH_ONLY,
        SHAPE_EMPTY_DELTA,
        SHAPE_USAGE_ONLY,
        SHAPE_ERROR_SHAPE,
        SHAPE_UNKNOWN,
    }
)

# ---------------------------------------------------------------------------
# Anthropic Messages / Responses (codex) event shapes.
#
# These two APIs announce their frame type and the consumer already reads it, so
# their shape is a normalised echo of that type rather than a payload
# inspection: an Anthropic ``content_block_delta`` says which SUBTYPE arrived
# only in the payload, and pretending otherwise would invent a signal that does
# not exist. The ``a:``/``c:`` prefixes keep the two APIs' enums disjoint.
# ---------------------------------------------------------------------------

SHAPE_A_PING = "a:ping"
SHAPE_A_MESSAGE_START = "a:message_start"
SHAPE_A_MESSAGE_DELTA = "a:message_delta"
SHAPE_A_MESSAGE_STOP = "a:message_stop"
SHAPE_A_CONTENT_BLOCK_START = "a:content_block_start"
SHAPE_A_CONTENT_BLOCK_DELTA = "a:content_block_delta"
SHAPE_A_CONTENT_BLOCK_STOP = "a:content_block_stop"
SHAPE_A_UNREADABLE = "a:unreadable"
SHAPE_A_OTHER = "a:other"

ANTHROPIC_EVENT_SHAPES = frozenset(
    {
        SHAPE_A_PING,
        SHAPE_A_MESSAGE_START,
        SHAPE_A_MESSAGE_DELTA,
        SHAPE_A_MESSAGE_STOP,
        SHAPE_A_CONTENT_BLOCK_START,
        SHAPE_A_CONTENT_BLOCK_DELTA,
        SHAPE_A_CONTENT_BLOCK_STOP,
        SHAPE_A_UNREADABLE,
        SHAPE_A_OTHER,
    }
)

SHAPE_C_PING = "c:ping"
SHAPE_C_QUEUED = "c:response.queued"
SHAPE_C_IN_PROGRESS = "c:response.in_progress"
SHAPE_C_CREATED = "c:response.created"
SHAPE_C_OUTPUT_ITEM_ADDED = "c:response.output_item.added"
SHAPE_C_OUTPUT_ITEM_DONE = "c:response.output_item.done"
SHAPE_C_OUTPUT_TEXT_DELTA = "c:output_text_delta"
SHAPE_C_FUNCTION_CALL = "c:function_call"
SHAPE_C_REASONING = "c:reasoning"
SHAPE_C_COMPLETED = "c:response.completed"
SHAPE_C_INCOMPLETE = "c:response.incomplete"
SHAPE_C_FAILED = "c:response.failed"
SHAPE_C_ERROR = "c:error"
SHAPE_C_UNREADABLE = "c:unreadable"
SHAPE_C_OTHER = "c:other"

CODEX_EVENT_SHAPES = frozenset(
    {
        SHAPE_C_PING,
        SHAPE_C_QUEUED,
        SHAPE_C_IN_PROGRESS,
        SHAPE_C_CREATED,
        SHAPE_C_OUTPUT_ITEM_ADDED,
        SHAPE_C_OUTPUT_ITEM_DONE,
        SHAPE_C_OUTPUT_TEXT_DELTA,
        SHAPE_C_FUNCTION_CALL,
        SHAPE_C_REASONING,
        SHAPE_C_COMPLETED,
        SHAPE_C_INCOMPLETE,
        SHAPE_C_FAILED,
        SHAPE_C_ERROR,
        SHAPE_C_UNREADABLE,
        SHAPE_C_OTHER,
    }
)

#: Complete closed set of every shape string this module can emit. A value
#: outside it is normalised to :data:`SHAPE_UNKNOWN` by
#: :func:`build_stream_diag`, so the wire can never carry an unbounded key.
STREAM_SHAPES = OPENAI_CHUNK_SHAPES | ANTHROPIC_EVENT_SHAPES | CODEX_EVENT_SHAPES

#: Wire-format ceilings. The wire object is budgeted at ~130 bytes inside
#: ``hermes_cli.oneshot._PROGRESS_MAX_EVENT_BYTES`` (1024).
STREAM_DIAG_MODE_MAX = 40
STREAM_DIAG_COUNTER_MAX = 10**9


class _Missing:
    """Sentinel for "this frame has no such field" (distinct from ``None``)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<missing>"


MISSING = _Missing()


def _field(frame: Any, name: str, default: Any = MISSING) -> Any:
    """Read *name* off an attribute object or a Mapping frame. Never raises.

    Mapping frames are read by key, attribute frames by attribute, and ``None``
    is normalised to *default* — a provider that spells "no choices" as
    ``choices=None`` and one that omits the key mean the same thing to every
    caller here.
    """
    if frame is None:
        return default
    try:
        if isinstance(frame, Mapping):
            value = frame.get(name, MISSING)
        else:
            value = getattr(frame, name, MISSING)
    except Exception:
        return default
    if value is None:
        return default
    return value


def _raw_field(frame: Any, name: str) -> tuple[bool, Any]:
    """Return ``(present, value)`` WITHOUT the ``None``-means-absent collapse.

    Only :data:`SHAPE_EMPTY_DELTA` needs this distinction, and it is the whole
    point of that category: a provider that says ``choices: null`` has told us
    its choices are empty, while a frame with no ``choices`` key at all is a
    frame we do not understand.
    """
    if frame is None:
        return (False, None)
    try:
        if isinstance(frame, Mapping):
            return (name in frame, frame.get(name))
        return hasattr(frame, name), getattr(frame, name, None)
    except Exception:
        return (False, None)


def _truthy(value: Any) -> bool:
    """Guarded truthiness.

    Mirrors the ``if choices:`` / ``if getattr(chunk, "usage", None):`` test the
    original discriminator was written as, so
    :func:`advance_for_shape`∘:func:`classify_openai_chunk` returns the identical
    boolean for every input, including the odd falsy-but-present payloads those
    truthiness tests used to fall through on.
    """
    if value is MISSING or value is None:
        return False
    try:
        return bool(value)
    except Exception:
        return False


def _carries(value: Any) -> bool:
    """Whether a delta field holds something. Presence and emptiness only.

    A non-string (a provider's list-of-parts content, say) counts as carrying
    output; only an absent field or an empty/None string does not.
    """
    if value is MISSING or value is None:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _first_choice(choices: Any) -> Any:
    """The first element of a choices payload, or ``None``.

    Indexing is attempted only for real sequence-like payloads; anything else
    (including a mock) degrades to ``None`` rather than raising.
    """
    try:
        return choices[0]
    except Exception:
        return None


def _has_error_shape(chunk: Any) -> bool:
    """Whether *chunk* carries an in-stream error payload.

    Covers the top-level markers and the SDK's ``model_extra`` bag (where a
    Pydantic model parks unknown wire fields), which is where the
    ``choices=None`` + ``error_type`` frame the classifier documents actually
    lands.
    """
    for name in ("error_type", "error_message", "error"):
        if _field(chunk, name) is not MISSING:
            return True
    extra = _field(chunk, "model_extra")
    if isinstance(extra, Mapping):
        return any(
            key in extra for key in ("error_type", "error_message", "error")
        )
    return False


def _classify_choice(choice: Any) -> str:
    """Classify ONE choice. Tool > reasoning > text > finish-only > empty."""
    delta = _field(choice, "delta")
    if delta is not MISSING:
        if _carries(_field(delta, "tool_calls")) or _carries(
            _field(delta, "function_call")
        ):
            return SHAPE_TOOL_CALL_DELTA
        if _carries(_field(delta, "reasoning_content")) or _carries(
            _field(delta, "reasoning")
        ):
            return SHAPE_REASONING_DELTA
        if _carries(_field(delta, "content")):
            return SHAPE_TEXT_DELTA
    if _field(choice, "finish_reason") is not MISSING:
        return SHAPE_FINISH_ONLY
    return SHAPE_EMPTY_DELTA


def classify_openai_chunk(chunk: Any) -> str:
    """Name the shape of one Chat Completions chunk.

    Precedence, and why it is this order:

    1. **truthy ``choices``** — a real choice payload always describes the frame,
       whatever else the frame carries. Inside a choice: tool > reasoning >
       text > finish-only > empty.
    2. **truthy ``usage`` with no choices** — :data:`SHAPE_USAGE_ONLY`. Checked
       ahead of the error markers below *on purpose*: this is the single shape
       ``_openai_chunk_advances`` demotes, and the parity invariant between the
       two is unconditional, so a frame that somehow carries both a usage
       payload and an error marker is labelled by the shape that decides its
       verdict.
    3. **error markers, no choices, no usage** — :data:`SHAPE_ERROR_SHAPE`. A
       real state change that ends the stream, ahead of the two "carries
       nothing" shapes so it is never filed as a keep-alive.
    4. **``choices`` present but falsy** (``[]``/``None``) — :data:`SHAPE_EMPTY_DELTA`.
       The frame acknowledges the field and holds nothing: the provider
       keep-alive that the denylist lets through as progress.
    5. **nothing recognisable** — :data:`SHAPE_UNKNOWN`.

    The ``choices``/``usage`` tests are truthiness tests, not presence tests,
    because that is precisely the shape of the code this replaces — a
    ``usage=0``/``choices=0`` payload must fall through the same way it always
    did, or the boolean and the label would disagree.
    """
    try:
        choices_present, choices = _raw_field(chunk, "choices")
        if _truthy(choices):
            return _classify_choice(_first_choice(choices))
        if _truthy(_field(chunk, "usage")):
            return SHAPE_USAGE_ONLY
        if _has_error_shape(chunk):
            return SHAPE_ERROR_SHAPE
        if choices_present:
            return SHAPE_EMPTY_DELTA
        return SHAPE_UNKNOWN
    except Exception:
        return SHAPE_UNKNOWN


#: Exact Anthropic event types, in the order the API sends them.
_ANTHROPIC_EVENT_TYPE_SHAPES: Dict[str, str] = {
    "ping": SHAPE_A_PING,
    "message_start": SHAPE_A_MESSAGE_START,
    "message_delta": SHAPE_A_MESSAGE_DELTA,
    "message_stop": SHAPE_A_MESSAGE_STOP,
    "content_block_start": SHAPE_A_CONTENT_BLOCK_START,
    "content_block_delta": SHAPE_A_CONTENT_BLOCK_DELTA,
    "content_block_stop": SHAPE_A_CONTENT_BLOCK_STOP,
}


def classify_anthropic_event(event_type: Any) -> str:
    """Normalise an Anthropic SSE event type to a bounded shape string.

    *event_type* is the value the call site already read off the frame; nothing
    here touches the payload. An absent, non-string, or over-long value is
    :data:`SHAPE_A_UNREADABLE`; a string this build does not recognise is
    :data:`SHAPE_A_OTHER` — the two are kept apart because "we could not read
    the type" and "a new type arrived" are different findings.
    """
    if not isinstance(event_type, str) or not event_type:
        return SHAPE_A_UNREADABLE
    if len(event_type) > STREAM_DIAG_MODE_MAX:
        return SHAPE_A_UNREADABLE
    return _ANTHROPIC_EVENT_TYPE_SHAPES.get(event_type, SHAPE_A_OTHER)


#: Exact Responses (codex) event types.
_CODEX_EVENT_TYPE_SHAPES: Dict[str, str] = {
    "ping": SHAPE_C_PING,
    "error": SHAPE_C_ERROR,
    "response.queued": SHAPE_C_QUEUED,
    "response.in_progress": SHAPE_C_IN_PROGRESS,
    "response.created": SHAPE_C_CREATED,
    "response.output_item.added": SHAPE_C_OUTPUT_ITEM_ADDED,
    "response.output_item.done": SHAPE_C_OUTPUT_ITEM_DONE,
    "response.completed": SHAPE_C_COMPLETED,
    "response.incomplete": SHAPE_C_INCOMPLETE,
    "response.failed": SHAPE_C_FAILED,
}


def classify_codex_event(event_type: Any) -> str:
    """Normalise a Responses SSE event type to a bounded shape string.

    The substring buckets mirror how the consumer itself dispatches
    (``_consume_codex_event_stream``): a text delta, a function-call frame
    (arguments deltas and the ``.done`` counterpart alike), and a reasoning
    frame are each one bucket, because "did it advance" does not turn on which
    of those arrived.
    """
    if not isinstance(event_type, str) or not event_type:
        return SHAPE_C_UNREADABLE
    if len(event_type) > STREAM_DIAG_MODE_MAX:
        return SHAPE_C_UNREADABLE
    exact = _CODEX_EVENT_TYPE_SHAPES.get(event_type)
    if exact is not None:
        return exact
    lowered = event_type.lower()
    if "output_text" in lowered:
        return SHAPE_C_OUTPUT_TEXT_DELTA
    if "function_call" in lowered:
        return SHAPE_C_FUNCTION_CALL
    if "reasoning" in lowered:
        return SHAPE_C_REASONING
    return SHAPE_C_OTHER


def advance_for_shape(shape: str) -> bool:
    """The forward-progress bit for a Chat Completions shape.

    Identical to the behaviour of ``_openai_chunk_advances``, and that function
    is a thin wrapper over this one, so the two cannot drift:

    * :data:`SHAPE_USAGE_ONLY` — the terminal usage frame — is the only demotion.
    * Everything else advances, **including** :data:`SHAPE_EMPTY_DELTA` and
      :data:`SHAPE_UNKNOWN`. A keep-alive that reads as progress is a
      misclassification; a real delta demoted to non-progress is a killed run.

    This is the Chat Completions policy only. The Anthropic and Responses sites
    keep their own discriminators (``_anthropic_event_advances`` /
    ``_codex_event_advances``), which demote their own positively-identified
    keep-alives; feeding an ``a:``/``c:`` shape here would answer a different
    question than the one those call sites ask.
    """
    return shape != SHAPE_USAGE_ONLY


def frame_field_presence(chunk: Any) -> Dict[str, bool]:
    """Field-presence booleans for one Chat Completions chunk.

    The forensic companion to :func:`classify_openai_chunk`: presence only, so a
    receipt can say "a delta arrived whose ``content`` field was present but
    empty" without ever holding the value. ``content=True`` with
    ``shape=empty_delta`` is precisely the empty-content keep-alive frame.
    """
    presence = {
        "content": False,
        "reasoning": False,
        "tool": False,
        "finish": False,
    }
    try:
        choice = _first_choice(_field(chunk, "choices"))
        delta = _field(choice, "delta")
        presence["content"] = _field(delta, "content") is not MISSING
        presence["reasoning"] = (
            _field(delta, "reasoning_content") is not MISSING
            or _field(delta, "reasoning") is not MISSING
        )
        presence["tool"] = (
            _field(delta, "tool_calls") is not MISSING
            or _field(delta, "function_call") is not MISSING
        )
        presence["finish"] = _field(choice, "finish_reason") is not MISSING
    except Exception:
        return {key: False for key in presence}
    return presence


def _bounded_mode(mode: Any) -> str:
    if not isinstance(mode, str):
        return ""
    return mode[:STREAM_DIAG_MODE_MAX]


def _bounded_shape(shape: Any) -> str:
    if isinstance(shape, str) and shape in STREAM_SHAPES:
        return shape
    return SHAPE_UNKNOWN


def _bounded_counter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value < 0:  # NaN, or negative
        return 0
    if value == float("inf"):
        return STREAM_DIAG_COUNTER_MAX
    return min(int(value), STREAM_DIAG_COUNTER_MAX)


def _bounded_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if value != value or value < 0:  # NaN, or negative
        return 0.0
    if value == float("inf"):
        return float(STREAM_DIAG_COUNTER_MAX)
    return round(min(float(value), STREAM_DIAG_COUNTER_MAX), 1)


def _bounded_fields(fields: Any) -> Dict[str, bool]:
    if not isinstance(fields, Mapping):
        return {"content": False, "reasoning": False, "tool": False, "finish": False}
    return {
        "content": fields.get("content") is True,
        "reasoning": fields.get("reasoning") is True,
        "tool": fields.get("tool") is True,
        "finish": fields.get("finish") is True,
    }


def build_stream_diag(
    *,
    mode: Any = "",
    shape: Any = "",
    frame_index: Any = 0,
    stream_seconds: Any = 0.0,
    repeat_frame: Any = False,
    mapping_frame: Any = False,
    fields: Any = None,
) -> Dict[str, Any]:
    """Build the bounded wire object carried on a STREAM progress event.

    Fixed keys, bounded scalars, no payload:

    ``mode``
        ``agent.api_mode`` — which of the three emit sites produced this.
    ``shape``
        One of :data:`STREAM_SHAPES`; anything else becomes
        :data:`SHAPE_UNKNOWN`, so the wire key space is closed.
    ``n``
        Frame index within this attempt (``_diag["chunks"]`` pre-increment).
    ``t``
        Seconds since this attempt opened, 1 decimal.
    ``rep``
        This frame is the *same object* as the previous one. Non-False means a
        local emitter is re-yielding a cached frame — which would make the
        "the provider is still sending" premise false.
    ``map``
        The frame arrived as a ``Mapping``, not an attribute object. Non-False
        means every ``getattr`` in the existing classifier missed, which is
        enough on its own to make every shape advance.
    ``d``
        Delta field-presence booleans (see :func:`frame_field_presence`).
    """
    return {
        "mode": _bounded_mode(mode),
        "shape": _bounded_shape(shape),
        "n": _bounded_counter(frame_index),
        "t": _bounded_seconds(stream_seconds),
        "rep": bool(repeat_frame),
        "map": bool(mapping_frame),
        "d": _bounded_fields(fields),
    }
