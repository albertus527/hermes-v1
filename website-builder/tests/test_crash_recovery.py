"""Crash-recovery / stranded-state and dispatch-classification regressions.

Covers:
  * a project stranded in QUEUED/RUNNING by a process death is recoverable,
    and the user journey (message -> intake -> READY) works again afterwards;
  * PUBLISHING / REVISION_REQUESTED keep their own resume paths and are never
    touched by the startup pass;
  * QA success is committed atomically (no observable state in which the
    tested snapshot is durable but the lifecycle was not advanced);
  * a named QA precondition keeps its own code instead of UNEXPECTED_QA_ERROR;
  * a lifecycle precondition rejected by intake is reported honestly and is not
    mistaken for an ambiguous remote operation;
  * dispatch() is total: a lock timeout or unexpected error returns a
    structured failure instead of escaping.

Everything here is offline. External boundaries (renderer, screenshot capture,
VISION, Telegram) are injected as mocks; the state store, lifecycle authority,
locking, intake and QA logic under test are all real.
"""

from __future__ import annotations

import os
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.channels.telegram import NormalizedMessage
from app.core.authz import AuthzError, ProjectAccess
from app.core.intake import IntakeProcessor
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore, reconcile_stranded_projects
from app.deploy.snapshot import record_checks, source_fingerprint
from app.qa.findings import DeterministicFindings, QAAttempt
from app.qa.orchestrator import QAOrchestrator
from app.qa.render import RenderHandle
from app.qa.screenshot import BrowserMetrics, ScreenshotSet
from app.sandbox.runner import ProjectRunner


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b""))


def _metrics(width: int, height: int) -> BrowserMetrics:
    return BrowserMetrics(
        inner_width=width, inner_height=height,
        document_client_width=width,
        document_scroll_width=width, body_scroll_width=width,
    )


def _store_with_project(tmp_path: Path, project_id: str = "app", **fields) -> ProjectStateStore:
    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer(project_id) as state:
        state.roles = {"owner": "telegram:1", "reviewers": ["telegram:2"], "viewers": []}
        state.brief = {"name": "Persisted", "what": "shop", "why": "visit"}
        for key, value in fields.items():
            setattr(state, key, value)
        store.save(state)
    return store


def _payload(event: int, text: str = "content"):
    return {"update_id": event,
            "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": text}}


def _owner():
    return AuthenticatedTelegramContext("1", "555")


def _age_file(path: Path, seconds: float = 3600.0) -> None:
    """Make a state file look like it was written by a PREVIOUS process.

    The startup pass deliberately ignores any project file modified at or
    after the moment it starts, so a fixture that wants to represent a crashed
    process must have a stale mtime.
    """
    stale = time.time() - seconds
    os.utime(path, (stale, stale))


# ---------------------------------------------------------------------------
# BUG-01 part 1: the startup pass resolves stranded in-flight lifecycles
# ---------------------------------------------------------------------------

def test_stranded_queued_project_recovers(tmp_path):
    """UNLUCKY USER: process died between the claim write and QUEUED -> RUNNING.

    Before the fix this project was wedged forever: every later message hit a
    LifecycleError that the dispatcher reported as EVENT_RECONCILIATION_REQUIRED.
    """
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.QUEUED.value)
    with store.acquire_writer("app") as state:
        state.dispatch_events["claim-1"] = {
            "action": "build", "status": "CLAIMED", "reached_remote": False}
        store.save(state)
    _age_file(store._project_path("app"))

    recovered = reconcile_stranded_projects(store)

    assert recovered == {"app": ProjectLifecycle.QUEUED.value}
    state = store.load("app")
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.failure["phase"] == "interrupted"
    assert state.failure["error_code"] == "OPERATION_INTERRUPTED"
    assert state.failure["interrupted_from"] == ProjectLifecycle.QUEUED.value
    # A stranded claim is left exactly as-is: its remote-boundary evidence is
    # still valid and recovery must not rewrite it.
    assert state.dispatch_events["claim-1"]["status"] == "CLAIMED"


def test_stranded_running_project_recovers_without_fabricating_qa(tmp_path):
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.RUNNING.value)
    _age_file(store._project_path("app"))

    recovered = reconcile_stranded_projects(store)

    assert recovered == {"app": ProjectLifecycle.RUNNING.value}
    state = store.load("app")
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    # Recovery must never invent a tested artifact: a stranded project has no
    # proven QA result, so preview admission must stay closed.
    assert not state.deployment.get("tested_snapshot")
    assert state.revisions.qa_revision == 0


