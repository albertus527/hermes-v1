"""P0.2 / P0.3 / P1.4 / P2.6 / P2.8 — orchestrator-level terminal handling
and crash-recovery tests for DEPLOYMENT_PROMOTED_TO_PRODUCTION and the
unresolved-deployment state machine.

The real VercelAdapter (over a canned transport) is delegated to by a thin
FakeVercel, so the orchestrator-level tests exercise the REAL
_deployment() classification, the first-deployment guard, and
find_deployment_by_operation_id() — not a hand-written result object.

Run with:  python3 -m pytest tests/test_preview_orchestrator_promoted_terminal.py -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.contracts import OperationResult
from app.core.state import ProjectStateStore
from app.deploy.adapters import HttpResponse, VercelAdapter
from app.deploy.preview import PreviewOrchestrator

from tests.test_preview_orchestrator import (
    _make_workspace, _preview_ready_state, _deps, FakeVercel, FakeTelegram,
    FakeSmoke,
)

TEAM = 'team_1'
NS = 'ns'
APP_FOR_PROJECT = 'proj'  # PreviewDeps.app_id_for defaults to project_id.
SHA = 'a' * 64


def _real_name(real_a):
    """The real adapter's deterministic Vercel project name for APP_FOR_PROJECT."""
    return real_a.project_name_for(APP_FOR_PROJECT)


def vercel_proj(name='wb', marker=None):
    """The exact project body FakeVercel threads to the orchestrator (its
    fake ``ensure_project``/``lookup_project`` return value). When the test
    delegates to the REAL adapter, ``name`` must be that adapter's own
    deterministic name AND ``marker`` its deterministic owner marker, so
    identity validation passes end to end."""
    return {'id': 'prj_1', 'name': name, 'accountId': TEAM,
            'env': [{'key': 'WEBSITE_BUILDER_OWNER',
                     'value': marker or 'wb-owner', 'type': 'plain'}]}


class RealDeployVercel(FakeVercel):
    """FakeVercel that delegates deploy/lookup to the REAL adapter over a
    canned transport. All project-identity validation is done by the real
    adapter against the exact project body the orchestrator threaded, and
    every canned transport response carries that same body."""

    def __init__(self, adapter, transport, prod_id=None):
        super().__init__()
        self._real = adapter
        self._transport = transport
        self._current_prod_id = prod_id

    def _current_production_id(self, project):
        # Delegate to the REAL adapter: the orchestrator's P2.8/E guard must
        # read the provider's authoritative binding, not a hardcoded value.
        return self._real._current_production_id(project)

    def deploy_static_files(self, app_id, project, files, operation_id,
                            source_revision, artifact_sha256, expected_name=None):
        self.deploy_calls += 1
        return self._real.deploy_static_files(
            app_id, project, files, operation_id, source_revision, artifact_sha256,
            expected_name=expected_name)

    def find_deployment_by_operation_id(self, app_id, project, operation_id,
                                        source_revision, artifact_sha256, expected_name=None):
        return self._real.find_deployment_by_operation_id(
            app_id, project, operation_id, source_revision, artifact_sha256,
            expected_name=expected_name)


def _real_adapter_over(proj, prod_id, op):
    """Real adapter + transport whose canned responses carry the SAME project
    identity the orchestrator threads, with the production binding pointing
    at the promoted deployment id. The project name and meta are normalized
    to the real adapter's deterministic name and the ACTUAL operation
    identity so identity validation passes end to end.

    The POST response's meta is built from the orchestrator's real
    operation_id/source_revision/artifact_sha256 so _deployment's meta
    check matches exactly.
    """
    real_a = VercelAdapter('secret', TEAM, NS, None)
    proj = dict(proj)
    proj['id'] = 'prj_1'
    proj['name'] = _real_name(real_a)
    proj['targets'] = {'production': {'id': prod_id}}
    proj['env'] = [
        {'key': 'WEBSITE_BUILDER_OWNER', 'value': real_a._marker(APP_FOR_PROJECT),
         'type': 'plain'}
    ]
    d = {'id': prod_id, 'projectId': proj['id'], 'teamId': TEAM,
         'name': proj['name'], 'target': 'production', 'url': 'tested.vercel.app',
         'readyState': 'READY', 'readySubstate': 'PROMOTED',
         'meta': real_a._meta(APP_FOR_PROJECT, op['operation_id'],
                               op['source_revision'], op['artifact_sha256'])}
    transport = CannedTransport([
        (200, proj),   # first-deployment guard binding read
        (201, d),      # POST response
        (200, proj),   # _deployment binding re-read
    ])
    real_a.transport = transport
    return real_a, transport


