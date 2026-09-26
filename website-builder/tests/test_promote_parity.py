"""Vercel promotion parity + same-operation recovery regression tests.

Local behavioral tests only: a fake transport and fake collaborators, no
network, no credentials, no Telegram, and no real Vercel mutation. The
confirmation loops take an injected clock and sleep so nothing here spends
real wall-clock time.

The canonical project id used throughout is ``tg-6329821361-p9``, the project
whose publish failed with ``PROMOTE_NOT_APPLIED`` and whose remote production
was subsequently applied out of band by an operator.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.deploy.adapters import HttpResponse, VercelAdapter
from app.projects.promote import (
    PREVIOUS_PRODUCTION_BOOTSTRAP, PromoteDeps, PromotionOrchestrator,
)
from app.sandbox.runner import ProjectRunner

# --- the exact p9 identity under test -------------------------------------
P9 = 'tg-6329821361-p9'
P9_OPERATION_ID = 'a4e8c7867fb0a14c831815d167fb8a5079efb1e7251b5aec8e153d4dc5c6cf50'
P9_DEPLOYMENT_ID = 'dpl_5mGK9GNLbqoPtbGqCEDcfiJb5yNT'
P9_SOURCE_REVISION = 3
P9_ARTIFACT = 'f' * 64
P9_SOURCE = 'e' * 64
OWNER = 'owner-1'

SHA = 'a' * 64


class Transport:
    """Records every call and replays a scripted response queue."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError('unexpected extra request: %s %s' % (method, url))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, body = value
        return HttpResponse(status, json.dumps(body).encode())

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == 'POST']


