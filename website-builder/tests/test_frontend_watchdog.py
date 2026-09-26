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
    ) -> "ProgressScript":
        # ``desc`` defaults to the old synthetic label so every existing test is
        # unchanged; forensic tests pass a real emitter description.
        self.schedule.setdefault(when, []).append(
            (run_id, kind, phase, f"{kind.lower()}:{phase}" if desc is None else desc)
        )
        return self

    def ready(self, when: float, run_id: str) -> "ProgressScript":
        return self.at(when, run_id, "channel_ready", "ready", desc="")

    def flush(self) -> None:
        due = self.schedule.pop(self.clock.now, None)
        for run_id, kind, phase, desc in due or []:
            line = json.dumps(
                {
                    "run_id": run_id,
                    "event": "channel_ready" if kind == "channel_ready" else "progress",
                    "kind": kind,
                    "phase": phase,
                    "desc": desc,
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
