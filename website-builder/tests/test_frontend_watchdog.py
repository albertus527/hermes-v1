"""Concurrency and supervision tests for the FRONTEND activity-aware watchdog.

Every test drives a fake monotonic clock, a fake child process, and a scripted
progress stream, so the whole decision surface is exercised with no real
waiting. The single real-process test (``test_timeout_leaves_no_orphan_tree``)
is the exception and is marked as such.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

from app.hermes import watchdog as wd

#: Sentinel for "the caller did not pass a stream_diag at all", so a test can
#: pass ``None``/junk and mean it.
_ABSENT = object()


# ---------------------------------------------------------------------------
# Fake clock, fake child, scripted progress stream.
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class FakeChild:
    """Minimal Popen stand-in with a scripted exit time."""

    _next_pid = 41000

    def __init__(self, clock: FakeClock, exit_at=None, stdout: str = "", stderr: str = ""):
        FakeChild._next_pid += 1
        self.pid = FakeChild._next_pid
        self._clock = clock
        self._exit_at = exit_at
        self._done = False
        self.killed = False
        self.wait_calls = 0
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)

    def poll(self):
        if self._done:
            return 0
        if self._exit_at is not None and self._clock.now >= self._exit_at:
            self._done = True
            return 0
        return None

    def wait(self, timeout=None):
        self.wait_calls += 1
        self._done = True
        return 0

    def kill(self):
        self.killed = True
        self._done = True


class ProgressScript:
    """Writes JSONL progress lines to a file at scheduled clock times."""

    def __init__(self, path: Path, clock: FakeClock) -> None:
        self.path = path
        self.clock = clock
        self.schedule: dict = {}
        self.fd = os.open(
            str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )

    def at(
        self,
        when: float,
        run_id: str,
        kind: str,
        phase: str,
        desc: Optional[str] = None,
        *,
        advance: Optional[bool] = None,
        stream_diag: object = _ABSENT,
    ) -> "ProgressScript":
        # ``desc`` defaults to the old synthetic label so every existing test is
        # unchanged; forensic tests pass a real emitter description.
        #
        # ``advance`` is OMITTED from the line when None, on purpose: that is how
        # an older producer's line looks, and the watchdog must keep treating it
        # as forward progress. Tests that need a keep-alive pass False.
        #
        # ``stream_diag`` is OMITTED unless supplied — including when it is
        # supplied as something malformed, which is how a producer that sends
        # junk (or a hostile one) reaches the reader.
        self.schedule.setdefault(when, []).append(
            (
                run_id,
                kind,
                phase,
                f"{kind.lower()}:{phase}" if desc is None else desc,
                advance,
                stream_diag,
            )
        )
        return self

    def ready(self, when: float, run_id: str) -> "ProgressScript":
        return self.at(when, run_id, "channel_ready", "ready", desc="")

    def flush(self) -> None:
        due = self.schedule.pop(self.clock.now, None)
        for run_id, kind, phase, desc, advance, stream_diag in due or []:
            event = {
                "run_id": run_id,
                "event": "channel_ready" if kind == "channel_ready" else "progress",
                "kind": kind,
                "phase": phase,
                "desc": desc,
            }
            if advance is not None:
                event["advance"] = advance
            if stream_diag is not _ABSENT:
                event["stream_diag"] = stream_diag
            line = json.dumps(event, separators=(",", ":")) + "\n"
            os.write(self.fd, line.encode("utf-8"))

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class Harness:
    """Runs ``supervise_frontend_run`` against a fake clock and fake child."""

    def __init__(
        self,
        tmp_path: Path,
        policy: wd.WatchdogPolicy,
        progress_path: Path = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.policy = policy
        self.clock = FakeClock()
        self.progress_path = progress_path or tmp_path / "progress.jsonl"
        self.script = ProgressScript(self.progress_path, self.clock)
        self.child: FakeChild = None  # type: ignore[assignment]
        self.killed: list = []
        self.spawn_kwargs: dict = {}

    def _sleep(self, delta: float) -> None:
        upcoming = [
            t for t in self.script.schedule if self.clock.now < t <= self.clock.now + delta
        ]
        if upcoming:
            self.clock.now = min(upcoming)
        else:
            self.clock.now += delta
        self.script.flush()

    def _spawn(self, cmd, **kwargs):
        self.spawn_kwargs = kwargs
        self.child = FakeChild(
            self.clock,
            exit_at=self._exit_at,
            stdout=self._stdout,
            stderr=self._stderr,
        )
        return self.child

    def _kill_tree(self, pid, *, sig=None) -> bool:
        self.killed.append((pid, sig))
        if self.child is not None:
            self.child.kill()
        return True

    def run(
        self,
        invocation_id: str,
        project_id: str = "proj",
        build_operation_id: str = "1",
        exit_at=None,
        stdout: str = "",
        stderr: str = "",
        diagnostics_dir=None,
        workspace=None,
        artifacts_probe=None,
        write_receipt=None,
    ):
        self._exit_at = exit_at
        self._stdout = stdout
        self._stderr = stderr
        # The supervisor reads the stream before its first sleep, so anything
        # scheduled at t=0 (notably the channel_ready handshake) must already be
        # on disk when it starts.
        self.script.flush()
        return wd.supervise_frontend_run(
            ["python", "-m", "hermes_cli.main", "-z", "build"],
            cwd=self.tmp_path,
            env={},
            project_id=project_id,
            invocation_id=invocation_id,
            build_operation_id=build_operation_id,
            progress_path=self.progress_path,
            policy=self.policy,
            clock=self.clock,
            sleep=self._sleep,
            spawn=self._spawn,
            kill_tree=self._kill_tree,
            diagnostics_dir=diagnostics_dir,
            workspace=workspace,
            artifacts_probe=artifacts_probe,
            write_receipt=write_receipt,
        )


def fast_policy(**overrides) -> wd.WatchdogPolicy:
    """Policy with small bounds so tests stay readable."""
    base = dict(
        idle_timeout_seconds=180.0,
        hard_max_runtime_seconds=2700.0,
        max_single_operation_seconds=900.0,
        legacy_wallclock_seconds=900.0,
        startup_grace_seconds=60.0,
        poll_interval_seconds=1.0,
        activity_log_min_interval_seconds=30.0,
    )
    base.update(overrides)
    return wd.WatchdogPolicy(**base)


@pytest.fixture
def harness(tmp_path):
    made = Harness(tmp_path, fast_policy())
    yield made
    made.script.close()


# ---------------------------------------------------------------------------
# The headline requirement: activity from one run must never refresh another.
# ---------------------------------------------------------------------------


def test_other_runs_activity_cannot_refresh_this_watchdog(harness):
    """Run A goes idle while Run B keeps working; A must still be terminated.

    Both runs write into the SAME progress file, so this exercises the real
    hazard: B's events are physically present when the supervisor evaluates A.
    Only the per-invocation id may refresh A's clock, and B's id differs.
    """
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started")
    for t in (60, 120, 180, 240, 300, 360, 420, 480, 540, 600, 660, 720, 780, 840):
        harness.script.at(t, "inv-B", "TOOL", "started").at(t, "inv-B", "TOOL", "completed")

    run_a = harness.run("inv-A", project_id="p10", exit_at=None)

    assert run_a.outcome == wd.OUTCOME_IDLE_TIMEOUT
    # B was writing every 60s into the same file the whole time.
    assert run_a.diagnostics["stale_event_count"] > 0
    assert run_a.diagnostics["progress_channel_confirmed"] is True


def test_busy_run_survives_while_the_idle_run_is_killed(harness):
    """B's continuous activity keeps B alive past A's idle deadline."""
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started")
    for t in range(60, 700, 60):
        harness.script.at(t, "inv-B", "TOOL", "started")
        harness.script.at(t, "inv-B", "TOOL", "completed")

    run_b = harness.run("inv-B", project_id="p11", exit_at=650)

    assert run_b.outcome is None
    assert run_b.returncode == 0
    assert harness.killed == []


def test_two_invocations_of_the_same_project_do_not_share_a_watchdog(harness):
    """Same project, different operation ids -> still isolated."""
    harness.script.ready(0, "inv-1").at(0, "inv-1", "MODEL", "started")
    for t in range(30, 400, 30):
        harness.script.at(t, "inv-2", "STREAM", "active")

    run = harness.run("inv-1", project_id="same-proj", build_operation_id="1", exit_at=None)

    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.diagnostics["build_operation_id"] == "1"


# ---------------------------------------------------------------------------
# Wall-clock cap removal, in-flight handling, and the hard fuse.
# ---------------------------------------------------------------------------


def test_continuous_progress_may_exceed_the_old_900s_cap(harness):
    """The pre-watchdog 900s wall clock no longer terminates a healthy run."""
    harness.script.ready(0, "inv-A")
    for t in range(0, 1500, 60):
        harness.script.at(t, "inv-A", "TOOL", "started")
        harness.script.at(t, "inv-A", "TOOL", "completed")

    run = harness.run("inv-A", exit_at=1450)

    assert run.outcome is None
    assert run.diagnostics["elapsed_seconds"] > 900


def test_long_single_operation_is_not_idle(harness):
    """A model request longer than idle_timeout is in flight, not idle."""
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started")

    run = harness.run("inv-A", exit_at=400)

    assert run.outcome is None
    assert run.diagnostics["active_operation"] == "MODEL"


def test_stuck_operation_is_cut_off_by_the_no_progress_backstop(harness):
    """max_single_operation_seconds is a no-progress backstop."""
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started")

    run = harness.run("inv-A", exit_at=None)

    # In flight, but silent for the full backstop -> idle-family termination.
    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.diagnostics["elapsed_seconds"] == pytest.approx(900.0, abs=2)


def test_valid_progress_re_protects_a_long_running_operation(harness):
    """Any progress event refreshes liveness, so a slow op is never cut off."""
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started")
    for t in range(60, 1600, 60):
        harness.script.at(t, "inv-A", "STREAM", "active")

    run = harness.run("inv-A", exit_at=1500)

    assert run.outcome is None
    assert run.diagnostics["elapsed_seconds"] > 900


def test_unknown_activity_still_refreshes_liveness_and_is_counted(harness):
    """Unclassified activity proves the child is alive; it is also observable."""
    harness.script.ready(0, "inv-A")
    for t in range(60, 1000, 60):
        harness.script.at(t, "inv-A", "UNKNOWN", "active")

    run = harness.run("inv-A", exit_at=950)

    assert run.outcome is None
    assert run.diagnostics["unknown_activity_count"] > 5


def test_hard_ceiling_terminates_a_pathological_run(harness):
    """Continuous progress still ends at the independent hard ceiling."""
    harness.script.ready(0, "inv-A")
    for t in range(0, 3000, 30):
        harness.script.at(t, "inv-A", "TOOL", "started")
        harness.script.at(t, "inv-A", "TOOL", "completed")

    run = harness.run("inv-A", exit_at=None)

    assert run.outcome == wd.OUTCOME_HARD_TIMEOUT
    # Never reported as an idle/stall failure.
    assert run.outcome != wd.OUTCOME_IDLE_TIMEOUT
    assert run.diagnostics["elapsed_seconds"] == pytest.approx(2700.0, abs=2)


# ---------------------------------------------------------------------------
# Observable activity vs. forward progress.
#
# ``advance=False`` events prove the child is alive and say nothing about
# whether it is getting anywhere. The bounds read the forward-progress clock,
# so a keep-alive-only run ends even though it never stops emitting events.
#
# The bound interaction these tests pin down: the idle branch requires
# ``not in_flight``, and ``is_in_flight`` holds while
# ``no_progress_for(now) < max_single_operation_seconds``. So a keep-alive-only
# run WITH a claimed operation is protected until the 900s backstop, and one
# with NO claimed operation is cut at the 180s idle bound.
# ---------------------------------------------------------------------------


def test_keepalives_do_not_refresh_forward_progress_and_are_cut_at_the_backstop(
    harness,
):
    """The p18 shape: an operation re-announced forever, never progressing.

    A genuine MODEL operation is claimed at t=0, then only keep-alive events
    follow — the 30s poll heartbeat (which classifies as MODEL/started) and
    ping-only stream frames. Under the previous behaviour each of those
    refreshed liveness and only the 45-minute fuse ended the run.
    """
    harness.script.ready(0, "inv-A").at(
        0, "inv-A", "MODEL", "started", desc="starting API call #1", advance=True
    )
    for t in range(30, 1500, 30):
        harness.script.at(
            t,
            "inv-A",
            "MODEL",
            "started",
            desc=f"waiting for stream response ({t}s, no chunks yet)",
            advance=False,
        )
        harness.script.at(
            t, "inv-A", "STREAM", "active", desc="receiving stream response",
            advance=False,
        )

    run = harness.run("inv-A", exit_at=None)

    # Cut by the no-progress backstop, NOT at the 180s idle bound: the in-flight
    # MODEL operation suppresses the idle branch until the backstop elapses too.
    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.diagnostics["active_operation"] == "MODEL"
    assert run.diagnostics["elapsed_seconds"] == pytest.approx(
        harness.policy.max_single_operation_seconds, abs=2
    )
    assert run.diagnostics["elapsed_seconds"] > harness.policy.idle_timeout_seconds
    assert run.diagnostics["keepalive_event_count"] > 20
    assert run.diagnostics["advance_event_count"] == 1
    # The activity clock never went quiet; only the progress clock did.
    assert run.diagnostics["activity_idle_for_seconds"] < 5.0
    assert run.diagnostics["idle_for_seconds"] >= (
        harness.policy.max_single_operation_seconds - 2
    )


def test_keepalive_re_announcement_does_not_restart_the_operation(harness):
    """Same-kind re-announcements neither clear nor re-arm the operation."""
    harness.script.ready(0, "inv-A").at(
        0, "inv-A", "MODEL", "started", desc="starting API call #1", advance=True
    )
    for t in range(30, 1500, 30):
        harness.script.at(
            t, "inv-A", "MODEL", "started",
            desc=f"waiting for stream response ({t}s, no chunks yet)",
            advance=False,
        )

    run = harness.run("inv-A", exit_at=None)

    # A poll heartbeat can never restart the operation's start timestamp, which
    # is what would make an in-flight operation un-terminable.
    assert run.diagnostics["active_operation_started_at"] == pytest.approx(
        0.0, abs=2
    )


def test_keepalive_only_run_without_an_operation_is_cut_at_the_idle_bound(harness):
    """No claimed operation => the 180s idle bound is the one that fires."""
    harness.script.ready(0, "inv-A")
    for t in range(30, 1500, 30):
        harness.script.at(
            t, "inv-A", "STREAM", "active", desc="receiving stream response",
            advance=False,
        )

    run = harness.run("inv-A", exit_at=None)

    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.diagnostics["active_operation"] is None
    assert run.diagnostics["elapsed_seconds"] == pytest.approx(
        harness.policy.idle_timeout_seconds, abs=2
    )


def test_genuine_progress_is_never_cut_at_any_duration(harness):
    """``max_single_operation_seconds`` is a no-progress backstop, not a cap.

    An operation that keeps advancing past the backstop and past the old 900s
    wall clock is never terminated. Only the hard elapsed fuse can end it.
    """
    harness.script.ready(0, "inv-A").at(
        0, "inv-A", "MODEL", "started", desc="starting API call #1", advance=True
    )
    for t in range(60, 2100, 60):
        harness.script.at(
            t, "inv-A", "STREAM", "active", desc="receiving stream response",
            advance=True,
        )

    run = harness.run("inv-A", exit_at=2000)

    assert run.outcome is None
    assert run.diagnostics["elapsed_seconds"] > 1900
    assert run.diagnostics["elapsed_seconds"] > (
        harness.policy.max_single_operation_seconds + 1000
    )


def test_progress_line_without_an_advance_key_still_advances(harness):
    """Back-compat: an older producer's line must not be demoted."""
    harness.script.ready(0, "inv-A").at(
        0, "inv-A", "MODEL", "started", desc="starting API call #1"
    )
    for t in range(60, 1500, 60):
        harness.script.at(t, "inv-A", "STREAM", "active")

    run = harness.run("inv-A", exit_at=1450)

    assert run.outcome is None
    assert run.diagnostics["keepalive_event_count"] == 0
    assert run.diagnostics["advance_event_count"] == run.diagnostics[
        "progress_event_count"
    ]


