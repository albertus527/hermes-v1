"""Local behavioral tests for Phase 9 preview orchestration using fake adapters."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.deploy.git_output import OutputGitRepository
from app.deploy.preview import PreviewDeps, PreviewOrchestrator
from app.deploy.snapshot import TestedSnapshot, source_fingerprint
from app.sandbox.runner import ProjectRunner


def _make_workspace(tmp_path):
    ws = tmp_path / 'ws'
    (ws / 'src').mkdir(parents=True)
    (ws / 'dist').mkdir()
    (ws / 'src' / 'App.tsx').write_bytes(b'x=1')
    (ws / 'dist' / 'index.html').write_bytes(b'<html>hi</html>')
    return ws


def _preview_ready_state(store, project_id, workspace):
    src_sha = source_fingerprint(workspace)
    snap = TestedSnapshot.capture(workspace)
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.RUNNING.value
        state.revisions.source_revision = 1
        state.revisions.qa_revision = 1
        state.deployment['checked'] = {
            'source_revision': 1,
            'source_sha256': snap.source_sha256,
            'artifact_sha256': snap.artifact_sha256,
        }
        state.deployment['tested_snapshot'] = snap.to_dict()
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        store.save(state)
    return snap


class FakeVercel:
    def __init__(self):
        self.ensure_calls = 0
        self.deploy_calls = 0
        self.bootstrap_calls = 0
        self._project = {'id': 'prj_1', 'name': 'wb', 'accountId': 'team'}
        # Records every expected_name threaded by the orchestrator so tests
        # can assert the canonical slug/legacy-None contract end to end.
        self.expected_names = []

    def ensure_project(self, app_id):
        self.ensure_calls += 1
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def ensure_project_with_slug(self, app_id, slug):
        self.ensure_calls += 1
        return OperationResult.ok({'project': self._project, 'app_id': app_id, 'slug': slug})

    def lookup_project(self, app_id):
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def ensure_bootstrap(self, app_id, project, expected_name=None):
        self.bootstrap_calls += 1
        self.expected_names.append(expected_name)
        return OperationResult.ok({'bootstrapped': False, 'already_current_production': True})

    def deploy_static_files(self, app_id, project, files, operation_id, source_revision, artifact_sha256, expected_name=None):
        self.deploy_calls += 1
        self.expected_names.append(expected_name)
        return OperationResult.ok({'deployment_id': 'dpl_1',
                                   'preview_url': 'https://tested.vercel.app',
                                   'state': 'READY'})

    def find_deployment_by_operation_id(self, app_id, project, operation_id, source_revision, artifact_sha256, expected_name=None):
        self.expected_names.append(expected_name)
        return OperationResult.ok({'deployment_id': 'dpl_1',
                                   'preview_url': 'https://tested.vercel.app',
                                   'state': 'READY'})


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_photo(self, chat_id, path, caption=''):
        self.sent.append(('photo', chat_id, path))
        return OperationResult.ok({'message_id': 1})

    def send_text(self, chat_id, text):
        self.sent.append(('text', chat_id, text))
        return OperationResult.ok({'message_id': 2})


class FakeSmoke:
    def __init__(self, success=True):
        self.success = success

    def run(self, url, out_dir):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        shot = Path(out_dir) / 'desktop.png'
        shot.write_bytes(b'\x89PNG\r\n\x1a\nfake')
        return OperationResult(success=self.success,
                               data={'desktop_screenshot': str(shot), 'mobile_screenshot': str(shot), 'url': url, 'failures': [] if self.success else ['x']},
                               error_code=None if self.success else 'SMOKE_FAILED')


def _deps(tmp_path, vercel=None, telegram=None, smoke=None, chat_id='123'):
    repo = OutputGitRepository(tmp_path / 'out', hermes_root=tmp_path / 'hermes')
    return PreviewDeps(
        vercel=vercel or FakeVercel(),
        telegram=telegram or FakeTelegram(),
        smoke=smoke or FakeSmoke(),
        output_repo=repo,
        chat_id_for=lambda pid, state: chat_id,
    )


def test_full_preview_flow_marks_latest_shown(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)

    result = orch.run_owned('proj', ws)

    assert result.success, result.error
    state = store.load('proj')
    assert state.revisions.preview_revision == 1
    assert state.deployment['latest_shown_preview']['preview_url'] == 'https://tested.vercel.app'
    assert len(deps.telegram.sent) == 2


# ---------------------------------------------------------------------------
# BLOCKER-1 — preview delivery reconciliation (at-most-once, fail-closed).
# ---------------------------------------------------------------------------


class _OutcomeTelegram(FakeTelegram):
    """Telegram fake whose photo/text sends return a scripted outcome."""

    def __init__(self, photo=None, text=None):
        super().__init__()
        self.photo = photo or OperationResult.ok({'message_id': 1})
        self.text = text or OperationResult.ok({'message_id': 2})

    def send_photo(self, chat_id, path, caption=''):
        self.sent.append(('photo', chat_id, path))
        return self.photo

    def send_text(self, chat_id, text):
        self.sent.append(('text', chat_id, text))
        return self.text


def _seed_intent(store, project_id, **fields):
    with store.acquire_writer(project_id) as state:
        intent = state.deployment.get('preview_intent') or {}
        intent.update(fields)
        state.deployment['preview_intent'] = intent
        store.save(state)


def _run_with_seeded_intent(tmp_path, fields, **deps_kw):
    """Run the exact delivery block by seeding a matching preview_intent
    (the orchestrator's own op-id is deterministic, so re-running reaches the
    delivery stage with prior delivery evidence present)."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path, **deps_kw)
    orch = PreviewOrchestrator(store, deps)
    # First run to mint the deterministic operation_id and intent row.
    first = orch.run_owned('proj', ws)
    assert first.success
    # Reset to PREVIEW_READY without latest_shown_preview so a re-run
    # re-enters delivery for the SAME operation_id, then seed the evidence.
    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        store.save(state)
    _seed_intent(store, 'proj', **fields)
    deps.telegram.sent.clear()
    return orch, deps, store