def test_recovered_project_accepts_a_new_message_end_to_end(tmp_path):
    """RETURNING USER: the wedge is gone; the real intake path re-admits the
    project and a subsequent build is admitted again."""
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.RUNNING.value)
    _age_file(store._project_path("app"))
    reconcile_stranded_projects(store)

    intake = IntakeProcessor(store, hermes_adapter=None)
    message = NormalizedMessage(
        event_id="1", user_id="1", conversation_id="555",
        text="Persisted shop for visit, a cafe called Persisted that sells coffee for visit")
    result = intake.process(message, "app")
    intake.apply_to_project("app", result, principal_id="telegram:1")

    state = store.load("app")
    assert state.lifecycle == ProjectLifecycle.READY.value

    # And the build admission gate that used to be permanently closed is open.
    dispatch = TelegramDispatcher(store, intake, builder=MagicMock())
    builder = dispatch.builder
    builder.build.return_value = MagicMock(success=True, error=None, error_code=None,
                                           reached_remote=False, workspace=Path("."))
    outcome = dispatch.dispatch(_payload(2), "app", "build", authenticated=_owner())
    assert outcome.error_code != "PROJECT_NOT_ACCEPTING_INPUT"
    assert outcome.error_code != "EVENT_RECONCILIATION_REQUIRED"
    assert builder.build.called


def test_reconcile_leaves_states_with_their_own_resume_paths_alone(tmp_path):
    """PUBLISHING (promote is_resume) and REVISION_REQUESTED (F6 re-drive) are
    DESIGNED to survive a crash. Recovery must not fail them closed."""
    for lifecycle in (
        ProjectLifecycle.PUBLISHING.value,
        ProjectLifecycle.REVISION_REQUESTED.value,
        ProjectLifecycle.LIVE.value,
        ProjectLifecycle.PREVIEW_READY.value,
        ProjectLifecycle.PAUSED.value,
        ProjectLifecycle.CANCELED.value,
        ProjectLifecycle.READY.value,
        ProjectLifecycle.DISCOVERING.value,
        ProjectLifecycle.WAITING_INPUT.value,
        ProjectLifecycle.FAILED.value,
    ):
        store = _store_with_project(tmp_path / lifecycle, lifecycle=lifecycle)
        _age_file(store._project_path("app"))
        assert reconcile_stranded_projects(store) == {}, lifecycle
        assert store.load("app").lifecycle == lifecycle


def test_reconcile_never_steals_a_project_from_a_live_writer(tmp_path):
    """A project whose writer lock is genuinely held by a concurrent writer
    must be left completely alone, even though its persisted lifecycle is
    stranded and its state file is old."""
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.RUNNING.value)
    _age_file(store._project_path("app"))
    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with store.acquire_writer("app"):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert holding.wait(5)
        before = store._project_path("app").read_bytes()
        assert reconcile_stranded_projects(store) == {}
        assert store._project_path("app").read_bytes() == before
    finally:
        release.set()
        holder.join(5)
    assert store.load("app").lifecycle == ProjectLifecycle.RUNNING.value


def test_reconcile_ignores_a_state_file_written_during_the_scan(tmp_path):
    """A file whose mtime is at/after the scan start belongs to a live writer.

    The scan records its start instant and skips anything modified at or after
    it, so a write that lands while the scan is walking the directory cannot
    be mistaken for a crashed process.
    """
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.RUNNING.value)
    path = store._project_path("app")
    _age_file(path)
    ahead = time.time() + 30
    os.utime(path, (ahead, ahead))

    assert reconcile_stranded_projects(store) == {}
    assert store.load("app").lifecycle == ProjectLifecycle.RUNNING.value


def test_reconcile_is_idempotent(tmp_path):
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.QUEUED.value)
    _age_file(store._project_path("app"))
    assert reconcile_stranded_projects(store) == {"app": ProjectLifecycle.QUEUED.value}
    first = store.load("app").failure
    assert reconcile_stranded_projects(store) == {}
    assert store.load("app").failure == first


def test_reconcile_skips_invalid_and_corrupt_state_files(tmp_path):
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.QUEUED.value)
    _age_file(store._project_path("app"))
    (store.root / "not a project id!.json").write_text("{}", encoding="utf-8")
    (store.root / "corrupt.json").write_text("{not json", encoding="utf-8")
    _age_file(store.root / "corrupt.json")

    assert reconcile_stranded_projects(store) == {"app": ProjectLifecycle.QUEUED.value}