def _lookup_canned(proj, prod_id, op, target=None, dep_id='dpl_1'):
    """Canned responses for the read-only reconcile branch: lookup page,
    deployment detail, guard re-read. ``target=None`` models a valid preview
    deployment; pass 'production' for a promoted one. The deployment entry
    carries the deterministic Vercel ``uid`` the real adapter keys on, plus
    the operation meta so identity validation passes."""
    real = VercelAdapter('secret', TEAM, NS, None)
    body = dict(proj)
    body['id'] = 'prj_1'
    body['name'] = _real_name(real)
    body['targets'] = {'production': {'id': prod_id}}
    body['env'] = [
        {'key': 'WEBSITE_BUILDER_OWNER', 'value': real._marker(APP_FOR_PROJECT),
         'type': 'plain'}
    ]
    meta = real._meta(APP_FOR_PROJECT, op['operation_id'],
                      op['source_revision'], op['artifact_sha256'])
    d = {'id': dep_id, 'uid': dep_id, 'projectId': body['id'], 'teamId': TEAM,
         'name': body['name'], 'target': target,
         'url': 'tested.vercel.app', 'readyState': 'READY', 'meta': meta}
    responses = [
        (200, {'deployments': [d], 'pagination': {'next': None}}),  # page
        (200, d),  # authoritative detail
        (200, body),  # _deployment guard re-read
    ]
    return responses, body


def _op_from_state(store):
    """Read the durable operation identity the orchestrator already created."""
    st = store.load('proj')
    intent = st.deployment['preview_intent']
    return {'operation_id': intent['operation_id'],
            'source_revision': intent['source_revision'],
            'artifact_sha256': intent['artifact_sha256']}


class CannedTransport:
    """Adapter transport with canned responses and call recording."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'data': data})
        status, body = self.responses.pop(0)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return HttpResponse(status, raw)


# ---------------------------------------------------------------------------
# P0.2 — orchestrator-level terminal handling
# ---------------------------------------------------------------------------

def test_promoted_direct_deploy_never_enters_smoke_or_delivery(tmp_path):
    """P0.2 T1: direct deploy returns DEPLOYMENT_PROMOTED_TO_PRODUCTION ->
    orchestrator returns it, no smoke, no delivery, no second POST."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    # Run the orchestrator once with the plain fake to create the durable
    # operation intent (operation_id, source_revision, artifact_sha256).
    vercel_probe = FakeVercel()
    deps_probe = _deps(tmp_path, vercel=vercel_probe)
    PreviewOrchestrator(store, deps_probe).run_owned('proj', ws)
    op = _op_from_state(store)

    real_a_probe = VercelAdapter('secret', TEAM, NS, None)
    proj = vercel_proj(name=_real_name(real_a_probe),
                       marker=real_a_probe._marker(APP_FOR_PROJECT))
    real_a, transport = _real_adapter_over(proj, 'dpl_promoted', op)
    vercel = RealDeployVercel(real_a, transport, prod_id='dpl_promoted')
    vercel._project = proj
    # Reset the intent so the orchestrator re-enters the deploy path (the
    # probe's successful run already marked it shown/SENT; we need the
    # pre-deploy state).
    with store.acquire_writer('proj') as st:
        st.deployment['preview_intent']['stage'] = 'created'
        st.deployment['preview_intent'].pop('smoke', None)
        st.deployment.pop('latest_shown_preview', None)
        st.deployment.pop('external_promotion', None)
        st.deployment['preview_intent'].pop('promoted_terminal', None)
        st.deployment['preview_intent'].pop('deployment_attempted', None)
        st.deployment['preview_intent'].pop('deployment_unresolved', None)
        st.deployment['preview_intent'].pop('notification_state', None)
        store.save(st)
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)

    assert not result.success
    assert result.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    assert result.data.get('promoted_to_production') is True
    assert result.data.get('deployment_id') == 'dpl_promoted'
    assert vercel.deploy_calls == 1  # never resent
    state = store.load('proj')
    # No smoke, no delivery.
    assert 'smoke' not in state.deployment.get('preview_intent', {})
    assert state.deployment.get('latest_shown_preview') is None
    # P0.3: terminal state persisted
    assert state.deployment['preview_intent'].get('promoted_terminal') is True
    # P2.6: external_promotion record persisted
    ep = state.deployment.get('external_promotion')
    assert ep is not None
    assert ep['operation_id'] == op['operation_id']
    assert ep['deployment_id'] == 'dpl_promoted'
    assert ep['rollback_attempted'] is False
    # P1.4: notification state primed (runtime sends exactly one).
    assert state.deployment['preview_intent'].get('notification_state') == 'NOT_ATTEMPTED'
    # No Telegram sent by the orchestrator itself.
    assert deps.telegram.sent == []