class Clock:
    """Injected wall clock: ``sleep_fn`` advances it, so a 60s budget with a
    ~2s step is exercised exactly, in zero real time."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def adapter(*responses):
    transport = Transport(*responses)
    return VercelAdapter('secret', 'team_1', 'installation', transport), transport


def project(a, name=None):
    return {'id': 'prj_1', 'name': name or a.project_name_for('app'),
            'accountId': 'team_1',
            'env': [{'key': 'WEBSITE_BUILDER_OWNER', 'value': a._marker('app'),
                     'type': 'plain'}]}


def identity(a, deployment_id='dpl_1', operation_id='operation', revision=2,
             artifact=SHA):
    return {'deployment_id': deployment_id, 'operation_id': operation_id,
            'source_revision': revision, 'artifact_sha256': artifact}


def meta(a, operation_id='operation', revision=2, artifact=SHA):
    return a._meta('app', operation_id, revision, artifact)


def deployment(a, deployment_id='dpl_1', operation_id='operation', revision=2,
               artifact=SHA, target=None, **extra):
    body = {'id': deployment_id, 'projectId': 'prj_1', 'teamId': 'team_1',
            'name': a.project_name_for('app'), 'target': target,
            'url': 'tested.vercel.app', 'readyState': 'READY',
            'meta': a._meta('app', operation_id, revision, artifact)}
    body.update(extra)
    return body


def bound(a, binding_id, job=None, name=None):
    """A project body whose canonical production binding is ``binding_id``."""
    p = project(a, name)
    p['targets'] = {} if binding_id is None else {'production': {'id': binding_id}}
    if job is not None:
        p['lastAliasRequest'] = job
    return p


def job(status, to_id, requested_at=1000, type_='promote', from_id=None):
    record = {'jobStatus': status, 'toDeploymentId': to_id,
              'requestedAt': requested_at, 'type': type_}
    if from_id is not None:
        record['fromDeploymentId'] = from_id
    return record


# ---------------------------------------------------------------------------
# 1. Official promote-success response shapes and statuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('status', [201, 202])
def test_alias_remap_accepts_every_documented_success_status(status):
    """Vercel documents 201 (applied) and 202 (queued) for
    POST /v10/projects/{id}/promote/{deploymentId}. 202 used to be read as a
    hard failure, which is what turned an accepted promotion into
    PROMOTE_NOT_APPLIED.
    """
    a, t = adapter()
    t.responses = [
        (200, deployment(a, target='production')),
        (status, {}),
        (200, bound(a, 'dpl_1')),
        (200, deployment(a, target='production')),
    ]
    result = a.promote_deployment('app', project(a), 'dpl_1', 'operation', 2, SHA,
                                  sleep_fn=lambda s: None,
                                  now=Clock().now)
    assert result.success
    assert result.data['production_url'] == 'https://tested.vercel.app'


@pytest.mark.parametrize('status', [200, 201])
def test_promote_by_creation_accepts_documented_success_statuses(status):
    """For a preview-target deployment the CLI promotes by creation, and
    POST /v13/deployments documents 200/201 with a NEW deployment id."""
    a, t = adapter()
    t.responses = [
        (200, deployment(a)),
        (status, {'id': 'dpl_prod', 'readyState': 'QUEUED'}),
        (200, deployment(a, 'dpl_prod')),
        (200, bound(a, 'dpl_prod')),
        (200, deployment(a, 'dpl_prod')),
    ]
    result = a.promote_deployment('app', project(a), 'dpl_1', 'operation', 2, SHA,
                                  sleep_fn=lambda s: None, now=Clock().now)
    assert result.success
    assert result.data['deployment_id'] == 'dpl_prod'
    assert result.data['mechanism'] == 'promote_by_creation'
    # The create carries the source deploymentId + target, and our identity --
    # but NO files/builds, so the approved bytes are inherited, never re-sent.
    create = json.loads(t.posts[0][2]['data'])
    assert create['deploymentId'] == 'dpl_1'
    assert create['target'] == 'production'
    assert create['meta']['action'] == 'promote'
    assert create['meta']['wbOperation'] == 'operation'
    assert 'files' not in create and 'builds' not in create


@pytest.mark.parametrize('status', [400, 401, 403, 410, 422])
def test_deterministic_provider_rejection_is_terminal(status):
    a, t = adapter()
    t.responses = [(200, deployment(a)), (status, {'error': {'code': 'x'}})]
    result = a.promote_deployment('app', project(a), 'dpl_1', 'operation', 2, SHA,
                                  sleep_fn=lambda s: None, now=Clock().now)
    assert not result.success
    assert result.error_code == 'PROMOTE_REJECTED'


# ---------------------------------------------------------------------------
# 2. Production binding moves while the deployment target stays preview
# ---------------------------------------------------------------------------

def test_binding_moving_is_authoritative_even_while_target_stays_preview():
    """Vercel does not rewrite a promoted deployment's own ``target``. The
    project's production binding is the authority, so a deployment that is
    genuinely live must confirm even though it is still marked preview.
    """
    a, t = adapter()
    t.responses = [
        (200, deployment(a)),                                    # pre-promote GET
        (201, {'id': 'dpl_prod', 'readyState': 'QUEUED'}),       # create
        (200, deployment(a, 'dpl_prod')),                        # ready wait
        (200, bound(a, 'dpl_prod')),                             # binding moved
        (200, deployment(a, 'dpl_prod')),                        # revalidate
    ]
    result = a.promote_deployment('app', project(a), 'dpl_1', 'operation', 2, SHA,
                                  sleep_fn=lambda s: None, now=Clock().now)
    assert result.success
    # The promoted deployment is confirmed purely by binding equality; its own
    # ``target`` is still preview and that must not matter.
    assert t.responses == []


def test_check_domain_production_no_longer_requires_target_production():
    """The custom-domain proof must not reject a genuinely live production
    deployment for its own target field, while still requiring the exact
    identity."""
    a, t = adapter()
    t.responses = [
        (200, bound(a, 'dpl_1')),
        (200, deployment(a, target=None)),
    ]
    result = a.check_domain_production('app', project(a), identity(a))
    assert result.success


# ---------------------------------------------------------------------------
# 3 + 4. Eventual consistency: an old binding during early polls is not a
#        final failure
# ---------------------------------------------------------------------------

def test_eventual_consistency_confirms_after_stale_polls():
    """The binding is still the bootstrap for the first three polls and only
    moves on the fourth: the loop must confirm, not fail."""
    a, t = adapter()
    clock = Clock()
    t.responses = [
        (200, bound(a, 'dpl_boot')),
        (200, bound(a, 'dpl_boot')),
        (200, bound(a, 'dpl_boot')),
        (200, bound(a, 'dpl_1', job('succeeded', 'dpl_1'))),
        (200, deployment(a, target=None)),
    ]
    result = a.confirm_production_promotion(
        'app', project(a), identity(a), sleep_fn=clock.sleep, now=clock.now)
    assert result.success
    assert result.data['status'] == 'PROMOTED'
    assert clock.sleeps == [2.0, 2.0, 2.0]
    assert 'NOT_PROMOTED' not in json.dumps(result.data)


def test_stale_binding_is_never_reported_as_not_promoted():
    """``reconcile_production_deployment`` may only say NOT_PROMOTED with
    positive evidence -- the provider reporting THIS promotion job as failed.
    A binding that merely points elsewhere is ambiguous."""
    a, t = adapter()
    t.responses = [
        (200, deployment(a)),
        (200, bound(a, 'dpl_boot', job('in-progress', 'dpl_1'))),
    ]
    result = a.reconcile_production_deployment('app', project(a), identity(a))
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_conclusive_job_failure_is_reported_as_not_promoted():
    a, t = adapter()
    t.responses = [
        (200, deployment(a)),
        (200, bound(a, 'dpl_boot', job('failed', 'dpl_1'))),
    ]
    result = a.reconcile_production_deployment('app', project(a), identity(a))
    assert result.success
    assert result.data['status'] == 'NOT_PROMOTED'


def test_terminal_job_failure_is_a_terminal_failure_not_a_success():
    """A conclusively-failed promotion is reported as a FAILURE result, so no
    caller can ever read a production URL for a deployment that is not live."""
    a, t = adapter()
    clock = Clock()
    t.responses = [(200, bound(a, 'dpl_boot', job('failed', 'dpl_1')))]
    result = a.confirm_production_promotion(
        'app', project(a), identity(a), sleep_fn=clock.sleep, now=clock.now)
    assert not result.success
    assert result.error_code == 'PROMOTE_NOT_APPLIED'
    assert 'production_url' not in json.dumps(result.data)
    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# 5. Timeout stays fail-closed, and is a real 60s wall-clock deadline
# ---------------------------------------------------------------------------

def test_confirmation_timeout_is_fail_closed_after_a_real_60s_deadline():
    a, t = adapter()
    clock = Clock()
    a._PROMOTION_CONFIRM_TIMEOUT = 60.0
    a._PROMOTION_CONFIRM_INTERVAL = 2.0
    for _ in range(40):
        t.responses.append((200, bound(a, 'dpl_boot')))
    result = a.confirm_production_promotion(
        'app', project(a), identity(a), sleep_fn=clock.sleep, now=clock.now)
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'
    # Exactly 30 polls over a real 60s deadline -- a deadline, not a poll cap.
    assert clock.sleeps == [2.0] * 30
    assert clock.now() - 1000.0 == 60.0


def test_unknown_or_malformed_job_state_is_ambiguous_not_negative():
    a, t = adapter()
    clock = Clock()
    for _ in range(40):
        t.responses.append((200, bound(a, 'dpl_boot', {'jobStatus': 'weird'})))
    result = a.confirm_production_promotion(
        'app', project(a), identity(a), sleep_fn=clock.sleep, now=clock.now)
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_rolling_release_state_fails_closed():
    """A rolling release changes what "promoted" means; we never enable one, so
    a truthy value is an unmodelled provider state and must never be read as a
    completed alias remap."""
    a, t = adapter()
    p = bound(a, 'dpl_1')
    p['rollingRelease'] = {'target': 'production'}
    t.responses = [(200, p)]
    result = a.confirm_production_promotion(
        'app', project(a), identity(a), sleep_fn=lambda s: None, now=Clock().now)
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


# ---------------------------------------------------------------------------
# 6-9. External adoption: the exact p9 scenario and its negative cases
# ---------------------------------------------------------------------------

def test_external_promotion_adopts_by_direct_identity():
    a, t = adapter()
    t.responses = [
        (200, bound(a, 'dpl_1')),
        (200, deployment(a, aliasAssigned=True)),
    ]
    result = a.reconcile_external_promotion('app', project(a), identity(a))
    assert result.success
    assert result.data['status'] == 'PROMOTED'
    assert result.data['proof'] == 'DIRECT_IDENTITY'


def test_external_promotion_adopts_by_provider_lineage():
    """The Vercel CLI's promote-by-creation mints a new id, so adoption must
    also accept Vercel's own record that this production deployment is the
    promote of our exact deployment."""
    a, t = adapter()
    t.responses = [
        (200, bound(a, 'dpl_cli', job('succeeded', 'dpl_cli', from_id='dpl_1'))),
        (200, deployment(a, 'dpl_cli', operation_id='other', aliasAssigned=True)),
    ]
    result = a.reconcile_external_promotion('app', project(a), identity(a))
    assert result.success
    assert result.data['status'] == 'PROMOTED'
    assert result.data['proof'] == 'PROVIDER_LINEAGE'
    assert result.data['deployment_id'] == 'dpl_cli'


def test_wrong_remote_deployment_is_never_adopted():
    a, t = adapter()
    t.responses = [(200, bound(a, 'dpl_unrelated'))]
    result = a.reconcile_external_promotion('app', project(a), identity(a))
    assert result.success
    assert result.data['status'] == 'PROMOTED_UNPROVEN'
    assert result.data['deployment_id'] == 'dpl_unrelated'


def test_incomplete_metadata_is_never_adopted():
    """A deployment created by a tool that carried only ``{action: 'promote'}``,
    with no provider lineage record, proves nothing about our artifact."""
    a, t = adapter()
    p = bound(a, 'dpl_cli')
    p['lastAliasRequest'] = None
    t.responses = [(200, p)]
    result = a.reconcile_external_promotion('app', project(a), identity(a))
    assert result.success
    assert result.data['status'] == 'PROMOTED_UNPROVEN'


def test_lineage_claimed_but_aliased_deployment_unready_fails_closed():
    a, t = adapter()
    body = deployment(a, 'dpl_cli', operation_id='other', aliasAssigned=False)
    body['readyState'] = 'BUILDING'
    t.responses = [
        (200, bound(a, 'dpl_cli', job('succeeded', 'dpl_cli', from_id='dpl_1'))),
        (200, body),
    ]
    result = a.reconcile_external_promotion('app', project(a), identity(a))
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_lineage_from_a_different_source_deployment_is_unproven():
    a, t = adapter()
    t.responses = [
        (200, bound(a, 'dpl_cli', job('succeeded', 'dpl_cli', from_id='dpl_other'))),
    ]
    result = a.reconcile_external_promotion('app', project(a), identity(a))
    assert result.data['status'] == 'PROMOTED_UNPROVEN'


# ---------------------------------------------------------------------------
# Orchestrator-level: the exact p9 recovery
# ---------------------------------------------------------------------------

class P9Vercel:
    """Fake that models p9's remote truth and records every promote POST.

    ``remote`` is what provider truth says: ('promoted_by_id',),
    ('promoted_by_lineage',), ('wrong',), ('incomplete',) or
    ('not_promoted',).
    """

    def __init__(self, remote):
        self.remote = remote
        self.promote_calls = []
        self.reconcile_external_calls = []
        self.reconcile_calls = []
        self._project = {'id': 'prj_1', 'name': 'pokeplay', 'accountId': 'team_1'}
        self._production_url = 'https://' + P9_DEPLOYMENT_ID + '.vercel.app'

    def lookup_project(self, app_id, *, expected_name=None):
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def canonical_production_url(self, app_id, project, *, expected_name=None):
        """The project's own public default domain, never the deployment host."""
        name = expected_name or self._project['name']
        return OperationResult.ok({
            'canonical_production_url': 'https://{}.vercel.app/'.format(name),
            'canonical_source': 'VERCEL_PROJECT_DOMAIN',
            'project_name': name,
        })

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        return OperationResult.ok({
            'deployment_id': 'dpl_bootstrap_p9',
            'bootstrap_proof': {'deployment_id': 'dpl_bootstrap_p9',
                                'bootstrap_operation_id': 'boot-op-p9'},
        })

    def reconcile_external_promotion(self, app_id, project, intended_identity, *,
                                     expected_name=None):
        self.reconcile_external_calls.append(dict(intended_identity))
        kind = self.remote
        if kind == 'promoted_by_id':
            return OperationResult.ok({
                'status': 'PROMOTED', 'deployment_id': P9_DEPLOYMENT_ID,
                'proof': 'DIRECT_IDENTITY', 'production_url': self._production_url})
        if kind == 'promoted_by_lineage':
            return OperationResult.ok({
                'status': 'PROMOTED', 'deployment_id': 'dpl_cli_created_p9',
                'proof': 'PROVIDER_LINEAGE',
                'production_url': 'https://dpl_cli_created_p9.vercel.app'})
        if kind in ('wrong', 'incomplete'):
            binding = 'dpl_someone_elses' if kind == 'wrong' else 'dpl_cli_created_p9'
            return OperationResult.ok({'status': 'PROMOTED_UNPROVEN',
                                       'deployment_id': binding})
        if kind == 'unreadable':
            return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                        error_code='PROMOTE_RECONCILIATION_REQUIRED')
        return OperationResult.ok({'status': 'PROMOTED_UNPROVEN', 'deployment_id': None})

    def reconcile_production_deployment(self, app_id, project, expected_identity, *,
                                        expected_name=None):
        self.reconcile_calls.append(dict(expected_identity))
        return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                    error_code='PROMOTE_RECONCILIATION_REQUIRED')

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        return OperationResult.ok({
            'deployment_id': deployment_id,
            'production_url': 'https://should-never-happen.vercel.app',
        })


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return OperationResult.ok({'message_id': 1})