def test_receipt_separates_advancing_from_keepalive_events(harness, tmp_path):
    """The counters a reader needs to tell "busy" from "progressing"."""
    harness.script.ready(0, "inv-A").at(
        0, "inv-A", "MODEL", "started", desc="starting API call #1", advance=True
    )
    for t in range(30, 700, 30):
        harness.script.at(
            t, "inv-A", "STREAM", "active", desc="receiving stream response",
            advance=False,
        )
    harness.script.at(700, "inv-A", "MODEL", "completed", desc="API call #1 completed")

    run = harness.run(
        "inv-A",
        exit_at=760,
        diagnostics_dir=tmp_path / "diag",
        workspace=tmp_path,
        artifacts_probe=lambda: False,
    )

    counters = run.diagnostics["forensics"]["counters"]
    assert counters["advance"] + counters["keepalive"] == counters[
        "total_progress_events"
    ]
    assert counters["keepalive"] > 10
    assert counters["advance"] == 2
    activity = run.diagnostics["forensics"]["activity"]
    # Every event kept the activity clock alive; only two moved the progress
    # clock, so the forward-progress gap dwarfs the activity gap.
    assert activity["longest_gap_seconds"] == pytest.approx(30.0, abs=2)
    assert activity["longest_forward_progress_gap_seconds"] == pytest.approx(
        700.0, abs=2
    )


# ---------------------------------------------------------------------------
# Degraded mode: mandatory CHANNEL_READY handshake.
# ---------------------------------------------------------------------------


def test_missing_channel_ready_falls_back_to_the_legacy_wall_clock(harness):
    """No handshake -> legacy 900s bound, never a hard-fuse-only 45 min run."""
    # No channel_ready is ever written.
    for t in range(30, 800, 30):
        harness.script.at(t, "inv-A", "TOOL", "started")

    run = harness.run("inv-A", exit_at=None)

    assert run.outcome == wd.OUTCOME_LEGACY_TIMEOUT
    assert run.diagnostics["progress_channel_confirmed"] is False
    assert run.diagnostics["elapsed_seconds"] == pytest.approx(900.0, abs=2)


def test_legacy_mode_never_reports_the_hard_fuse(harness):
    harness.script.at(0, "inv-A", "MODEL", "started")
    harness.policy.legacy_wallclock_seconds = 400.0
    harness.policy.hard_max_runtime_seconds = 2700.0

    run = harness.run("inv-A", exit_at=None)

    assert run.outcome == wd.OUTCOME_LEGACY_TIMEOUT
    assert run.outcome != wd.OUTCOME_HARD_TIMEOUT


def test_late_handshake_keeps_the_watchdog_active(harness):
    """A slow-booting child that does announce is still fully supervised."""
    harness.policy.startup_grace_seconds = 10.0
    harness.script.ready(20, "inv-A")
    harness.script.at(20, "inv-A", "MODEL", "started")

    run = harness.run("inv-A", exit_at=None)

    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.diagnostics["progress_channel_confirmed"] is True


# ---------------------------------------------------------------------------
# Event-stream hygiene.
# ---------------------------------------------------------------------------


def test_events_from_a_stale_invocation_id_are_ignored(harness):
    harness.script.ready(0, "inv-A")
    for t in range(30, 400, 30):
        harness.script.at(t, "inv-previous-run", "TOOL", "started")

    run = harness.run("inv-A", exit_at=None)

    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.diagnostics["stale_event_count"] > 5
    assert run.diagnostics["progress_event_count"] == 0


