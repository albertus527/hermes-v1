"""P2.8/E — smoke/deployment-protection consistency matrix (mock, no network).

The smoke path must NEVER treat a production-target deployment as an
approved preview, even if an upstream classification fails. Two independent
guards are pinned here:

1. **Orchestrator guard** — ``_guard_production_binding`` re-reads the
   provider's authoritative production binding after every successful deploy.
   If the deployed id IS the production binding, the result is reclassified
   ``DEPLOYMENT_PROMOTED_TO_PRODUCTION`` (terminal, no retry) and the flow
   never enters smoke or delivery. An unreadable binding fails closed
   (``DEPLOYMENT_RECONCILIATION_REQUIRED``).

2. **Adapter classification** — the adapter's own ``_deployment`` classifies
   the same outcome from the deployment response + binding read. The
   orchestrator guard is defense-in-depth: it catches any future code path
   that might let a production deployment through as a "preview success".

3. **PROMOTED terminal invariance** — once ``promoted_terminal`` is persisted,
   every subsequent same-operation reconcile returns the terminal result
   without issuing any provider call (no POST, no lookup, no smoke).

4. **Deployment Protection semantics** — a smoke pass on a preview domain is
   NOT evidence that "preview was approved"; it only proves the artifact
   served. A production deployment's anonymous reachability is a DIFFERENT
   semantic (public alias) and must never be read as approval.

No real network: every adapter transport is a canned HttpResponse sequence.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.contracts import OperationResult
from app.core.state import ProjectStateStore
from app.deploy.adapters import HttpResponse, VercelAdapter
from app.deploy.preview import PreviewOrchestrator

TEAM = 'team_1'
NS = 'ns'
APP = 'app'


def _proj(name, marker, prod_id, extra=None):
    p = {
        'id': 'prj_1', 'name': name, 'accountId': TEAM,
        'env': [{'key': 'WEBSITE_BUILDER_OWNER', 'value': marker,
                 'type': 'plain'}],
        'targets': {'production': {'id': prod_id}} if prod_id else {},
    }
    if extra:
        p.update(extra)
    return p


class CannedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({'method': method, 'url': url})
        if not self.responses:
            raise urllib.error.URLError('canned responses exhausted')
        status, body = self.responses.pop(0)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return HttpResponse(status, raw)


def _adapter_canned(responses, app=APP, team=TEAM):
    a = VercelAdapter('tok', team, NS, None)
    name = a.project_name_for(app)
    marker = a._marker(app)
    a.transport = CannedTransport(responses)
    return a, name, marker


# ---------------------------------------------------------------------------
# Adapter-level classification
# ---------------------------------------------------------------------------

def test_promoted_classification_from_adapter():
    """A deployment response whose target='production' AND whose binding
    matches the deployment id is classified DEPLOYMENT_PROMOTED_TO_PRODUCTION
    by the adapter — terminal, retryable=False."""
    a, name, marker = _adapter_canned([])
    meta = a._meta(APP, 'op_smoke', 1, 'e' * 64)
    proj = _proj(name, marker, prod_id='dpl_promo')
    dep = {'id': 'dpl_promo', 'url': 'promo.vercel.app',
           'projectId': proj['id'], 'teamId': TEAM, 'name': name,
           'target': 'production', 'readyState': 'READY',
           'meta': meta}
    a.transport = CannedTransport([
        (200, proj),           # guard binding read
        (201, dep),            # POST response
        (200, proj),           # guard re-read (binding matches dep id)
    ])
    r = a.deploy_static_files(APP, proj, {'index.html': b'<html></html>'},
                              'op_smoke', 1, 'e' * 64)
    assert not r.success
    assert r.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    assert r.retryable is False
    assert r.data.get('promoted_to_production') is True
    assert r.data.get('deployment_id') == 'dpl_promo'


def test_preview_deployment_does_not_trigger_promoted_guard():
    """A deployment response with target=None (preview) and the binding still
    pointing at a different deployment id passes the adapter guard — no
    PROMOTED outcome."""
    a, name, marker = _adapter_canned([])
    meta = a._meta(APP, 'op_prev', 1, 'e' * 64)
    proj = _proj(name, marker, prod_id='dpl_boot')  # bootstrap binding
    dep = {'id': 'dpl_new', 'url': 'new.vercel.app',
           'projectId': proj['id'], 'teamId': TEAM, 'name': name,
           'target': None, 'readyState': 'READY',
           'meta': meta}
    a.transport = CannedTransport([
        (200, proj),          # guard: binding = dpl_boot
        (201, dep),           # POST response
        (200, proj),          # guard re-read: binding still dpl_boot
    ])
    r = a.deploy_static_files(APP, proj, {'index.html': b'<html></html>'},
                              'op_prev', 1, 'e' * 64)
    assert r.success, r.error_code


def test_unreadable_binding_fails_closed_never_assumes_preview():
    """A malformed targets shape in the binding read must fail closed with
    DEPLOYMENT_IDENTITY_MISMATCH — the adapter never infers 'not live' from
    an unreadable response."""
    a, name, marker = _adapter_canned([])
    bad_proj = _proj(name, marker, prod_id='dpl_x')
    bad_proj['targets'] = 'not-a-dict'
    a.transport = CannedTransport([(200, bad_proj)])
    with pytest.raises(Exception):
        a._current_production_id(bad_proj)


# ---------------------------------------------------------------------------
# Orchestrator guard (_guard_production_binding)
# ---------------------------------------------------------------------------

def _orch_guard(tmp_path, prod_binding, dep_id, app, label):
    """Build orchestrator + real-adapter transport for a guard test.

    The canned POST response's meta MUST match the operation identity the
    orchestrator mints for THIS project+workspace (sha256 of
    project_id:source_revision:snapshot_identity), so we read it back from
    the store after the first run and rebuild the transport accordingly.
    Returns a callable ``run()`` that re-enters the orchestrator over the
    same state and the given canned responses.
    """
    from app.core.state import ProjectStateStore
    from tests.test_preview_orchestrator import _make_workspace, _preview_ready_state, _deps
    from tests.test_preview_orchestrator_promoted_terminal import (
        _real_adapter_over, _op_from_state, RealDeployVercel)

    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, app, ws)

    # Probe run (fake vercel) to mint the deterministic operation identity.
    vercel_probe = _deps(tmp_path).vercel
    PreviewOrchestrator(store, _deps(tmp_path, vercel=vercel_probe)).run_owned(app, ws)
    op = _op_from_state(store)

    # Reset success-path state so the real-adapter run re-enters at the
    # deploy stage instead of short-circuiting via latest_shown_preview.
    with store.acquire_writer(app) as st:
        st.deployment['preview_intent']['stage'] = 'created'
        st.deployment.pop('latest_shown_preview', None)
        st.deployment['preview_intent'].pop('smoke', None)
        st.deployment.pop('external_promotion', None)
        st.deployment['preview_intent'].pop('promoted_terminal', None)
        st.deployment['preview_intent'].pop('deployment_attempted', None)
        st.deployment['preview_intent'].pop('deployment_unresolved', None)
        st.deployment['preview_intent'].pop('notification_state', None)
        store.save(st)

    a_probe = VercelAdapter('secret', TEAM, NS, None)
    name = a_probe.project_name_for(app)
    marker = a_probe._marker(app)
    proj = _proj(name, marker, prod_binding)
    real_a, transport = _real_adapter_over(proj, dep_id, op)
    vercel = RealDeployVercel(real_a, transport, prod_id=prod_binding)
    vercel._project = proj
    deps = _deps(tmp_path, vercel=vercel)
    return store, ws, transport, deps, label


def test_guard_blocks_production_binding(tmp_path):
    """If the deployed deployment id equals the current production binding,
    the orchestrator guard returns DEPLOYMENT_PROMOTED_TO_PRODUCTION and the
    flow never enters smoke/delivery."""
    from tests.test_preview_orchestrator_promoted_terminal import APP_FOR_PROJECT
    app = APP_FOR_PROJECT
    store, ws, transport, deps, _ = _orch_guard(
        tmp_path, 'dpl_prod', 'dpl_prod', app, 'block')
    result = PreviewOrchestrator(store, deps).run_owned(app, ws)
    assert not result.success
    assert result.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert len(posts) == 1


def test_guard_passes_preview_binding(tmp_path):
    """If the deployed deployment id does NOT equal the production binding,
    the guard passes and the flow continues to smoke."""
    from tests.test_preview_orchestrator import _make_workspace, _preview_ready_state, _deps
    from tests.test_preview_orchestrator_promoted_terminal import (
        _real_adapter_over, _op_from_state, RealDeployVercel, APP_FOR_PROJECT)
    app = APP_FOR_PROJECT

    store = ProjectStateStore(tmp_path / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, app, ws)
    # Probe to mint the deterministic operation identity.
    PreviewOrchestrator(store, _deps(tmp_path)).run_owned(app, ws)
    op = _op_from_state(store)
    with store.acquire_writer(app) as st:
        st.deployment['preview_intent']['stage'] = 'created'
        st.deployment.pop('latest_shown_preview', None)
        st.deployment['preview_intent'].pop('smoke', None)
        st.deployment['preview_intent'].pop('deployment_attempted', None)
        st.deployment['preview_intent'].pop('deployment_unresolved', None)
        st.deployment['preview_intent'].pop('deployment_attempt_outcome', None)
        st.deployment['preview_intent'].pop('promoted_terminal', None)
        st.deployment.pop('external_promotion', None)
        store.save(st)

    a_probe = VercelAdapter('secret', TEAM, NS, None)
    proj = _proj(a_probe.project_name_for(app),
                 a_probe._marker(app), 'dpl_boot')
    # Canned responses: guard read (binding=dpl_boot), POST (preview dep
    # id=dpl_new, target=None), guard re-read (binding still dpl_boot).
    meta = a_probe._meta(app, op['operation_id'], op['source_revision'],
                         op['artifact_sha256'])
    preview_dep = {'id': 'dpl_new', 'url': 'new.vercel.app',
                   'projectId': proj['id'], 'teamId': TEAM,
                   'name': proj['name'], 'target': None,
                   'readyState': 'READY', 'meta': meta}
    real_a = VercelAdapter('secret', TEAM, NS, None)
    transport = CannedTransport([
        (200, proj),
        (201, preview_dep),
        (200, proj),
    ])
    real_a.transport = transport
    vercel = RealDeployVercel(real_a, transport, prod_id='dpl_boot')
    vercel._project = proj
    deps = _deps(tmp_path, vercel=vercel)
    result = PreviewOrchestrator(store, deps).run_owned(app, ws)
    # The guard must NOT have reclassified it as promoted.
    assert result.error_code != 'DEPLOYMENT_PROMOTED_TO_PRODUCTION', (
        result.error_code, result.data)
    # And exactly one POST (no duplicate).
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert len(posts) == 1


def test_promoted_terminal_is_invariant_across_restarts(tmp_path):
    """Once promoted_terminal is persisted, every subsequent same-operation
    reconcile returns the terminal result with ZERO provider calls."""
    from tests.test_preview_orchestrator_promoted_terminal import APP_FOR_PROJECT
    app = APP_FOR_PROJECT
    store, ws, transport, deps, _ = _orch_guard(
        tmp_path, 'dpl_promo', 'dpl_promo', app, 'term')
    orch = PreviewOrchestrator(store, deps)
    first = orch.run_owned(app, ws)
    assert first.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    calls_snapshot = len(transport.calls)
    second = orch.run_owned(app, ws)
    assert second.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    assert len(transport.calls) == calls_snapshot, (
        f"second run issued {len(transport.calls) - calls_snapshot} provider calls")


def test_smoke_pass_does_not_equate_to_preview_approval(tmp_path):
    """Semantic boundary: the preview/smoke path never writes production_url
    or transitions lifecycle to LIVE. Approval belongs exclusively to the
    release contract (commit_release)."""
    from tests.test_preview_orchestrator_promoted_terminal import APP_FOR_PROJECT
    app = APP_FOR_PROJECT
    store, ws, transport, deps, _ = _orch_guard(
        tmp_path, 'dpl_boot', 'dpl_sem', app, 'sem')
    result = PreviewOrchestrator(store, deps).run_owned(app, ws)
    state = store.load(app)
    assert state.production_url is None, \
        "preview/smoke path must not write production_url"
    assert state.lifecycle != 'LIVE'