def test_reconcile_respects_explicitly_active_projects(tmp_path):
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.RUNNING.value)
    _age_file(store._project_path("app"))
    assert reconcile_stranded_projects(store, active_projects={"app"}) == {}
    assert store.load("app").lifecycle == ProjectLifecycle.RUNNING.value


# ---------------------------------------------------------------------------
# BUG-01 part 2: QA success is committed atomically
# ---------------------------------------------------------------------------

def _qa_orchestrator(tmp_path: Path):
    store = ProjectStateStore(tmp_path / "state")
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "proj"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "App.tsx").write_text("// real content", encoding="utf-8")
    (workspace / "src" / "main.tsx").write_text("// entry", encoding="utf-8")
    (workspace / "index.html").write_text("<html></html>", encoding="utf-8")
    (workspace / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    (workspace / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
    (workspace / "dist").mkdir()
    (workspace / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    runner = ProjectRunner(workspace_root, store)

    capture = MagicMock()
    adapter = MagicMock()
    adapter.vision_inspect.return_value = {
        "pass": True, "blocking": [], "observations": [], "summary": "Looks good."}

    def _screenshots(url, qa_dir, attempt):
        qa_dir.mkdir(parents=True, exist_ok=True)
        desktop = qa_dir / "desktop.png"
        mobile = qa_dir / "mobile.png"
        desktop.write_bytes(_png(1440, 900))
        mobile.write_bytes(_png(390, 844))
        return ScreenshotSet(
            desktop=desktop, mobile=mobile,
            desktop_metrics=_metrics(1440, 900), mobile_metrics=_metrics(390, 844))

    capture.capture.side_effect = _screenshots
    orchestrator = QAOrchestrator(
        runner, store, hermes_adapter=adapter,
        renderer=MagicMock(), screenshot_capture=capture)
    orchestrator.renderer.start.return_value = RenderHandle(
        project_id="proj", port=5100, process=MagicMock(), url="http://127.0.0.1:5100/")
    return store, workspace, orchestrator


def _queue_to_running(store: ProjectStateStore) -> None:
    store.transition_lifecycle("proj", ProjectLifecycle.READY)
    store.transition_lifecycle("proj", ProjectLifecycle.QUEUED)
    store.transition_lifecycle("proj", ProjectLifecycle.RUNNING)


def test_qa_success_commits_snapshot_and_transition_together(tmp_path):
    """No observable persisted state may exist in which the tested snapshot is
    durable but the lifecycle was not advanced: that window stranded projects."""
    store, workspace, orchestrator = _qa_orchestrator(tmp_path)
    _queue_to_running(store)
    record_checks(store, "proj", workspace, source_fingerprint(workspace))
    assert store.load("proj").deployment.get("checked")

    seen: List[Tuple[str, bool]] = []
    real_save = store.save

    def _recording_save(state):
        real_save(state)
        seen.append((state.lifecycle, bool(state.deployment.get("tested_snapshot"))))

    store.save = _recording_save  # type: ignore[method-assign]
    try:
        result = orchestrator.run("proj", workspace,
                                  {"name": "N", "what": "w", "why": "y"},
                                  {"version": 1})
    finally:
        store.save = real_save  # type: ignore[method-assign]

    assert result.success, result.error
    assert store.load("proj").lifecycle == ProjectLifecycle.PREVIEW_READY.value
    # Every save the run produced: RUNNING never coexists with a durable
    # tested snapshot.
    assert not any(lifecycle == ProjectLifecycle.RUNNING.value and has_snapshot
                   for lifecycle, has_snapshot in seen), seen
    final = store.load("proj")
    assert final.deployment.get("tested_snapshot")
    assert final.revisions.qa_revision == final.revisions.source_revision


# ---------------------------------------------------------------------------
# BUG-12: named QA preconditions keep their own code
# ---------------------------------------------------------------------------

def test_stale_qa_binding_keeps_its_code(tmp_path):
    store, workspace, orchestrator = _qa_orchestrator(tmp_path)
    _queue_to_running(store)
    with store.acquire_writer("proj") as state:
        # The checked binding claims a DIFFERENT source revision than the
        # project currently has, so the passing result is stale.
        state.deployment["checked"] = {
            "source_sha256": "s" * 64, "artifact_sha256": "a" * 64,
            "source_revision": 99, "artifact_revision": 99}
        store.save(state)

    result = orchestrator.run("proj", workspace,
                              {"name": "N", "what": "w", "why": "y"}, {"version": 1})

    assert not result.success
    assert result.error == "STALE_QA_BINDING"
    assert store.load("proj").failure["error"] == "STALE_QA_BINDING"


def test_vision_required_keeps_its_code(tmp_path):
    """A final-pass attempt that never produced VISION evidence must surface
    VISION_REQUIRED, not the opaque UNEXPECTED_QA_ERROR."""
    store, workspace, orchestrator = _qa_orchestrator(tmp_path)
    _queue_to_running(store)
    record_checks(store, "proj", workspace, source_fingerprint(workspace))

    clean = QAAttempt(
        attempt=0,
        deterministic=DeterministicFindings(
            render_ok=True, desktop_screenshot_ok=True, mobile_screenshot_ok=True,
            design_dna_valid=True, source_present=True, build_ok=True, typecheck_ok=True),
        vision=None)
    assert clean.final_pass  # the branch is reachable

    orchestrator._run_one_attempt = lambda *a, **k: clean

    result = orchestrator.run("proj", workspace,
                              {"name": "N", "what": "w", "why": "y"}, {"version": 1})

    assert not result.success
    assert result.error == "VISION_REQUIRED"
    assert store.load("proj").failure["error"] == "VISION_REQUIRED"


def test_unexpected_error_still_reports_unexpected(tmp_path):
    """Regression: UNEXPECTED_QA_ERROR stays reserved for real surprises."""
    store, workspace, orchestrator = _qa_orchestrator(tmp_path)
    _queue_to_running(store)
    orchestrator._run_one_attempt = MagicMock(side_effect=RuntimeError("boom"))

    result = orchestrator.run("proj", workspace,
                              {"name": "N", "what": "w", "why": "y"}, {"version": 1})

    assert not result.success
    assert result.error == "UNEXPECTED_QA_ERROR"
    assert store.load("proj").lifecycle == ProjectLifecycle.FAILED.value


def test_infrastructure_error_still_reports_its_own_code(tmp_path):
    """A real RenderError is infrastructure, not an unexpected surprise, and
    must keep its own code rather than collapsing to UNEXPECTED_QA_ERROR."""
    from app.qa.render import RenderError

    store, workspace, orchestrator = _qa_orchestrator(tmp_path)
    _queue_to_running(store)
    orchestrator.renderer.start.side_effect = RenderError("boom")

    result = orchestrator.run("proj", workspace,
                              {"name": "N", "what": "w", "why": "y"}, {"version": 1})

    assert not result.success
    assert result.error == "INFRASTRUCTURE_ERROR:render_failed:boom"
    assert store.load("proj").lifecycle == ProjectLifecycle.FAILED.value


# ---------------------------------------------------------------------------
# BUG-07: a lifecycle precondition is not an ambiguous remote operation
# ---------------------------------------------------------------------------

def test_intake_on_cancelled_project_is_rejected_without_writing_a_claim(tmp_path):
    """CONFUSED USER: sends an ordinary message to a closed project."""
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.CANCELED.value)
    dispatch = TelegramDispatcher(store, IntakeProcessor(store, hermes_adapter=None))

    result = dispatch.dispatch(_payload(1), "app", "intake", authenticated=_owner())

    assert not result.success
    assert result.error_code == "PROJECT_NOT_ACCEPTING_INPUT"
    assert result.error_code != "EVENT_RECONCILIATION_REQUIRED"
    # Rejected before the claim block: no claim churn, so a replay is stable
    # and the claim ledger cannot fill with permanently dead entries.
    assert store.load("app").dispatch_events == {}
    assert store.load("app").lifecycle == ProjectLifecycle.CANCELED.value


def test_pause_on_failed_project_is_not_reported_as_reconciliation_required(tmp_path):
    """UNLUCKY USER: says 'pause' to a failed project.

    FAILED -> PAUSED is not a legal edge, so the real IntakeProcessor raises
    LifecycleError. That must be reported as an honest precondition rejection,
    not as "a previous operation needs reconciliation".
    """
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.FAILED.value)
    dispatch = TelegramDispatcher(store, IntakeProcessor(store, hermes_adapter=None))

    result = dispatch.dispatch(_payload(1, "tunggu dulu"), "app", "intake",
                               authenticated=_owner())

    assert not result.success
    assert result.error_code == "PROJECT_NOT_ACCEPTING_INPUT"
    assert result.error_code != "EVENT_RECONCILIATION_REQUIRED"
    # The claim must not be left CLAIMED, or every replay would fail closed.
    claims = store.load("app").dispatch_events
    assert claims and all(c["status"] != "CLAIMED" for c in claims.values())
    assert store.load("app").lifecycle == ProjectLifecycle.FAILED.value


def test_normal_intake_still_succeeds(tmp_path):
    """Regression: the new gate must not block ordinary intake."""
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.READY.value)
    dispatch = TelegramDispatcher(store, IntakeProcessor(store, hermes_adapter=None))

    result = dispatch.dispatch(
        _payload(1, "Persisted shop for visit, a cafe called Persisted sells coffee"),
        "app", "intake", authenticated=_owner())

    assert result.success, result.error_code
    claims = store.load("app").dispatch_events
    assert len(claims) == 1
    assert next(iter(claims.values()))["status"] == "DONE"