class FakeSmoke:
    def __init__(self, success=True):
        self.success = success
        self.calls = []

    def run(self, url, out_dir):
        self.calls.append(url)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return OperationResult(
            success=self.success,
            data={'url': url, 'failures': [] if self.success else ['broken']},
            error_code=None if self.success else 'SMOKE_FAILED')


def _seed_p9_state(store, project_id=P9):
    """p9 exactly as it is on disk: FAILED, PROMOTE_NOT_APPLIED, an intact
    promotion_intent still at stage ``publishing``, KNOWN_BOOTSTRAP previous
    production and no production_url."""
    shown = {
        'operation_id': P9_OPERATION_ID,
        'source_revision': P9_SOURCE_REVISION,
        'preview_url': 'https://protected-preview.vercel.app',
        'deployment_id': P9_DEPLOYMENT_ID,
        'source_sha256': P9_SOURCE,
        'artifact_sha256': P9_ARTIFACT,
        'shown_at': 1.0,
    }
    approval = dict(shown, approved_by=OWNER, approved_at=2.0)
    with store.acquire_writer(project_id) as state:
        state.owner_id = OWNER
        state.roles['owner'] = OWNER
        state.lifecycle = ProjectLifecycle.FAILED.value
        state.revisions.source_revision = P9_SOURCE_REVISION
        state.revisions.qa_revision = P9_SOURCE_REVISION
        state.revisions.preview_revision = P9_SOURCE_REVISION
        state.deployment['latest_shown_preview'] = shown
        state.deployment['approval'] = approval
        state.deployment['promotion_intent'] = {
            'operation_id': P9_OPERATION_ID,
            'deployment_id': P9_DEPLOYMENT_ID,
            'previous_production': None,
            'previous_production_class': PREVIOUS_PRODUCTION_BOOTSTRAP,
            'previous_production_deployment_id': None,
            'stage': 'publishing',
            'created_at': 3.0,
        }
        state.failure = {
            'phase': 'promotion', 'error': 'PROMOTE_FAILED',
            'error_code': 'PROMOTE_NOT_APPLIED', 'failed_at': 4.0,
        }
        state.production_url = None
        store.save(state)
    return shown


