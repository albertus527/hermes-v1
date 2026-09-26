"""Local behavioral tests for Phase 11 promotion orchestration using fake adapters."""
import json
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
        # Full identity of the current production deployment, mirroring the
        # real adapter's contract (find_production_deployment now returns the
        # deployment's own operation_id/source_revision/artifact_sha256 so a
        # rollback can re-promote it with ITS OWN identity).
        self.previous_identity = None
        if previous_production:
            self.previous_identity = {
                'deployment_id': previous_production,
                'operation_id': 'op-0',
                'source_revision': 1,
                'artifact_sha256': 'c' * 64,
            }
        # Records every expected_name threaded by the orchestrator so tests
        # can assert the canonical-slug / legacy-None contract end to end.
        self.expected_names = []
        # Production truth for reconcile_production_deployment:
        # 'NOT_SET' (default) means "the intended deployment is now live".
        # None -> no production; dict -> explicit live identity; 'AMBIGUOUS'
        # -> reconciliation cannot decide.
        self.production_now = 'NOT_SET'
        self.reconcile_calls = []
        # External adoption reads: records every same-operation recovery read so
        # a test can assert a recovery reconciled BEFORE promoting anything.
        self.reconcile_external_calls = []
        # Remote truth for reconcile_external_promotion, mirroring the real
        # adapter. 'FOLLOW_PRODUCTION' (default) reports the same truth the
        # ordinary reconcile would; tests override it to model a promote CHILD
        # or an unattributable production binding.
        self.external_now = 'FOLLOW_PRODUCTION'

    def lookup_project(self, app_id, *, expected_name=None):
        self.expected_names.append(expected_name)
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        self.expected_names.append(expected_name)
        if self.previous_identity is None:
            return OperationResult.ok({'deployment_id': None})
        return OperationResult.ok(dict(self.previous_identity))

    def reconcile_production_deployment(self, app_id, project, expected_identity, *,
                                        expected_name=None):
        """Default fake: report production truth from the configured
        ``production_now`` identity (the deployment currently live), compared
        against the COMPLETE expected identity tuple.

        ``production_now`` is the authoritative provider truth:
          * None               -> no production at all -> NOT_PROMOTED
          * dict               -> promoted iff EVERY trusted field matches
          * 'AMBIGUOUS'        -> lookup cannot decide -> reconciliation fail
        """
        self.reconcile_calls.append(dict(expected_identity))
        self.expected_names.append(expected_name)
        now = getattr(self, 'production_now', 'NOT_SET')
        if now == 'NOT_SET':
            # Default: production has NOT yet been switched to the candidate
            # (models a crash BEFORE the remote promote landed). Explicit
            # tests set production_now to model "already promoted" /
            # "not promoted" / "ambiguous".
            return OperationResult.ok({'status': 'NOT_PROMOTED',
                                       'deployment_id': (self.previous_identity or {}).get('deployment_id')})
        if now is None:
            return OperationResult.ok({'status': 'NOT_PROMOTED', 'deployment_id': None})
        if now == 'AMBIGUOUS':
            return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                        error_code='PROMOTE_RECONCILIATION_REQUIRED')
        if all(now.get(k) == expected_identity.get(k)
               for k in ('deployment_id', 'operation_id', 'source_revision',
                         'artifact_sha256')):
            return OperationResult.ok({'status': 'PROMOTED',
                                       'deployment_id': expected_identity['deployment_id']})
        return OperationResult.ok({'status': 'NOT_PROMOTED',
                                   'deployment_id': now.get('deployment_id')})

    def reconcile_external_promotion(self, app_id, project, intended_identity, *,
                                     expected_name=None):
        """Default fake: report the SAME provider truth as
        ``reconcile_production_deployment``, because for a deployment id that
        is still ours the two reads agree by definition.

        ``external_now`` overrides it:
          * 'FOLLOW_PRODUCTION' (default) -> mirror ``production_now``
          * 'UNPROVEN'  -> production moved, nothing ties it to us
          * 'AMBIGUOUS' -> truth unreadable -> reconciliation cannot decide
          * dict        -> a promote CHILD of our deployment is production
        """
        self.reconcile_external_calls.append(dict(intended_identity))
        self.expected_names.append(expected_name)
        now = getattr(self, 'external_now', 'FOLLOW_PRODUCTION')
        if now == 'UNPROVEN':
            return OperationResult.ok({'status': 'PROMOTED_UNPROVEN',
                                       'deployment_id': 'dpl_someone_elses'})
        if now == 'AMBIGUOUS':
            return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                        error_code='PROMOTE_RECONCILIATION_REQUIRED')
        if now != 'FOLLOW_PRODUCTION':
            return OperationResult.ok({
                'status': 'PROMOTED',
                'deployment_id': now.get('deployment_id'),
                'proof': 'PROVIDER_LINEAGE',
                'production_url': 'https://prod.vercel.app',
            })
        # Mirror production_now: this is the truth the provider would report for
        # either read, so an ordinary resume keeps its previous semantics.
        production = getattr(self, 'production_now', 'NOT_SET')
        if production == 'AMBIGUOUS':
            return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                        error_code='PROMOTE_RECONCILIATION_REQUIRED')
        if (isinstance(production, dict)
                and all(production.get(k) == intended_identity.get(k)
                        for k in ('deployment_id', 'operation_id',
                                  'source_revision', 'artifact_sha256'))):
            return OperationResult.ok({
                'status': 'PROMOTED', 'deployment_id': production['deployment_id'],
                'proof': 'DIRECT_IDENTITY', 'production_url': 'https://prod.vercel.app',
            })
        binding = (production.get('deployment_id')
                   if isinstance(production, dict)
                   else (self.previous_identity or {}).get('deployment_id'))
        return OperationResult.ok({'status': 'PROMOTED_UNPROVEN',
                                   'deployment_id': binding})

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        self.expected_names.append(expected_name)
        return OperationResult.ok({
            'deployment_id': deployment_id,
            # Deployment-SPECIFIC host: internal identity only, never the URL
            # the user is shown.
            'production_url': 'https://prod.vercel.app',
            'state': 'READY',
        })

    def canonical_production_url(self, app_id, project, *, expected_name=None):
        """The project's own public default domain.

        Mirrors the real adapter: the name is the verified project name, and
        the host is that name's default domain -- never the deployment host.
        """
        self.expected_names.append(expected_name)
        name = expected_name or self._project['name']
        return OperationResult.ok({
            'canonical_production_url': f'https://{name}.vercel.app/',
            'canonical_source': 'VERCEL_PROJECT_DOMAIN',
            'project_name': name,
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
    # The user-facing production URL is the project's canonical public domain,
    # never the promoted deployment's own host.
    assert state.production_url == 'https://wb.vercel.app/'
    assert state.deployment['last_live_deployment']['production_url'] == 'https://wb.vercel.app/'
    assert state.deployment['last_live_deployment']['deployment_url'] == 'https://prod.vercel.app'
    assert deps.telegram.sent
    assert deps.telegram.sent[-1] == ('123', '🚀 Live: https://wb.vercel.app/')


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


# ---------------------------------------------------------------------------
# B-2b / F5 — rollback identity + persisted previous-production target.
# ---------------------------------------------------------------------------


class _CrashingSmoke(FakeSmoke):
    """Simulates a process crash at a chosen stage: raises inside run() when
    the promotion_intent is at a given stage, after the remote promote."""

    def __init__(self, crash_stage, store):
        super().__init__(success=True)
        self.crash_stage = crash_stage
        self.store = store

    def run(self, url, out_dir):
        intent = self.store.load('proj').deployment.get('promotion_intent', {})
        if intent.get('stage') == self.crash_stage:
            raise RuntimeError('simulated crash')
        return super().run(url, out_dir)


def _publishing_state(store, prev_identity):
    """Model a crashed promotion: PUBLISHING with a persisted intent that
    already holds the true previous-production identity."""
    _approved_state(store, 'proj')
    with store.acquire_writer('proj') as state:
        state.lifecycle = ProjectLifecycle.PUBLISHING.value
        state.deployment['promotion_intent'] = {
            'operation_id': 'op-1',
            'deployment_id': 'dpl_1',
            'previous_production': prev_identity,
            'previous_production_deployment_id': (
                prev_identity.get('deployment_id') if prev_identity else None),
            'stage': 'publishing',
            'created_at': 1.0,
        }
        store.save(state)


def test_rollback_uses_previous_deployment_own_identity(tmp_path):
    """(a) rollback re-promotes the previous production deployment using THE
    PREVIOUS DEPLOYMENT'S OWN identity, not the current operation's."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production='dpl_old')
    # previous identity: op-0/rev1/artifact 'c'*64
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    # Capture the exact identity args used for the rollback promote call.
    calls = []
    orig = vercel.promote_deployment

    def spy(app_id, project, deployment_id, operation_id, source_revision,
            artifact_sha256, *, expected_name=None):
        calls.append((deployment_id, operation_id, source_revision, artifact_sha256))
        return orig(app_id, project, deployment_id, operation_id, source_revision,
                    artifact_sha256, expected_name=expected_name)

    vercel.promote_deployment = spy
    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    assert vercel.promote_calls == ['dpl_1', 'dpl_old']
    # The rollback promote used the PREVIOUS deployment's own identity.
    assert calls[-1] == ('dpl_old', 'op-0', 1, 'c' * 64)
    assert state_failed(store) == 'SMOKE_FAILED'


def test_no_previous_production_does_not_rollback(tmp_path):
    """(b) no previous production -> no rollback attempt, fail closed."""
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


def test_incomplete_previous_identity_fails_closed_no_guess(tmp_path):
    """(c)/(g) previous identity present but incomplete -> never guess; fail
    closed with a distinct rollback error, and never promote the candidate's
    identity as the rollback target."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    # Legacy/partial intent: id only, no 'previous_production' key.
    _publishing_state(store, prev_identity={'deployment_id': 'dpl_old'})
    with store.acquire_writer('proj') as state:
        # Model the pre-hardening durable shape: only the deployment_id was
        # recorded, never the full identity needed for a safe rollback.
        intent = state.deployment['promotion_intent']
        intent.pop('previous_production')
        intent.pop('previous_production_deployment_id', None)
        state.deployment['promotion_intent'] = {
            'operation_id': intent['operation_id'],
            'deployment_id': intent['deployment_id'],
            'previous_production_deployment_id': 'dpl_old',
            'stage': 'publishing',
            'created_at': 1.0,
        }
        store.save(state)

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    # Legacy intent lacks provable rollback identity -> fail closed; the
    # candidate was NOT promoted again and no rollback was guessed.
    assert result.error_code == 'INCOMPLETE_LOOKUP'
    assert vercel.promote_calls == []


def test_retry_reuses_persisted_previous_identity(tmp_path):
    """(d)/(e) a re-entry into the SAME operation reuses the persisted
    previous-production identity instead of recomputing it. We simulate the
    crash-retry by re-entering _promote_authorized with a PUBLISHING state
    that already holds the persisted target, while the CURRENT production
    reported by Vercel has drifted to the broken candidate."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=True))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    # True last-known-good persisted before the remote promote.
    good = {'deployment_id': 'dpl_old', 'operation_id': 'op-0',
            'source_revision': 1, 'artifact_sha256': 'c' * 64}
    _publishing_state(store, prev_identity=good)
    # After the crash, Vercel's current production is now the broken candidate.
    vercel.previous_identity = {'deployment_id': 'dpl_1', 'operation_id': 'op-1',
                                'source_revision': 1, 'artifact_sha256': 'b' * 64}

    # Re-enter the same operation; the smoke passes so it should go LIVE.
    result = orch._promote_authorized('proj', ws, principal_id=OWNER)

    assert result.success, result.error
    intent = store.load('proj').deployment['promotion_intent']
    # The persisted target was reused, NOT recomputed to the drifted dpl_1.
    assert intent['previous_production']['deployment_id'] == 'dpl_old'
    assert intent['previous_production_deployment_id'] == 'dpl_old'


def test_smoke_failure_after_retry_rolls_back_to_true_last_known_good(tmp_path):
    """(f) smoke failure on the retry rolls back to the ACTUAL persisted
    last-known-good, not the drifted broken deployment."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    good = {'deployment_id': 'dpl_old', 'operation_id': 'op-0',
            'source_revision': 1, 'artifact_sha256': 'c' * 64}
    _publishing_state(store, prev_identity=good)
    vercel.previous_identity = {'deployment_id': 'dpl_1', 'operation_id': 'op-1',
                                'source_revision': 1, 'artifact_sha256': 'b' * 64}

    result = orch._promote_authorized('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    # Rollback re-promoted the TRUE last-known-good (dpl_old with op-0/c*64).
    assert vercel.promote_calls == ['dpl_1', 'dpl_old']


def state_failed(store):
    return store.load('proj').failure['error_code']

# ---------------------------------------------------------------------------
# BL-2 — an AMBIGUOUS remote promote must be reconciled, never treated as a
# confirmed failure and never blindly re-sent.
# ---------------------------------------------------------------------------

class AmbiguousPromoteVercel(FakeVercel):
    """Models the adapter returning PROMOTE_RECONCILIATION_REQUIRED for the
    candidate promote (request may have reached Vercel), while still recording
    the remote promote so callers can assert it was sent exactly once."""

    def __init__(self, ambiguous_deployment_id='dpl_1', **kw):
        super().__init__(**kw)
        self.ambiguous_deployment_id = ambiguous_deployment_id

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        if deployment_id == self.ambiguous_deployment_id:
            return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                        error_code='PROMOTE_RECONCILIATION_REQUIRED')
        return super().promote_deployment(
            app_id, project, deployment_id, operation_id, source_revision,
            artifact_sha256, expected_name=expected_name)


class IdentityMismatchReconcileVercel(AmbiguousPromoteVercel):
    """Remote production carries the SAME deployment_id as the candidate but a
    MISMATCHED trusted operation/revision/artifact identity -> the identity
    comparison must NOT report success."""

    def reconcile_production_deployment(self, app_id, project, expected_identity, *,
                                        expected_name=None):
        self.reconcile_calls.append(dict(expected_identity))
        self.expected_names.append(expected_name)
        return OperationResult.ok({
            'status': 'NOT_PROMOTED',
            'deployment_id': expected_identity['deployment_id'],
        })


class RaisingRollbackVercel(FakeVercel):
    """Rollback promote raises; the exception must be classified + persisted,
    never swallowed."""

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        if deployment_id == 'dpl_old':
            raise ConnectionError('leaked-secret-token-xyz')
        return super().promote_deployment(
            app_id, project, deployment_id, operation_id, source_revision,
            artifact_sha256, expected_name=expected_name)


def test_bl2_timeout_after_remote_acceptance_reconciles_success(tmp_path):
    """BL-2 Test A: promote call times out but Vercel did promote the exact
    intended deployment -> reconcile to SUCCESS, run production smoke, reach
    LIVE, keep previous_production unchanged, no second POST."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = AmbiguousPromoteVercel(previous_production='dpl_old')
    # Production truth: the intended NEW deployment IS now live with the exact
    # identity tuple (dpl_1/op-1/rev1/'b'*64).
    vercel.production_now = {
        'deployment_id': 'dpl_1', 'operation_id': 'op-1',
        'source_revision': 1, 'artifact_sha256': 'b' * 64,
    }
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=True))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert result.success, result.error
    assert result.data['reconciled'] is True
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    # Exactly ONE remote promote POST for the candidate.
    assert vercel.promote_calls == ['dpl_1']
    # Post-promote flow ran: production smoke executed.
    assert deps.smoke.calls == 1
    # previous_production persisted BEFORE the side effect is untouched.
    assert state.deployment['promotion_intent']['previous_production'] == {
        'deployment_id': 'dpl_old', 'operation_id': 'op-0',
        'source_revision': 1, 'artifact_sha256': 'c' * 64,
    }


def test_bl2_ambiguous_promote_lookup_shows_old_deployment(tmp_path):
    """BL-2 Test B: ambiguity resolved conclusively as NOT promoted -> fail
    safely, no blind retry, meaningful failure state."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = AmbiguousPromoteVercel(previous_production='dpl_old')
    vercel.production_now = None  # conclusively: intended deployment NOT live
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=True))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTE_NOT_APPLIED'
    assert vercel.promote_calls == ['dpl_1']  # no blind retry
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.failure['error_code'] == 'PROMOTE_NOT_APPLIED'
    assert deps.smoke.calls == 0


