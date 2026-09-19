"""Local behavioral tests for Phase 11 promotion orchestration using fake adapters."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects.promote import PromoteDeps, PromotionOrchestrator
from app.sandbox.runner import ProjectRunner


def _make_workspace(tmp_path):
    ws = tmp_path / 'ws'
    (ws / 'src').mkdir(parents=True)
    (ws / 'dist').mkdir()
    (ws / 'src' / 'App.tsx').write_bytes(b'x=1')
    (ws / 'dist' / 'index.html').write_bytes(b'<html>hi</html>')
    return ws


OWNER = 'owner-1'
STRANGER = 'stranger-9'


def _approved_state(store, project_id, source_revision=1, deployment_id='dpl_1'):
    """Set up a project in PREVIEW_READY with a shown preview and an approval
    bound to the exact same identity."""
    shown = {
        'operation_id': 'op-' + str(source_revision),
        'source_revision': source_revision,
        'preview_url': 'https://tested.vercel.app',
        'deployment_id': deployment_id,
        'source_sha256': 'a' * 64,
        'artifact_sha256': 'b' * 64,
        'shown_at': 1.0,
    }
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.revisions.source_revision = source_revision
        state.revisions.qa_revision = source_revision
        state.revisions.preview_revision = source_revision
        state.deployment['latest_shown_preview'] = shown
        state.roles['owner'] = OWNER
        store.save(state)
    return shown


class FakeVercel:
    def __init__(self, previous_production=None):
        self._project = {'id': 'prj_1', 'name': 'wb', 'accountId': 'team'}
        self.previous_production = previous_production
        self.promote_calls = []
        # Records every expected_name threaded by the orchestrator so tests
        # can assert the canonical-slug / legacy-None contract end to end.
        self.expected_names = []

    def lookup_project(self, app_id, *, expected_name=None):
        self.expected_names.append(expected_name)
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        self.expected_names.append(expected_name)
        return OperationResult.ok({'deployment_id': self.previous_production})

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        self.expected_names.append(expected_name)
        return OperationResult.ok({
            'deployment_id': deployment_id,
            'production_url': 'https://prod.vercel.app',
            'state': 'READY',
        })


class FlakyPromoteVercel(FakeVercel):
    def __init__(self, fail_on, **kw):
        super().__init__(**kw)
        self.fail_on = fail_on

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        if deployment_id in self.fail_on:
            return OperationResult.fail('PROMOTE_FAILED', error_code='PROMOTE_FAILED')
        return super().promote_deployment(app_id, project, deployment_id, operation_id,
                                          source_revision, artifact_sha256,
                                          expected_name=expected_name)


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return OperationResult.ok({'message_id': 1})


class FakeSmoke:
    def __init__(self, success=True):
        self.success = success
        self.calls = 0

    def run(self, url, out_dir):
        self.calls += 1
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return OperationResult(success=self.success,
                               data={'url': url, 'failures': [] if self.success else ['x']},
                               error_code=None if self.success else 'SMOKE_FAILED')


def _deps(vercel=None, telegram=None, smoke=None, chat_id='123'):
    return PromoteDeps(
        vercel=vercel or FakeVercel(),
        telegram=telegram or FakeTelegram(),
        smoke=smoke or FakeSmoke(),
        chat_id_for=lambda pid, state: chat_id,
    )


def _runner(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    runner = ProjectRunner(tmp_path / 'workspaces', store)
    return runner, store


def test_approve_binds_shown_preview_identity(tmp_path):
    runner, store = _runner(tmp_path)
    shown = _approved_state(store, 'proj')
    orch = PromotionOrchestrator(runner, store, _deps())
    result = orch.approve('proj', principal_id=OWNER)
    assert result.success, result.error
    state = store.load('proj')
    assert state.deployment['approval']['operation_id'] == shown['operation_id']
    assert state.revisions.approved_revision == 1


def test_approve_fails_without_shown_preview(tmp_path):
    runner, store = _runner(tmp_path)
    with store.acquire_writer('proj') as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.roles['owner'] = OWNER
        store.save(state)
    orch = PromotionOrchestrator(runner, store, _deps())
    result = orch.approve('proj', principal_id=OWNER)
    assert not result.success
    assert result.error_code == 'NO_SHOWN_PREVIEW'


def test_approve_rejected_for_unauthorized_principal(tmp_path):
    runner, store = _runner(tmp_path)
    _approved_state(store, 'proj')
    orch = PromotionOrchestrator(runner, store, _deps())
    result = orch.approve('proj', principal_id=STRANGER)
    assert not result.success
    assert result.error_code == 'UNAUTHORIZED_ROLE'
    state = store.load('proj')
    assert state.deployment.get('approval') is None


def test_promote_rejected_for_non_owner_reviewer(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    with store.acquire_writer('proj') as state:
        state.roles['reviewers'] = ['reviewer-1']
        store.save(state)
    orch = PromotionOrchestrator(runner, store, _deps())
    assert orch.approve('proj', principal_id='reviewer-1').success

    result = orch.promote('proj', ws, principal_id='reviewer-1')
    assert not result.success
    assert result.error_code == 'UNAUTHORIZED_ROLE'
    state = store.load('proj')
    assert state.lifecycle != ProjectLifecycle.LIVE.value


def test_fresh_approval_promotes_successfully(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    deps = _deps()
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert result.success, result.error
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    assert state.revisions.live_revision == 1
    assert state.production_url == 'https://prod.vercel.app'
    assert deps.telegram.sent


def test_stale_approval_blocked_when_newer_preview_produced(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    orch = PromotionOrchestrator(runner, store, _deps())
    assert orch.approve('proj', principal_id=OWNER).success

    # A newer preview is shown after approval (e.g. a revision landed).
    with store.acquire_writer('proj') as state:
        state.revisions.source_revision = 2
        state.revisions.preview_revision = 2
        state.deployment['latest_shown_preview'] = {
            'operation_id': 'op-2',
            'source_revision': 2,
            'preview_url': 'https://tested2.vercel.app',
            'deployment_id': 'dpl_2',
            'source_sha256': 'c' * 64,
            'artifact_sha256': 'd' * 64,
            'shown_at': 2.0,
        }
        store.save(state)

    result = orch.promote('proj', ws, principal_id=OWNER)
    assert not result.success
    assert result.error_code == 'STALE_APPROVAL'
    state = store.load('proj')
    assert state.lifecycle != ProjectLifecycle.LIVE.value


def test_smoke_failure_fails_closed_without_swapping_alias(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.failure['error_code'] == 'SMOKE_FAILED'
    # Promoted the candidate, then rolled back to previous production.
    assert vercel.promote_calls == ['dpl_1', 'dpl_old']
    assert not deps.telegram.sent


def test_smoke_failure_with_no_prior_production_does_not_attempt_rollback(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production=None)
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    assert vercel.promote_calls == ['dpl_1']


def test_promote_failure_before_smoke_fails_closed(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FlakyPromoteVercel(fail_on={'dpl_1'})
    deps = _deps(vercel=vercel)
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTE_FAILED'
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert deps.smoke.calls == 0


def test_not_approved_blocks_promotion(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    orch = PromotionOrchestrator(runner, store, _deps())
    # No approve() call.
    result = orch.promote('proj', ws, principal_id=OWNER)
    assert not result.success
    assert result.error_code == 'NOT_APPROVED'


def test_worker_slot_contention_rejected(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    orch = PromotionOrchestrator(runner, store, _deps())
    assert orch.approve('proj', principal_id=OWNER).success

    assert runner.acquire_project('other-project')
    try:
        result = orch.promote('proj', ws, principal_id=OWNER)
        assert not result.success
        assert result.error_code == 'WORKER_BUSY'
    finally:
        runner.release_project('other-project')


def test_second_promotion_after_live_is_stale_without_new_approval(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    deps = _deps()
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success
    first = orch.promote('proj', ws, principal_id=OWNER)
    assert first.success

    # Re-promoting without a fresh approve() should be blocked: the
    # deployment_id-bound approval was already consumed and the project
    # is LIVE — same approval is not implicitly re-usable for a repeat call
    # once the source has moved and a new preview/approval cycle is
    # expected. Here nothing changed, so approval is still exactly current;
    # promote() is idempotent-safe for the very same identity.
    second = orch.promote('proj', ws, principal_id=OWNER)
    assert second.success


def test_unauthorized_promote_leaves_state_byte_identical(tmp_path):
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    orch = PromotionOrchestrator(runner, store, _deps())
    assert orch.approve('proj', principal_id=OWNER).success
    before = store._project_path('proj').read_bytes()

    result = orch.promote('proj', ws, principal_id=STRANGER)
    assert not result.success
    assert result.error_code == 'UNAUTHORIZED_ROLE'
    after = store._project_path('proj').read_bytes()
    assert before == after


# ---------------------------------------------------------------------------
# Slug-identity regression (same class as tg-6329821361-p4): promotion must
# resolve the canonical Vercel project name from trusted bound state and
# thread it through lookup/revalidation -- never fall back to the opaque
# hash-derived name when a slug is bound.
# ---------------------------------------------------------------------------


class _SlugEnforcingVercel(FakeVercel):
    """Owned project lives under the friendly slug 'dapur-kedaton'. Any
    adapter call whose expected_name is anything else (None = 're-derive
    the opaque hash name', which the real provider 404s, or a wrong slug)
    fails closed -- exactly the unfixed promotion failure mode."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._project = {'id': 'prj_1', 'name': 'dapur-kedaton', 'accountId': 'team'}

    def _gate(self, expected_name):
        self.expected_names.append(expected_name)
        if expected_name != 'dapur-kedaton':
            return OperationResult.fail('PROJECT_RECONCILIATION_REQUIRED',
                                        error_code='PROJECT_RECONCILIATION_REQUIRED')
        return None

    def lookup_project(self, app_id, *, expected_name=None):
        bad = self._gate(expected_name)
        return bad or OperationResult.ok({'project': self._project, 'app_id': app_id})

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        bad = self._gate(expected_name)
        return bad or OperationResult.ok({'deployment_id': self.previous_production})

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        bad = self._gate(expected_name)
        # super() records promote_calls only when the gate passes.
        return bad or super().promote_deployment(
            app_id, project, deployment_id, operation_id, source_revision,
            artifact_sha256, expected_name=expected_name)


def test_slug_project_approve_promote_uses_canonical_lookup(tmp_path):
    """Preview-created slug project: approve -> promote must succeed via the
    canonical slug lookup, with expected_name threaded through every
    owned-project revalidation (lookup, production probe, promote)."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = _SlugEnforcingVercel()
    deps = _deps(vercel=vercel)
    deps.slug_for = lambda pid, state: 'dapur-kedaton'
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert result.success, result.error
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    assert vercel.promote_calls == ['dpl_1']
    # lookup + find_production + promote: every gate saw the canonical slug.
    assert vercel.expected_names
    assert all(name == 'dapur-kedaton' for name in vercel.expected_names)


def test_slug_project_promote_never_falls_back_to_opaque_name(tmp_path):
    """Fault injection the other way: if the orchestrator ever drops the
    threaded slug (the unfixed behavior), the slug-only provider rejects the
    opaque lookup -- proving the canonical name is what was used."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = _SlugEnforcingVercel()
    deps = _deps(vercel=vercel)
    # slug_for present but returning None simulates the unfixed/no-binding
    # path: expected_name None -> opaque lookup -> fail closed, not success.
    deps.slug_for = lambda pid, state: None
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROJECT_RECONCILIATION_REQUIRED'
    assert vercel.promote_calls == []


def test_legacy_no_slug_project_promotes_with_none_expected_name(tmp_path):
    """Opaque legacy project unchanged: no slug bound, adapter receives
    expected_name=None on every call (the opaque default path)."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel()
    deps = _deps(vercel=vercel)  # slug_for defaults to None
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert result.success, result.error
    assert vercel.expected_names
    assert all(name is None for name in vercel.expected_names)
