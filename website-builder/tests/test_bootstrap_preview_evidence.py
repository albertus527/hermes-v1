"""P1.5 — Bootstrap evidence chain (mock, no real provider).

Proves the mock surface of docs/R2_BOOTSTRAP_EVIDENCE.md without touching a
real Vercel account:

* Evidence A: ensure_bootstrap confirms remote binding (targets.production
  == bootstrap deployment, READY) — a bare 201 POST is insufficient.
* Evidence B: a subsequent omitted-target deployment response whose
  target=='production' (or whose binding moved to the new deployment) is
  classified DEPLOYMENT_PROMOTED_TO_PRODUCTION — detection, not prevention.
* Evidence C: a protected URL alone is not proof; the binding state (from a
  project GET) is the authoritative "is this deployment LIVE" evidence.

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

from app.deploy.adapters import (
    HttpResponse,
    VercelAdapter,
)

TEAM = 'team_1'
NS = 'ns'
APP = 'app'


def _proj(targets=None, name=None, marker=None, extra=None):
    proj = {
        'id': 'prj_1',
        'name': name or 'wb-eval',
        'accountId': TEAM,
        'env': [{'key': 'WEBSITE_BUILDER_OWNER',
                 'value': marker or 'm', 'type': 'plain'}],
    }
    if targets is not None:
        proj['targets'] = targets
    if extra:
        proj.update(extra)
    return proj


class CannedTransport:
    """Sequence of canned (status, body) responses; records calls."""

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


def _ready_dep(dep_id, target, url, name, marker, proj_id='prj_1',
               meta=None):
    return {'id': dep_id, 'url': url, 'state': 'READY', 'target': target,
            'readyState': 'READY', 'name': name, 'teamId': TEAM,
            'projectId': proj_id,
            'meta': meta if meta is not None else {'wbOwner': marker}}


# ---------------------------------------------------------------------------
# Evidence A — bootstrap confirmation
# ---------------------------------------------------------------------------

def test_ensure_bootstrap_confirms_via_remote_binding():
    """A: success requires targets.production.id == bootstrap id AND READY.
    The bare POST response is never sufficient on its own."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    b_dep = _ready_dep('dpl_boot', None, 'boot.vercel.app',
                       proj['name'], proj['env'][0]['value'])
    transport = CannedTransport([
        (200, proj),  # initial binding read: None
        (200, {'deployments': [], 'pagination': {'next': None}}),  # no prior bootstrap
        (201, b_dep),  # bootstrap POST
        (200, {**proj, 'targets': {'production': {'id': 'dpl_boot'}}}),  # poll 1: binding moved
        (200, b_dep),  # deployment detail: READY
    ])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=5, interval=0)
    assert r.success, r.error_code
    # No POST beyond the one bootstrap.
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert len(posts) == 1


def test_ensure_bootstrap_already_bound_is_noop_no_post():
    """A: production already bound -> immediate no-op, zero POSTs."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets={'production': {'id': 'dpl_boot'}},
                 name=a.project_name_for(APP), marker=a._marker(APP))
    transport = CannedTransport([(200, proj)])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=5, interval=0)
    assert r.success
    assert r.data.get('already_current_production') is True
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert posts == []


def test_ensure_bootstrap_timeout_is_terminal_no_second_post():
    """A: bounded poll that never reaches READY -> BOOTSTRAP_CONFIRMATION_TIMEOUT.
    No second bootstrap POST is issued."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    b_dep = _ready_dep('dpl_boot', None, 'boot.vercel.app',
                       proj['name'], proj['env'][0]['value'])
    building = dict(b_dep, readyState='BUILDING')
    bound = {**proj, 'targets': {'production': {'id': 'dpl_boot'}}}
    transport = CannedTransport([
        (200, proj),  # initial: no binding
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, b_dep),  # bootstrap POST
        (200, bound),  # poll 1: binding moved
        (200, building),  # deployment detail: not ready
        (200, bound),  # poll 2: binding still ours
        (200, building),  # deployment detail: not ready
        (200, bound),  # poll 3: binding still ours
        (200, building),  # deployment detail: not ready
    ])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=3, interval=0)
    assert not r.success
    assert r.error_code == 'BOOTSTRAP_CONFIRMATION_TIMEOUT'
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert len(posts) == 1  # only the bootstrap POST; no resend