def test_promoted_direct_deploy_does_not_post_again_on_restart(tmp_path):
    """P0.2 T3: after a promoted terminal is persisted, a restart (second
    run_owned) must reconcile read-only and NOT issue a second POST."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    vercel_probe = FakeVercel()
    deps_probe = _deps(tmp_path, vercel=vercel_probe)
    PreviewOrchestrator(store, deps_probe).run_owned('proj', ws)
    op = _op_from_state(store)

    real_a_probe = VercelAdapter('secret', TEAM, NS, None)
    proj = vercel_proj(name=_real_name(real_a_probe),
                       marker=real_a_probe._marker(APP_FOR_PROJECT))
    real_a, transport = _real_adapter_over(proj, 'dpl_promoted', op)
    # The restart takes the read-only reconcile branch, so the lookup path's
    # canned responses (promoted deployment) must also be available.
    # The reconciled branch's lookup consumes three responses (page, detail,
    # guard). Build them with a fresh adapter so the meta is deterministic
    # and independent of the first run's adapter instance.
    real_probe2 = VercelAdapter('secret', TEAM, NS, None)
    proj2 = vercel_proj(name=_real_name(real_probe2),
                        marker=real_probe2._marker(APP_FOR_PROJECT))
    lookup_responses, _ = _lookup_canned(proj2, 'dpl_promoted', op,
                                         target='production', dep_id='dpl_promoted')
    transport.responses.extend(lookup_responses)
    vercel = RealDeployVercel(real_a, transport, prod_id='dpl_promoted')
    vercel._project = proj
    with store.acquire_writer('proj') as st:
        st.deployment['preview_intent']['stage'] = 'created'
        st.deployment['preview_intent'].pop('smoke', None)
        st.deployment.pop('latest_shown_preview', None)
        st.deployment.pop('external_promotion', None)
        st.deployment['preview_intent'].pop('promoted_terminal', None)
        st.deployment['preview_intent'].pop('deployment_attempted', None)
        st.deployment['preview_intent'].pop('deployment_unresolved', None)
        st.deployment['preview_intent'].pop('notification_state', None)
        store.save(st)
    deps = _deps(tmp_path, vercel=vercel)
    orch = PreviewOrchestrator(store, deps)
    first = orch.run_owned('proj', ws)
    assert first.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    assert vercel.deploy_calls == 1
    # Simulate a process restart: the first run's promoted-terminal persist
    # kept deployment_attempted=True (so the read-only reconcile branch is
    # taken) and recorded the outcome. The orchestrator must return the SAME
    # terminal result without issuing a second POST.
    with store.acquire_writer('proj') as st:
        st.deployment['preview_intent']['deployment_attempted'] = True
        st.deployment['preview_intent']['deployment_attempt_outcome'] = 'ok'
        store.save(st)
    second = orch.run_owned('proj', ws)
    assert second.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    assert vercel.deploy_calls == 1
    # The reconciled branch issued zero POSTs: only the first run's one.
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert len(posts) == 1


def test_unresolved_prior_attempt_does_not_authorize_second_post(tmp_path):
    """P0.3 W3: deployment_attempted=True + outcome unresolved -> on restart
    only read-only reconcile is allowed; NOT_FOUND/AMBIGUOUS keeps the
    operation unresolved and NEVER issues a second POST."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    # Create the orchestrator once so the durable preview_intent exists, then
    # crash-simulate by hand: the intent is marked attempted+unresolved and
    # the stage is forced back to 'created' (a real crash leaves it that way),
    # and deployment_attempted is persisted so the restart takes the
    # read-only reconcile branch.
    vercel_probe = FakeVercel()
    deps_probe = _deps(tmp_path, vercel=vercel_probe)
    PreviewOrchestrator(store, deps_probe).run_owned('proj', ws)
    with store.acquire_writer('proj') as st:
        st.deployment['preview_intent']['stage'] = 'created'
        st.deployment['preview_intent']['deployment_attempted'] = True
        st.deployment['preview_intent']['deployment_attempt_outcome'] = 'unresolved'
        st.deployment['preview_intent']['deployment_unresolved'] = True
        # Clear any success-path state so the orchestrator re-enters at the
        # deploy stage instead of short-circuiting via latest_shown_preview.
        st.deployment.pop('latest_shown_preview', None)
        st.deployment['preview_intent'].pop('promoted_terminal', None)
        st.deployment['preview_intent'].pop('smoke', None)
        store.save(st)

    real_a = VercelAdapter('secret', TEAM, NS, None)
    proj = vercel_proj(name=_real_name(real_a), marker=real_a._marker(APP_FOR_PROJECT))
    # Lookup finds nothing (empty page) -> NOT_FOUND; guard reads the binding.
    proj['targets'] = {'production': {'id': 'dpl_bootstrap'}}
    # The read-only reconcile branch consumes: page (empty), deployment detail
    # (never reached on NOT_FOUND), guard project re-read. Provide the page
    # and the guard body so the lookup completes and NOT_FOUND is returned.
    # The orchestrator threads `vercel._project` (from FakeVercel, name='wb')
    # as the project body, NOT the real adapter's name. For _project_valid to
    # pass with the real adapter, `vercel._project` must carry the real
    # adapter's name and marker.
    vercel_proj_real = vercel_proj(
        name=_real_name(real_a), marker=real_a._marker(APP_FOR_PROJECT))
    vercel_proj_real['targets'] = {'production': {'id': 'dpl_bootstrap'}}
    transport = CannedTransport([
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (200, vercel_proj_real),  # guard re-read
    ])
    real_a.transport = transport
    vercel = RealDeployVercel(real_a, transport, prod_id='dpl_bootstrap')
    vercel._project = vercel_proj_real
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert not result.success
    assert result.error_code in ('NOT_FOUND', 'DEPLOYMENT_RECONCILIATION_REQUIRED',
                                 'AMBIGUOUS_DEPLOYMENT')
    # CRITICAL: no second POST.
    assert vercel.deploy_calls == 0
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert posts == []
    state = store.load('proj')
    assert state.deployment['preview_intent'].get('deployment_unresolved') is True