def test_genuine_ambiguous_operation_still_reports_reconciliation_required(tmp_path):
    """Regression: a real ambiguous remote effect must NOT be softened into
    the new precondition code."""
    store = _store_with_project(tmp_path)
    refs = MagicMock()
    refs.add_upload.side_effect = TimeoutError("ambiguous effect")
    dispatch = TelegramDispatcher(store, None, reference_intake=refs)

    result = dispatch.dispatch(_payload(1), "app", "reference_upload",
                               authenticated=_owner())

    assert result.error_code == "EVENT_RECONCILIATION_REQUIRED"


# ---------------------------------------------------------------------------
# BUG-15: the auto-build sub-claim must not fail the intake claim
# ---------------------------------------------------------------------------

def test_auto_build_sub_claim_failure_does_not_fail_the_intake_claim(tmp_path):
    """UNLUCKY USER: the build ran and delivered, but writing the build
    sub-claim's bookkeeping hit a lock timeout.

    The intake itself succeeded. Reporting EVENT_RECONCILIATION_REQUIRED here
    told the user an operation needed reconciliation for a turn that worked,
    and burned the intake claim so the outcome was never reported.
    """
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.READY.value)
    intake = IntakeProcessor(store, hermes_adapter=None)
    builder = MagicMock()
    flags = {"build_ran": False}

    def _build(*_args, **_kwargs):
        flags["build_ran"] = True
        return MagicMock(
            success=True, error=None, error_code=None, reached_remote=True,
            diagnostics={}, workspace=Path("."))

    builder.build.side_effect = _build
    dispatch = TelegramDispatcher(store, intake, builder=builder)

    real_acquire = store.acquire_writer

    def _flaky_acquire(project_id, **kwargs):
        # Let intake and the build proceed. Fail ONLY the writer block that
        # finalizes the build sub-claim, i.e. the first acquire after the
        # build has run.
        if flags["build_ran"]:
            raise TimeoutError("Could not acquire writer lock")
        return real_acquire(project_id, **kwargs)

    store.acquire_writer = _flaky_acquire  # type: ignore[method-assign]
    try:
        result = dispatch.dispatch(
            _payload(1, "Persisted shop for visit, a cafe called Persisted "
                        "that sells coffee for visit"),
            "app", "intake", authenticated=_owner())
    finally:
        store.acquire_writer = real_acquire  # type: ignore[method-assign]

    assert builder.build.called, "the build should still have run"
    # The intake outcome is reported, NOT a reconciliation requirement.
    assert result.success, result.error_code
    assert result.data.get("build_triggered") is True
    assert result.data.get("build_success") is True
    # The intake claim is NOT burned as FAILED. It is left CLAIMED, which is
    # the correct fail-closed outcome when the store itself is unwritable
    # (every subsequent write fails too) — and it is a materially different
    # state from the pre-fix behaviour, which recorded FAILED and told the
    # user an operation needed reconciliation.
    intake_claims = [c for c in store.load("app").dispatch_events.values()
                     if c["action"] == "intake"]
    assert intake_claims
    assert all(c["status"] == "CLAIMED" for c in intake_claims), intake_claims


