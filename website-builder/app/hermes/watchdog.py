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

Forensic receipts (observation only)
------------------------------------
A timeout code says a bound fired; it never says *which* non-termination mode
occurred. The in-memory :meth:`FrontendInvocation.diagnostics` alone cannot
answer that: it keeps one ``last_progress_kind`` and one event scalar, so the
kind/phase distribution is discarded and nothing about the workspace is
recorded. Every supervised run therefore also emits a bounded
``frontend_forensics/1`` receipt (see :func:`_build_receipt`) that carries the
kind/phase counters, the longest true silence between two progress signals, a
metadata-only workspace change fingerprint, artifact completeness, and the
tool names seen.

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
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from hermes_cli.oneshot import PROGRESS_FILE_ENV, PROGRESS_ID_ENV

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
#: Measured from the last progress event, never from operation start, so any
#: valid stream/progress event keeps refreshing liveness. This only fires for an
#: operation that has genuinely gone silent.
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

#: Most recent normalized activity events retained for the receipt.
RECENT_ACTIVITY_LEN = 20

#: Receipts retained per project directory before the oldest are pruned.
MAX_RECEIPTS_PER_PROJECT = 20

#: Receipt schema identifier. Bump on any incompatible field change.
FORENSICS_SCHEMA = "frontend_forensics/1"

#: Watchdog outcome codes.
OUTCOME_IDLE_TIMEOUT = "FRONTEND_IDLE_TIMEOUT"
OUTCOME_HARD_TIMEOUT = "FRONTEND_HARD_TIMEOUT"
OUTCOME_LEGACY_TIMEOUT = "FRONTEND_LEGACY_TIMEOUT"

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
_TOOL_DESC_PREFIXES = ("executing tool: ", "tool completed: ")

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