def test_delivery_provably_unsent_photo_redrives(tmp_path):
    """(b)/(i) an explicit Telegram rejection on the photo is provably
    NOT_SENT -> a re-drive may proceed and deliver."""
    # Model the prior attempt as NOT_SENT.
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)
    first = orch.run_owned('proj', ws)
    assert first.success
    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        # Prior delivery provably did not send the photo.
        state.deployment['preview_intent']['photo_attempted'] = True
        state.deployment['preview_intent']['photo_outcome'] = 'NOT_SENT'
        store.save(state)
    deps.telegram.sent.clear()

    result = orch.run_owned('proj', ws)

    assert result.success, result.error
    assert any(kind == 'photo' for kind, *_ in deps.telegram.sent)


def test_delivery_ambiguous_photo_never_duplicates(tmp_path):
    """(d)/(g) an ambiguous transport failure on the photo is PENDING ->
    fail closed, never resend a possibly-delivered photo."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)
    assert orch.run_owned('proj', ws).success
    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        state.deployment['preview_intent']['photo_attempted'] = True
        state.deployment['preview_intent']['photo_outcome'] = 'PENDING'
        store.save(state)
    deps.telegram.sent.clear()

    result = orch.run_owned('proj', ws)

    assert not result.success
    assert result.error_code == 'DELIVERY_RECONCILIATION_REQUIRED'
    assert deps.telegram.sent == []  # no duplicate photo


def test_delivery_ambiguous_text_never_duplicates(tmp_path):
    """(h) ambiguous text PENDING -> fail closed, never resend text."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)
    assert orch.run_owned('proj', ws).success
    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        # Photo already SENT (confirmed), text ambiguous.
        state.deployment['preview_intent']['photo_attempted'] = True
        state.deployment['preview_intent']['photo_outcome'] = 'SENT'
        state.deployment['preview_intent']['photo_message_id'] = 1
        state.deployment['preview_intent']['text_attempted'] = True
        state.deployment['preview_intent']['text_outcome'] = 'PENDING'
        store.save(state)
    deps.telegram.sent.clear()

    result = orch.run_owned('proj', ws)

    assert not result.success
    assert result.error_code == 'DELIVERY_RECONCILIATION_REQUIRED'
    assert deps.telegram.sent == []