# ---------------------------------------------------------------------------
# BUG-08: dispatch() is total, and a read never takes the writer lock
# ---------------------------------------------------------------------------

def test_dispatch_returns_structured_failure_on_writer_lock_timeout(tmp_path):
    store = _store_with_project(tmp_path)
    dispatch = TelegramDispatcher(store, None)
    dispatch.access = MagicMock()
    dispatch.access.read.side_effect = TimeoutError("Could not acquire writer lock")

    result = dispatch.dispatch(_payload(1), "app", "read", authenticated=_owner())

    assert not result.success
    assert result.error_code == "WRITER_LOCK_TIMEOUT"
    assert result.error_code != "EVENT_RECONCILIATION_REQUIRED"


def test_dispatch_returns_structured_failure_on_unexpected_error(tmp_path):
    store = _store_with_project(tmp_path)
    dispatch = TelegramDispatcher(store, None)
    dispatch.access = MagicMock()
    dispatch.access.read.side_effect = RuntimeError("boom")

    result = dispatch.dispatch(_payload(1), "app", "read", authenticated=_owner())

    assert not result.success
    assert result.error_code == "DISPATCH_INTERNAL_ERROR"


def test_dispatch_never_raises_on_a_malformed_payload(tmp_path):
    store = _store_with_project(tmp_path)
    dispatch = TelegramDispatcher(store, None)
    result = dispatch.dispatch({"update_id": 1}, "app", "intake", authenticated=_owner())
    assert not result.success