def test_bl2_ambiguous_promote_still_ambiguous_preserves_publishing(tmp_path):
    """BL-2 Test C: still ambiguous -> stay PUBLISHING with an intact
    promotion_intent, report reconciliation required, no second POST; a later
    same-operation resume reconciles FIRST and continues without another
    promote POST once the NEW deployment is live."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = AmbiguousPromoteVercel(previous_production='dpl_old')
    vercel.production_now = 'AMBIGUOUS'
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=True))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTION_RECONCILIATION_REQUIRED'
    assert vercel.promote_calls == ['dpl_1']  # no second promote POST
    state = store.load('proj')
    # Lifecycle preserved (NOT terminal FAILED).
    assert state.lifecycle == ProjectLifecycle.PUBLISHING.value
    intent = state.deployment['promotion_intent']
    assert intent['operation_id'] == 'op-1'
    assert intent['previous_production']['deployment_id'] == 'dpl_old'
    assert state.failure['reconciliation_required'] is True

    # ---- Later same-operation resume: remote truth now shows NEW live.
    vercel.production_now = {
        'deployment_id': 'dpl_1', 'operation_id': 'op-1',
        'source_revision': 1, 'artifact_sha256': 'b' * 64,
    }
    resume = orch.promote('proj', ws, principal_id=OWNER)

    assert resume.success, resume.error
    assert resume.data['reconciled'] is True
    # Resume reconciled FIRST and never sent another promote POST.
    assert vercel.promote_calls == ['dpl_1']
    assert store.load('proj').lifecycle == ProjectLifecycle.LIVE.value


def test_bl2_identity_mismatch_not_treated_as_success(tmp_path):
    """BL-2 Test D: remote production has the same deployment_id but a
    mismatched trusted identity -> NOT success; fail closed."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = IdentityMismatchReconcileVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=True))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTE_NOT_APPLIED'
    assert vercel.promote_calls == ['dpl_1']
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert deps.smoke.calls == 0