def test_unresolved_lookup_hit_clears_the_mark_and_proceeds(tmp_path):
    """P0.3 W2: prior attempt unresolved, but the read-only reconcile FINDS
    the provider's deployment -> the mark is cleared and the flow continues
    from the found deployment (zero POSTs)."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    vercel_probe = FakeVercel()
    deps_probe = _deps(tmp_path, vercel=vercel_probe)
    PreviewOrchestrator(store, deps_probe).run_owned('proj', ws)
    with store.acquire_writer('proj') as st:
        st.deployment['preview_intent']['stage'] = 'created'
        st.deployment['preview_intent']['deployment_attempted'] = True
        st.deployment['preview_intent']['deployment_attempt_outcome'] = 'unresolved'
        st.deployment['preview_intent']['deployment_unresolved'] = True
        store.save(st)

    op = _op_from_state(store)
    real_probe = VercelAdapter('secret', TEAM, NS, None)
    proj = vercel_proj(name=_real_name(real_probe),
                       marker=real_probe._marker(APP_FOR_PROJECT))
    # The read-only reconcile branch: guard read, lookup page, detail,
    # guard re-read — all for a VALID PREVIEW deployment (target=None).
    responses, _ = _lookup_canned(proj, 'dpl_bootstrap', op, target=None)
    real_a2 = VercelAdapter('secret', TEAM, NS, None)
    transport2 = CannedTransport(responses)
    real_a2.transport = transport2
    vercel = RealDeployVercel(real_a2, transport2, prod_id='dpl_bootstrap')
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert result.success, result.error  # found a valid preview deployment
    assert vercel.deploy_calls == 0
    posts = [c for c in transport2.calls if c['method'] == 'POST']
    assert posts == []


def test_promoted_guard_catches_upstream_classification_failure(tmp_path):
    """P2.8/E: even if a code path returns a production-bound deployment as a
    "success", the orchestrator's guard refuses it as an approved preview."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    # A fake vercel that RETURNS a production-bound deployment as success —
    # simulating a future upstream classification bug.
    class ProdSuccessVercel(FakeVercel):
        def deploy_static_files(self, app_id, project, files, operation_id,
                                source_revision, artifact_sha256, expected_name=None):
            self.deploy_calls += 1
            return OperationResult.ok({
                'deployment_id': 'dpl_promoted',
                'preview_url': 'https://public.vercel.app',
                'state': 'READY',
                'deployment': {'id': 'dpl_promoted', 'target': 'production'},
            })

        def _current_production_id(self, project):
            return 'dpl_promoted'  # binding points at the same deployment

    vercel = ProdSuccessVercel()
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert not result.success
    assert result.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    assert result.data.get('promoted_to_production') is True
    # No smoke, no delivery, no latest_shown_preview.
    state = store.load('proj')
    assert state.deployment.get('latest_shown_preview') is None
    assert deps.telegram.sent == []
    assert state.deployment['preview_intent'].get('promoted_terminal') is True
    ep = state.deployment.get('external_promotion')
    assert ep is not None and ep['deployment_id'] == 'dpl_promoted'


def test_guard_refuses_when_binding_read_unreadable(tmp_path):
    """P2.8/E fail-closed: an unreadable/ambiguous production binding read
    must never be treated as evidence the deployment is preview-safe."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    class BrokenBindingVercel(FakeVercel):
        def _current_production_id(self, project):
            raise ValueError('PRODUCTION_BINDING_LOOKUP_FAILED')

    vercel = BrokenBindingVercel()
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert not result.success
    assert result.error_code == 'DEPLOYMENT_RECONCILIATION_REQUIRED'
    assert store.load('proj').deployment.get('latest_shown_preview') is None


def test_normal_preview_path_unaffected_by_guard(tmp_path):
    """When the production binding points at a DIFFERENT deployment, the
    guard is inert and the preview path proceeds normally."""
    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)

    class BoundedVercel(FakeVercel):
        def _current_production_id(self, project):
            return 'dpl_bootstrap'  # different from the fake's dpl_1

    vercel = BoundedVercel()
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned('proj', ws)
    assert result.success, result.error
    assert store.load('proj').deployment.get('latest_shown_preview') is not None