def test_delivery_legacy_attempt_without_outcome_fails_closed(tmp_path):
    """(f) BACKWARD COMPAT: a legacy row with *_attempted True but no
    *_outcome is UNKNOWN -> fail closed, never resent (missing != safe)."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)
    assert orch.run_owned('proj', ws).success
    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        intent = state.deployment['preview_intent']
        intent['photo_attempted'] = True
        intent.pop('photo_outcome', None)
        intent.pop('text_outcome', None)
        store.save(state)
    deps.telegram.sent.clear()

    result = orch.run_owned('proj', ws)

    assert not result.success
    assert result.error_code == 'DELIVERY_RECONCILIATION_REQUIRED'
    assert deps.telegram.sent == []


def test_delivery_crash_between_photo_and_text(tmp_path):
    """(f) crash after photo SENT but before text: photo is confirmed SENT
    (no duplicate) and the text is not yet attempted, so delivery completes
    with exactly one new text message."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)
    assert orch.run_owned('proj', ws).success
    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        intent = state.deployment['preview_intent']
        # Model the crash window exactly: the photo was delivered and
        # confirmed SENT; the process died BEFORE the text send, so the text
        # was never attempted and carries no outcome yet.
        intent['photo_attempted'] = True
        intent['photo_outcome'] = 'SENT'
        intent['photo_message_id'] = 1
        intent.pop('text_attempted', None)
        intent.pop('text_outcome', None)
        intent.pop('text_message_id', None)
        store.save(state)
    deps.telegram.sent.clear()

    result = orch.run_owned('proj', ws)

    assert result.success, result.error
    kinds = [k for k, *_ in deps.telegram.sent]
    assert kinds == ['text']  # photo not resent; text delivered once
    assert store.load('proj').deployment['latest_shown_preview']['operation_id']


def test_delivery_before_remote_send_succeeds_normally(tmp_path):
    """(a)/(c) no prior attempt -> both sends happen exactly once and the
    outcomes are persisted as SENT."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)

    result = orch.run_owned('proj', ws)

    assert result.success, result.error
    assert [k for k, *_ in deps.telegram.sent] == ['photo', 'text']
    intent = store.load('proj').deployment['preview_intent']
    assert intent['photo_outcome'] == 'SENT'
    assert intent['text_outcome'] == 'SENT'


def test_delivery_rejected_photo_marks_not_sent(tmp_path):
    """(b) an explicit Telegram rejection is persisted as NOT_SENT (distinct
    from ambiguous), so a later run may safely re-drive it."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    rejected = OperationResult.fail('TELEGRAM_REJECTED', error_code='TELEGRAM_REJECTED')
    deps = _deps(tmp_path, telegram=_OutcomeTelegram(photo=rejected))
    orch = PreviewOrchestrator(store, deps)

    result = orch.run_owned('proj', ws)

    assert not result.success
    intent = store.load('proj').deployment['preview_intent']
    assert intent['photo_outcome'] == 'NOT_SENT'