def test_read_succeeds_while_another_holder_owns_the_writer_lock(tmp_path):
    """A status query must not be able to block a build.

    Holds the exclusive writer lock in another thread for the duration of the
    read. A read implemented with acquire_writer would block for its full 30s
    timeout instead of returning immediately.
    """
    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.READY.value)
    access = ProjectAccess(store)
    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with store.acquire_writer("app"):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert holding.wait(5)
        view = access.read("app", principal_id="telegram:1")
        assert view["project_id"] == "app"
        assert view["lifecycle"] == ProjectLifecycle.READY.value
        assert view["source_revision"] == 0
    finally:
        release.set()
        holder.join(5)


# ---------------------------------------------------------------------------
# BUG-11: valid screenshot evidence must not be rejected over a scrollbar
# ---------------------------------------------------------------------------

def _screenshot_set(tmp_path: Path, *, inner, client) -> object:
    """A ScreenshotSet with a real PNG and the given live viewport metrics."""
    from app.qa.screenshot import ScreenshotSet

    desktop = tmp_path / "desktop.png"
    desktop.write_bytes(_png(1440, 900))
    return ScreenshotSet(
        desktop=desktop, mobile=None,
        desktop_metrics=BrowserMetrics(
            inner_width=inner[0], inner_height=inner[1],
            document_client_width=client,
            document_scroll_width=inner[0], body_scroll_width=inner[0]),
    )


def test_viewport_evidence_survives_a_reserved_scrollbar(tmp_path):
    """MOBILE / DESKTOP USER on a page taller than the viewport.

    ``documentElement.clientWidth`` is innerWidth minus any space the browser
    reserves for a vertical scrollbar. Requiring exact equality rejected a
    perfectly valid capture as an infrastructure failure, which burns no repair
    attempt and lands the project in FAILED with nothing actionable.
    """
    from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions

    # 1440 viewport, 15px scrollbar reserved -> clientWidth 1425.
    screenshots = _screenshot_set(tmp_path, inner=(1440, 900), client=1425)
    validate_screenshot_dimensions(screenshots)  # must NOT raise


def test_viewport_evidence_is_still_rejected_when_the_viewport_was_not_applied(tmp_path):
    """The guard must keep its teeth: a genuinely wrong viewport is rejected."""
    from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions

    screenshots = _screenshot_set(tmp_path, inner=(1300, 900), client=1300)
    try:
        validate_screenshot_dimensions(screenshots)
    except ScreenshotError as exc:
        assert "innerWidth" in str(exc)
    else:  # pragma: no cover - guard
        raise AssertionError("a wrong viewport must be rejected")

    # And a clientWidth far below the viewport is not a scrollbar, it is a bug.
    screenshots = _screenshot_set(tmp_path, inner=(1440, 900), client=900)
    try:
        validate_screenshot_dimensions(screenshots)
    except ScreenshotError as exc:
        assert "clientWidth" in str(exc)
    else:  # pragma: no cover - guard
        raise AssertionError("a collapsed clientWidth must be rejected")