def test_malformed_and_partial_lines_are_skipped(harness, tmp_path):
    """Garbage and half-written lines never crash or refresh the watchdog."""
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started")
    with harness.progress_path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write('{"run_id": "inv-A", "event": "progress"}\n')
        handle.write('{"run_id": "inv-A", "kind": "TOOL", "phase": "started"}\n')  # partial

    run = harness.run("inv-A", exit_at=None)

    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT


# ---------------------------------------------------------------------------
# Non-timeout outcomes and diagnostics.
# ---------------------------------------------------------------------------


def test_completed_subprocess_is_a_normal_success(harness):
    harness.script.ready(0, "inv-A")
    harness.script.at(10, "inv-A", "MODEL", "started")
    harness.script.at(20, "inv-A", "MODEL", "completed")

    run = harness.run("inv-A", exit_at=25, stdout='{"success": true}')

    assert run.outcome is None
    assert run.returncode == 0
    assert run.stdout == '{"success": true}'
    assert harness.killed == []


def test_output_capture_is_bounded(harness):
    huge = "x" * 50000
    harness.script.ready(0, "inv-A")

    run = harness.run("inv-A", exit_at=5, stdout=huge, stderr=huge)

    assert len(run.stdout) <= wd.MAX_CAPTURED_OUTPUT_CHARS
    assert len(run.stderr) <= wd.MAX_CAPTURED_OUTPUT_CHARS
    assert wd._OUTPUT_TRUNCATION_MARKER in run.stdout


def test_output_capture_bound_survives_many_lines(harness):
    """A child emitting many lines cannot grow the capture without bound."""
    many = "line of chatter\n" * 20000
    harness.script.ready(0, "inv-A")

    run = harness.run("inv-A", exit_at=5, stdout=many)

    assert len(run.stdout) <= wd.MAX_CAPTURED_OUTPUT_CHARS
    assert wd._OUTPUT_TRUNCATION_MARKER in run.stdout


def test_output_within_the_bound_is_kept_verbatim(harness):
    """No truncation marker when the child's output actually fits."""
    small = "all good\n" * 10
    harness.script.ready(0, "inv-A")

    run = harness.run("inv-A", exit_at=5, stdout=small)

    assert run.stdout == small
    assert wd._OUTPUT_TRUNCATION_MARKER not in run.stdout


def test_diagnostics_never_carry_output_bodies(harness):
    harness.script.ready(0, "inv-A")
    harness.script.at(5, "inv-A", "MODEL", "started")

    run = harness.run("inv-A", exit_at=10, stdout="SECRET-PROMPT-TEXT")

    assert "SECRET-PROMPT-TEXT" not in json.dumps(run.diagnostics)
    assert set(run.diagnostics) >= {
        "invocation_id", "project_id", "build_operation_id", "pid",
        "elapsed_seconds", "idle_for_seconds", "active_operation",
        "progress_event_count", "unknown_activity_count", "stale_event_count",
    }


# ---------------------------------------------------------------------------
# Configuration resolution.
# ---------------------------------------------------------------------------


def test_policy_defaults_come_from_named_constants():
    policy = wd.resolve_watchdog_policy({})
    assert policy.idle_timeout_seconds == wd.FRONTEND_IDLE_TIMEOUT_SECONDS
    assert policy.hard_max_runtime_seconds == wd.FRONTEND_HARD_MAX_RUNTIME_SECONDS
    assert policy.max_single_operation_seconds == wd.FRONTEND_MAX_SINGLE_OPERATION_SECONDS


def test_shipped_config_matches_the_named_constants():
    policy = wd.resolve_watchdog_policy()
    assert policy.idle_timeout_seconds == 180.0
    assert policy.hard_max_runtime_seconds == 2700.0


def test_invalid_config_falls_back_instead_of_disabling_a_bound():
    policy = wd.resolve_watchdog_policy(
        {"idle_timeout_seconds": "not-a-number", "hard_max_runtime_seconds": -5}
    )
    assert policy.idle_timeout_seconds == wd.FRONTEND_IDLE_TIMEOUT_SECONDS
    assert policy.hard_max_runtime_seconds == wd.FRONTEND_HARD_MAX_RUNTIME_SECONDS


# ---------------------------------------------------------------------------
# End-to-end: the real oneshot emitter, driven by a real child process, observed
# by the real supervisor. Proves the env-var contract and the JSONL wire format
# that the fake-script tests above stand in for.
# ---------------------------------------------------------------------------


def test_real_child_with_real_oneshot_emitter_is_supervised(tmp_path):
    """Supervisor -> env vars -> real child -> real emitter -> JSONL -> supervisor."""
    from hermes_cli import oneshot as real_oneshot

    child = tmp_path / "child.py"
    child.write_text(
        "import os, sys, time\n"
        "from hermes_cli import oneshot\n"
        "from agent.progress_events import make_progress_payload\n"
        "emitter = oneshot._build_progress_emitter()\n"
        "if emitter is None:\n"
        "    sys.exit(9)\n"
        "# Announce progress exactly the way the real agent does.\n"
        "emitter.on_progress('MODEL', make_progress_payload('starting API call #1'))\n"
        "for i in range(5):\n"
        "    time.sleep(0.1)\n"
        "    emitter.on_progress('STREAM',"
        " make_progress_payload('receiving stream response'))\n"
        "emitter.on_progress('MODEL', make_progress_payload('API call #1 completed'))\n"
        "emitter.close()\n"
        "sys.stdout.write('{\"success\": true}')\n",
        encoding="utf-8",
    )

    # Bounds are loose relative to the emitter's 5s active-event coalescing on
    # purpose: this test proves the plumbing, not the timing policy (the policy
    # is covered by the fake-clock tests above).
    policy = wd.WatchdogPolicy(
        idle_timeout_seconds=10.0,
        hard_max_runtime_seconds=60.0,
        max_single_operation_seconds=30.0,
        legacy_wallclock_seconds=60.0,
        startup_grace_seconds=10.0,
        poll_interval_seconds=0.05,
    )
    run = wd.supervise_frontend_run(
        [sys.executable, str(child)],
        cwd=tmp_path,
        # Production hands the child os.environ.copy() plus overrides; the
        # supervisor only adds the progress variables to whatever it is given.
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        project_id="e2e",
        invocation_id="e2e-invocation",
        build_operation_id="1",
        progress_path=tmp_path / "progress.jsonl",
        policy=policy,
    )

    assert run.returncode == 0, "child failed to build the real emitter"
    assert run.outcome is None, "a healthy child emitting progress was terminated"
    assert run.stdout == '{"success": true}'
    assert run.diagnostics["progress_channel_confirmed"] is True
    # ready handshake + MODEL started + coalesced STREAM + MODEL completed
    assert run.diagnostics["progress_event_count"] >= 3
    assert run.diagnostics["active_operation"] is None
    # The child's env carried exactly the contract the real oneshot reads.
    assert real_oneshot.PROGRESS_FILE_ENV == "HERMES_ONESHOT_PROGRESS_FILE"
    assert real_oneshot.PROGRESS_ID_ENV == "HERMES_ONESHOT_PROGRESS_ID"


