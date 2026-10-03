"""Activity-aware supervision for one exact FRONTEND invocation.

Why
---
A fixed wall-clock timeout kills healthy builds.  The p10 E2E showed the
FRONTEND run still making model calls at the instant the 900s timer fired, so
the build died mid-work.  Elapsed time is the wrong liveness signal.

This module supervises ONE child process and decides aliveness from the
child's own progress stream (see ``agent/progress_events.py``), never from
global or cross-process state.

Invocation isolation
--------------------
Every supervised run gets a :class:`FrontendInvocation` created inside the
call that spawned it and threaded explicitly through the monitor.  It is never
module state, never a class attribute, and never shared.  The progress stream
additionally carries the invocation id, and every event whose id does not match
is discarded.  So another project's activity — and a previous or concurrent
invocation of the *same* project — cannot refresh this run's watchdog.

Two failure modes, two codes
----------------------------
``FRONTEND_IDLE_TIMEOUT``
    No progress for ``idle_timeout_seconds`` and no in-flight operation.
``FRONTEND_HARD_TIMEOUT``
    Pathological run that never stops making progress. Independent upper bound;
    never reported as an idle/stall failure.
``FRONTEND_LEGACY_TIMEOUT``
    Degraded mode: the child never announced a progress channel, so liveness is
    unknowable and supervision falls back to the pre-watchdog wall-clock bound.
    Deliberately neither of the codes above — we cannot honestly attribute a
    degraded-mode timeout to idle or to the hard fuse.

Operator cancellation
---------------------
``FRONTEND_CANCELLED``
    Application shutdown asked *this* invocation to stop. It is deliberately
    NOT one of the three codes above and is excluded from
    :data:`TIMED_OUT_OUTCOMES`: nothing about the run's health is implied, and a
    cancelled run must never be admitted by the caller's artifact-recovery path.

    The request itself is a bare flag (``app.hermes.cancellation``) set by the
    signal handler, which never touches the child. The poll loop stays the sole
    owner of ``proc`` and leaves through the *same* ``_terminate_tree``
    escalation a timeout uses — SIGTERM, grace, then the strongest kill — on
    this invocation's own pid and nothing else.

Two clocks, not one
------------------
Observable activity and forward progress are different facts.  ``advance=False``
events (poll heartbeats, retry backoff, tool-run keeps, stale-reconnect
notices, ping-only stream frames) prove the child is alive and say nothing about
whether it is getting anywhere.  This module therefore keeps two clocks:

``last_activity_at``
    Any accepted event. Drives ``activity_idle_for_seconds`` and the
    longest-silence statistics — observation only.
``last_progress_at``
    Only events the child marked ``advance=True``: a new operation claimed, an
    operation completed, a tool ran, or a stream frame that carried model
    output. Drives ``idle_for_seconds``, the idle bound, and the
    no-progress backstop.

A keep-alive-heavy run can be 100% active and 0% progressing; only the progress
clock sees that.

Forensic receipts (observation only)
-------------------------------------
A timeout code says a bound fired; it never says *which* non-termination mode
occurred. The in-memory :meth:`FrontendInvocation.diagnostics` alone cannot
answer that: it keeps one ``last_progress_kind`` and one event scalar, so the
kind/phase distribution is discarded and nothing about the workspace is
recorded. Every supervised run therefore also emits a bounded
``frontend_forensics/3`` receipt (see :func:`_build_receipt`) that carries the
kind/phase counters, the advancing-vs-keepalive split, the longest true silence
between two progress signals (and between two activity signals), a
metadata-only workspace change fingerprint, artifact completeness, the tool
names seen, and the ``stream_frames`` block: which STREAM frame shape was being
emitted as active/advancing, how many frames of each shape, the within-attempt
frame index and elapsed time reached, and the two identity fingerprints
(``repeat_frame_count`` — the child saw the same frame object twice in a row —
and ``mapping_frame_count`` — frames arrived as mappings, which makes every
``getattr`` in the classifier miss). That block exists because "the stream was
alive" and "the stream was producing output" are different facts, and only the
frame shape tells them apart after the fact.

This is observation, not control. Nothing in the receipt is read back by the
supervision decision, no bound, prompt, toolset, skill, or convergence rule
depends on it, and it is never surfaced in the user-facing error reply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from hermes_cli.oneshot import PROGRESS_FILE_ENV, PROGRESS_ID_ENV

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    # The registry is a control-plane rendezvous, not supervision state: this
    # module never imports it, it only ever calls register()/release() on
    # whatever the caller passes. Keeping the import type-only preserves the
    # module's import surface exactly as it is today.
    from app.hermes.cancellation import FrontendRunCanceller

try:  # pragma: no cover - exercised wherever the Hermes repo root is importable
    from agent.deadline import kill_process_tree

    _KILL_TREE_ERROR: Optional[str] = None
except ImportError as exc:  # pragma: no cover - stripped env
    kill_process_tree = None  # type: ignore[assignment]
    _KILL_TREE_ERROR = str(exc)

try:  # pragma: no cover - same import-path caveat as agent.deadline above
    from agent.session_activity import ACTIVITY_DESCRIPTION_MAX as _ACTIVITY_DESCRIPTION_MAX
except ImportError:  # pragma: no cover - stripped env
    # Mirrors agent/session_activity.py:19 so this module stays importable when
    # the Hermes repo root is not on sys.path (same convention as :120-125).
    _ACTIVITY_DESCRIPTION_MAX = 120

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named, configurable bounds. Defaults live here; operators override them in
# website-builder/config/default.yaml under website_builder.frontend_watchdog.
# ---------------------------------------------------------------------------

#: No progress for this long with nothing in flight => idle. A long provider
#: request is NOT idle: it is an in-flight operation, refreshed by its own
#: start/stream/complete events.
FRONTEND_IDLE_TIMEOUT_SECONDS = 180.0

#: Pathological-run fuse. Generous and independent of the idle bound: a
#: continuously-active loop must still be able to end.
FRONTEND_HARD_MAX_RUNTIME_SECONDS = 2700.0

#: No-progress backstop that applies EVEN WHILE AN OPERATION IS IN FLIGHT.
#: Measured from the last PROGRESS event (never from operation start, and never
#: from a keep-alive), so any genuine stream/progress event keeps refreshing
#: liveness.
#:
#: Semantics, precisely: this is the maximum duration of **no genuine forward
#: progress** while an operation remains in flight. It is NOT a maximum
#: operation duration. A genuinely advancing stream of any length is never cut,
#: because its progress events keep sliding the window; only the hard elapsed
#: fuse ends such a run. What this bound does terminate is an operation whose
#: only output for this long is keep-alive activity.
FRONTEND_MAX_SINGLE_OPERATION_SECONDS = 900.0

#: Wall-clock bound used when the child never announced a progress channel.
#: Matches the pre-watchdog behaviour exactly, so a degraded supervisor is
#: never *more* permissive than what it replaced.
FRONTEND_LEGACY_WALLCLOCK_SECONDS = 900.0

#: The child must announce its progress channel within this grace. Startup
#: imports the Hermes runtime, which is slow; the grace is generous but far
#: below the legacy bound, so a healthy child always wins.
PROGRESS_CHANNEL_STARTUP_GRACE_SECONDS = 60.0

#: How often the supervisor drains the progress stream and evaluates bounds.
PROGRESS_POLL_INTERVAL_SECONDS = 1.0

#: Minimum gap between ``FRONTEND activity`` log lines for one invocation.
#: Boundary events (started/completed/timeouts) always log.
ACTIVITY_LOG_MIN_INTERVAL_SECONDS = 30.0

#: Bounded stdout/stderr capture for final diagnostics.
OUTPUT_CAPTURE_LIMIT_CHARS = 2000
#: Rolling tail kept once a stream overflows, so the end of a child's output
#: (usually the actual error) survives truncation.
_OUTPUT_TAIL_LIMIT_CHARS = 500
_OUTPUT_TRUNCATION_MARKER = "\n...[truncated]...\n"
#: Hard ceiling on one captured stream, marker included.
MAX_CAPTURED_OUTPUT_CHARS = (
    OUTPUT_CAPTURE_LIMIT_CHARS + len(_OUTPUT_TRUNCATION_MARKER) + _OUTPUT_TAIL_LIMIT_CHARS
)

#: SIGTERM -> SIGKILL escalation window.
TERMINATE_GRACE_SECONDS = 10.0

# ---------------------------------------------------------------------------
# Forensic receipt bounds. Observation only: nothing below participates in any
# supervision decision. They exist so a pathological workspace or a pathological
# run can neither grow a receipt without bound nor stall the poll loop.
# ---------------------------------------------------------------------------

#: Cadence of the workspace metadata fingerprint. A baseline is taken at
#: launch, then one sample per interval. 256 samples x 30s covers the worst
#: case of two back-to-back 45-minute runs.
WORKSPACE_SAMPLE_INTERVAL_SECONDS = 30.0

#: Files examined per sample. A larger tree is marked ``truncated`` rather than
#: walked further, so the supervisor loop is never held up by the workspace.
WORKSPACE_SAMPLE_MAX_FILES = 2000

#: Hard ceiling on samples recorded for one invocation.
MAX_SAMPLE_SERIES = 256

#: Ceiling on retained relative mutated paths. The counts above are unaffected.
MAX_MUTATED_PATHS = 50

#: Ceiling on distinct tool names counted. Names are not arguments.
MAX_TOOL_NAME_DISTINCT = 32

#: Ceiling on distinct STREAM frame shapes counted. The producer's taxonomy is a
#: closed enum (see ``agent/stream_shapes.py``); the cap is here so a future
#: producer that widens it cannot grow the receipt, and so a hostile one cannot
#: either.
MAX_STREAM_SHAPE_KEYS = 12

#: Ceiling on distinct ``api_mode`` values counted. Realistically 1-2 per run.
MAX_API_MODE_KEYS = 4

#: Ceiling on one wire ``stream_diag`` shape/mode string, mirroring
#: ``agent.stream_shapes.STREAM_DIAG_MODE_MAX``.
STREAM_DIAG_TOKEN_MAX = 40

#: A shape string is a lower-case enum token, optionally namespaced by its API
#: (``empty_delta``, ``a:content_block_delta``, ``c:response.in_progress``).
#: Anything else is ignored rather than guessed at: a shape is only useful as a
#: taxonomy member, and an unrecognisable key would be a false finding.
_STREAM_DIAG_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z0-9_.\-]{1,40})?$")

#: Most recent normalized activity events retained for the receipt.
RECENT_ACTIVITY_LEN = 20

#: Receipts retained per project directory before the oldest are pruned.
MAX_RECEIPTS_PER_PROJECT = 20

#: Receipt schema identifier. Bump on any incompatible field change.
#: v2 adds the ``advance`` split (``counters.advance``/``counters.keepalive``,
#: ``activity.longest_forward_progress_gap_seconds``,
#: ``diagnostics.activity_idle_for_seconds``) alongside v1.
#: v3 adds the ``stream_frames`` block — WHICH post-close frame shape was being
#: emitted as STREAM/active/advance=true. Additive alongside v2.
#: v4 makes the per-tool counters INVOCATION counts and adds
#: ``counters.tool_error_count``. v3's ``tool_names`` counted every event that
#: carried a name, so one sequential invocation contributed twice (its
#: ``executing tool: X`` and its ``tool completed: X``) and the histogram
#: totalled ~2x the calls actually made — which is exactly the kind of number a
#: reader must not build a metric on. ``counters.model_completed`` becomes the
#: model-call count for the same reason: the classifier's ``" completed"``
#: suffix rule also matches non-model lifecycle text, so the phase-derived count
#: over-counted by one per compaction episode.
FORENSICS_SCHEMA = "frontend_forensics/4"

#: Watchdog outcome codes.
OUTCOME_IDLE_TIMEOUT = "FRONTEND_IDLE_TIMEOUT"
OUTCOME_HARD_TIMEOUT = "FRONTEND_HARD_TIMEOUT"
OUTCOME_LEGACY_TIMEOUT = "FRONTEND_LEGACY_TIMEOUT"

#: Application shutdown asked this exact invocation to stop. Deliberately NOT a
#: member of :data:`TIMED_OUT_OUTCOMES`: a cancelled run says nothing about the
#: run's health, and callers gate artifact recovery on ``timed_out``, so a
#: cancelled run must never be admitted by that path.
OUTCOME_CANCELLED = "FRONTEND_CANCELLED"

#: Exit status reported for a cancelled run. 124 is the established timeout
#: status and stays exclusively that; 130 (128 + SIGINT) is the conventional
#: "interrupted" status. The authoritative discriminator is the outcome code
#: itself, so this stays stable regardless of which signal triggered it.
CANCELLED_EXIT_CODE = 130

TIMED_OUT_OUTCOMES = frozenset(
    {OUTCOME_IDLE_TIMEOUT, OUTCOME_HARD_TIMEOUT, OUTCOME_LEGACY_TIMEOUT}
)

# Wire constants mirrored from agent/progress_events.py. Duplicated as literals
# so this module stays importable when the Hermes repo root is not on sys.path.
_CHANNEL_READY_EVENT = "channel_ready"
_PHASE_STARTED = "started"
_PHASE_COMPLETED = "completed"
_PHASE_ACTIVE = "active"
_KIND_UNKNOWN = "UNKNOWN"
_KIND_MODEL = "MODEL"
_KIND_TOOL = "TOOL"
_KIND_STREAM = "STREAM"

# The only two descriptions that carry a tool name. Both are built from the tool
# name and a duration by the emitter (see the ``executing tool:`` /
# ``tool completed:`` sites), never from arguments, so the first
# whitespace-delimited token after the prefix is a name and not a payload.
_TOOL_STARTED_DESC_PREFIX = "executing tool: "
_TOOL_COMPLETED_DESC_PREFIX = "tool completed: "
_TOOL_DESC_PREFIXES = (_TOOL_STARTED_DESC_PREFIX, _TOOL_COMPLETED_DESC_PREFIX)

#: Suffix the emitter appends to a tool-completion description when the call
#: returned an error. It is built as
#: ``f"tool completed: {name} ({duration:.1f}s){' (error)' if is_error else ''}"``.
#: A refused write therefore reaches the progress channel looking exactly like a
#: successful one — the same kind/phase, and ``advance=True`` either way — so the
#: receipt needs its own failure counter or a rejection loop is invisible.
_TOOL_ERROR_DESC_SUFFIX = " (error)"

#: The model-call completion boundary, emitted as exactly
#: ``f"API call #{n} completed"``. Matched explicitly instead of by
#: KIND_MODEL/PHASE_COMPLETED because the classifier's ``" completed"`` SUFFIX
#: rule is deliberately broad and also matches unrelated lifecycle text —
#: notably the compression heartbeat's terminal ``"context compression
#: completed"`` — which would add one phantom model call per compaction episode.
_MODEL_CALL_DESC_PREFIX = "API call #"
_MODEL_CALL_DESC_SUFFIX = " completed"

_WATCHDOG_CONFIG_SECTION = ("website_builder", "frontend_watchdog")
_CONFIG_KEYS = {
    "idle_timeout_seconds": FRONTEND_IDLE_TIMEOUT_SECONDS,
    "hard_max_runtime_seconds": FRONTEND_HARD_MAX_RUNTIME_SECONDS,
    "max_single_operation_seconds": FRONTEND_MAX_SINGLE_OPERATION_SECONDS,
    "legacy_wallclock_seconds": FRONTEND_LEGACY_WALLCLOCK_SECONDS,
    "startup_grace_seconds": PROGRESS_CHANNEL_STARTUP_GRACE_SECONDS,
}


# ---------------------------------------------------------------------------
# Forensic description hygiene.
#
# ``bound_activity_description`` clamps the wire description but does not
# redact it, and not every emitter goes through the two mapped prefixes: an
# unmapped description still refreshes liveness and would otherwise be stored
# verbatim. A forensic receipt must therefore re-clamp and redact: it records
# that a tool ran, never what it was given, where it pointed, or which payload
# it carried.
# ---------------------------------------------------------------------------

_REDACTED = "<redacted>"

# scheme://... (query strings and fragments included)
_FORENSIC_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>|]*")
# C:\... and C:/... ; a bare "C:" is left alone so "C:" in prose survives.
_FORENSIC_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|,;)]*")
# /usr/... , /src/App.tsx , /tmp/x/y . The leading lookbehind keeps ordinary
# words containing a slash ("and/or", "3/4") intact.
_FORENSIC_POSIX_PATH_RE = re.compile(
    r"(?<![\w.\-])/(?:[^\s\"'<>|]+/)+[^\s\"'<>|]*"
    r"|(?<![\w.\-])/[A-Za-z0-9._~\-]{2,}(?![\w/])"
)
# 32+ hex characters, then 32+ base64-ish characters with no whitespace.
_FORENSIC_HEX_RE = re.compile(r"\b[0-9A-Fa-f]{32,}\b")
_FORENSIC_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")

#: A tool name is a bare identifier. Anything else after the known prefix is
#: treated as absent rather than guessed at.
_FORENSIC_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def normalize_forensic_desc(raw: Optional[str]) -> str:
    """Clamp and redact one activity description for forensic storage.

    Pure, total, and I/O-free: any input yields a bounded string. Whitespace is
    collapsed, the result is re-clamped to the shared description budget, and
    URLs, absolute paths, and long hex/base64 runs are replaced with
    ``<redacted>``. Redaction runs after clamping so a payload cannot push a
    path out of the retained window.
    """
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return ""
    text = text[: _ACTIVITY_DESCRIPTION_MAX - 1] + "…" if len(text) > _ACTIVITY_DESCRIPTION_MAX else text
    for pattern in (
        _FORENSIC_URL_RE,
        _FORENSIC_WIN_PATH_RE,
        _FORENSIC_POSIX_PATH_RE,
        _FORENSIC_HEX_RE,
        _FORENSIC_B64_RE,
    ):
        text = pattern.sub(_REDACTED, text)
    return text[:_ACTIVITY_DESCRIPTION_MAX]


def forensic_tool_name(raw_desc: Optional[str]) -> Optional[str]:
    """Extract the tool name from a known tool-lifecycle description.

    Only the two prefixes the emitter actually produces are honoured, and only
    the first whitespace-delimited token after them. A description that does
    not match returns ``None`` rather than guessing, so an unmapped
    description can never smuggle an argument into the receipt.
    """
    text = (raw_desc or "").strip()
    for prefix in _TOOL_DESC_PREFIXES:
        if not text.startswith(prefix):
            continue
        token = text[len(prefix):].split(" ", 1)[0].strip().lstrip("(")
        if _FORENSIC_TOOL_NAME_RE.match(token):
            return token
        return None
    return None


def forensic_tool_failed(raw_desc: Optional[str]) -> bool:
    """Whether a tool-completion description reports the call as failed.

    Only the completion prefix is honoured: a ``started`` description never
    carries the outcome, so treating one as a failure would be a guess. This is
    the receipt's only view of tool failure — the progress channel itself
    classifies a refused call exactly like a successful one.
    """
    text = (raw_desc or "").strip()
    if not text.startswith(_TOOL_COMPLETED_DESC_PREFIX):
        return False
    return text.endswith(_TOOL_ERROR_DESC_SUFFIX)


def forensic_model_call_completed(raw_desc: Optional[str]) -> bool:
    """Whether a description is the model-call completion boundary.

    The single source of truth for "one model call happened". Deliberately
    narrower than KIND_MODEL/PHASE_COMPLETED, which the classifier's
    ``" completed"`` suffix rule also produces for other lifecycle text.
    """
    text = (raw_desc or "").strip()
    if not text.startswith(_MODEL_CALL_DESC_PREFIX):
        return False
    return text.endswith(_MODEL_CALL_DESC_SUFFIX)


class WatchdogUnavailable(RuntimeError):
    """Whole-tree termination is unavailable; supervision must not be attempted."""


# ---------------------------------------------------------------------------
# STREAM frame-shape diagnostics (observation only).
#
# The child may attach a small ``stream_diag`` object to a STREAM event naming
# the shape of the frame that arrived, so a receipt can answer "which frame kept
# this stream alive" instead of only "the stream was alive". It is built by
# ``agent/stream_shapes.py`` and is a closed enum plus counters — no content.
#
# The validation below is deliberately paranoid and TOTAL: the child is another
# process, the object arrives once per stream frame, and a malformed one must
# never be able to raise into the poll loop or to change any bound. A value that
# does not fit the shape of the record is simply not recorded.
# ---------------------------------------------------------------------------


def _normalize_diag_token(raw: Any) -> Optional[str]:
    """Return a bounded enum token, or ``None`` if *raw* is not one."""
    if not isinstance(raw, str) or len(raw) > STREAM_DIAG_TOKEN_MAX:
        return None
    if not _STREAM_DIAG_TOKEN_RE.match(raw):
        return None
    return raw


def _normalize_diag_number(raw: Any) -> Optional[float]:
    """Return a finite, non-negative number, or ``None``."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value