def test_inner_height_mismatch_is_still_rejected(tmp_path):
    from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions

    screenshots = _screenshot_set(tmp_path, inner=(1440, 800), client=1440)
    try:
        validate_screenshot_dimensions(screenshots)
    except ScreenshotError as exc:
        assert "innerHeight" in str(exc)
    else:  # pragma: no cover - guard
        raise AssertionError("a wrong innerHeight must be rejected")


def test_dispatch_and_revision_ledgers_stay_bounded(tmp_path):
    """A long-lived project must not grow its state document without limit.

    Every save rewrites and fsyncs the whole document, so unbounded ledgers
    make each subsequent write progressively more expensive.
    """
    from app.core.state import (
        DISPATCH_EVENT_RETENTION,
        PENDING_REVISION_RETENTION,
    )

    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer("app") as state:
        state.roles = {"owner": "telegram:1", "reviewers": [], "viewers": []}
        for i in range(DISPATCH_EVENT_RETENTION * 3):
            state.dispatch_events[f"k{i:05d}"] = {
                "action": "intake", "status": "DONE"}
        state.prune_bounded_ledgers()
        store.save(state)

    state = store.load("app")
    assert len(state.dispatch_events) == DISPATCH_EVENT_RETENTION
    # Newest are kept.
    assert "k01500" not in state.dispatch_events
    assert f"k{(DISPATCH_EVENT_RETENTION * 3) - 1:05d}" in state.dispatch_events

    with store.acquire_writer("app") as state:
        for i in range(PENDING_REVISION_RETENTION * 3):
            state.pending_revisions.append(
                {"seq": i, "principal_id": "telegram:1", "applied": True})
        state.prune_bounded_ledgers()
        store.save(state)

    state = store.load("app")
    assert len(state.pending_revisions) == PENDING_REVISION_RETENTION


def test_pruning_never_drops_a_claimed_or_unapplied_entry(tmp_path):
    """The two entries pruning must never lose:
    a CLAIMED dispatch claim (a possibly-remote in-flight operation whose
    evidence must survive) and an unapplied revision reservation (what the
    revision re-drive path looks for).
    """
    from app.core.state import DISPATCH_EVENT_RETENTION, PENDING_REVISION_RETENTION

    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer("app") as state:
        state.roles = {"owner": "telegram:1", "reviewers": [], "viewers": []}
        for i in range(DISPATCH_EVENT_RETENTION * 2):
            state.dispatch_events[f"old{i:05d}"] = {
                "action": "build", "status": "DONE"}
        # The oldest claim is still in flight.
        state.dispatch_events["old00000"] = {
            "action": "build", "status": "CLAIMED", "reached_remote": True}
        state.prune_bounded_ledgers()
        store.save(state)

    assert store.load("app").dispatch_events["old00000"]["status"] == "CLAIMED"

    with store.acquire_writer("app") as state:
        for i in range(PENDING_REVISION_RETENTION * 2):
            state.pending_revisions.append(
                {"seq": i, "principal_id": "telegram:1", "applied": True})
        # The oldest reservation has not been applied yet.
        state.pending_revisions[0]["applied"] = False
        state.prune_bounded_ledgers()
        store.save(state)

    revisions = store.load("app").pending_revisions
    assert any(not e.get("applied") for e in revisions), revisions


def test_pruning_is_a_no_op_below_the_retention_bound(tmp_path):
    from app.core.state import DISPATCH_EVENT_RETENTION

    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer("app") as state:
        state.roles = {"owner": "telegram:1", "reviewers": [], "viewers": []}
        for i in range(10):
            state.dispatch_events[f"k{i}"] = {"action": "intake", "status": "DONE"}
        state.prune_bounded_ledgers()
        store.save(state)

    assert len(store.load("app").dispatch_events) == 10
    assert DISPATCH_EVENT_RETENTION > 10


def test_dispatch_claim_written_this_turn_is_always_retained(tmp_path):
    """The pruning call shares the writer block with the claim append, so the
    claim just written can never be the one discarded."""
    from app.core.state import DISPATCH_EVENT_RETENTION

    store = _store_with_project(tmp_path, lifecycle=ProjectLifecycle.READY.value)
    dispatch = TelegramDispatcher(store, IntakeProcessor(store, hermes_adapter=None))
    for i in range(DISPATCH_EVENT_RETENTION + 20):
        dispatch.dispatch(
            _payload(i, "Persisted shop for visit, a cafe called Persisted "
                        "that sells coffee for visit"),
            "app", "intake", authenticated=_owner())

    state = store.load("app")
    assert len(state.dispatch_events) <= DISPATCH_EVENT_RETENTION
    assert state.dispatch_events
    # The most recent claim is the last one, and is terminal.
    newest = list(state.dispatch_events)[-1]
    assert state.dispatch_events[newest]["status"] in ("DONE", "FAILED")