def test_a_real_childs_frame_shapes_reach_the_receipt(tmp_path):
    """The whole diagnostic chain, with the real producer on both ends.

    Same child-process harness as above, but the STREAM events carry a
    ``stream_diag`` built by the real ``agent.stream_shapes`` and attached by the
    real emitter — so what lands in the receipt is exactly what a supervised
    FRONTEND run would leave behind for the p19 forensics to read. This is the
    CI-substitute for reading one real invocation's receipt by hand.
    """
    child = tmp_path / "shapes.py"
    child.write_text(
        "import sys, time\n"
        "from hermes_cli import oneshot\n"
        "from agent.progress_events import make_progress_payload\n"
        "from agent.stream_shapes import build_stream_diag, classify_openai_chunk\n"
        "from types import SimpleNamespace as NS\n"
        "emitter = oneshot._build_progress_emitter()\n"
        "if emitter is None:\n"
        "    sys.exit(9)\n"
        "frames = [\n"
        "    NS(choices=[NS(delta=NS(content='', tool_calls=None))]),\n"
        "    NS(choices=[], usage=NS(prompt_tokens=1)),\n"
        "]\n"
        "emitter.on_progress('MODEL', make_progress_payload('starting API call #1'))\n"
        "for i, frame in enumerate(frames):\n"
        "    emitter.on_progress('STREAM', make_progress_payload(\n"
        "        'receiving stream response',\n"
        "        advance=classify_openai_chunk(frame) != 'usage_only',\n"
        "        stream_diag=build_stream_diag(\n"
        "            mode='chat_completions',\n"
        "            shape=classify_openai_chunk(frame),\n"
        "            frame_index=i,\n"
        "            stream_seconds=1.0 * i,\n"
        "        )))\n"
        "    # Past the emitter's 5s active-event coalescing window, so the\n"
        "    # second frame's shape survives as its own line.\n"
        "    time.sleep(5.2)\n"
        "emitter.on_progress('MODEL', make_progress_payload('API call #1 completed'))\n"
        "emitter.close()\n",
        encoding="utf-8",
    )

    policy = wd.WatchdogPolicy(
        idle_timeout_seconds=20.0,
        hard_max_runtime_seconds=120.0,
        max_single_operation_seconds=60.0,
        legacy_wallclock_seconds=120.0,
        startup_grace_seconds=10.0,
        poll_interval_seconds=0.05,
    )
    run = wd.supervise_frontend_run(
        [sys.executable, str(child)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        project_id="e2e-shapes",
        invocation_id="e2e-shapes-invocation",
        build_operation_id="1",
        progress_path=tmp_path / "progress.jsonl",
        policy=policy,
        diagnostics_dir=tmp_path / "diag",
    )

    assert run.returncode == 0, "child failed to build the real emitter"
    assert run.outcome is None

    receipt = json.loads(
        (tmp_path / "diag" / "e2e-shapes-invocation.json").read_text(encoding="utf-8")
    )
    frames = receipt["stream_frames"]
    assert frames["shapes"] == {"empty_delta": 1, "usage_only": 1}
    # The empty-content keep-alive is the one that kept the run alive — which is
    # exactly the p19 finding this block exists to make readable.
    assert frames["advancing_by_shape"] == {"empty_delta": 1}
    assert frames["api_modes"] == {"chat_completions": 2}
    assert frames["last_shape"] == "usage_only"
    assert frames["max_frame_index"] == 1
    assert receipt["counters"]["stream_active"] == 2
    # MODEL started + MODEL completed + the one advancing frame.
    assert receipt["counters"]["advance"] == 3
    # ... and the only non-advancing event is the terminal usage frame.
    assert receipt["counters"]["keepalive"] == 1


def test_supervisor_propagates_the_progress_env_contract(tmp_path):
    """The env the supervisor sets is the env the oneshot emitter reads."""
    from hermes_cli import oneshot as real_oneshot

    seen = {}

    class _Capture(wd.WatchdogPolicy):
        pass

    def _spawn(cmd, **kwargs):
        seen.update(kwargs["env"])
        child = FakeChild(FakeClock(), exit_at=0)
        return child

    wd.supervise_frontend_run(
        ["noop"],
        cwd=tmp_path,
        env={"PROJECT_ID": "p1"},
        project_id="p1",
        invocation_id="inv-xyz",
        build_operation_id="2",
        progress_path=tmp_path / "p.jsonl",
        policy=_Capture(),
        spawn=_spawn,
        kill_tree=lambda pid, sig=None: True,
    )

    assert seen[real_oneshot.PROGRESS_ID_ENV] == "inv-xyz"
    assert seen[real_oneshot.PROGRESS_FILE_ENV].endswith("p.jsonl")
    # Existing child environment is preserved, not replaced.
    assert seen["PROJECT_ID"] == "p1"


# ---------------------------------------------------------------------------
# Forensic receipts (observation only).
#
# The point of the receipt is that the NEXT 45-minute run can be classified
# without a live reproduction. Every test below therefore either pins a
# counter/discriminator, or locks a property the receipt must NOT have — most
# importantly that the artifacts probe is never wired into any decision.
# ---------------------------------------------------------------------------

#: Documented ceiling for one receipt. Nothing here is allowed to grow the file
#: with the length of a run: 256 samples, 20 ring entries, 32 tool names, 50
#: mutated paths, and clamped descriptions are all hard bounds.
RECEIPT_SIZE_CEILING_BYTES = 65536


def _read_receipt(diagnostics_dir: Path, invocation_id: str) -> dict:
    return json.loads((diagnostics_dir / f"{invocation_id}.json").read_text("utf-8"))


def _complete_workspace(tmp_path: Path, name: str = "ws") -> Path:
    """A workspace with the two sampled artifacts present."""
    ws = tmp_path / name
    (ws / "src").mkdir(parents=True)
    (ws / "design-dna.json").write_text('{"v": 1}', encoding="utf-8")
    (ws / "src" / "App.tsx").write_text("export const A = 1;", encoding="utf-8")
    (ws / "src" / "main.tsx").write_text("main", encoding="utf-8")
    return ws


# --- counters and ring buffer ---------------------------------------------


def test_counters_classify_every_kind_and_phase_exactly(harness):
    """MODEL/TOOL/STREAM/UNKNOWN each land in their own counter.

    The 45-minute verdict is read off these numbers: a stream-dominant run and
    a model-dominant run are different failure modes and are currently
    indistinguishable, because only ``last_progress_kind`` survived.
    """
    harness.script.ready(0, "inv-A")
    for t in (10, 20):
        harness.script.at(t, "inv-A", "MODEL", "started", desc=f"starting API call #{t}")
    harness.script.at(30, "inv-A", "MODEL", "completed", desc="API call #20 completed")
    for t in (40, 50, 60):
        harness.script.at(
            t, "inv-A", "TOOL", "started", desc=f"executing tool: write_file"
        )
    for t in (70, 80):
        harness.script.at(
            t, "inv-A", "TOOL", "completed", desc="tool completed: write_file (0.4s)"
        )
    for t in (90, 100, 110, 120):
        harness.script.at(
            t, "inv-A", "STREAM", "active", desc="receiving stream response"
        )
    for t in (130, 140):
        harness.script.at(t, "inv-A", "UNKNOWN", "active", desc="provider retry backoff")

    run = harness.run("inv-A", exit_at=200)

    counters = run.diagnostics["forensics"]["counters"]
    assert counters["model_started"] == 2
    assert counters["model_completed"] == 1
    assert counters["tool_started"] == 3
    assert counters["tool_completed"] == 2
    assert counters["stream_active"] == 4
    assert counters["unknown_active"] == 2
    assert counters["total_progress_events"] == run.diagnostics["progress_event_count"]


def test_model_and_tool_boundary_events_are_exact_despite_coalescing(harness):
    """Boundary events are never coalesced, so these two are not lower bounds.

    The emitter coalesces active-phase events to at most one per 5s, which
    makes STREAM/UNKNOWN counts lower bounds only. ``started``/``completed``
    are exact, so the model-vs-tool ratio that separates a request loop from a
    tool loop is trustworthy.
    """
    harness.script.ready(0, "inv-A")
    for t in range(10, 400, 10):
        harness.script.at(t, "inv-A", "MODEL", "started", desc="starting API call #1")
        harness.script.at(t, "inv-A", "MODEL", "completed", desc="API call #1 completed")
    harness.script.at(410, "inv-A", "MODEL", "completed", desc="API call #1 completed")

    run = harness.run("inv-A", exit_at=450)

    counters = run.diagnostics["forensics"]["counters"]
    assert counters["model_started"] == 39
    assert counters["model_completed"] == 40


def test_recent_activity_ring_is_bounded_and_evicts_oldest(harness):
    """20 entries max, oldest evicted, each carrying offset/kind/phase/desc."""
    harness.script.ready(0, "inv-A")
    for i in range(40):
        harness.script.at(
            i, "inv-A", "TOOL", "started", desc=f"executing tool: tool{i:02d}"
        )

    run = harness.run("inv-A", exit_at=45)

    ring = run.diagnostics["forensics"]["recent_activity"]
    assert len(ring) == 20
    assert set(ring[0]) == {"offset_seconds", "kind", "phase", "desc"}
    # Oldest 20 evicted: tool00..tool19 gone, tool20..tool39 retained in order.
    assert [entry["desc"].split()[-1] for entry in ring] == [
        f"tool{i:02d}" for i in range(20, 40)
    ]
    assert [entry["offset_seconds"] for entry in ring] == [
        pytest.approx(float(i), abs=0.1) for i in range(20, 40)
    ]
    assert all(entry["kind"] == "TOOL" and entry["phase"] == "started" for entry in ring)


def test_tool_names_are_counted_capped_and_never_arguments(harness):
    """Tool NAMES are diagnostics; arguments are not."""
    harness.script.ready(0, "inv-A")
    for i in range(40):
        harness.script.at(
            i, "inv-A", "TOOL", "started", desc=f"executing tool: tool{i:02d}"
        )
    harness.script.at(50, "inv-A", "TOOL", "completed", desc="tool completed: tool00 (0.1s)")
    # An unmapped description must not be mined for a name.
    harness.script.at(60, "inv-A", "UNKNOWN", "active", desc="executing toolX: secret")

    run = harness.run("inv-A", exit_at=65)

    names = run.diagnostics["forensics"]["tool_names"]
    assert names["tool00"] == 2
    assert len(names) == wd.MAX_TOOL_NAME_DISTINCT
    assert run.diagnostics["forensics"]["tool_names_truncated"] is True
    assert "secret" not in json.dumps(names)


# --- description redaction --------------------------------------------------


def test_normalize_forensic_desc_redacts_urls_paths_and_long_runs():
    """The wire desc is clamped, never trusted: it can carry anything."""
    hex_run = "a" * 32 + "0123456789abcdef"

    url = wd.normalize_forensic_desc("fetching https://api.example.com/v1/x?k=abc")
    assert "example.com" not in url and "http" not in url

    win = wd.normalize_forensic_desc(r"wrote C:\Users\Bob\secret\App.tsx")
    assert "Bob" not in win and "C:" not in win

    posix = wd.normalize_forensic_desc("wrote /home/bob/proj/src/App.tsx")
    assert "/home" not in posix and "App.tsx" not in posix

    hashed = wd.normalize_forensic_desc(f"blob {hex_run} done")
    assert hex_run not in hashed

    # The known-good shape survives intact: a name is the diagnostic.
    assert wd.normalize_forensic_desc("executing tool: write_file") == (
        "executing tool: write_file"
    )
    assert wd.normalize_forensic_desc("tool completed: read_file (12.5s)") == (
        "tool completed: read_file (12.5s)"
    )
    assert wd.normalize_forensic_desc("receiving stream response") == (
        "receiving stream response"
    )


def test_normalize_forensic_desc_is_bounded_and_total():
    """Pure, total, and hard-bounded whatever it is handed."""
    assert wd.normalize_forensic_desc(None) == ""
    assert wd.normalize_forensic_desc("") == ""
    assert wd.normalize_forensic_desc("  a \n\t b  ") == "a b"
    assert len(wd.normalize_forensic_desc("x" * 5000)) <= wd._ACTIVITY_DESCRIPTION_MAX
    # A description that is only a payload redacts to the placeholder, not to
    # an empty string that would look like "nothing happened".
    assert wd.normalize_forensic_desc("b" * 40) == "<redacted>"


# --- workspace sampler -----------------------------------------------------


def test_unchanged_workspace_yields_a_constant_fingerprint(tmp_path):
    """No workspace movement must not be reported as mutation."""
    ws = _complete_workspace(tmp_path)
    sampler = wd.WorkspaceSampler(ws, None, started_at=0.0)

    sampler.sample(0.0)
    first = sampler.metadata_fingerprint
    sampler.sample(30.0)

    assert first
    assert sampler.metadata_fingerprint == first
    assert sampler.distinct_fingerprints == 1
    assert sampler.source_mutation_count == 0
    assert sampler.unique_source_files_mutated() == 0
    assert sampler.mutated_paths() == []
    assert sampler.design_dna_present is True
    assert sampler.app_tsx_present is True


def test_touching_one_file_is_one_mutation_of_one_file(tmp_path):
    """The A-vs-B split rests on unique-file count, not on event count."""
    ws = _complete_workspace(tmp_path)
    sampler = wd.WorkspaceSampler(ws, None, started_at=0.0)

    sampler.sample(0.0)
    before = sampler.metadata_fingerprint
    # A different length, not a same-size swap: the fingerprint is metadata, so
    # a same-size rewrite inside one filesystem mtime tick is deliberately
    # invisible (asserted separately) and must not be relied on here.
    (ws / "src" / "App.tsx").write_text(
        "export const App = () => <main />;", encoding="utf-8"
    )
    sampler.sample(30.0)

    assert sampler.metadata_fingerprint != before
    assert sampler.distinct_fingerprints == 2
    assert sampler.source_mutation_count == 1
    assert sampler.unique_source_files_mutated() == 1
    assert sampler.mutated_paths() == ["src/App.tsx"]
    assert sampler.first_mutation_offset_seconds == 30.0
    assert sampler.last_mutation_offset_seconds == 30.0


def test_fingerprint_ignores_content_and_never_opens_a_file(tmp_path, monkeypatch):
    """The fingerprint is (relpath, size, mtime_ns) and nothing else.

    This is the property that makes "no source contents" hold absolutely, so
    it is asserted two ways: a same-length content swap with a pinned mtime is
    invisible, and any attempt to read a file during a sample raises.
    """
    ws = _complete_workspace(tmp_path)
    target = ws / "src" / "App.tsx"
    stat = target.stat()
    sampler = wd.WorkspaceSampler(ws, None, started_at=0.0)
    sampler.sample(0.0)
    before = sampler.metadata_fingerprint

    # A same-length content swap with the mtime pinned back: the fingerprint
    # must not move, and the scan must not have read the bytes to notice.
    target.write_text("export const B = 9;", encoding="utf-8")
    assert target.stat().st_size == stat.st_size
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def _no_reads(*_a, **_k):
        raise AssertionError("the sampler must never read file contents")

    monkeypatch.setattr("builtins.open", _no_reads)
    monkeypatch.setattr(Path, "open", _no_reads)
    monkeypatch.setattr(Path, "read_text", _no_reads)
    monkeypatch.setattr(Path, "read_bytes", _no_reads)
    sampler.sample(30.0)

    assert sampler.metadata_fingerprint == before
    assert sampler.source_mutation_count == 0
    # The scan demonstrably still saw both artifacts, so the pass above was a
    # real comparison and not a scan that silently failed.
    assert sampler.app_tsx_present is True
    assert sampler.design_dna_present is True


def test_sampler_marks_truncation_past_the_file_cap_and_still_returns(
    tmp_path, monkeypatch
):
    """A pathological tree must be reported, not walked to the end."""
    ws = tmp_path / "big"
    (ws / "src").mkdir(parents=True)
    for i in range(12):
        (ws / "src" / f"f{i}.tsx").write_text("x", encoding="utf-8")
    monkeypatch.setattr(wd, "WORKSPACE_SAMPLE_MAX_FILES", 5)

    sampler = wd.WorkspaceSampler(ws, None, started_at=0.0)
    sampler.sample(0.0)
    receipt = sampler.receipt()

    assert receipt["truncated"] is True
    assert receipt["samples"] == 1
    assert receipt["app_tsx_present"] is False


def test_sampler_excludes_node_modules_and_dot_directories(tmp_path):
    """Vendored and hidden trees are not the build's source of truth."""
    ws = _complete_workspace(tmp_path)
    for excluded in ("node_modules", ".git", ".next"):
        target = ws / "src" / excluded
        target.mkdir()
        (target / "junk.tsx").write_text("junk", encoding="utf-8")
    (ws / "src" / ".hidden.tsx").write_text("hidden", encoding="utf-8")

    sampler = wd.WorkspaceSampler(ws, None, started_at=0.0)
    sampler.sample(0.0)
    (ws / "src" / "App.tsx").write_text("changed", encoding="utf-8")
    sampler.sample(30.0)

    assert sampler.mutated_paths() == ["src/App.tsx"]
    assert sampler.unique_source_files_mutated() == 1


def test_sample_series_is_capped(tmp_path):
    """A 45-minute run must not be able to grow the sample series for ever."""
    ws = _complete_workspace(tmp_path)
    sampler = wd.WorkspaceSampler(ws, None, started_at=0.0)
    for t in range(0, 30000, 30):
        sampler.sample(float(t), force=True)

    assert sampler.samples == wd.MAX_SAMPLE_SERIES


# --- artifacts probe is observation only -----------------------------------


def test_a_raising_artifacts_probe_cannot_change_the_outcome(harness, tmp_path):
    """A forensic observation that explodes must not terminate anything."""
    def _boom():
        raise RuntimeError("probe exploded")

    harness.script.ready(0, "inv-A")
    for t in range(30, 300, 30):
        harness.script.at(t, "inv-A", "MODEL", "started", desc="starting API call #1")
        harness.script.at(t, "inv-A", "MODEL", "completed", desc="API call #1 completed")

    run = harness.run(
        "inv-A", exit_at=320, workspace=tmp_path, artifacts_probe=_boom
    )

    assert run.outcome is None
    assert run.returncode == 0
    assert harness.killed == []
    assert run.diagnostics["forensics"]["artifacts"]["probe_failed"] is True


def test_a_raising_sampler_cannot_change_the_outcome(harness, tmp_path, monkeypatch):
    """Same contract for the workspace scan itself."""
    def _boom(*_a, **_k):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(wd.WorkspaceSampler, "_scan", _boom)
    harness.script.ready(0, "inv-A")
    for t in range(30, 300, 30):
        harness.script.at(t, "inv-A", "MODEL", "started", desc="starting API call #1")
        harness.script.at(t, "inv-A", "MODEL", "completed", desc="API call #1 completed")

    run = harness.run(
        "inv-A",
        exit_at=320,
        workspace=tmp_path,
        diagnostics_dir=tmp_path / "diag",
        artifacts_probe=lambda: True,
    )

    assert run.outcome is None
    assert run.returncode == 0
    assert harness.killed == []
    assert run.diagnostics["forensics"]["artifacts"]["complete"] is False


def test_first_complete_offset_is_recorded_once_and_never_overwritten(harness):
    """Artifact completeness is a timeline, not a boolean."""
    harness.script.ready(0, "inv-A")
    for t in range(0, 700, 30):
        harness.script.at(t, "inv-A", "MODEL", "started", desc="starting API call #1")
    # Complete exactly once, at t=300, then (wrongly) report incomplete after.
    harness.script.at(300, "inv-A", "TOOL", "started", desc="executing tool: write_file")

    run = harness.run(
        "inv-A",
        exit_at=700,
        diagnostics_dir=harness.tmp_path / "diag",
        artifacts_probe=lambda: harness.clock.now == 300,
    )

    artifacts = run.diagnostics["forensics"]["artifacts"]
    assert artifacts["first_complete_offset_seconds"] == 300.0
    # The end state is the end state: complete, then a regression to incomplete.
    assert artifacts["complete_at_end"] is False


def test_first_complete_offset_stays_null_when_never_complete(harness):
    """A null means only "not observed before the end" — never "too slow"."""
    harness.script.ready(0, "inv-A")
    for t in range(0, 400, 30):
        harness.script.at(t, "inv-A", "MODEL", "started", desc="starting API call #1")

    run = harness.run(
        "inv-A",
        exit_at=420,
        diagnostics_dir=harness.tmp_path / "diag",
        artifacts_probe=lambda: False,
    )

    artifacts = run.diagnostics["forensics"]["artifacts"]
    assert artifacts["probe_available"] is True
    assert artifacts["first_complete_offset_seconds"] is None
    assert artifacts["complete"] is False


def test_complete_artifacts_do_not_terminate_a_silent_run(harness):
    """BOUNDARY LOCK: this is the convergence guard, and it is not implemented.

    The artifacts are complete from the first sample, and the run then goes
    silent. It MUST still be terminated by the idle bound. If this test ever
    fails, someone has wired the probe into the supervision decision — which is
    the deferred change, not this one.
    """
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started")

    run = harness.run(
        "inv-A", exit_at=None, artifacts_probe=lambda: True
    )

    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.terminated is True
    assert run.diagnostics["forensics"]["artifacts"]["complete"] is True


def test_complete_artifacts_do_not_end_a_run_that_is_otherwise_alive(harness):
    """The mirror image: still no early exit on a converging-shaped run."""
    harness.script.ready(0, "inv-A")
    for t in range(0, 700, 30):
        harness.script.at(t, "inv-A", "TOOL", "started", desc="executing tool: write_file")
        harness.script.at(t, "inv-A", "TOOL", "completed", desc="tool completed: write_file (1.0s)")

    run = harness.run(
        "inv-A", exit_at=690, artifacts_probe=lambda: harness.clock.now >= 300
    )

    assert run.outcome is None
    assert harness.killed == []
    assert run.diagnostics["forensics"]["artifacts"]["first_complete_offset_seconds"] == 300.0


# --- the predicate the corrected verdict rests on --------------------------


def test_in_flight_protection_suppresses_the_idle_bound_and_shows_the_gap(harness):
    """A hard-timeout run may contain multi-minute silences. The receipt shows it.

    In-flight protection suppresses the 180s idle bound for up to
    ``max_single_operation_seconds`` (900s), so a run of repeated long
    operations each under that bound can reach the fuse with real silences
    between events. ``longest_gap_seconds`` is what separates that pattern from
    continuously-active work — and nothing else in the record can.
    """
    harness.script.ready(0, "inv-A").at(0, "inv-A", "MODEL", "started", desc="starting API call #1")
    # 800s of true silence, still inside the 900s in-flight bound.
    harness.script.at(800, "inv-A", "TOOL", "completed", desc="tool completed: write_file (800.0s)")
    for t in range(860, 1460, 60):
        harness.script.at(t, "inv-A", "STREAM", "active", desc="receiving stream response")

    run = harness.run("inv-A", exit_at=1500)

    assert run.outcome is None
    assert run.diagnostics["elapsed_seconds"] > harness.policy.idle_timeout_seconds
    assert run.diagnostics["forensics"]["activity"]["longest_gap_seconds"] == pytest.approx(
        800.0, abs=1
    )


def test_continuously_active_run_reports_a_short_longest_gap(harness):
    """The contrasting signature: real activity has no multi-minute silence."""
    harness.script.ready(0, "inv-A")
    for t in range(0, 1500, 30):
        harness.script.at(t, "inv-A", "TOOL", "started", desc="executing tool: write_file")
        harness.script.at(t, "inv-A", "TOOL", "completed", desc="tool completed: write_file (0.2s)")

    run = harness.run("inv-A", exit_at=1500)

    gap = run.diagnostics["forensics"]["activity"]["longest_gap_seconds"]
    assert gap <= harness.policy.idle_timeout_seconds


# --- receipt persistence ---------------------------------------------------


def test_receipt_is_written_for_every_terminal_path(tmp_path):
    """Success and all three timeout codes, each with its own outcome recorded.

    Every terminal path writes, because the whole question is "which mode
    happened" — a run that produced no receipt on one of the four would be the
    one run we could not classify. Each case drives the distinct bound it
    names: silence for idle, continuous progress for the hard fuse, and no
    handshake for degraded mode.
    """
    cases = (
        (None, 25, True, False),
        (wd.OUTCOME_IDLE_TIMEOUT, None, True, False),
        (wd.OUTCOME_HARD_TIMEOUT, None, True, True),
        (wd.OUTCOME_LEGACY_TIMEOUT, None, False, True),
    )
    for index, (expected, exit_at, confirmed, busy) in enumerate(cases):
        made = Harness(
            tmp_path,
            fast_policy(),
            progress_path=tmp_path / f"progress-{index}.jsonl",
        )
        try:
            if confirmed:
                made.script.ready(0, "inv-x")
            made.script.at(0, "inv-x", "MODEL", "started", desc="starting API call #1")
            if busy:
                for t in range(60, 3000, 60):
                    made.script.at(t, "inv-x", "TOOL", "started", desc="executing tool: write_file")
                    made.script.at(t, "inv-x", "TOOL", "completed", desc="tool completed: write_file (0.5s)")
            diag = tmp_path / f"diag-{index}"
            run = made.run("inv-x", diagnostics_dir=diag, exit_at=exit_at)

            assert run.outcome == expected, expected
            receipt = _read_receipt(diag, "inv-x")
            assert receipt["schema"] == wd.FORENSICS_SCHEMA
            assert receipt["outcome"] == expected
            assert receipt["channel_confirmed"] is confirmed
            assert run.diagnostics["forensics_receipt"].endswith("inv-x.json")
        finally:
            made.script.close()


def test_receipt_survives_the_runs_directory_being_removed(tmp_path):
    """A sibling of runs/, so runs_dir.rmdir() + progress unlink cannot take it.

    This is the durability reason the receipt is not written under the
    per-invocation runs directory the supervisor already cleans up.
    """
    hermes = tmp_path / "hermes-home"
    runs_dir = hermes / "runs" / "inv-durable"
    runs_dir.mkdir(parents=True)
    diag = hermes / "diagnostics" / "proj-durable"

    made = Harness(
        tmp_path, fast_policy(), progress_path=runs_dir / "progress.jsonl"
    )
    try:
        made.script.ready(0, "inv-durable")
        made.script.at(10, "inv-durable", "TOOL", "started", desc="executing tool: write_file")
        made.run(
            "inv-durable", project_id="proj-durable", exit_at=20, diagnostics_dir=diag
        )

        # The structural property: the receipt directory is a SIBLING of runs/,
        # so the caller's runs_dir.rmdir() and the supervisor's
        # progress_path.unlink() cannot reach it. rmtree rather than rmdir so
        # the test asserts the layout instead of Windows' open-file rules.
        assert diag != runs_dir
        assert runs_dir not in diag.parents
        made.script.close()
        shutil.rmtree(runs_dir)
        assert not runs_dir.exists()
        assert _read_receipt(diag, "inv-durable")["outcome"] is None
    finally:
        made.script.close()


def test_receipt_is_bounded_for_a_very_long_noisy_run(tmp_path):
    """Receipt size must not scale with the length or noisiness of the run."""
    made = Harness(tmp_path, fast_policy(hard_max_runtime_seconds=100_000.0))
    diag = tmp_path / "diag"
    try:
        made.script.ready(0, "inv-noisy")
        for i in range(300):
            made.script.at(
                i * 30, "inv-noisy", "TOOL", "started", desc=f"executing tool: tool{i}"
            )
        # 300 events spanning 0..8970s: 301 sampling opportunities, capped at 256.
        run = made.run("inv-noisy", exit_at=9000, diagnostics_dir=diag)
        path = diag / "inv-noisy.json"
        assert path.stat().st_size < RECEIPT_SIZE_CEILING_BYTES

        receipt = _read_receipt(diag, "inv-noisy")
        assert receipt["counters"]["total_progress_events"] == 300
        assert receipt["workspace"]["samples"] == wd.MAX_SAMPLE_SERIES
        assert len(receipt["recent_activity"]) == wd.RECENT_ACTIVITY_LEN
        assert len(receipt["tool_names"]) == wd.MAX_TOOL_NAME_DISTINCT
        assert receipt["tool_names_truncated"] is True
        assert run.outcome is None
    finally:
        made.script.close()


# --- STREAM frame shapes ----------------------------------------------------
#
# Which post-provider-close frame shape was emitted as STREAM/active/advance=
# true. A timeout code says a bound fired; only this says the stream was alive
# AND what it was made of. Nothing here feeds a bound — the counters are folded
# after the liveness decision, exactly like every other forensic field.


def _stream_diag(shape="empty_delta", **overrides):
    diag = {
        "mode": "chat_completions",
        "shape": shape,
        "n": 1200,
        "t": 61.2,
        "rep": False,
        "map": False,
        "d": {"content": True, "reasoning": False, "tool": False, "finish": False},
    }
    diag.update(overrides)
    return diag


def test_a_shape_carrying_stream_lands_in_the_receipt(harness):
    """The whole point: WHICH shape kept this stream alive, for how long."""
    harness.script.ready(0, "inv-A")
    harness.script.at(
        1, "inv-A", "STREAM", "active", desc="receiving stream response",
        advance=True, stream_diag=_stream_diag("text_delta", n=10, t=1.0),
    )
    # The keep-alive the denylist lets through as progress, over and over.
    for i in range(2, 12):
        harness.script.at(
            i, "inv-A", "STREAM", "active", desc="receiving stream response",
            advance=True, stream_diag=_stream_diag("empty_delta", n=10 * i, t=1.0 * i),
        )
    # The terminal usage frame is demoted, and is recorded as such.
    harness.script.at(
        20, "inv-A", "STREAM", "active", desc="receiving stream response",
        advance=False, stream_diag=_stream_diag("usage_only", n=120, t=20.0),
    )

    run = harness.run("inv-A", exit_at=25)

    frames = run.diagnostics["forensics"]["stream_frames"]
    assert frames["shapes"] == {"text_delta": 1, "empty_delta": 10, "usage_only": 1}
    # The advance split is the finding: empty_delta is what kept the run alive.
    assert frames["advancing_by_shape"] == {"text_delta": 1, "empty_delta": 10}
    assert frames["api_modes"] == {"chat_completions": 12}
    assert frames["last_shape"] == "usage_only"
    assert frames["repeat_frame_count"] == 0
    assert frames["mapping_frame_count"] == 0
    assert frames["max_frame_index"] == 120
    assert frames["max_frame_seconds"] == 20.0
    assert frames["first_offset_seconds"] == 1.0
    assert frames["last_offset_seconds"] == 20.0
    assert frames["shapes_truncated"] is False
    assert frames["api_modes_truncated"] is False


def test_the_two_identity_fingerprints_are_counted(harness):
    """``rep`` and ``map`` are the two findings that change the diagnosis.

    rep: the child saw the same frame OBJECT twice in a row, so a local
    emitter is re-yielding a cached frame and "the provider is still sending"
    is false. map: frames arrived as Mappings, so every getattr in the
    classifier missed and everything advances regardless of content.
    """
    harness.script.ready(0, "inv-A")
    harness.script.at(
        1, "inv-A", "STREAM", "active", desc="receiving stream response",
        stream_diag=_stream_diag("empty_delta", rep=True, map=True),
    )
    harness.script.at(
        2, "inv-A", "STREAM", "active", desc="receiving stream response",
        stream_diag=_stream_diag("unknown", rep=False, map=True),
    )
    harness.script.at(
        3, "inv-A", "STREAM", "active", desc="receiving stream response",
        stream_diag=_stream_diag("text_delta"),
    )

    run = harness.run("inv-A", exit_at=6)

    frames = run.diagnostics["forensics"]["stream_frames"]
    assert frames["repeat_frame_count"] == 1
    assert frames["mapping_frame_count"] == 2
    assert frames["shapes"] == {"empty_delta": 1, "unknown": 1, "text_delta": 1}
    # An api_mode-less diag (or one the validator drops) folds nothing rather
    # than inventing a key.
    assert frames["api_modes"] == {"chat_completions": 3}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "empty_delta",
        42,
        ["empty_delta"],
        {},
        {"mode": "chat_completions"},
        {"shape": ""},
        {"shape": None},
        {"shape": 7},
        # Not a taxonomy token: a shape is only useful as an enum member.
        {"shape": "../../etc/passwd"},
        {"shape": "Empty Delta"},
        {"shape": "empty_delta" * 20},
    ],
)
def test_an_unusable_diagnostic_is_ignored_and_changes_no_bound(harness, bad):
    harness.script.ready(0, "inv-A")
    harness.script.at(
        1, "inv-A", "STREAM", "active", desc="receiving stream response",
        advance=True, stream_diag=bad,
    )
    harness.script.at(
        2, "inv-A", "STREAM", "active", desc="receiving stream response",
        advance=False, stream_diag=bad,
    )
    harness.script.at(5, "inv-A", "TOOL", "started", desc="executing tool: terminal")
    harness.script.at(6, "inv-A", "TOOL", "completed", desc="tool completed: terminal (0.1s)")

    run = harness.run("inv-A", exit_at=10)

    # Liveness is decided exactly as it would have been: the events are still
    # accepted, counted, and able to keep the run alive. (2 STREAM + 1 TOOL
    # start advance; the one demoted STREAM frame is the only keep-alive.)
    counters = run.diagnostics["forensics"]["counters"]
    assert counters["stream_active"] == 2
    assert counters["advance"] == 3
    assert counters["keepalive"] == 1
    # Nothing was recorded from the junk.
    frames = run.diagnostics["forensics"]["stream_frames"]
    assert frames["shapes"] == {}
    assert frames["advancing_by_shape"] == {}
    assert frames["api_modes"] == {}
    assert frames["last_shape"] is None
    assert frames["first_offset_seconds"] is None