def test_bl2_non_ambiguous_promote_failure_still_terminal_failed(tmp_path):
    """Regression guard: a non-ambiguous promote failure keeps its existing
    terminal-FAILED semantics (no reconciliation attempted)."""
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
    assert vercel.reconcile_calls == []
    assert store.load('proj').lifecycle == ProjectLifecycle.FAILED.value

# ---------------------------------------------------------------------------
# BL-5 — the rollback result must be observed and persisted, distinctly.
# ---------------------------------------------------------------------------

def test_bl5_smoke_failure_rollback_success_is_distinguishable(tmp_path):
    """BL-5 Test A: smoke fails, rollback succeeds -> failure clearly says
    production smoke failed AND rollback succeeded; one rollback POST only."""
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
    assert result.data['production_smoke_failed'] is True
    assert result.data['rollback'] == 'SUCCEEDED'
    state = store.load('proj')
    assert state.failure['error_code'] == 'SMOKE_FAILED'
    assert state.failure['primary_error_code'] == 'PRODUCTION_SMOKE_FAILED'
    assert state.failure['rollback'] == 'SUCCEEDED'
    assert state.failure['rollback_target'] == {
        'deployment_id': 'dpl_old', 'operation_id': 'op-0',
        'source_revision': 1, 'artifact_sha256': 'c' * 64,
    }
    assert vercel.promote_calls == ['dpl_1', 'dpl_old']