class WatchdogUnavailable(RuntimeError):
    """Whole-tree termination is unavailable; supervision must not be attempted."""


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
    child_pid: Optional[int] = None
    active_operation: Optional[str] = None
    active_operation_started_at: Optional[float] = None
    progress_channel_confirmed: bool = False
    progress_offset: int = 0
    last_progress_kind: Optional[str] = None
    unknown_activity_count: int = 0
    progress_event_count: int = 0
    stale_event_count: int = 0
    last_logged_activity_at: Optional[float] = None
    # --- Forensic detail. Additive only: nothing above reads any of it, and
    # --- no bound below consults it.
    last_progress_phase: Optional[str] = None
    model_started_count: int = 0
    model_completed_count: int = 0
    tool_started_count: int = 0
    tool_completed_count: int = 0
    stream_active_count: int = 0
    first_activity_at: Optional[float] = None
    #: Longest true silence between two accepted progress signals, measured
    #: across EVERY accepted event including UNKNOWN and STREAM. This is the
    #: discriminator between continuously-active work and in-flight-protected
    #: silence: in-flight protection suppresses the idle bound for up to
    #: ``max_single_operation_seconds``, so a hard-timeout run can still contain
    #: multi-minute gaps.
    longest_activity_gap_seconds: float = 0.0
    recent_activity: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=RECENT_ACTIVITY_LEN)
    )
    tool_name_counts: Dict[str, int] = field(default_factory=dict)
    tool_name_counts_truncated: bool = False

    def note_activity(self, now: float, kind: str) -> None:
        """Record progress from THIS invocation. Refreshes liveness.

        Every accepted event refreshes liveness, including ``UNKNOWN``: an
        unclassified activity stamp still proves the child is alive, and
        refusing to count it would let an unrecognised (but healthy) activity
        description look like a hang. Unknown events are counted separately for
        observability instead.

        The gap since the previous accepted event is measured *before*
        ``last_activity_at`` moves, so ``longest_activity_gap_seconds`` is the
        real silence between any two progress signals. Liveness itself is
        unchanged: ``last_activity_at`` is still set to *now*.
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

    def note_event_detail(self, now: float, kind: str, phase: str, desc: str) -> None:
        """Fold one accepted event into the forensic counters and ring buffer.

        Called after :meth:`note_activity` and :meth:`note_operation` so the
        liveness decision has already been made; this only records.
        """
        self.last_progress_phase = phase
        if kind == _KIND_MODEL:
            if phase == _PHASE_STARTED:
                self.model_started_count += 1
            elif phase == _PHASE_COMPLETED:
                self.model_completed_count += 1
        elif kind == _KIND_TOOL:
            if phase == _PHASE_STARTED:
                self.tool_started_count += 1
            elif phase == _PHASE_COMPLETED:
                self.tool_completed_count += 1
        elif kind == _KIND_STREAM and phase == _PHASE_ACTIVE:
            self.stream_active_count += 1

        # The tool name is parsed from the RAW description (before redaction)
        # because a name is not an argument; only the clamped, redacted form is
        # ever stored.
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

    def note_operation(self, now: float, kind: str, phase: str) -> None:
        """Track the in-flight operation from typed boundary events only.

        An unclassified event deliberately neither claims nor clears an
        operation, so a description this build does not recognise can never
        cause a premature termination.
        """
        if phase == _PHASE_STARTED:
            if self.active_operation != kind:
                self.active_operation = kind
                self.active_operation_started_at = now
        elif phase == _PHASE_COMPLETED:
            self.active_operation = None
            self.active_operation_started_at = None

    def no_progress_for(self, now: float) -> float:
        return max(0.0, now - self.last_activity_at)

    def is_in_flight(self, now: float, max_single_operation_seconds: float) -> bool:
        """True when an operation is in flight AND has recently been heard from.

        The no-progress backstop bounds how long an operation may stay silent
        regardless of being "in flight"; it is measured from the last progress
        event, so legitimate streaming keeps an operation protected.
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
        """
        data = {
            "invocation_id": self.invocation_id,
            "project_id": self.project_id,
            "build_operation_id": self.build_operation_id,
            "pid": self.child_pid,
            "elapsed_seconds": round(max(0.0, now - self.started_at), 1),
            "idle_for_seconds": round(self.no_progress_for(now), 1),
            "active_operation": self.active_operation,
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
        invocation.note_activity(now, kind)
        invocation.note_operation(now, kind, phase)
        # Forensic detail is recorded after the liveness decision above, so it
        # can observe an event but can never influence whether it was accepted.
        invocation.note_event_detail(now, kind, phase, str(event.get("desc") or ""))


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
) -> Dict[str, Any]:
    """Assemble the bounded ``frontend_forensics/1`` receipt.

    The receipt records what kind of activity happened and whether the workspace
    moved. It never records prompts, model responses, tool arguments, file
    contents, absolute paths, URLs, or credentials: descriptions are clamped
    and redacted, mutated paths are workspace-relative, and the receipt's own
    location is stored as a bare filename.
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
        "returncode": int(returncode),
        "elapsed_seconds": round(max(0.0, now - started), 1),
        "idle_for_seconds": round(invocation.no_progress_for(now), 1),
        "channel_confirmed": invocation.progress_channel_confirmed,
        "policy": {
            "idle": policy.idle_timeout_seconds,
            "hard": policy.hard_max_runtime_seconds,
            "max_single_operation": policy.max_single_operation_seconds,
        },
        "counters": {
            "model_started": invocation.model_started_count,
            "model_completed": invocation.model_completed_count,
            "tool_started": invocation.tool_started_count,
            "tool_completed": invocation.tool_completed_count,
            "stream_active": invocation.stream_active_count,
            "unknown_active": invocation.unknown_activity_count,
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
            "longest_gap_seconds": round(invocation.longest_activity_gap_seconds, 1),
            "last_kind": invocation.last_progress_kind,
            "last_phase": invocation.last_progress_phase,
            "active_operation_at_end": invocation.active_operation,
        },
        "workspace": {
            key: value for key, value in observed.items() if key != "artifacts"
        },
        "artifacts": observed["artifacts"],
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
) -> SupervisedRun:
    """Run *cmd* as one supervised, activity-aware invocation.

    *clock*, *sleep*, *spawn*, and *kill_tree* are injectable so the whole
    decision surface is testable with a fake clock and fake child, with no real
    waiting.

    *diagnostics_dir*, *workspace*, *artifacts_probe*, and *write_receipt* are
    the forensic-receipt seam. All four default to ``None``, so every existing
    caller and test runs exactly as before. When *diagnostics_dir* is set, a
    bounded receipt is written for EVERY terminal path — success and all three
    timeout codes — and a write failure is swallowed: a receipt must never
    change the supervision result.
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
    proc = spawn(
        list(cmd),
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_spawn_kwargs(),
    )
    invocation = FrontendInvocation(
        invocation_id=invocation_id,
        project_id=project_id,
        build_operation_id=build_operation_id,
        started_at=start,
        # Until the first progress event the child has proved nothing, so the
        # idle clock starts at launch rather than at some later confirmation.
        last_activity_at=start,
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
                if not invocation.progress_channel_confirmed:
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
                        outcome = OUTCOME_HARD_TIMEOUT
                    else:
                        no_progress = invocation.no_progress_for(now)
                        in_flight = invocation.is_in_flight(
                            now, policy.max_single_operation_seconds
                        )
                        if no_progress >= policy.idle_timeout_seconds and not in_flight:
                            outcome = OUTCOME_IDLE_TIMEOUT

            if outcome is not None or exited:
                break
            sleep(policy.poll_interval_seconds)

        returncode = proc.poll()
        tree_kill_attempted = False
        if returncode is None:
            # Timed out: tear down the whole tree, then take the code.
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