@pytest.mark.parametrize(
    "bad, dropped",
    [
        ({"n": -1}, "n"),
        ({"n": True}, "n"),
        ({"t": "61.2"}, "t"),
        ({"t": float("inf")}, "t"),
        ({"rep": "yes"}, "rep"),
        ({"map": 1}, "map"),
        ({"mode": "m" * 200}, "mode"),
    ],
)
def test_one_unusable_scalar_does_not_discard_the_shape(harness, bad, dropped):
    """The shape is the finding; a bogus counter beside it is just dropped.

    Discarding a whole diagnostic because its ``n`` arrived as a string would
    throw away the one thing the receipt exists to record, over a field no
    reader depends on.
    """
    harness.script.ready(0, "inv-A")
    harness.script.at(
        1, "inv-A", "STREAM", "active", desc="receiving stream response",
        stream_diag=_stream_diag("empty_delta", **bad),
    )
    harness.script.at(2, "inv-A", "TOOL", "started", desc="executing tool: terminal")
    harness.script.at(3, "inv-A", "TOOL", "completed", desc="tool completed: terminal (0.1s)")

    run = harness.run("inv-A", exit_at=6)

    frames = run.diagnostics["forensics"]["stream_frames"]
    assert frames["shapes"] == {"empty_delta": 1}
    # The unusable scalar is dropped; the well-formed one beside it survives.
    if dropped == "n":
        assert frames["last_frame_index"] is None
        assert frames["max_frame_index"] == 0
        assert frames["last_frame_seconds"] == 61.2
    if dropped == "t":
        assert frames["last_frame_seconds"] is None
        assert frames["max_frame_seconds"] == 0.0
        assert frames["last_frame_index"] == 1200
    if dropped == "mode":
        assert frames["api_modes"] == {}
    if dropped == "rep":
        assert frames["repeat_frame_count"] == 0
    if dropped == "map":
        assert frames["mapping_frame_count"] == 0