def normalize_stream_diag(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate one wire ``stream_diag`` object. Never raises.

    Returns ``None`` — meaning "record nothing" — for a missing, non-mapping,
    wrong-typed, or over-long value, and for one whose ``shape`` is not a
    taxonomy token. Absent stays absent: a child built before this object
    existed keeps working exactly as before.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        shape = _normalize_diag_token(raw.get("shape"))
        if shape is None:
            return None
        diag: Dict[str, Any] = {"shape": shape}
        mode = raw.get("mode")
        if isinstance(mode, str) and len(mode) <= STREAM_DIAG_TOKEN_MAX:
            diag["mode"] = mode
        for key in ("rep", "map"):
            if isinstance(raw.get(key), bool):
                diag[key] = raw[key]
        for key in ("n", "t"):
            value = _normalize_diag_number(raw.get(key))
            if value is not None:
                diag[key] = value
        return diag
    except Exception:  # pragma: no cover - Mapping .get on a hostile subclass
        return None


@dataclass
class WatchdogPolicy:
    """Resolved bounds for one supervised invocation."""

    idle_timeout_seconds: float = FRONTEND_IDLE_TIMEOUT_SECONDS
    hard_max_runtime_seconds: float = FRONTEND_HARD_MAX_RUNTIME_SECONDS
    max_single_operation_seconds: float = FRONTEND_MAX_SINGLE_OPERATION_SECONDS
    legacy_wallclock_seconds: float = FRONTEND_LEGACY_WALLCLOCK_SECONDS
    startup_grace_seconds: float = PROGRESS_CHANNEL_STARTUP_GRACE_SECONDS
    poll_interval_seconds: float = PROGRESS_POLL_INTERVAL_SECONDS
    activity_log_min_interval_seconds: float = ACTIVITY_LOG_MIN_INTERVAL_SECONDS


@dataclass
class FrontendInvocation:
    """Per-invocation supervision state.

    One instance per spawned child. Holding this as a local (never module or
    class state) is what makes cross-invocation interference structurally
    impossible rather than merely unlikely.
    """

    invocation_id: str
    project_id: str
    build_operation_id: str
    started_at: float
    last_activity_at: float
    #: Forward-progress clock. Only an ``advance=True`` event moves it; see the
    #: module docstring. Initialised to ``started_at`` for the same reason
    #: ``last_activity_at`` is: before any event the child has proved nothing.
    last_progress_at: float
    child_pid: Optional[int] = None
    active_operation: Optional[str] = None
    active_operation_started_at: Optional[float] = None
    progress_channel_confirmed: bool = False
    progress_offset: int = 0
    last_progress_kind: Optional[str] = None
    unknown_activity_count: int = 0
    progress_event_count: int = 0
    advance_event_count: int = 0
    keepalive_event_count: int = 0
    stale_event_count: int = 0
    last_logged_activity_at: Optional[float] = None
    # --- Forensic detail. Additive only: nothing above reads any of it, and
    # --- no bound below consults it.
    last_progress_phase: Optional[str] = None
    model_started_count: int = 0
    model_completed_count: int = 0
    tool_started_count: int = 0
    tool_completed_count: int = 0
    #: Tool invocations the emitter reported as failed. A subset of
    #: ``tool_completed_count``, and the only thing in the receipt that
    #: separates a refused call from a successful one.
    tool_error_count: int = 0
    stream_active_count: int = 0
    first_activity_at: Optional[float] = None
    #: Longest true silence between two accepted progress signals, measured
    #: across EVERY accepted event including UNKNOWN and STREAM. This is the
    #: discriminator between continuously-active work and in-flight-protected
    #: silence: in-flight protection suppresses the idle bound for up to
    #: ``max_single_operation_seconds``, so a hard-timeout run can still contain
    #: multi-minute gaps.
    longest_activity_gap_seconds: float = 0.0
    #: Same, restricted to ``advance=True`` events. This is what separates "the
    #: child was busy" from "the child was making progress", and it is the
    #: number a reader compares against ``policy.max_single_operation``.
    longest_forward_progress_gap_seconds: float = 0.0
    recent_activity: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=RECENT_ACTIVITY_LEN)
    )
    tool_name_counts: Dict[str, int] = field(default_factory=dict)
    tool_name_counts_truncated: bool = False
    # --- STREAM frame shapes. Additive forensics: which post-provider-close
    # --- frame shape was emitted as STREAM/active/advance=true. Counts are
    # --- LOWER BOUNDS — the transport coalesces active events inside a 5s
    # --- window (hermes_cli/oneshot.py), so a run's true frame count is at
    # --- least these numbers, never fewer. They are attributed correctly
    # --- (newest event wins, and the shape recorded on a retained line is the
    # --- shape that was firing), they are just undercounted.
    stream_shape_counts: Dict[str, int] = field(default_factory=dict)
    stream_shape_advance_counts: Dict[str, int] = field(default_factory=dict)
    stream_shape_counts_truncated: bool = False
    api_mode_counts: Dict[str, int] = field(default_factory=dict)
    api_mode_counts_truncated: bool = False
    #: Frames the child saw as the SAME object as the one before it, and frames
    #: that arrived as a Mapping. Both falsy in a healthy run: a repeated
    #: object means a local re-emitter, and a Mapping frame makes every
    #: ``getattr`` in the classifier miss.
    repeat_frame_count: int = 0
    mapping_frame_count: int = 0
    last_stream_shape: Optional[str] = None
    stream_shape_first_offset: Optional[float] = None
    stream_shape_last_offset: Optional[float] = None
    #: Newest observed frame index within an attempt, and the largest
    #: within-attempt elapsed time and frame index seen. Together these are
    #: what separates "the provider was still streaming real output" from
    #: "N content-free keep-alives arrived over T seconds".
    stream_frame_index: Optional[float] = None
    stream_frame_seconds: Optional[float] = None
    stream_max_frame_seconds: float = 0.0
    stream_max_frame_index: float = 0.0

    def note_activity(self, now: float, kind: str, advance: bool = True) -> None:
        """Record progress from THIS invocation. Refreshes liveness.

        Every accepted event refreshes *observable activity*, including
        ``UNKNOWN``: an unclassified activity stamp still proves the child is
        alive, and refusing to count it would let an unrecognised (but healthy)
        activity description look like a hang. Unknown events are counted
        separately for observability instead.

        Only ``advance=True`` events refresh the forward-progress clock that
        the idle bound and the no-progress backstop read. A missing ``advance``
        key arrives here as ``True`` (the caller decides), so an older producer
        behaves exactly as it did before this distinction existed.

        Both gaps are measured *before* their clock moves, so the
        ``longest_*_gap_seconds`` values are the real silence between two
        signals of that class.
        """
        gap = max(0.0, now - self.last_activity_at)
        if self.longest_activity_gap_seconds < gap:
            self.longest_activity_gap_seconds = gap
        self.last_activity_at = now
        if self.first_activity_at is None:
            self.first_activity_at = now
        self.last_progress_kind = kind
        self.progress_event_count += 1
        if kind == _KIND_UNKNOWN:
            self.unknown_activity_count += 1
        if advance:
            progress_gap = max(0.0, now - self.last_progress_at)
            if self.longest_forward_progress_gap_seconds < progress_gap:
                self.longest_forward_progress_gap_seconds = progress_gap
            self.last_progress_at = now
            self.advance_event_count += 1
        else:
            self.keepalive_event_count += 1

    def note_event_detail(
        self,
        now: float,
        kind: str,
        phase: str,
        desc: str,
        *,
        advance: bool = True,
        stream_diag: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Fold one accepted event into the forensic counters and ring buffer.

        Called after :meth:`note_activity` and :meth:`note_operation` so the
        liveness decision has already been made; this only records.

        ``model_started_count`` / ``tool_started_count`` count phase events and
        are NOT invocation counts: several descriptions classify as
        KIND_MODEL/PHASE_STARTED, and a concurrent batch announces itself once
        for the whole batch. ``model_completed_count``, ``tool_completed_count``
        and ``tool_name_counts`` are per-invocation.
        """
        self.last_progress_phase = phase
        tool_completed = kind == _KIND_TOOL and phase == _PHASE_COMPLETED
        if kind == _KIND_MODEL:
            if phase == _PHASE_STARTED:
                self.model_started_count += 1
            # Counted from the boundary description, not from the phase: the
            # classifier also produces KIND_MODEL/PHASE_COMPLETED for unrelated
            # lifecycle text ending in " completed", and a model-call count that
            # includes compaction heartbeats is not a model-call count.
            if phase == _PHASE_COMPLETED and forensic_model_call_completed(desc):
                self.model_completed_count += 1
        elif kind == _KIND_TOOL:
            if phase == _PHASE_STARTED:
                self.tool_started_count += 1
            elif tool_completed:
                self.tool_completed_count += 1
                if forensic_tool_failed(desc):
                    self.tool_error_count += 1
        elif kind == _KIND_STREAM and phase == _PHASE_ACTIVE:
            self.stream_active_count += 1

        self._note_stream_shape(now, advance, stream_diag)

        # The tool name is parsed from the RAW description (before redaction)
        # because a name is not an argument; only the clamped, redacted form is
        # ever stored. Counted on the COMPLETION boundary only, so one
        # invocation contributes exactly one — a `started` event would double it.
        if tool_completed:
            name = forensic_tool_name(desc)
            if name is not None:
                known = name in self.tool_name_counts
                if known or len(self.tool_name_counts) < MAX_TOOL_NAME_DISTINCT:
                    self.tool_name_counts[name] = self.tool_name_counts.get(name, 0) + 1
                else:
                    self.tool_name_counts_truncated = True

        self.recent_activity.append(
            {
                "offset_seconds": round(max(0.0, now - self.started_at), 1),
                "kind": kind,
                "phase": phase,
                "desc": normalize_forensic_desc(desc),
            }
        )

    def _note_stream_shape(
        self,
        now: float,
        advance: bool,
        stream_diag: Optional[Mapping[str, Any]],
    ) -> None:
        """Fold one validated ``stream_diag`` into the frame-shape counters.

        A no-op for an absent or unvalidated object, which is what an older
        child sends. Nothing here is read by a bound: the whole point is to
        record, after the fact, WHICH shape a keep-alive-looking stream was
        actually made of.
        """
        if not isinstance(stream_diag, Mapping):
            return
        shape = stream_diag.get("shape")
        if not isinstance(shape, str):
            return
        offset = max(0.0, now - self.started_at)
        # The shape dict is the admission gate for BOTH counters, so the
        # advancing split can never hold a key the shape count rejected — one
        # bound, one truncation flag, and no second unbounded dict.
        known = shape in self.stream_shape_counts
        if known or len(self.stream_shape_counts) < MAX_STREAM_SHAPE_KEYS:
            self.stream_shape_counts[shape] = self.stream_shape_counts.get(shape, 0) + 1
        else:
            self.stream_shape_counts_truncated = True
        if advance and shape in self.stream_shape_counts:
            self.stream_shape_advance_counts[shape] = (
                self.stream_shape_advance_counts.get(shape, 0) + 1
            )
        mode = stream_diag.get("mode")
        if isinstance(mode, str) and mode:
            known_mode = mode in self.api_mode_counts
            if known_mode or len(self.api_mode_counts) < MAX_API_MODE_KEYS:
                self.api_mode_counts[mode] = self.api_mode_counts.get(mode, 0) + 1
            else:
                self.api_mode_counts_truncated = True
        if stream_diag.get("rep") is True:
            self.repeat_frame_count += 1
        if stream_diag.get("map") is True:
            self.mapping_frame_count += 1
        self.last_stream_shape = shape
        if self.stream_shape_first_offset is None:
            self.stream_shape_first_offset = round(offset, 1)
        self.stream_shape_last_offset = round(offset, 1)
        index = _normalize_diag_number(stream_diag.get("n"))
        if index is not None:
            self.stream_frame_index = index
            self.stream_max_frame_index = max(self.stream_max_frame_index, index)
        seconds = _normalize_diag_number(stream_diag.get("t"))
        if seconds is not None:
            self.stream_frame_seconds = seconds
            self.stream_max_frame_seconds = max(self.stream_max_frame_seconds, seconds)

    def note_operation(self, now: float, kind: str, phase: str) -> None:
        """Track the in-flight operation from typed boundary events only.

        An unclassified event deliberately neither claims nor clears an
        operation, so a description this build does not recognise can never
        cause a premature termination.
        """
        if phase == _PHASE_STARTED:
            # Same-kind re-announcement is a NO-OP, by construction: a poll
            # heartbeat re-announcing an in-flight MODEL operation must neither
            # clear it nor restart ``active_operation_started_at``, or an
            # operation could be re-armed forever.
            if self.active_operation != kind:
                self.active_operation = kind
                self.active_operation_started_at = now
        elif phase == _PHASE_COMPLETED:
            self.active_operation = None
            self.active_operation_started_at = None

    def no_progress_for(self, now: float) -> float:
        """Seconds since the last GENUINE FORWARD PROGRESS.

        This is the bound-driving clock: the idle bound and the no-progress
        backstop both read it. An ``advance=False`` keep-alive never moves it.
        """
        return max(0.0, now - self.last_progress_at)

    def no_activity_for(self, now: float) -> float:
        """Seconds since the last event of ANY kind. Observation only."""
        return max(0.0, now - self.last_activity_at)

    def is_in_flight(self, now: float, max_single_operation_seconds: float) -> bool:
        """True when an operation is in flight AND has recently progressed.

        ``max_single_operation_seconds`` is the maximum duration of *no genuine
        forward progress* while an operation is in flight — not a maximum
        operation duration. A genuinely advancing stream keeps refreshing the
        progress clock and stays protected for as long as it keeps advancing;
        what eventually ends an operation is silence from forward progress,
        whether or not keep-alive events keep arriving.
        """
        if self.active_operation is None:
            return False
        return self.no_progress_for(now) < max_single_operation_seconds

    def diagnostics(
        self, now: float, forensics: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        """Bounded operator metadata. Never includes prompts or output bodies.

        *forensics* is the receipt built for this invocation. It is attached
        verbatim when supplied and omitted when not, so a direct caller of
        ``diagnostics(now)`` still sees exactly the pre-existing key set.

        ``idle_for_seconds`` is measured from genuine forward progress (it is
        the number the bounds actually read); ``activity_idle_for_seconds`` is
        the any-event reading, reported next to it so both are visible.
        """
        data = {
            "invocation_id": self.invocation_id,
            "project_id": self.project_id,
            "build_operation_id": self.build_operation_id,
            "pid": self.child_pid,
            "elapsed_seconds": round(max(0.0, now - self.started_at), 1),
            "idle_for_seconds": round(self.no_progress_for(now), 1),
            "activity_idle_for_seconds": round(self.no_activity_for(now), 1),
            "advance_event_count": self.advance_event_count,
            "keepalive_event_count": self.keepalive_event_count,
            "active_operation": self.active_operation,
            "active_operation_started_at": self.active_operation_started_at,
            "last_progress_kind": self.last_progress_kind,
            "progress_event_count": self.progress_event_count,
            "unknown_activity_count": self.unknown_activity_count,
            "stale_event_count": self.stale_event_count,
            "progress_channel_confirmed": self.progress_channel_confirmed,
        }
        if forensics is not None:
            data["forensics"] = dict(forensics)
        return data


@dataclass
class SupervisedRun:
    """Result of supervising one child process."""

    returncode: int
    stdout: str
    stderr: str
    outcome: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    terminated: bool = False
    tree_kill_attempted: bool = False


class WorkspaceSampler:
    """Bounded, metadata-only change detector for one workspace.

    The fingerprint is a digest over ``(relpath, size, mtime_ns)`` for
    ``design-dna.json`` and ``src/**``. File *contents* are never opened, read,
    hashed, or stored, so the "no source contents" property holds absolutely:
    this is a change detector, not a content or source hash. It is deliberately
    also blind to a same-size rewrite whose mtime lands in the same filesystem
    tick — and on the hosts this was measured on that tick can be coarse enough
    for two rapid writes to share it. The counts it produces are therefore lower
    bounds, which is the honest reading.

    Nothing here influences supervision. :meth:`sample` is called from the poll
    loop, so every public entry point swallows exceptions: a forensic
    observation must never be able to change the supervision result.
    """

    def __init__(
        self,
        workspace: Optional[Path],
        artifacts_probe: Optional[Callable[[], bool]] = None,
        *,
        started_at: float = 0.0,
    ) -> None:
        self._workspace = Path(workspace) if workspace is not None else None
        self._probe = artifacts_probe
        self._started_at = started_at
        self._last_sample_at: Optional[float] = None
        self._last_fingerprint: Optional[str] = None
        self._prev_entries: Dict[str, Tuple[int, int]] = {}
        self._unique_mutated: Set[str] = set()
        self._mutated_paths: List[str] = []
        self.samples = 0
        self.distinct_fingerprints = 0
        self.source_mutation_count = 0
        self.first_mutation_offset_seconds: Optional[float] = None
        self.last_mutation_offset_seconds: Optional[float] = None
        self.first_complete_offset_seconds: Optional[float] = None
        self.complete_at_end = False
        self.design_dna_present = False
        self.app_tsx_present = False
        self.truncated = False
        self.probe_failed = False

    @property
    def probe_available(self) -> bool:
        return self._probe is not None

    @property
    def metadata_fingerprint(self) -> Optional[str]:
        """Digest over ``(relpath, size, mtime_ns)`` from the last sample.

        A change detector, not a content or source hash: a same-size rewrite
        inside one filesystem mtime tick is deliberately invisible, so every
        mutation count derived from this is a lower bound.
        """
        return self._last_fingerprint

    def sample(self, now: float, *, force: bool = False) -> None:
        """Take one sample if the interval has elapsed. Never raises."""
        try:
            self._sample(now, force)
        except Exception:
            logger.debug("FRONTEND workspace sampling failed", exc_info=True)

    def _sample(self, now: float, force: bool) -> None:
        if self.samples >= MAX_SAMPLE_SERIES:
            return
        if (
            not force
            and self._last_sample_at is not None
            and (now - self._last_sample_at) < WORKSPACE_SAMPLE_INTERVAL_SECONDS
        ):
            return
        self._last_sample_at = now
        self.samples += 1

        entries, truncated = self._scan()
        self.truncated = self.truncated or truncated
        self.design_dna_present = "design-dna.json" in entries
        self.app_tsx_present = "src/App.tsx" in entries

        fingerprint = self._fingerprint(entries)
        previous = self._last_fingerprint
        changed = [
            rel
            for rel, meta in entries.items()
            if self._prev_entries.get(rel) != meta
        ] if previous is not None else []
        self._last_fingerprint = fingerprint
        self._prev_entries = entries
        if previous is None:
            # The launch baseline establishes "before"; it is not a mutation.
            self.distinct_fingerprints = 1
        elif fingerprint != previous:
            self.distinct_fingerprints += 1
            self._record_mutation(now, changed)

        self._probe_artifacts(now)

    def _scan(self) -> Tuple[Dict[str, Tuple[int, int]], bool]:
        """Collect ``(relpath -> (size, mtime_ns))``. Stat only, never open."""
        entries: Dict[str, Tuple[int, int]] = {}
        root = self._workspace
        if root is None:
            return entries, False
        truncated = False
        dna = root / "design-dna.json"
        try:
            if dna.is_file():
                entries["design-dna.json"] = _stat_meta(os.stat(dna))
        except OSError:
            pass
        src = root / "src"
        try:
            is_dir = src.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            return entries, truncated
        try:
            walker = os.walk(src)
            for dirpath, dirnames, filenames in walker:
                dirnames[:] = [
                    name
                    for name in dirnames
                    if name != "node_modules" and not name.startswith(".")
                ]
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    if len(entries) >= WORKSPACE_SAMPLE_MAX_FILES:
                        truncated = True
                        break
                    full = Path(dirpath) / name
                    try:
                        entries[full.relative_to(root).as_posix()] = _stat_meta(
                            os.stat(full)
                        )
                    except (OSError, ValueError):
                        continue
                if truncated:
                    break
        except OSError:
            pass
        return entries, truncated

    @staticmethod
    def _fingerprint(entries: Mapping[str, Tuple[int, int]]) -> str:
        digest = hashlib.sha256()
        for rel in sorted(entries):
            size, mtime_ns = entries[rel]
            digest.update(f"{rel}\0{size}\0{mtime_ns}\n".encode("utf-8"))
        return digest.hexdigest()

    def _record_mutation(self, now: float, changed: Sequence[str]) -> None:
        self.source_mutation_count += 1
        offset = round(max(0.0, now - self._started_at), 1)
        if self.first_mutation_offset_seconds is None:
            self.first_mutation_offset_seconds = offset
        self.last_mutation_offset_seconds = offset
        for rel in changed:
            if rel in self._unique_mutated:
                continue
            self._unique_mutated.add(rel)
            if len(self._mutated_paths) < MAX_MUTATED_PATHS:
                self._mutated_paths.append(rel)

    def _probe_artifacts(self, now: float) -> None:
        """At most one probe call per sample; first True wins, never rewritten."""
        if self._probe is None:
            return
        try:
            complete = bool(self._probe())
        except Exception:
            self.probe_failed = True
            return
        self.complete_at_end = complete
        if complete and self.first_complete_offset_seconds is None:
            self.first_complete_offset_seconds = round(
                max(0.0, now - self._started_at), 1
            )

    def unique_source_files_mutated(self) -> int:
        return len(self._unique_mutated)

    def mutated_paths(self) -> List[str]:
        return list(self._mutated_paths)

    def receipt(self) -> Dict[str, Any]:
        """Bounded workspace + artifacts view for the forensic receipt."""
        return {
            "sample_interval_seconds": WORKSPACE_SAMPLE_INTERVAL_SECONDS,
            "samples": self.samples,
            "distinct_fingerprints": self.distinct_fingerprints,
            "source_mutation_count": self.source_mutation_count,
            "unique_source_files_mutated": self.unique_source_files_mutated(),
            "first_mutation_offset_seconds": self.first_mutation_offset_seconds,
            "last_mutation_offset_seconds": self.last_mutation_offset_seconds,
            "mutated_paths": self.mutated_paths(),
            "design_dna_present": self.design_dna_present,
            "app_tsx_present": self.app_tsx_present,
            "truncated": self.truncated,
            "artifacts": {
                "probe_available": self.probe_available,
                "probe_failed": self.probe_failed,
                "complete": self.complete_at_end,
                "first_complete_offset_seconds": self.first_complete_offset_seconds,
                "complete_at_end": self.complete_at_end,
            },
        }


def resolve_watchdog_policy(
    config: Optional[Mapping[str, Any]] = None,
) -> WatchdogPolicy:
    """Build a policy from optional config, falling back to the named constants.

    Invalid or non-positive values are ignored with a warning rather than
    resolved as "unbounded" — a bad config must never silently disable a bound.
    """
    if config is None:
        config = load_watchdog_config()
    values: Dict[str, float] = {}
    for key, default in _CONFIG_KEYS.items():
        raw = config.get(key) if isinstance(config, Mapping) else None
        if raw is None or isinstance(raw, bool):
            values[key] = default
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "frontend_watchdog.%s: invalid value %r; using default %s",
                key, raw, default,
            )
            values[key] = default
            continue
        if value != value or value <= 0:  # NaN or non-positive
            logger.warning(
                "frontend_watchdog.%s: non-positive value %r; using default %s",
                key, raw, default,
            )
            values[key] = default
            continue
        values[key] = value
    return WatchdogPolicy(
        idle_timeout_seconds=values["idle_timeout_seconds"],
        hard_max_runtime_seconds=values["hard_max_runtime_seconds"],
        max_single_operation_seconds=values["max_single_operation_seconds"],
        legacy_wallclock_seconds=values["legacy_wallclock_seconds"],
        startup_grace_seconds=values["startup_grace_seconds"],
    )


def load_watchdog_config() -> Dict[str, Any]:
    """Read ``website_builder.frontend_watchdog`` from the Website Builder config.

    Mirrors ``app.runtime.load_runtime_config``'s file lookup. Returns an empty
    mapping when the file is absent or unreadable, which simply leaves every
    named constant in force.
    """
    path = Path(__file__).parent.parent.parent / "config" / "default.yaml"
    try:
        if not path.is_file():
            return {}
        import yaml

        with path.open("r", encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle) or {}
        for key in _WATCHDOG_CONFIG_SECTION:
            if not isinstance(cfg, Mapping):
                return {}
            cfg = cfg.get(key) or {}
        return dict(cfg) if isinstance(cfg, Mapping) else {}
    except Exception:
        return {}


class _BoundedOutput:
    """Thread-safe bounded capture preserving head and tail.

    ``subprocess.run(capture_output=True)`` buffers a child's entire output in
    memory; this keeps the same head+tail diagnostics while making the bound
    real, so a pathological child cannot exhaust the supervisor.

    While the output fits it is kept verbatim. On the first write that would
    overflow, the accumulated text is frozen as the head and the tail starts
    rolling, so the retained size never exceeds
    ``MAX_CAPTURED_OUTPUT_CHARS`` regardless of how much the child emits.
    """

    def __init__(self, limit: int = OUTPUT_CAPTURE_LIMIT_CHARS) -> None:
        self._limit = limit
        self._tail_limit = _OUTPUT_TAIL_LIMIT_CHARS
        self._lock = threading.Lock()
        self._buf = ""
        self._head = ""
        self._tail = ""
        self._truncated = False

    def append(self, text: str) -> None:
        with self._lock:
            if not self._truncated:
                if len(self._buf) + len(text) <= self._limit:
                    self._buf += text
                    return
                self._truncated = True
                self._head = self._buf
                self._buf = ""
            self._tail = (self._tail + text)[-self._tail_limit:]

    def value(self) -> str:
        with self._lock:
            if not self._truncated:
                return self._buf
            return self._head + _OUTPUT_TRUNCATION_MARKER + self._tail


def _stat_meta(st: os.stat_result) -> Tuple[int, int]:
    """``(size, mtime_ns)`` for one stat result, portable to old filesystems."""
    mtime_ns = getattr(st, "st_mtime_ns", None)
    if mtime_ns is None:  # pragma: no cover - every supported platform has it
        mtime_ns = int(st.st_mtime * 1_000_000_000)
    return int(st.st_size), int(mtime_ns)


def _drain(stream: Any, sink: "_BoundedOutput") -> None:
    """Read one pipe to EOF into *sink*.

    Runs on its own thread so a chatty child can never deadlock the supervisor
    on a full pipe buffer.
    """
    try:
        if stream is None:
            return
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            sink.append(
                chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
            )
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _spawn_kwargs() -> Dict[str, Any]:
    """Process-group kwargs so the child's whole tree stays reachable.

    Mirrors the supervised-spawn precedent in ``cron/scheduler.py``.
    """
    kwargs: Dict[str, Any] = {"start_new_session": True}
    if sys.platform == "win32":
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags

            creationflags = windows_hide_flags()
        except Exception:
            creationflags = 0
        kwargs = {
            "creationflags": creationflags
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            "encoding": "utf-8",
            "errors": "replace",
        }
    return kwargs


def _read_progress_events(
    path: Path, invocation: FrontendInvocation, now: float
) -> None:
    """Drain new progress lines into *invocation*, discarding foreign ones.

    A line is accepted only when it is complete, parseable, and carries this
    exact invocation id. Anything else is skipped, including a partially written
    trailing line, which is left for the next poll.
    """
    try:
        if not path.is_file():
            return
        with path.open("rb") as handle:
            handle.seek(invocation.progress_offset)
            data = handle.read()
            if not data:
                return
            consumed = len(data)
            tail = data.rfind(b"\n")
            if tail == -1:
                return  # no complete line yet
            invocation.progress_offset += tail + 1
    except OSError:
        return

    for raw in data[: tail + 1].split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, Mapping):
            continue
        run_id = str(event.get("run_id") or "")
        if not run_id or run_id != invocation.invocation_id:
            # Another invocation's activity. It must never refresh this one.
            invocation.stale_event_count += 1
            continue
        if str(event.get("event") or "") == _CHANNEL_READY_EVENT:
            invocation.progress_channel_confirmed = True
            continue
        kind = str(event.get("kind") or _KIND_UNKNOWN)
        phase = str(event.get("phase") or _PHASE_ACTIVE)
        # Absent/invalid ``advance`` means True: a child built before this
        # distinction existed keeps refreshing the progress clock exactly as
        # before, so the watchdog can never be stricter than its predecessor
        # against an older producer.
        raw_advance = event.get("advance", True)
        advance = raw_advance if isinstance(raw_advance, bool) else True
        invocation.note_activity(now, kind, advance)
        invocation.note_operation(now, kind, phase)
        # Forensic detail is recorded after the liveness decision above, so it
        # can observe an event but can never influence whether it was accepted.
        # ``stream_diag`` is optional: absent (an older child) simply folds
        # nothing, and a malformed one is dropped by the total validator.
        invocation.note_event_detail(
            now,
            kind,
            phase,
            str(event.get("desc") or ""),
            advance=advance,
            stream_diag=normalize_stream_diag(event.get("stream_diag")),
        )


def _terminate_tree(
    proc: Any,
    grace_seconds: float,
    kill_tree: Callable[..., bool],
) -> Tuple[bool, bool]:
    """Terminate the whole invocation tree, escalating SIGTERM -> strongest.

    Returns ``(terminated, tree_kill_attempted)``. Only *this* invocation's own
    child pid is ever signalled; no unrelated Website Builder or Hermes process
    is touched.
    """
    attempted = False
    import signal as _signal

    if proc.poll() is not None:
        return True, attempted
    # ``None`` means "strongest" and is exactly ``kill_process_tree``'s own
    # default contract (POSIX: SIGKILL; Windows: sig is ignored and
    # ``taskkill /F /T`` is used). Naming signal.SIGKILL directly would raise
    # AttributeError on Windows, where the constant does not exist.
    for sig in (_signal.SIGTERM, None):
        if proc.poll() is not None:
            break
        attempted = True
        try:
            kill_tree(proc.pid, sig=sig)
        except TypeError:
            # Test doubles / older signature without the keyword.
            try:
                kill_tree(proc.pid)
            except Exception:
                logger.warning(
                    "process tree kill failed for pid %s", proc.pid, exc_info=True
                )
        except Exception:
            logger.warning(
                "process tree kill failed for pid %s", proc.pid, exc_info=True
            )
        if sig is not None:
            # Grace window before escalating to the strongest signal.
            try:
                proc.wait(timeout=grace_seconds)
            except Exception:
                pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=grace_seconds)
    except Exception:
        pass
    return True, attempted


def _log_activity(
    invocation: FrontendInvocation, now: float, policy: WatchdogPolicy
) -> None:
    """Rate-limited ``FRONTEND activity`` line.

    Never logs prompts, source, credentials, tokens, protected URLs, or model
    response bodies — only the event kind and how long the run had been idle.
    """
    last = invocation.last_logged_activity_at
    if last is not None and (now - last) < policy.activity_log_min_interval_seconds:
        return
    invocation.last_logged_activity_at = now
    logger.info(
        "FRONTEND activity invocation=%s type=%s idle_for=%.0fs",
        invocation.invocation_id,
        invocation.last_progress_kind or _KIND_UNKNOWN,
        invocation.no_progress_for(now),
    )


def _build_receipt(
    invocation: FrontendInvocation,
    sampler: WorkspaceSampler,
    policy: WatchdogPolicy,
    *,
    outcome: Optional[str],
    returncode: int,
    now: float,
    cancel_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the bounded ``frontend_forensics/3`` receipt.

    The receipt records what kind of activity happened and whether the workspace
    moved. It never records prompts, model responses, tool arguments, file
    contents, absolute paths, URLs, or credentials: descriptions are clamped
    and redacted, mutated paths are workspace-relative, and the receipt's own
    location is stored as a bare filename.

    *cancel_reason* is caller-supplied metadata only (which signal asked the run
    to stop); it is not read back by any decision.
    """
    started = invocation.started_at
    observed = sampler.receipt()
    return {
        "schema": FORENSICS_SCHEMA,
        "invocation_id": invocation.invocation_id,
        "project_id": invocation.project_id,
        "build_operation_id": invocation.build_operation_id,
        "pid": invocation.child_pid,
        "outcome": outcome,
        # Additive within ``/2``: lets an operator tell "an operator stopped
        # this" from "we gave up on this" without string-matching the outcome.
        # ``outcome`` already records the fact; these make it legible.
        "cancelled": outcome == OUTCOME_CANCELLED,
        "cancel_reason": cancel_reason if outcome == OUTCOME_CANCELLED else None,
        "returncode": int(returncode),
        "elapsed_seconds": round(max(0.0, now - started), 1),
        "idle_for_seconds": round(invocation.no_progress_for(now), 1),
        "activity_idle_for_seconds": round(invocation.no_activity_for(now), 1),
        "channel_confirmed": invocation.progress_channel_confirmed,
        "policy": {
            "idle": policy.idle_timeout_seconds,
            "hard": policy.hard_max_runtime_seconds,
            "max_single_operation": policy.max_single_operation_seconds,
        },
        "counters": {
            # ``*_started`` are PHASE-EVENT counts and can exceed the number of
            # logical calls (several descriptions classify as a model start; a
            # concurrent batch announces itself once for the whole batch). The
            # ``*_completed``/``model_completed`` figures and every ``tool_names``
            # entry are per-invocation, so they are the numbers to reason with.
            "model_started": invocation.model_started_count,
            "model_completed": invocation.model_completed_count,
            "tool_started": invocation.tool_started_count,
            "tool_completed": invocation.tool_completed_count,
            # Subset of ``tool_completed``: invocations the child reported as
            # failed. Without it a run whose tool calls were being refused is
            # indistinguishable from one that made them all.
            "tool_error_count": invocation.tool_error_count,
            "stream_active": invocation.stream_active_count,
            "unknown_active": invocation.unknown_activity_count,
            # ``advance`` + ``keepalive`` always equals ``total_progress_events``.
            # This is the discriminator the p18 investigation needed: a run can
            # be 100% active and 0% progressing, and these two separate that.
            "advance": invocation.advance_event_count,
            "keepalive": invocation.keepalive_event_count,
            "total_progress_events": invocation.progress_event_count,
            "stale_events_discarded": invocation.stale_event_count,
        },
        "activity": {
            "first_offset_seconds": (
                None
                if invocation.first_activity_at is None
                else round(max(0.0, invocation.first_activity_at - started), 1)
            ),
            "last_offset_seconds": round(
                max(0.0, invocation.last_activity_at - started), 1
            ),
            "last_progress_offset_seconds": round(
                max(0.0, invocation.last_progress_at - started), 1
            ),
            "longest_gap_seconds": round(invocation.longest_activity_gap_seconds, 1),
            "longest_forward_progress_gap_seconds": round(
                invocation.longest_forward_progress_gap_seconds, 1
            ),
            "last_kind": invocation.last_progress_kind,
            "last_phase": invocation.last_progress_phase,
            "active_operation_at_end": invocation.active_operation,
        },
        "workspace": {
            key: value for key, value in observed.items() if key != "artifacts"
        },
        "artifacts": observed["artifacts"],
        "stream_frames": {
            # Lower bounds: the transport coalesces active events, so a run's
            # true frame count is >= these. Attribution is exact — the shape on a
            # retained line is the shape that was firing.
            "shapes": dict(invocation.stream_shape_counts),
            "advancing_by_shape": dict(invocation.stream_shape_advance_counts),
            "api_modes": dict(invocation.api_mode_counts),
            "repeat_frame_count": invocation.repeat_frame_count,
            "mapping_frame_count": invocation.mapping_frame_count,
            "last_shape": invocation.last_stream_shape,
            "last_frame_index": invocation.stream_frame_index,
            "last_frame_seconds": invocation.stream_frame_seconds,
            "max_frame_index": invocation.stream_max_frame_index,
            "max_frame_seconds": invocation.stream_max_frame_seconds,
            "first_offset_seconds": invocation.stream_shape_first_offset,
            "last_offset_seconds": invocation.stream_shape_last_offset,
            "shapes_truncated": invocation.stream_shape_counts_truncated,
            "api_modes_truncated": invocation.api_mode_counts_truncated,
        },
        # One entry per COMPLETED invocation, so the values sum to
        # ``counters.tool_completed`` (modulo truncation) rather than to roughly
        # twice it. Names only — never arguments, by construction.
        "tool_names": dict(invocation.tool_name_counts),
        "tool_names_truncated": invocation.tool_name_counts_truncated,
        "recent_activity": [dict(entry) for entry in invocation.recent_activity],
        # Bare filename within the per-project diagnostics directory. The
        # receipt outlives runs/, so an absolute path would be both a leak and a
        # pointer at nothing by the time an operator reads it.
        "forensics_receipt": f"{invocation.invocation_id}.json",
    }


def _prune_receipts(directory: Path) -> None:
    """Keep only the newest :data:`MAX_RECEIPTS_PER_PROJECT` receipts."""
    try:
        receipts = [p for p in directory.glob("*.json") if p.is_file()]
    except OSError:
        return
    if len(receipts) <= MAX_RECEIPTS_PER_PROJECT:
        return
    try:
        receipts.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    except OSError:
        return
    for stale in receipts[MAX_RECEIPTS_PER_PROJECT:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _write_receipt(
    directory: Path, invocation_id: str, receipt: Mapping[str, Any]
) -> str:
    """Persist one receipt atomically. Returns the file name written.

    ``mkstemp`` + ``fsync`` + ``os.replace`` mirrors the in-repo atomic-write
    precedent in ``app/core/state.py``: a reader never sees a half-written
    receipt, and a crash mid-write leaves the previous one intact.
    """
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{invocation_id}.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, directory / name)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    _prune_receipts(directory)
    return name


def supervise_frontend_run(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    project_id: str,
    invocation_id: str,
    build_operation_id: str,
    progress_path: Path,
    policy: Optional[WatchdogPolicy] = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    spawn: Optional[Callable[..., Any]] = None,
    kill_tree: Optional[Callable[..., bool]] = None,
    terminate_grace_seconds: float = TERMINATE_GRACE_SECONDS,
    diagnostics_dir: Optional[Path] = None,
    workspace: Optional[Path] = None,
    artifacts_probe: Optional[Callable[[], bool]] = None,
    write_receipt: Optional[Callable[[Path, str, Mapping[str, Any]], str]] = None,
    run_canceller: Optional["FrontendRunCanceller"] = None,
) -> SupervisedRun:
    """Run *cmd* as one supervised, activity-aware invocation.

    *clock*, *sleep*, *spawn*, and *kill_tree* are injectable so the whole
    decision surface is testable with a fake clock and fake child, with no real
    waiting.

    *diagnostics_dir*, *workspace*, *artifacts_probe*, and *write_receipt* are
    the forensic-receipt seam. All four default to ``None``, so every existing
    caller and test runs exactly as before. When *diagnostics_dir* is set, a
    bounded receipt is written for EVERY terminal path — success, all three
    timeout codes, and operator cancellation — and a write failure is swallowed:
    a receipt must never change the supervision result.

    *run_canceller* is the operator-cancellation seam. It defaults to ``None``,
    so with no canceller supplied nothing can cancel this run and every existing
    caller and test behaves byte-for-byte as before. When one is supplied, this
    invocation registers itself on entry and is released on exit; a shutdown
    request only ever sets that invocation's own event, and this poll loop —
    the sole owner of ``proc`` — performs the teardown through the existing
    ``_terminate_tree`` escalation. The registry is also the single source of
    truth for *why* the run was stopped, which the receipt then records.
    """
    if kill_process_tree is None and kill_tree is None:
        raise WatchdogUnavailable(
            "whole-tree termination unavailable; refusing to supervise "
            f"({_KILL_TREE_ERROR})"
        )
    policy = policy or resolve_watchdog_policy()
    spawn = spawn or subprocess.Popen
    kill = kill_tree or kill_process_tree

    env = dict(env)
    env[PROGRESS_FILE_ENV] = str(progress_path)
    env[PROGRESS_ID_ENV] = invocation_id

    start = clock()
    # Registered BEFORE the child exists. Registering afterwards would leave a
    # window in which a live child is unreachable from a shutdown request —
    # which is exactly the orphan this seam exists to prevent.
    cancel_event = run_canceller.register(invocation_id) if run_canceller else None
    cancel_reason = ""
    try:
        proc = spawn(
            list(cmd),
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_spawn_kwargs(),
        )
    except BaseException:
        if run_canceller is not None:
            run_canceller.release(invocation_id)
        raise
    invocation = FrontendInvocation(
        invocation_id=invocation_id,
        project_id=project_id,
        build_operation_id=build_operation_id,
        started_at=start,
        # Until the first progress event the child has proved nothing, so both
        # the activity clock and the forward-progress clock start at launch
        # rather than at some later confirmation.
        last_activity_at=start,
        last_progress_at=start,
        child_pid=getattr(proc, "pid", None),
    )

    out_sink = _BoundedOutput()
    err_sink = _BoundedOutput()
    sampler = WorkspaceSampler(workspace, artifacts_probe, started_at=start)
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_sink), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_sink), daemon=True),
    ]
    for reader in readers:
        reader.start()

    logger.info(
        "FRONTEND started project=%s invocation=%s pid=%s",
        project_id, invocation_id, invocation.child_pid,
    )

    outcome: Optional[str] = None
    warned_unconfirmed = False
    try:
        while True:
            now = clock()
            _read_progress_events(progress_path, invocation, now)
            # Observation only. Sampled here so a run that dies between two
            # polls still leaves a sample, and strictly after the progress read
            # so the workspace can never gate the liveness decision below.
            sampler.sample(now)
            if invocation.progress_event_count and not outcome:
                _log_activity(invocation, now, policy)

            exited = proc.poll() is not None
            if not exited:
                elapsed = now - invocation.started_at
                # Operator cancellation is evaluated FIRST and outranks every
                # bound, so a run that is asked to stop is never reported as a
                # timeout that happened to fire on the same poll. It is still
                # below the ``exited`` check above: a child that already
                # finished reports its true result, not a cancellation.
                if cancel_event is not None and cancel_event.is_set():
                    outcome = OUTCOME_CANCELLED
                    # Read the reason NOW, while this invocation is still
                    # registered; the ``finally`` releases it below.
                    if run_canceller is not None:
                        cancel_reason = run_canceller.reason_for(invocation_id)
                elif not invocation.progress_channel_confirmed:
                    # Mandatory handshake not satisfied: we have no liveness
                    # signal for this child, so supervision degrades to the
                    # pre-watchdog wall-clock bound. Deliberately NOT a
                    # hard-fuse-only run, which would be far more permissive
                    # than what this replaced.
                    if (
                        not warned_unconfirmed
                        and elapsed >= policy.startup_grace_seconds
                    ):
                        warned_unconfirmed = True
                        logger.warning(
                            "FRONTEND progress channel unconfirmed for "
                            "project=%s invocation=%s pid=%s after %.0fs; "
                            "falling back to the %.0fs wall-clock bound",
                            project_id, invocation_id, invocation.child_pid,
                            elapsed, policy.legacy_wallclock_seconds,
                        )
                    if elapsed >= policy.legacy_wallclock_seconds:
                        outcome = OUTCOME_LEGACY_TIMEOUT
                else:
                    if elapsed >= policy.hard_max_runtime_seconds:
                        # Pathological-run fuse. Raw wall clock, independent of
                        # both clocks above: a continuously-advancing run still
                        # ends here and only here.
                        outcome = OUTCOME_HARD_TIMEOUT
                    else:
                        no_progress = invocation.no_progress_for(now)
                        in_flight = invocation.is_in_flight(
                            now, policy.max_single_operation_seconds
                        )
                        # ``max_single_operation_seconds`` is the maximum
                        # duration of no genuine forward progress while an
                        # operation is in flight — NOT a maximum operation
                        # duration. An advancing stream is never cut here; an
                        # operation whose only output is keep-alive activity is,
                        # once its progress has been silent for that long.
                        if no_progress >= policy.idle_timeout_seconds and not in_flight:
                            outcome = OUTCOME_IDLE_TIMEOUT

            if outcome is not None or exited:
                break
            sleep(policy.poll_interval_seconds)

        returncode = proc.poll()
        tree_kill_attempted = False
        if returncode is None:
            # A bound fired or cancellation was requested: tear down the whole
            # tree through the one existing escalation, then take the code.
            _, tree_kill_attempted = _terminate_tree(
                proc, terminate_grace_seconds, kill
            )
            returncode = proc.poll()
        if returncode is None:
            try:
                returncode = proc.wait(timeout=terminate_grace_seconds)
            except Exception:
                returncode = -1
    finally:
        # Release first: the run is over, so a later shutdown signal must not be
        # able to target it. Cheap and non-blocking, so doing it here cannot
        # delay the teardown that precedes it.
        if run_canceller is not None:
            run_canceller.release(invocation_id)
        # A surviving grandchild can hold the inherited pipe write end open, so
        # a reader may still be blocked. Bound the join and rely on the daemon
        # flag: no pipe handle or thread may outlive this call.
        for reader in readers:
            reader.join(timeout=2.0)
        try:
            progress_path.unlink()
        except OSError:
            pass

    end = clock()
    # One last sample so the receipt describes the end state (and the final
    # artifact completeness) even for a run that ended between two intervals.
    sampler.sample(end, force=True)

    # Built unconditionally: the counters cost nothing to keep and the durable
    # channel at the adapter carries whatever lands in the diagnostics dict, so
    # the block must not depend on whether a file was requested.
    receipt: Optional[Dict[str, Any]] = None
    receipt_path: Optional[str] = None
    try:
        receipt = _build_receipt(
            invocation,
            sampler,
            policy,
            outcome=outcome,
            returncode=int(returncode),
            now=end,
            cancel_reason=cancel_reason,
        )
    except Exception:
        logger.warning(
            "FRONTEND forensic receipt build failed for invocation=%s",
            invocation_id, exc_info=True,
        )
    if receipt is not None and diagnostics_dir is not None:
        try:
            name = (write_receipt or _write_receipt)(
                Path(diagnostics_dir), invocation_id, receipt
            )
            receipt["forensics_receipt"] = name
            receipt_path = str(Path(diagnostics_dir) / name)
        except Exception:
            # A failed receipt must never change the supervision result.
            logger.warning(
                "FRONTEND forensic receipt write failed for invocation=%s",
                invocation_id, exc_info=True,
            )

    if outcome == OUTCOME_IDLE_TIMEOUT:
        logger.warning(
            "FRONTEND idle-timeout project=%s invocation=%s pid=%s",
            project_id, invocation_id, invocation.child_pid,
        )
    elif outcome == OUTCOME_HARD_TIMEOUT:
        logger.warning(
            "FRONTEND hard-timeout project=%s invocation=%s pid=%s",
            project_id, invocation_id, invocation.child_pid,
        )
    elif outcome == OUTCOME_LEGACY_TIMEOUT:
        logger.warning(
            "FRONTEND legacy-timeout project=%s invocation=%s pid=%s "
            "(no progress channel; fell back to wall-clock bound)",
            project_id, invocation_id, invocation.child_pid,
        )
    elif outcome == OUTCOME_CANCELLED:
        # Never reported as a completion: the child did NOT finish on its own.
        logger.info(
            "FRONTEND cancelled project=%s invocation=%s pid=%s reason=%s",
            project_id, invocation_id, invocation.child_pid,
            cancel_reason or "unspecified",
        )
    else:
        logger.info(
            "FRONTEND completed project=%s invocation=%s elapsed=%.0fs",
            project_id, invocation_id, end - invocation.started_at,
        )

    diagnostics = invocation.diagnostics(end, forensics=receipt)
    # Absolute location is useful to an operator holding this in memory; it is
    # deliberately kept OUT of the receipt file itself.
    diagnostics["forensics_receipt"] = receipt_path

    return SupervisedRun(
        returncode=int(returncode),
        stdout=out_sink.value(),
        stderr=err_sink.value(),
        outcome=outcome,
        diagnostics=diagnostics,
        terminated=outcome is not None,
        tree_kill_attempted=tree_kill_attempted,
    )