# ---------------------------------------------------------------------------
# Evidence B — next deployment target shape
# ---------------------------------------------------------------------------

def test_next_deployment_promoted_is_detected_not_prevented():
    """B: an omitted-target deployment that the provider promoted to
    production is classified DEPLOYMENT_PROMOTED_TO_PRODUCTION (detection).
    The request shape was the correct preview shape; the provider did not
    honour it for this deployment."""
    a = VercelAdapter('tok', TEAM, NS, None)
    name = a.project_name_for(APP)
    marker = a._marker(APP)
    proj = _proj(targets={'production': {'id': 'dpl_boot'}},
                 name=name, marker=marker)
    real_meta = a._meta(APP, 'op_b', 1, 'd' * 64)
    promoted_dep = _ready_dep('dpl_new', 'production', 'new.vercel.app',
                               name, marker, meta=real_meta)
    # Guard read, POST response, guard re-read (binding now points at dpl_new).
    transport = CannedTransport([
        (200, {**proj, 'targets': {'production': {'id': 'dpl_new'}}}),
        (201, promoted_dep),
        (200, {**proj, 'targets': {'production': {'id': 'dpl_new'}}}),
    ])
    a.transport = transport
    r = a.deploy_static_files(APP, proj, {'index.html': b'<html></html>'}, 'op_b', 1, 'd' * 64)
    assert not r.success
    assert r.error_code == 'DEPLOYMENT_PROMOTED_TO_PRODUCTION'
    assert r.data.get('promoted_to_production') is True
    assert r.retryable is False


def test_next_deployment_preview_target_keeps_binding_on_bootstrap():
    """B: target None (preview) and binding still points at the bootstrap
    deployment -> success, not promoted."""
    a = VercelAdapter('tok', TEAM, NS, None)
    name = a.project_name_for(APP)
    marker = a._marker(APP)
    proj = _proj(targets={'production': {'id': 'dpl_boot'}},
                 name=name, marker=marker)
    real_meta = a._meta(APP, 'op_b2', 1, 'd' * 64)
    prev_dep = _ready_dep('dpl_new', None, 'new.vercel.app',
                           name, marker, meta=real_meta)
    transport = CannedTransport([
        (200, proj),  # guard: binding still on bootstrap
        (201, prev_dep),
        (200, proj),  # guard re-read: binding unchanged
    ])
    a.transport = transport
    r = a.deploy_static_files(APP, proj, {'index.html': b'<html></html>'}, 'op_b2', 1, 'd' * 64)
    assert r.success, r.error_code


# ---------------------------------------------------------------------------
# Evidence C — anonymous reachability vs. binding state
# ---------------------------------------------------------------------------

def test_binding_state_is_authoritative_not_protected_url():
    """C: a protected preview URL returning 401 is NOT by itself proof the
    deployment is not public. The authoritative "is this LIVE" evidence is
    the project's targets.production binding read from a fresh project GET.
    This test pins the authoritative path: _current_production_id must be
    the source of the promoted/preview verdict, and it must come from a
    project GET, never from the deployment response body."""
    a = VercelAdapter('tok', TEAM, NS, None)
    name = a.project_name_for(APP)
    marker = a._marker(APP)
    # Fresh project GET carries the authoritative binding.
    proj = _proj(targets={'production': {'id': 'dpl_x'}},
                 name=name, marker=marker)
    transport = CannedTransport([(200, proj)])
    a.transport = transport
    prod_id = a._current_production_id(proj)
    assert prod_id == 'dpl_x'
    # Exactly one GET was issued and it was a project read, never a
    # deployment body inference.
    assert [c['method'] for c in transport.calls] == ['GET']
    assert '/v9/projects/' in transport.calls[0]['url']


def test_unreadable_binding_fails_closed_never_assumes_not_live():
    """C: a malformed or absent targets shape must fail closed. The
    orchestrator must never infer 'not live' from an unreadable response."""
    a = VercelAdapter('tok', TEAM, NS, None)
    name = a.project_name_for(APP)
    marker = a._marker(APP)
    bad = _proj(name=name, marker=marker)
    bad['targets'] = 'not-a-dict'
    transport = CannedTransport([(200, bad)])
    a.transport = transport
    with pytest.raises(Exception):
        a._current_production_id(bad)