def test_an_absent_diagnostic_still_works_exactly_as_before(harness):
    """An older child keeps working: absence is not a failure."""
    harness.script.ready(0, "inv-A")
    for i in range(1, 6):
        harness.script.at(
            i, "inv-A", "STREAM", "active", desc="receiving stream response"
        )
    harness.script.at(6, "inv-A", "TOOL", "started", desc="executing tool: terminal")
    harness.script.at(7, "inv-A", "TOOL", "completed", desc="tool completed: terminal (0.1s)")

    run = harness.run("inv-A", exit_at=10)

    forensics = run.diagnostics["forensics"]
    assert forensics["counters"]["stream_active"] == 5
    assert forensics["stream_frames"]["shapes"] == {}
    assert forensics["stream_frames"]["last_shape"] is None


def test_shape_counts_are_capped_and_flagged(harness):
    """A producer that widens the taxonomy cannot grow the receipt."""
    harness.script.ready(0, "inv-A")
    for i in range(wd.MAX_STREAM_SHAPE_KEYS + 5):
        harness.script.at(
            i,
            "inv-A",
            "STREAM",
            "active",
            desc="receiving stream response",
            stream_diag=_stream_diag(f"shape{i:02d}", n=i, t=float(i)),
        )
    for i in range(wd.MAX_API_MODE_KEYS + 3):
        harness.script.at(
            wd.MAX_STREAM_SHAPE_KEYS + 5 + i,
            "inv-A",
            "STREAM",
            "active",
            desc="receiving stream response",
            stream_diag=_stream_diag("text_delta", mode=f"mode{i}"),
        )

    run = harness.run("inv-A", exit_at=60)

    frames = run.diagnostics["forensics"]["stream_frames"]
    assert len(frames["shapes"]) == wd.MAX_STREAM_SHAPE_KEYS
    assert frames["shapes_truncated"] is True
    # The advancing split is gated on the same admission, so it can never hold a
    # key the shape count rejected — one bound, one flag, no second loose dict.
    assert set(frames["advancing_by_shape"]) <= set(frames["shapes"])
    assert len(frames["api_modes"]) == wd.MAX_API_MODE_KEYS
    assert frames["api_modes_truncated"] is True


