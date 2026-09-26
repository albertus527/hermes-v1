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
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from hermes_cli.oneshot import PROGRESS_FILE_ENV, PROGRESS_ID_ENV

try:  # pragma: no cover - exercised wherever the Hermes repo root is importable
    from agent.deadline import kill_process_tree

    _KILL_TREE_ERROR: Optional[str] = None
except ImportError as exc:  # pragma: no cover - stripped env
    kill_process_tree = None  # type: ignore[assignment]
    _KILL_TREE_ERROR = str(exc)

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
_KIND_UNKNOWN = "UNKNOWN"

_WATCHDOG_CONFIG_SECTION = ("website_builder", "frontend_watchdog")
_CONFIG_KEYS = {
    "idle_timeout_seconds": FRONTEND_IDLE_TIMEOUT_SECONDS,
    "hard_max_runtime_seconds": FRONTEND_HARD_MAX_RUNTIME_SECONDS,
    "max_single_operation_seconds": FRONTEND_MAX_SINGLE_OPERATION_SECONDS,
    "legacy_wallclock_seconds": FRONTEND_LEGACY_WALLCLOCK_SECONDS,
    "startup_grace_seconds": PROGRESS_CHANNEL_STARTUP_GRACE_SECONDS,
}


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

    def note_activity(self, now: float, kind: str) -> None:
        """Record progress from THIS invocation. Refreshes liveness.

        Every accepted event refreshes liveness, including ``UNKNOWN``: an
        unclassified activity stamp still proves the child is alive, and
        refusing to count it would let an unrecognised (but healthy) activity
        description look like a hang. Unknown events are counted separately for
        observability instead.
        """
        self.last_activity_at = now
        self.last_progress_kind = kind
        self.progress_event_count += 1
        if kind == _KIND_UNKNOWN:
            self.unknown_activity_count += 1

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

    def diagnostics(self, now: float) -> Dict[str, Any]:
        """Bounded operator metadata. Never includes prompts or output bodies."""
        return {
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
        phase = str(event.get("phase") or "active")
        invocation.note_activity(now, kind)
        invocation.note_operation(now, kind, phase)


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
) -> SupervisedRun:
    """Run *cmd* as one supervised, activity-aware invocation.

    *clock*, *sleep*, *spawn*, and *kill_tree* are injectable so the whole
    decision surface is testable with a fake clock and fake child, with no real
    waiting.
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

    return SupervisedRun(
        returncode=int(returncode),
        stdout=out_sink.value(),
        stderr=err_sink.value(),
        outcome=outcome,
        diagnostics=invocation.diagnostics(end),
        terminated=outcome is not None,
        tree_kill_attempted=tree_kill_attempted,
    )