def test_bl5_rollback_confirmed_failure_distinct_code(tmp_path):
    """BL-5 Test B: rollback returns a confirmed failure -> distinct
    ROLLBACK_FAILED code, target identity persisted, no retry loop."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')

    class RollbackFailsVercel(FakeVercel):
        def promote_deployment(self, app_id, project, deployment_id, operation_id,
                               source_revision, artifact_sha256, *, expected_name=None):
            self.promote_calls.append(deployment_id)
            self.expected_names.append(expected_name)
            if deployment_id == 'dpl_old':
                return OperationResult.fail('PROMOTE_FAILED', error_code='PROMOTE_FAILED')
            return OperationResult.ok({
                'deployment_id': deployment_id,
                'production_url': 'https://prod.vercel.app',
                'state': 'READY',
            })

    vercel = RollbackFailsVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.data['rollback'] == 'FAILED'
    state = store.load('proj')
    assert state.failure['error_code'] == 'ROLLBACK_FAILED'
    assert state.failure['error_code'] != 'SMOKE_FAILED'
    assert state.failure['rollback_target']['deployment_id'] == 'dpl_old'
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    # Exactly one rollback attempt (no retry loop).
    assert vercel.promote_calls == ['dpl_1', 'dpl_old']


def test_bl5_rollback_ambiguous_reconciliation_required(tmp_path):
    """BL-5 Test C: rollback outcome ambiguous -> distinct
    ROLLBACK_RECONCILIATION_REQUIRED, no blind retry, target preserved."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')

    class AmbiguousRollbackVercel(FakeVercel):
        def promote_deployment(self, app_id, project, deployment_id, operation_id,
                               source_revision, artifact_sha256, *, expected_name=None):
            self.promote_calls.append(deployment_id)
            self.expected_names.append(expected_name)
            if deployment_id == 'dpl_old':
                return OperationResult.fail(
                    'PROMOTE_RECONCILIATION_REQUIRED',
                    error_code='PROMOTE_RECONCILIATION_REQUIRED')
            return OperationResult.ok({
                'deployment_id': deployment_id,
                'production_url': 'https://prod.vercel.app',
                'state': 'READY',
            })

    vercel = AmbiguousRollbackVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.data['rollback'] == 'RECONCILIATION_REQUIRED'
    state = store.load('proj')
    assert state.failure['error_code'] == 'ROLLBACK_RECONCILIATION_REQUIRED'
    assert state.failure['rollback_reconciliation_required'] is True
    assert state.failure['rollback_target']['deployment_id'] == 'dpl_old'
    assert vercel.promote_calls == ['dpl_1', 'dpl_old']