def test_every_shape_the_producer_can_emit_survives_validation():
    """The taxonomy and this validator must not drift apart.

    The producer's enum is closed; the supervisor matches it with a shape
    regex and a length cap rather than a copied list. If a new shape could
    exceed either, it would be silently dropped in the field — the failure this
    asserts cannot happen.
    """
    from agent.stream_shapes import STREAM_SHAPES, STREAM_DIAG_MODE_MAX

    for shape in STREAM_SHAPES:
        assert wd.normalize_stream_diag({"shape": shape}) == {"shape": shape}, shape
        assert len(shape) <= wd.STREAM_DIAG_TOKEN_MAX
    # The api_mode values the three emit sites send are tokens too.
    for mode in ("chat_completions", "anthropic_messages", "codex_responses"):
        normalized = wd.normalize_stream_diag({"shape": "empty_delta", "mode": mode})
        assert normalized["mode"] == mode
    assert STREAM_DIAG_MODE_MAX <= wd.STREAM_DIAG_TOKEN_MAX


def test_stream_shape_counts_are_lower_bounds_not_exact_counts(harness):
    """Coalescing undercounts; it never misattributes.

    The transport drops any second active event within 5s, so a run's true
    frame count is at least the recorded one. The shape on a retained line is
    still the shape that was actually firing — newest event wins.
    """
    harness.script.ready(0, "inv-A")
    for i in range(6):
        harness.script.at(
            i * 10, "inv-A", "STREAM", "active", desc="receiving stream response",
            stream_diag=_stream_diag("empty_delta", n=1000 + i, t=10.0 * i),
        )
    harness.script.at(200, "inv-A", "TOOL", "started", desc="executing tool: terminal")
    harness.script.at(201, "inv-A", "TOOL", "completed", desc="tool completed: terminal (0.1s)")

    run = harness.run("inv-A", exit_at=210)

    frames = run.diagnostics["forensics"]["stream_frames"]
    # One retained line per distinct second in the script; the emitter's 5s
    # window would have coalesced any that landed closer than that.
    assert sum(frames["shapes"].values()) == 6
    assert frames["shapes"] == {"empty_delta": 6}
    # ... and it is a lower bound, because ``max_frame_index`` is the child's own
    # per-attempt index, which ran far ahead of the six frames that survived.
    assert frames["max_frame_index"] == 1005
    assert frames["max_frame_seconds"] == 50.0


def test_a_shape_carrying_line_still_refreshes_liveness_and_progress(harness):
    """The diagnostic is observation: it cannot change any decision."""
    harness.script.ready(0, "inv-A")
    harness.script.at(
        1, "inv-A", "MODEL", "started", desc="starting API call #1"
    )
    for i in range(2, 30):
        harness.script.at(
            i, "inv-A", "STREAM", "active", desc="receiving stream response",
            advance=True, stream_diag=_stream_diag("empty_delta", n=i * 40, t=float(i)),
        )
    harness.script.at(
        30, "inv-A", "MODEL", "completed", desc="API call #1 completed"
    )
    harness.script.at(31, "inv-A", "TOOL", "started", desc="executing tool: terminal")
    harness.script.at(32, "inv-A", "TOOL", "completed", desc="tool completed: terminal (0.1s)")

    # max_single_operation is 900s in fast_policy, and these keep-alives advance,
    # so the run is never cut — exactly as it would be with no diagnostics at all.
    run = harness.run("inv-A", exit_at=40)

    assert run.outcome is None
    frames = run.diagnostics["forensics"]["stream_frames"]
    assert frames["shapes"] == {"empty_delta": 28}
    assert frames["advancing_by_shape"] == {"empty_delta": 28}


