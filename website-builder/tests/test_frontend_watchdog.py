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
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.hermes import watchdog as wd


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

    def at(self, when: float, run_id: str, kind: str, phase: str) -> "ProgressScript":
        self.schedule.setdefault(when, []).append((run_id, kind, phase))
        return self

    def ready(self, when: float, run_id: str) -> "ProgressScript":
        return self.at(when, run_id, "channel_ready", "ready")

    def flush(self) -> None:
        due = self.schedule.pop(self.clock.now, None)
        for run_id, kind, phase in due or []:
            line = json.dumps(
                {
                    "run_id": run_id,
                    "event": "channel_ready" if kind == "channel_ready" else "progress",
                    "kind": kind,
                    "phase": phase,
                    "desc": f"{kind.lower()}:{phase}",
                },
                separators=(",", ":"),
            ) + "\n"
            os.write(self.fd, line.encode("utf-8"))

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class Harness:
    """Runs ``supervise_frontend_run`` against a fake clock and fake child."""

    def __init__(self, tmp_path: Path, policy: wd.WatchdogPolicy) -> None:
        self.tmp_path = tmp_path
        self.policy = policy
        self.clock = FakeClock()
        self.progress_path = tmp_path / "progress.jsonl"
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
# Real-process tree teardown. The one test that spends real time.
# ---------------------------------------------------------------------------


def test_timeout_leaves_no_orphan_tree(tmp_path):
    """A real invocation tree is fully torn down on timeout.

    Spawns a child that itself spawns a grandchild, forces an idle timeout
    using real (tiny) bounds, and asserts both PIDs are gone. This is the only
    guarantee mocks cannot make: whole-tree termination of real descendants.
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
             "phase": "ready", "desc": ""}
        )
        + "\n"
        + json.dumps(
            {"run_id": "tree-run", "event": "progress", "kind": "MODEL",
             "phase": "started", "desc": "starting API call #1"}
        )
        + "\n",
        encoding="utf-8",
    )

    policy = wd.WatchdogPolicy(
        idle_timeout_seconds=1.0,
        # No-progress backstop just above the idle bound, so the invocation is
        # cut off as IDLE well before the (deliberately distant) hard ceiling.
        max_single_operation_seconds=2.0,
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

    # Give the OS a moment to reap, then confirm neither PID survives.
    deadline = time.monotonic() + 10.0
    child_pid = run.diagnostics["pid"]
    grandchild_pid = int(grandchild_marker.read_text().strip())

    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    while time.monotonic() < deadline and (_alive(child_pid) or _alive(grandchild_pid)):
        time.sleep(0.1)

    assert not _alive(child_pid), f"child {child_pid} orphaned"
    assert not _alive(grandchild_pid), f"grandchild {grandchild_pid} orphaned"