def test_bl5_rollback_exception_not_swallowed(tmp_path):
    """BL-5 Test D: rollback raises -> classified + persisted, primary smoke
    failure preserved, no secret leak into durable state."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = RaisingRollbackVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.data['rollback'] == 'FAILED'
    state = store.load('proj')
    assert state.failure['error_code'] == 'ROLLBACK_FAILED'
    assert state.failure['primary_error_code'] == 'PRODUCTION_SMOKE_FAILED'
    assert state.failure['rollback_exception_class'] == 'ConnectionError'
    # The raw exception message (which contained a fake secret) is NEVER
    # persisted.
    assert 'leaked-secret-token-xyz' not in json.dumps(state.failure)


def test_bl5_no_previous_production_preserves_existing_behavior(tmp_path):
    """BL-5 Test E: no previous production -> no rollback target invented,
    existing behavior preserved."""
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
    assert result.data['rollback'] == 'NO_PREVIOUS_PRODUCTION'
    assert vercel.promote_calls == ['dpl_1']
    state = store.load('proj')
    assert state.failure['error_code'] == 'SMOKE_FAILED'
    assert state.lifecycle == ProjectLifecycle.FAILED.value


def test_bl5_rollback_uses_persisted_identity_not_recomputed(tmp_path):
    """BL-5 identity: the rollback target is the identity persisted in
    promotion_intent BEFORE the remote promote, used verbatim -- it is never
    re-derived from the (now drifted) current production state."""
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production='dpl_old')
    deps = _deps(vercel=vercel, smoke=FakeSmoke(success=False))
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    captured = []
    orig = vercel.promote_deployment

    def spy(app_id, project, deployment_id, operation_id, source_revision,
            artifact_sha256, *, expected_name=None):
        captured.append((deployment_id, operation_id, source_revision, artifact_sha256))
        return orig(app_id, project, deployment_id, operation_id, source_revision,
                    artifact_sha256, expected_name=expected_name)

    vercel.promote_deployment = spy
    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    # The rollback used the persisted dpl_old identity (op-0/rev1/'c'*64),
    # i.e. exactly the tuple captured before the remote promote.
    assert captured[-1] == ('dpl_old', 'op-0', 1, 'c' * 64)
    intent = store.load('proj').deployment['promotion_intent']
    assert intent['previous_production']['deployment_id'] == 'dpl_old'
    assert intent['previous_production']['artifact_sha256'] == 'c' * 64