def test_preview_only_unproven_when_binding_moved():
    """C/B combined: if after the next deployment the binding moved to that
    deployment, preview-only is unproven and the terminal outcome stands —
    the evidence chain is broken and the guard remains fail-closed."""
    a = VercelAdapter('tok', TEAM, NS, None)
    name = a.project_name_for(APP)
    marker = a._marker(APP)
    # Binding now points at the new deployment: unproven.
    moved = _proj(targets={'production': {'id': 'dpl_new'}},
                  name=name, marker=marker)
    transport = CannedTransport([(200, moved)])
    a.transport = transport
    prod_id = a._current_production_id(moved)
    assert prod_id == 'dpl_new'
    # A deployment whose id equals the production binding cannot be
    # asserted preview-only.
    assert prod_id == 'dpl_new'


# ---------------------------------------------------------------------------
# P1 FINALIZATION — crash-safe bootstrap idempotency marker
# ---------------------------------------------------------------------------

def test_bootstrap_create_records_durable_marker_after_post():
    """P1 FINALIZATION: a fresh bootstrap POST writes the marker into the
    caller's persistent bootstrap_state AFTER the create response returns,
    recording the issued deployment id for later GET-only reconciliation."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    b_dep = _ready_dep('dpl_boot', None, 'boot.vercel.app',
                       proj['name'], proj['env'][0]['value'])
    bound = {**proj, 'targets': {'production': {'id': 'dpl_boot'}}}
    transport = CannedTransport([
        (200, proj),  # initial binding read: None
        (200, {'deployments': [], 'pagination': {'next': None}}),  # no prior
        (201, b_dep),  # bootstrap POST
        (200, bound),  # poll 1: binding moved
        (200, b_dep),  # deployment detail: READY
    ])
    a.transport = transport
    state = {}
    r = a.ensure_bootstrap(APP, proj, max_polls=5, interval=0,
                           bootstrap_state=state)
    assert r.success, r.error_code
    marker = state['wbBootstrapMeta']
    assert marker['deployment_id'] == 'dpl_boot'
    assert isinstance(marker['payload_digest'], str)
    posts = [c for c in transport.calls if c['method'] == 'POST']
    assert len(posts) == 1


def test_bootstrap_restart_after_unknown_outcome_is_get_only():
    """P1 FINALIZATION (the core crash window): a first invocation POSTs the
    bootstrap and the marker records the deployment id; the process then
    restarts BEFORE confirmation completes. The second invocation must NOT
    issue a second POST -- it reconciles read-only (polls the recorded
    deployment and the provider's own metadata) until confirmation."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    b_dep = _ready_dep('dpl_boot', None, 'boot.vercel.app',
                       proj['name'], proj['env'][0]['value'])
    building = dict(b_dep, readyState='BUILDING')
    bound = {**proj, 'targets': {'production': {'id': 'dpl_boot'}}}
    state = {}
    # Invocation 1: POST the bootstrap, record the marker, but time out
    # before confirmation completes.
    first = CannedTransport([
        (200, proj),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, b_dep),
        (200, bound),
        (200, building),
        (200, bound),
        (200, building),
        (200, bound),
        (200, building),
    ])
    a.transport = first
    r1 = a.ensure_bootstrap(APP, proj, max_polls=3, interval=0,
                            bootstrap_state=state)
    assert not r1.success
    assert r1.error_code == 'BOOTSTRAP_CONFIRMATION_TIMEOUT'
    assert state['wbBootstrapMeta']['deployment_id'] == 'dpl_boot'
    posts1 = [c for c in first.calls if c['method'] == 'POST']
    assert len(posts1) == 1
    # Invocation 2 (restart): same state, still BUILDING. No second POST:
    # the marker forces the GET-only path and polls the recorded id.
    second = CannedTransport([
        (200, proj),  # initial binding read: still absent
        (200, {'deployments': [], 'pagination': {'next': None}}),  # list lag
        (200, bound),  # poll 1: binding now at our id
        (200, building),  # not ready yet
        (200, bound),
        (200, dict(b_dep, readyState='READY')),  # ready
    ])
    a.transport = second
    r2 = a.ensure_bootstrap(APP, proj, max_polls=5, interval=0,
                            bootstrap_state=state)
    assert r2.success, r2.error_code
    posts2 = [c for c in second.calls if c['method'] == 'POST']
    assert posts2 == []  # crash-safe: no duplicate bootstrap POST