def test_read_still_enforces_authorization(tmp_path):
    store = _store_with_project(tmp_path)
    access = ProjectAccess(store)
    try:
        access.read("app", principal_id="telegram:999")
    except AuthzError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("unauthorized read must fail")


# ---------------------------------------------------------------------------
# BUG-09 / BUG-10: every code the system can emit has deliberate user copy
# ---------------------------------------------------------------------------

def test_every_emittable_error_code_has_specific_user_copy():
    """IMPATIENT / UNLUCKY USER: the reply must name the real problem.

    These are the codes the orchestrators actually surface. Each must render to
    its own deliberate message, NOT the generic fallback — that is exactly how
    WORKER_BUSY and OUTPUT_COMMIT_FAILED previously became invisible
    "Something went wrong" with no hint to retry.
    """
    from app.runtime import ERROR_MESSAGES, _FALLBACK_ERROR_TEXT, render_error_message

    for code in (
        # lifecycle / storage preconditions
        "PROJECT_NOT_ACCEPTING_INPUT",
        "WRITER_LOCK_TIMEOUT",
        "DISPATCH_INTERNAL_ERROR",
        # contention
        "WORKER_BUSY",
        "PREVIEW_BUSY",
        # local packaging
        "OUTPUT_COMMIT_FAILED",
        # QA preconditions
        "STALE_QA_BINDING",
        "VISION_REQUIRED",
    ):
        assert code in ERROR_MESSAGES, f"{code} has no user-facing copy"
        rendered = render_error_message(code)
        assert rendered != _FALLBACK_ERROR_TEXT, f"{code} renders generically"
        assert rendered == ERROR_MESSAGES[code]

    # The genuinely-unexplained codes stay generic BY DECISION, and are listed.
    from app.runtime import _INTENTIONALLY_GENERIC_CODES
    for code in ("UNEXPECTED_BUILD_ERROR", "UNEXPECTED_QA_ERROR"):
        assert code in _INTENTIONALLY_GENERIC_CODES
        assert render_error_message(code) == "An unexpected error occurred. Please try again."


def test_prefixed_and_unknown_codes_render_safely():
    from app.runtime import _FALLBACK_ERROR_TEXT, render_error_message

    assert render_error_message("CHEAP_CHECKS_FAILED:npm_build") == (
        "Build verification failed (npm_build). Please try again.")
    assert render_error_message("INFRASTRUCTURE_ERROR:render_failed:boom") == (
        "An infrastructure error occurred during verification. Please try again.")
    # An unmapped code must never leak internals.
    unknown = render_error_message("SOME_FUTURE_CODE")
    assert unknown == _FALLBACK_ERROR_TEXT
    assert render_error_message(None) == _FALLBACK_ERROR_TEXT
    assert render_error_message("") == _FALLBACK_ERROR_TEXT


def test_error_reply_is_dropped_rather_than_sent_to_a_fake_chat(tmp_path):
    """An unresolvable chat id must not send to a placeholder like "unknown":
    the transport rejects it, so the user silently gets nothing."""
    from app.core.contracts import OperationResult
    from app.runtime import ERROR_MESSAGES, TelegramReceiveLoop

    store = _store_with_project(tmp_path)
    telegram_out = MagicMock()
    telegram_out.send_text.return_value = OperationResult.ok()
    loop = TelegramReceiveLoop(
        bot_token="t", dispatcher=MagicMock(), telegram_out=telegram_out)

    loop._send_error_reply(None, "WORKER_BUSY")
    loop._send_error_reply("", "WORKER_BUSY")
    assert telegram_out.send_text.call_count == 0, "must not send to a placeholder chat"

    loop._send_error_reply("555", "WORKER_BUSY")
    assert telegram_out.send_text.call_count == 1
    chat, text = telegram_out.send_text.call_args[0]
    assert chat == "555"
    # Specific copy, not the generic fallback, and it tells the user to retry.
    assert text == ERROR_MESSAGES["WORKER_BUSY"]
    assert "try again shortly" in text.lower()
