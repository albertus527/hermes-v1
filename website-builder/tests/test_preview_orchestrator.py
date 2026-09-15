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
        self._project = {'id': 'prj_1', 'name': 'wb', 'accountId': 'team'}

    def ensure_project(self, app_id):
        self.ensure_calls += 1
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def lookup_project(self, app_id):
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def deploy_static_files(self, app_id, project, files, operation_id, source_revision, artifact_sha256):
        self.deploy_calls += 1
        return OperationResult.ok({'deployment_id': 'dpl_1',
                                   'preview_url': 'https://tested.vercel.app',
                                   'state': 'READY'})

    def find_deployment_by_operation_id(self, app_id, project, operation_id, source_revision, artifact_sha256):
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