def test_the_diagnostic_cannot_smuggle_a_payload_into_the_receipt(harness, tmp_path):
    """A shape is an enum and a counter. Nothing else on the wire is kept.

    Even a producer (or a hostile one) that puts a model delta in an extra key
    of the diagnostic gets it dropped by the validator, because the receipt
    records only the closed set of scalar fields.
    """
    diag = tmp_path / "diag"
    secret = "sk-live-CANARY-9f3a2b7c"
    harness.script.ready(0, "inv-A")
    harness.script.at(
        1, "inv-A", "STREAM", "active",
        desc="receiving stream response",
        stream_diag={
            **_stream_diag("empty_delta"),
            "content": secret,
            "delta": {"text": secret},
            "tool_calls": [{"function": {"arguments": secret}}],
        },
    )
    harness.script.at(2, "inv-A", "TOOL", "started", desc="executing tool: terminal")
    harness.script.at(3, "inv-A", "TOOL", "completed", desc="tool completed: terminal (0.1s)")

    run = harness.run("inv-A", exit_at=6, diagnostics_dir=diag)

    body = (diag / "inv-A.json").read_text(encoding="utf-8")
    assert secret not in body
    assert secret not in json.dumps(run.diagnostics)
    # ... and the finding itself survives.
    assert run.diagnostics["forensics"]["stream_frames"]["shapes"] == {"empty_delta": 1}


def test_pruning_keeps_the_newest_receipts_per_project(tmp_path):
    """Bounded disk: one project keeps at most its newest receipts."""
    diag = tmp_path / "diag"
    for i in range(wd.MAX_RECEIPTS_PER_PROJECT + 5):
        name = wd._write_receipt(diag, f"inv-{i:03d}", {"schema": wd.FORENSICS_SCHEMA})
        os.utime(diag / name, ns=(1_000_000_000 + i * 1_000_000_000,) * 2)

    kept = sorted(p.name for p in diag.glob("*.json"))
    assert len(kept) == wd.MAX_RECEIPTS_PER_PROJECT
    # The five oldest were pruned; the newest survived.
    assert "inv-024.json" in kept
    assert "inv-000.json" not in kept
    # No temp files left behind by the atomic write.
    assert list(diag.glob("*.tmp")) == []


def test_receipt_write_failure_leaves_the_run_untouched(harness, tmp_path):
    """A forensic write that fails must not change outcome or returncode."""
    def _boom(*_a, **_k):
        raise OSError("disk full")

    harness.script.ready(0, "inv-A")
    for t in range(30, 300, 30):
        harness.script.at(t, "inv-A", "TOOL", "started", desc="executing tool: write_file")
        harness.script.at(t, "inv-A", "TOOL", "completed", desc="tool completed: write_file (0.3s)")

    run = harness.run(
        "inv-A", exit_at=320, diagnostics_dir=tmp_path / "diag", write_receipt=_boom
    )

    assert run.outcome is None
    assert run.returncode == 0
    assert run.diagnostics["forensics_receipt"] is None
    assert list((tmp_path / "diag").glob("*.json")) == []


def test_receipt_leaks_no_prompt_absolute_path_or_url(harness, tmp_path):
    """The receipt is an operator artifact, so it is held to the strictest bar.

    Two structural guarantees are asserted here. First, nothing outside a
    progress description is ever recorded: the prompt travels in the child's
    argv and appears in its output, and neither reaches the receipt. Second, a
    description IS attacker-influenced text, so a URL or an absolute path in
    one is redacted rather than stored.
    """
    prompt = "You are FRONTEND, the website designer and builder for R1"
    harness.script.ready(0, "inv-A")
    for t in (10, 20, 30):
        harness.script.at(
            t, "inv-A", "TOOL", "started", desc=f"executing tool: write_file via https://x.test/a"
        )
    harness.script.at(
        40, "inv-A", "TOOL", "completed", desc=f"tool completed: write_file ({tmp_path})"
    )

    diag = tmp_path / "diag"
    run = harness.run(
        "inv-A", exit_at=50, diagnostics_dir=diag, workspace=tmp_path,
        stdout=prompt,
    )
    assert run.outcome is None

    text = (diag / "inv-A.json").read_text("utf-8")
    assert prompt not in text
    assert str(tmp_path) not in text
    assert "https://" not in text
    assert "x.test" not in text
    # The one place a path may appear is workspace-relative, and only that.
    for entry in json.loads(text)["workspace"]["mutated_paths"]:
        assert not entry.startswith("/") and ":" not in entry


def test_receipt_reports_the_policy_actually_in_force(harness, tmp_path):
    """A receipt read without the config in hand must still be interpretable."""
    harness.script.ready(0, "inv-A")
    harness.script.at(10, "inv-A", "MODEL", "started", desc="starting API call #1")

    run = harness.run(
        "inv-A",
        exit_at=20,
        diagnostics_dir=tmp_path / "diag",
        workspace=tmp_path,
        artifacts_probe=lambda: False,
    )
    policy = run.diagnostics["forensics"]["policy"]

    assert policy["idle"] == harness.policy.idle_timeout_seconds
    assert policy["hard"] == harness.policy.hard_max_runtime_seconds
    assert policy["max_single_operation"] == harness.policy.max_single_operation_seconds
    assert run.diagnostics["forensics"]["channel_confirmed"] is True
    assert run.diagnostics["forensics"]["invocation_id"] == "inv-A"


def test_degraded_mode_receipt_records_an_unconfirmed_channel(harness, tmp_path):
    """Degraded mode must be readable too, not only the fully supervised path."""
    for t in range(30, 800, 30):
        harness.script.at(t, "inv-A", "TOOL", "started", desc="executing tool: write_file")

    run = harness.run(
        "inv-A", exit_at=None, diagnostics_dir=tmp_path / "diag", workspace=tmp_path
    )

    receipt = _read_receipt(tmp_path / "diag", "inv-A")
    assert run.outcome == wd.OUTCOME_LEGACY_TIMEOUT
    assert receipt["outcome"] == wd.OUTCOME_LEGACY_TIMEOUT
    assert receipt["channel_confirmed"] is False


def test_no_diagnostics_dir_writes_nothing_but_still_reports_in_memory(harness):
    """The file is opt-in; the in-memory block is not."""
    harness.script.ready(0, "inv-A")
    harness.script.at(5, "inv-A", "MODEL", "started", desc="starting API call #1")

    run = harness.run("inv-A", exit_at=10)

    assert run.diagnostics["forensics_receipt"] is None
    assert run.diagnostics["progress_event_count"] == 1
    assert run.diagnostics["forensics"]["counters"]["model_started"] == 1
    assert list(harness.tmp_path.glob("**/*.json")) == []


# ---------------------------------------------------------------------------
# Real-process tree teardown. The one test that spends real time.
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    """True when *pid* is a live process.

    A zombie still answers ``os.kill(pid, 0)`` on POSIX until it is reaped, so a
    killed-but-unreaped grandchild would read as an orphan. Zombies hold no
    resources and execute nothing, so they are not survivors.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _await_file(path: Path, deadline: float) -> str:
    """Bounded wait for *path* to appear and be readable, then return it."""
    while time.monotonic() < deadline:
        try:
            return path.read_text().strip()
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"{path} never appeared before the deadline")


def test_timeout_leaves_no_orphan_tree(tmp_path):
    """A real invocation tree is fully torn down on timeout.

    Spawns a child that itself spawns a grandchild, forces an idle timeout
    using real bounds, and asserts both PIDs are gone. This is the only
    guarantee mocks cannot make: whole-tree termination of real descendants.

    The bounds are deliberately loose relative to child startup. The child has
    to boot an interpreter, spawn a *second* interpreter, and write a marker
    file, and this test kills it on a wall-clock budget measured from launch —
    under full-suite parallel load those two boots do not always fit inside a
    2s window, which made this test flaky for reasons unrelated to
    supervision. ``max_single_operation_seconds`` sets how long the run is
    allowed before the backstop fires, so it is the bound that has to clear
    startup; ``idle_timeout_seconds`` is checked first but is gated on the same
    backstop having elapsed (see ``is_in_flight``).
    """
    if wd.kill_process_tree is None:  # pragma: no cover
        pytest.skip("whole-tree termination unavailable")

    grandchild_marker = tmp_path / "grandchild.pid"
    script = tmp_path / "tree.py"
    script.write_text(
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    progress = tmp_path / "progress.jsonl"
    progress.write_text(
        json.dumps(
            {"run_id": "tree-run", "event": "channel_ready", "kind": "channel_ready",
             "phase": "ready", "desc": "", "advance": True}
        )
        + "\n"
        + json.dumps(
            {"run_id": "tree-run", "event": "progress", "kind": "MODEL",
             "phase": "started", "desc": "starting API call #1", "advance": True}
        )
        + "\n",
        encoding="utf-8",
    )

    policy = wd.WatchdogPolicy(
        idle_timeout_seconds=1.0,
        # The no-progress backstop is what has to clear child startup, so it is
        # the only bound with real headroom. 8s is comfortable for two interpreter
        # boots under parallel load and still keeps the test short. The hard
        # ceiling stays deliberately distant.
        max_single_operation_seconds=8.0,
        hard_max_runtime_seconds=60.0,
        legacy_wallclock_seconds=120.0,
        startup_grace_seconds=5.0,
        poll_interval_seconds=0.1,
    )
    run = wd.supervise_frontend_run(
        [sys.executable, str(script), str(grandchild_marker)],
        cwd=tmp_path,
        env={},
        project_id="tree",
        invocation_id="tree-run",
        build_operation_id="1",
        progress_path=progress,
        policy=policy,
    )

    assert run.outcome == wd.OUTCOME_IDLE_TIMEOUT
    assert run.terminated is True
    assert run.tree_kill_attempted is True
    # The in-flight MODEL operation is what protected the run past the idle
    # bound, so the backstop is the bound that must have fired.
    assert run.diagnostics["active_operation"] == "MODEL"
    assert run.diagnostics["elapsed_seconds"] == pytest.approx(
        policy.max_single_operation_seconds, abs=1.5
    )

    # Give the OS a moment to reap, then confirm neither PID survives.
    deadline = time.monotonic() + 10.0
    child_pid = run.diagnostics["pid"]
    grandchild_pid = int(_await_file(grandchild_marker, deadline))

    while time.monotonic() < deadline and (_alive(child_pid) or _alive(grandchild_pid)):
        time.sleep(0.1)

    assert not _alive(child_pid), f"child {child_pid} orphaned"
    assert not _alive(grandchild_pid), f"grandchild {grandchild_pid} orphaned"