def test_delivery_ambiguous_photo_marks_pending(tmp_path):
    """(d)/(e) an ambiguous transport failure is persisted as PENDING (not
    NOT_SENT), so a later run fails closed rather than duplicating."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    ambiguous = OperationResult.fail('AMBIGUOUS_SEND', error_code='AMBIGUOUS_SEND')
    deps = _deps(tmp_path, telegram=_OutcomeTelegram(photo=ambiguous))
    orch = PreviewOrchestrator(store, deps)

    result = orch.run_owned('proj', ws)

    assert not result.success
    intent = store.load('proj').deployment['preview_intent']
    assert intent['photo_outcome'] == 'PENDING'


def test_requires_qa_bound_tested_snapshot(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    with store.acquire_writer('proj') as state:
        state.lifecycle = ProjectLifecycle.RUNNING.value
        store.save(state)
    deps = _deps(tmp_path)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert not result.success
    assert result.error_code == 'QA_REQUIRED'


def test_rejects_source_drift_since_qa(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    (ws / 'src' / 'App.tsx').write_bytes(b'x=2')  # drift after QA bound snapshot
    deps = _deps(tmp_path)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert not result.success
    assert 'STALE_SOURCE' in (result.error_code or '') or 'STALE' in (result.error or '')


def test_smoke_failure_blocks_delivery(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path, smoke=FakeSmoke(success=False))
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    assert not deps.telegram.sent
    state = store.load('proj')
    assert state.revisions.preview_revision == 0


def test_no_delivery_target_fails_closed_after_smoke(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path, chat_id=None)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert not result.success
    assert result.error_code == 'NO_DELIVERY_TARGET'


def test_deploy_ambiguous_failure_reconciled_via_lookup_not_resend(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    class FlakyVercel(FakeVercel):
        def deploy_static_files(self, *a, **kw):
            self.deploy_calls += 1
            return OperationResult.fail('DEPLOYMENT_RECONCILIATION_REQUIRED',
                                        error_code='DEPLOYMENT_RECONCILIATION_REQUIRED')

    vercel = FlakyVercel()
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert result.success
    assert vercel.deploy_calls == 1  # never blindly resent


def test_second_run_after_shown_reuses_deterministic_git_branch(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    snap = _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    orch = PreviewOrchestrator(store, deps)
    first = orch.run_owned('proj', ws)
    assert first.success
    identity = deps.output_repo.commit('proj', snap)
    assert identity['branch'].endswith(snap.identity)


def test_deployment_already_live_never_shown_as_preview(tmp_path):
    """Regression: if the Vercel adapter proves a deployment is already the
    project's CANONICAL production target (per app/deploy/adapters.py's
    production-binding check), the orchestrator must never mark it as
    latest_shown_preview, never send the Telegram preview, and never resend
    on the reconciliation fallback.
    """
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    class AlreadyLiveVercel(FakeVercel):
        def deploy_static_files(self, *a, **kw):
            self.deploy_calls += 1
            return OperationResult.fail('DEPLOYMENT_ALREADY_LIVE',
                                        error_code='DEPLOYMENT_ALREADY_LIVE')

        def find_deployment_by_operation_id(self, *a, **kw):
            return OperationResult.fail('DEPLOYMENT_ALREADY_LIVE',
                                        error_code='DEPLOYMENT_ALREADY_LIVE')

    vercel = AlreadyLiveVercel()
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)

    assert not result.success
    assert result.error_code == 'DEPLOYMENT_ALREADY_LIVE'
    assert vercel.deploy_calls == 1  # never blindly resent
    state = store.load('proj')
    assert 'latest_shown_preview' not in state.deployment
    assert not deps.telegram.sent


# ---------------------------------------------------------------------------
# Bootstrap confirmation barrier: real-content deploy must never happen
# before ensure_bootstrap proves (via remote provider truth) that the
# bootstrap deployment actually became current production.
# ---------------------------------------------------------------------------

def test_real_content_deploy_never_called_before_bootstrap_confirms(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    class UnconfirmedBootstrapVercel(FakeVercel):
        def ensure_bootstrap(self, app_id, project, expected_name=None):
            self.bootstrap_calls += 1
            # Not yet confirmed -- must block the real content deploy.
            return OperationResult.fail('BOOTSTRAP_RECONCILIATION_REQUIRED',
                                        error_code='BOOTSTRAP_RECONCILIATION_REQUIRED')

    vercel = UnconfirmedBootstrapVercel()
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)

    assert not result.success
    assert result.error_code == 'BOOTSTRAP_RECONCILIATION_REQUIRED'
    assert vercel.bootstrap_calls == 1
    assert vercel.deploy_calls == 0  # real content POST never issued
    assert not deps.telegram.sent


def test_real_content_deploy_blocked_on_bootstrap_confirmation_timeout(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    class TimingOutBootstrapVercel(FakeVercel):
        def ensure_bootstrap(self, app_id, project, expected_name=None):
            self.bootstrap_calls += 1
            return OperationResult.fail('BOOTSTRAP_CONFIRMATION_TIMEOUT',
                                        error_code='BOOTSTRAP_CONFIRMATION_TIMEOUT')

    vercel = TimingOutBootstrapVercel()
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)

    assert not result.success
    assert result.error_code == 'BOOTSTRAP_CONFIRMATION_TIMEOUT'
    assert vercel.deploy_calls == 0
    assert not deps.telegram.sent


def test_real_content_deploy_proceeds_exactly_once_after_bootstrap_confirmed(tmp_path):
    """Once ensure_bootstrap reports confirmed success, exactly one real
    content deploy follows -- the normal happy path, made explicit as a
    regression against ever double-deploying or skipping the deploy."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    vercel = FakeVercel()  # ensure_bootstrap already returns confirmed-style ok
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)

    assert result.success
    assert vercel.bootstrap_calls == 1
    assert vercel.deploy_calls == 1


# ---------------------------------------------------------------------------
# HIGH-5: preview-worker lock removal must not allow parallel preview
# execution — the ProjectRunner single worker slot (MAX_WORKERS=1) is the
# serializer. Dispatch-side callers acquire it; build/revise callers hold it.
# ---------------------------------------------------------------------------


def _slot_guarded_orch(tmp_path, runner):
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    deps = _deps(tmp_path)
    return store, ws, PreviewOrchestrator(store, deps, runner=runner)


def test_external_caller_blocked_when_worker_slot_held(tmp_path):
    """A dispatch-side reconcile while a build/preview owns the slot fails busy."""
    runner = ProjectRunner(tmp_path / 'workspaces', ProjectStateStore(tmp_path / 'rs'))
    assert runner.acquire_project('other-build')
    try:
        _, ws, orch = _slot_guarded_orch(tmp_path, runner)
        result = orch.run_owned('proj', ws)
    finally:
        runner.release_project('other-build')
    assert not result.success
    assert result.error_code == 'PREVIEW_BUSY'