def _orchestrator(tmp_path, vercel, smoke=None, project_id=P9):
    store = ProjectStateStore(tmp_path / 'state')
    runner = ProjectRunner(workspace_root=tmp_path / 'ws',
                           state_store=store,
                           hermes_home=tmp_path / 'hermes')
    orch = PromotionOrchestrator(runner, store, PromoteDeps(
        vercel=vercel, telegram=FakeTelegram(), smoke=smoke or FakeSmoke(),
        chat_id_for=lambda pid, state: 'chat-1'))
    return orch, store


P9_CANONICAL_URL = 'https://pokeplay.vercel.app/'


@pytest.mark.parametrize('remote,expected_live,deployment_url', [
    ('promoted_by_id', True, 'https://' + P9_DEPLOYMENT_ID + '.vercel.app'),
    ('promoted_by_lineage', True, 'https://dpl_cli_created_p9.vercel.app'),
])
def test_p9_recovers_to_live_with_zero_promote_posts(tmp_path, remote,
                                                     expected_live, deployment_url):
    """local FAILED + durable intent + remote already promoted -> reconcile,
    production smoke, persist LIVE, with zero promote POSTs."""
    vercel = P9Vercel(remote)
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.resume_publish(P9, workspace, principal_id=OWNER)

    assert result.success, result.error_code
    # The whole point: adoption issues no promote request at all.
    assert vercel.promote_calls == []
    state = store.load(P9)
    assert state.lifecycle == (ProjectLifecycle.LIVE.value if expected_live
                               else state.lifecycle)
    # The user-facing URL is the project's canonical public domain; the
    # deployment-specific host is kept only as internal identity.
    assert state.production_url == P9_CANONICAL_URL
    assert state.deployment['last_live_deployment']['production_url'] == \
        P9_CANONICAL_URL
    assert state.deployment['last_live_deployment']['deployment_url'] == \
        deployment_url
    assert state.failure is None
    assert state.deployment['promotion_intent']['stage'] == 'live'
    assert state.deployment['promotion_intent']['canonical_production_url'] == \
        P9_CANONICAL_URL
    assert state.deployment['last_live_deployment']['operation_id'] == P9_OPERATION_ID
    # The smoke checked the canonical url, not the deployment host.
    assert orch.deps.smoke.calls == [P9_CANONICAL_URL]
    # A successful recovery may not touch the running loop's Telegram surface
    # for anything but the LIVE notification.
    assert orch.deps.telegram.sent == [('chat-1', f'🚀 Live: {P9_CANONICAL_URL}')]