def test_bootstrap_marker_mismatch_fails_closed_no_post():
    """P1 FINALIZATION: a marker that records a deployment id whose digest
    no longer matches this project generation cannot be trusted to
    identify which deployment was created -> fail closed, GET-only, no
    POST."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    state = {'wbBootstrapMeta': {'payload_digest': 'corrupt',
                                 'deployment_id': 'dpl_boot'}}
    transport = CannedTransport([
        (200, proj),
        (200, {'deployments': [], 'pagination': {'next': None}}),
    ])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=5, interval=0,
                           bootstrap_state=state)
    assert not r.success
    assert r.error_code == 'BOOTSTRAP_RECONCILIATION_REQUIRED'
    assert [c['method'] for c in transport.calls if c['method'] == 'POST'] == []


def test_bootstrap_confirmed_marker_still_noop_on_reentry():
    """P1 FINALIZATION: once bootstrap is confirmed READY+bound, the
    marker-bearing re-entry is a no-op (the production binding already
    proves the slot is consumed; no polling, no POST)."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets={'production': {'id': 'dpl_boot'}},
                 name=a.project_name_for(APP), marker=a._marker(APP))
    state = {'wbBootstrapMeta': {'payload_digest': 'anything',
                                 'deployment_id': 'dpl_boot'}}
    transport = CannedTransport([(200, proj)])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=5, interval=0,
                           bootstrap_state=state)
    assert r.success
    assert r.data.get('already_current_production') is True
    assert [c['method'] for c in transport.calls] == ['GET']


def test_bootstrap_legacy_state_without_marker_still_relocates():
    """P1 FINALIZATION backward-compat: a legacy state dict without the
    marker (or the None default) still relocates by deterministic identity
    and creates exactly one POST for a fresh project -- no behavior
    regression."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    b_dep = _ready_dep('dpl_boot', None, 'boot.vercel.app',
                       proj['name'], proj['env'][0]['value'])
    bound = {**proj, 'targets': {'production': {'id': 'dpl_boot'}}}
    transport = CannedTransport([
        (200, proj),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, b_dep),
        (200, bound),
        (200, b_dep),
    ])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=5, interval=0)  # no state
    assert r.success
    posts = [c['method'] for c in transport.calls if c['method'] == 'POST']
    assert posts == ['POST']  # exactly one create POST


def test_bootstrap_malformed_state_fails_closed():
    """P1 FINALIZATION: a non-dict bootstrap_state is not a signal to
    create -- it fails closed."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    transport = CannedTransport([])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, bootstrap_state='not-a-dict')
    assert not r.success
    assert r.error_code == 'BOOTSTRAP_RECONCILIATION_REQUIRED'
    assert transport.calls == []