def test_external_caller_acquires_and_releases_slot(tmp_path):
    """A dispatch-side reconcile acquires the slot and releases it afterwards."""
    runner = ProjectRunner(tmp_path / 'workspaces', ProjectStateStore(tmp_path / 'rs'))
    _, ws, orch = _slot_guarded_orch(tmp_path, runner)
    result = orch.run_owned('proj', ws)
    assert result.success
    # Slot released after the run — a new mutation may proceed.
    assert runner.acquire_project('next-op')
    runner.release_project('next-op')


def test_slot_held_caller_bypasses_acquisition(tmp_path):
    """Build/revise already own the slot — nested preview must not reacquire."""
    runner = ProjectRunner(tmp_path / 'workspaces', ProjectStateStore(tmp_path / 'rs'))
    _, ws, orch = _slot_guarded_orch(tmp_path, runner)
    assert runner.acquire_project('proj')
    try:
        result = orch.run_owned('proj', ws, slot_held=True)
        assert result.success
    finally:
        runner.release_project('proj')


# ---------------------------------------------------------------------------
# tg-6329821361-p4 regression: the canonical friendly slug resolved BEFORE
# ensure_project_with_slug must be threaded as expected_name through every
# downstream adapter call that revalidates the owned project -- never
# re-deriving the opaque hash name (the unfixed failure class).
# ---------------------------------------------------------------------------


class _SlugEnforcingVercel(FakeVercel):
    """Fake adapter mirroring _project_valid semantics for a slug-named
    project: the owned project exists under 'dapur-kedaton'; any downstream
    call whose expected_name is anything else (including None, which means
    're-derive the opaque hash name' at the real adapter) fails closed with
    PROJECT_IDENTITY_MISMATCH -- exactly what the real adapter did to p4
    before the orchestrator threaded the canonical slug."""

    def __init__(self):
        super().__init__()
        self._project = {'id': 'prj_1', 'name': 'dapur-kedaton', 'accountId': 'team'}

    def _identity_gate(self, expected_name):
        self.expected_names.append(expected_name)
        if expected_name != 'dapur-kedaton':
            return OperationResult.fail('PROJECT_IDENTITY_MISMATCH',
                                        error_code='PROJECT_IDENTITY_MISMATCH')
        return None

    def ensure_bootstrap(self, app_id, project, expected_name=None):
        self.bootstrap_calls += 1
        bad = self._identity_gate(expected_name)
        return bad or OperationResult.ok({'bootstrapped': False,
                                          'already_current_production': True})

    def deploy_static_files(self, app_id, project, files, operation_id,
                            source_revision, artifact_sha256, expected_name=None):
        self.deploy_calls += 1
        bad = self._identity_gate(expected_name)
        return bad or OperationResult.ok({'deployment_id': 'dpl_1',
                                          'preview_url': 'https://tested.vercel.app',
                                          'state': 'READY'})

    def find_deployment_by_operation_id(self, app_id, project, operation_id,
                                        source_revision, artifact_sha256, expected_name=None):
        bad = self._identity_gate(expected_name)
        return bad or OperationResult.ok({'deployment_id': 'dpl_1',
                                          'preview_url': 'https://tested.vercel.app',
                                          'state': 'READY'})


def test_canonical_slug_threaded_through_all_downstream_identity_checks(tmp_path):
    """Full p4 production sequence at orchestrator level: slug_for resolves
    'dapur-kedaton' -> ensure_project_with_slug -> same returned project ->
    ensure_bootstrap -> deploy_static_files -> readiness lookup. Every
    downstream revalidation must receive the canonical slug."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    vercel = _SlugEnforcingVercel()
    deps = _deps(tmp_path, vercel=vercel)
    deps.slug_for = lambda pid, state: 'dapur-kedaton'
    deps.bind_slug = lambda pid, state, slug: None
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert result.success, result.error
    assert vercel.bootstrap_calls == 1
    assert vercel.deploy_calls == 1
    # Bootstrap + deploy + readiness poll(s): every gate saw the canonical slug.
    assert vercel.expected_names
    assert all(name == 'dapur-kedaton' for name in vercel.expected_names)


def test_no_slug_configured_threads_none_preserving_legacy_opaque_path(tmp_path):
    """When no slug is bound/derivable the orchestrator threads None, so the
    adapter validates against the legacy opaque hash-derived name exactly as
    before -- no behavior change for pre-slug projects."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    vercel = FakeVercel()
    deps = _deps(tmp_path, vercel=vercel)  # slug_for defaults to None
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert result.success, result.error
    assert vercel.expected_names
    assert all(name is None for name in vercel.expected_names)