def test_p9_recovery_refuses_wrong_remote_deployment(tmp_path):
    """A production deployment that is not ours is NOT adopted, and the
    project is left exactly as it was."""
    vercel = P9Vercel('wrong')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    before = json.dumps(store.load(P9).to_dict(), sort_keys=True)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.resume_publish(P9, workspace, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTION_IDENTITY_UNPROVEN'
    assert vercel.promote_calls == []
    assert orch.deps.smoke.calls == []
    state = store.load(P9)
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.failure['error_code'] == 'PROMOTION_IDENTITY_UNPROVEN'
    # The durable intent is preserved verbatim so a later human/operator
    # decision is still possible from the original record.
    assert state.deployment['promotion_intent']['stage'] == 'publishing'
    assert state.deployment['promotion_intent']['operation_id'] == P9_OPERATION_ID
    assert json.dumps(state.to_dict(), sort_keys=True) != before


def test_p9_recovery_fails_closed_on_incomplete_remote_metadata(tmp_path):
    vercel = P9Vercel('incomplete')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.resume_publish(P9, workspace, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTION_IDENTITY_UNPROVEN'
    assert vercel.promote_calls == []
    assert store.load(P9).lifecycle == ProjectLifecycle.FAILED.value


def test_p9_recovery_keeps_reconcilable_state_when_truth_is_unreadable(tmp_path):
    """An unreadable provider read is NOT a failure and NOT an adoption: the
    project goes back to PUBLISHING with its intent intact so it stays
    resumable."""
    vercel = P9Vercel('unreadable')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.resume_publish(P9, workspace, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTION_RECONCILIATION_REQUIRED'
    assert vercel.promote_calls == []
    state = store.load(P9)
    assert state.lifecycle == ProjectLifecycle.PUBLISHING.value
    assert state.failure['reconciliation_required'] is True
    assert state.failure['target_identity']['deployment_id'] == P9_DEPLOYMENT_ID


def test_duplicate_recovery_never_issues_a_second_remote_promote(tmp_path):
    """Running the recovery twice must not produce a second promote: the second
    run is the existing LIVE idempotent no-op."""
    vercel = P9Vercel('promoted_by_id')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    first = orch.resume_publish(P9, workspace, principal_id=OWNER)
    second = orch.resume_publish(P9, workspace, principal_id=OWNER)

    assert first.success
    assert second.success
    assert vercel.promote_calls == []
    assert len(orch.deps.smoke.calls) == 1


def test_resume_refuses_a_project_that_is_not_a_recovery(tmp_path):
    """resume_publish must never become a generic retry door."""
    vercel = P9Vercel('promoted_by_id')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)
    with store.acquire_writer(P9) as state:
        # A FAILED project from another phase, with a foreign intent.
        state.lifecycle = ProjectLifecycle.FAILED.value
        state.failure = {'phase': 'qa', 'error_code': 'SMOKE_FAILED'}
        state.deployment['promotion_intent'] = {
            'operation_id': 'someone-elses-operation', 'stage': 'publishing',
        }
        store.save(state)

    result = orch.resume_publish(P9, workspace, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'RESUME_NOT_APPLICABLE'
    assert vercel.promote_calls == []
    assert vercel.reconcile_external_calls == []
    assert store.load(P9).lifecycle == ProjectLifecycle.FAILED.value


def test_resume_requires_the_project_owner(tmp_path):
    vercel = P9Vercel('promoted_by_id')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.resume_publish(P9, workspace, principal_id='stranger-9')

    assert not result.success
    assert result.error_code == 'UNAUTHORIZED_ROLE'
    assert vercel.promote_calls == []
    assert vercel.reconcile_external_calls == []
    assert store.load(P9).lifecycle == ProjectLifecycle.FAILED.value


# ---------------------------------------------------------------------------
# Orchestrator-level terminal vs ambiguous taxonomy
# ---------------------------------------------------------------------------

class PromoteOutcomeVercel(P9Vercel):
    """p9's remote truth, but with a chosen promote result so the terminal vs
    ambiguous classification can be driven directly."""

    def __init__(self, outcome):
        super().__init__('not_promoted')
        self.outcome = outcome

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        if isinstance(self.outcome, tuple):
            return OperationResult.fail(self.outcome[0], error_code=self.outcome[1])
        return self.outcome


def _seed_first_publish(store, project_id=P9):
    """A genuine FIRST publish: no pre-existing promotion_intent, so nothing is
    a resume and the promote POST is actually reached."""
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.failure = None
        state.deployment.pop('promotion_intent', None)
        state.deployment.pop('last_live_deployment', None)
        store.save(state)


@pytest.mark.parametrize('error_code', ['PROMOTE_REJECTED', 'PROMOTE_NOT_APPLIED',
                                        'PROMOTE_FAILED', 'INVALID_DEPLOYMENT_ID'])
def test_conclusive_promote_failures_are_terminal(tmp_path, error_code):
    """A deterministic refusal or a conclusively-failed promotion is terminal
    FAILED with the provider's own code -- never silently downgraded to
    ambiguity."""
    vercel = PromoteOutcomeVercel(('PROMOTE_FAILED', error_code))
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    _seed_first_publish(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.promote(P9, workspace, principal_id=OWNER)

    assert not result.success
    assert result.error_code == error_code
    assert vercel.promote_calls == [P9_DEPLOYMENT_ID]
    state = store.load(P9)
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.failure['error_code'] == error_code
    assert state.production_url is None


def test_ambiguous_promote_stays_reconcilable_and_never_terminal(tmp_path):
    """An ambiguous promote must leave the project resumable: PUBLISHING, the
    intent intact, reconciliation_required set -- and no production URL."""
    vercel = PromoteOutcomeVercel(('PROMOTE_RECONCILIATION_REQUIRED',
                                    'PROMOTE_RECONCILIATION_REQUIRED'))
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p9_state(store)
    _seed_first_publish(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.promote(P9, workspace, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTION_RECONCILIATION_REQUIRED'
    state = store.load(P9)
    assert state.lifecycle == ProjectLifecycle.PUBLISHING.value
    assert state.failure['error_code'] == 'PROMOTION_RECONCILIATION_REQUIRED'
    assert state.failure['reconciliation_required'] is True
    assert state.deployment['promotion_intent']['operation_id'] == P9_OPERATION_ID
    assert state.production_url is None
    assert len(vercel.promote_calls) == 1