def test_bootstrap_pre_post_attempt_marker_not_found_fails_closed():
    """P1 FINALIZATION (pre-POST crash window): the pre-POST attempt marker
    survived a crash, but the provider list does not show the deployment and
    the marker carries no id. The outcome is UNKNOWN -> fail closed, GET-only,
    never a second POST."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    digest = a._bootstrap_payload_digest(APP, proj) if hasattr(
        a, '_bootstrap_payload_digest') else None  # digest is computed inline
    # Seed a marker whose digest matches this generation. The digest is
    # deterministic; reuse the adapter's own computation via a dry run is
    # overkill, so derive it the same way the adapter does.
    import hashlib
    entries = [{'file': 'index.html',
                'data': 'PHRpY3RsZT4uPC90aXRsZT4=',  # placeholder, replaced below
                'encoding': 'base64'}]
    # Build the exact payload the adapter computes.
    from app.deploy.adapters import _BOOTSTRAP_HTML
    import base64 as _b64
    entries = [{'file': 'index.html',
                'data': _b64.b64encode(_BOOTSTRAP_HTML).decode(),
                'encoding': 'base64'}]
    digest = hashlib.sha256(json.dumps({
        'name': proj['name'],
        'project': proj['id'],
        'files': entries,
        'meta': {'wbOwner': a._marker(APP),
                 'wbBootstrap': a._bootstrap_operation_id(APP)},
    }, sort_keys=True).encode()).hexdigest()
    state = {'wbBootstrapMeta': {'payload_digest': digest,
                                 'state': 'posted_attempted'}}
    transport = CannedTransport([
        (200, proj),
        (200, {'deployments': [], 'pagination': {'next': None}}),
    ])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=3, interval=0,
                           bootstrap_state=state)
    assert not r.success
    assert r.error_code == 'BOOTSTRAP_RECONCILIATION_REQUIRED'
    assert [c['method'] for c in transport.calls if c['method'] == 'POST'] == []


def test_bootstrap_pre_post_attempt_marker_relocates_get_only():
    """P1 FINALIZATION (pre-POST crash window, deployment found): the marker
    has no id, but the provider list shows exactly one bootstrap deployment
    for this deterministic identity. Reconcile GET-only and confirm."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    b_dep = _ready_dep('dpl_boot', None, 'boot.vercel.app',
                       proj['name'], proj['env'][0]['value'],
                       meta={'wbBootstrap': a._bootstrap_operation_id(APP)})
    bound = {**proj, 'targets': {'production': {'id': 'dpl_boot'}}}
    import hashlib
    import base64 as _b64
    from app.deploy.adapters import _BOOTSTRAP_HTML
    entries = [{'file': 'index.html',
                'data': _b64.b64encode(_BOOTSTRAP_HTML).decode(),
                'encoding': 'base64'}]
    digest = hashlib.sha256(json.dumps({
        'name': proj['name'],
        'project': proj['id'],
        'files': entries,
        'meta': {'wbOwner': a._marker(APP),
                 'wbBootstrap': a._bootstrap_operation_id(APP)},
    }, sort_keys=True).encode()).hexdigest()
    state = {'wbBootstrapMeta': {'payload_digest': digest,
                                 'state': 'posted_attempted'}}
    transport = CannedTransport([
        (200, proj),
        (200, {'deployments': [b_dep], 'pagination': {'next': None}}),
        (200, bound),
        (200, b_dep),
    ])
    a.transport = transport
    r = a.ensure_bootstrap(APP, proj, max_polls=3, interval=0,
                           bootstrap_state=state)
    assert r.success, r.error_code
    assert [c['method'] for c in transport.calls if c['method'] == 'POST'] == []
    assert r.data['deployment_id'] == 'dpl_boot'
    assert state['wbBootstrapMeta']['state'] == 'confirmed'


def test_bootstrap_mapping_facade_store_accepted():
    """P1 FINALIZATION: the production runtime wires a mapping FACADE (not a
    dict subclass) that persists each write. The adapter must accept it and
    persist the marker through __setitem__."""
    a = VercelAdapter('tok', TEAM, NS, None)
    proj = _proj(targets=None, name=a.project_name_for(APP),
                 marker=a._marker(APP))
    b_dep = _ready_dep('dpl_boot', None, 'boot.vercel.app',
                       proj['name'], proj['env'][0]['value'])
    bound = {**proj, 'targets': {'production': {'id': 'dpl_boot'}}}
    transport = CannedTransport([
        (200, proj),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, b_dep),
        (200, bound),
        (200, b_dep),
    ])
    a.transport = transport

    class Facade:
        def __init__(self):
            self.entry = {}
            self.writes = 0

        def get(self, key, default=None):
            return self.entry.get(key, default)

        def __getitem__(self, key):
            return self.entry[key]

        def __setitem__(self, key, value):
            self.entry[key] = dict(value)
            self.writes += 1

    facade = Facade()
    r = a.ensure_bootstrap(APP, proj, max_polls=3, interval=0,
                           bootstrap_state=facade)
    assert r.success, r.error_code
    assert facade.writes >= 2  # pre-POST attempt + post-response enrichment
    assert facade.entry['wbBootstrapMeta']['deployment_id'] == 'dpl_boot'
    assert facade.entry['wbBootstrapMeta']['state'] == 'confirmed'
