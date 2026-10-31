"""P2.7 — reconcile_external_promotion evidence audit (mock, no real provider).

Pins the dual-evidence contract for read-only adoption of a promotion that
was applied outside this process:

* DIRECT_IDENTITY — the project's production binding IS the intended
  deployment id AND that deployment still carries the exact trusted
  operation identity.
* PROVIDER_LINEAGE — the binding is a different deployment, but the
  provider's own lastAliasRequest records type=='promote',
  from==intended, to==binding, jobStatus=='succeeded', and the bound
  deployment is READY with its alias assigned.

Ambiguous, stale, or unreadable provider evidence must fail closed
(PROMOTE_RECONCILIATION_REQUIRED); "no production deployment" must never be
read as proof of anything (PROMOTED_UNPROVEN). No real network: every
adapter transport is a canned HttpResponse sequence.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deploy.adapters import HttpResponse, VercelAdapter

TEAM = 'team_1'
NS = 'ns'
APP = 'app'
ARTIFACT = 'd' * 64


def _proj(name, marker, targets=None, last_alias=None, extra=None):
    proj = {
        'id': 'prj_1',
        'name': name,
        'accountId': TEAM,
        'env': [{'key': 'WEBSITE_BUILDER_OWNER', 'value': marker, 'type': 'plain'}],
    }
    if targets is not None:
        proj['targets'] = targets
    if last_alias is not None:
        proj['lastAliasRequest'] = last_alias
    if extra:
        proj.update(extra)
    return proj


def _dep(dep_id, url, ready=True, alias=True, meta=None, proj_id='prj_1'):
    return {
        'id': dep_id, 'url': url, 'projectId': proj_id, 'teamId': TEAM,
        'readyState': 'READY' if ready else 'ERROR',
        'aliasAssigned': alias,
        'meta': meta or {},
    }


def _with_name(body, name):
    out = dict(body)
    out['name'] = name
    return out


def _identity(deployment_id='dpl_orig'):
    return {
        'deployment_id': deployment_id,
        'operation_id': 'op-ext',
        'source_revision': 1,
        'artifact_sha256': ARTIFACT,
    }


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


def _adapter(responses, name=None, marker=None, app=APP):
    a = VercelAdapter('tok', TEAM, NS, None)
    if name is None:
        name = a.project_name_for(app)
    if marker is None:
        marker = a._marker(app)
    a.transport = CannedTransport(responses)
    return a, name, marker


# ---------------------------------------------------------------------------
# Matching evidence
# ---------------------------------------------------------------------------

def test_direct_identity_proof():
    """Binding == intended id AND full meta match -> PROMOTED / DIRECT."""
    a, name, marker = _adapter([])
    meta = a._meta(APP, 'op-ext', 1, ARTIFACT)
    proj = _proj(name, marker, targets={'production': {'id': 'dpl_orig'}})
    a.transport = CannedTransport([
        (200, proj),                                   # promotion state
        (200, _with_name(_dep('dpl_orig', 'myproject-dpl.vercel.app', meta=meta), name)),
    ])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert r.success
    assert r.data['status'] == 'PROMOTED'
    assert r.data['proof'] == 'DIRECT_IDENTITY'
    assert r.data['deployment_id'] == 'dpl_orig'


def test_provider_lineage_proof():
    """Binding is a NEW deployment id but the provider's own lastAliasRequest
    attests from==intended -> PROMOTED / PROVIDER_LINEAGE."""
    a, name, marker = _adapter([])
    meta = a._meta(APP, 'op-ext', 1, ARTIFACT)
    proj = _proj(
        name, marker,
        targets={'production': {'id': 'dpl_new'}},
        last_alias={
            'jobStatus': 'succeeded', 'type': 'promote',
            'fromDeploymentId': 'dpl_orig', 'toDeploymentId': 'dpl_new',
            'requestedAt': 123,
        },
    )
    a.transport = CannedTransport([
        (200, proj),
        (200, _with_name(_dep('dpl_new', 'myproject-dpl.vercel.app', meta=meta), name)),
    ])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert r.success
    assert r.data['status'] == 'PROMOTED'
    assert r.data['proof'] == 'PROVIDER_LINEAGE'


# ---------------------------------------------------------------------------
# Conflicting / incomplete evidence
# ---------------------------------------------------------------------------

def test_binding_points_at_unrelated_deployment_is_unproven():
    """Binding is a deployment id with no trusted meta and no lineage ->
    PROMOTED_UNPROVEN, never a trusted adoption."""
    a, name, marker = _adapter([])
    proj = _proj(name, marker, targets={'production': {'id': 'dpl_other'}})
    a.transport = CannedTransport([(200, proj)])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert r.success
    assert r.data['status'] == 'PROMOTED_UNPROVEN'


def test_meta_drift_fails_closed():
    """Binding == intended id but stored meta is not the approved identity ->
    PROMOTE_RECONCILIATION_REQUIRED (fail closed, never guess)."""
    a, name, marker = _adapter([])
    wrong_meta = a._meta(APP, 'op-OTHER', 1, ARTIFACT)
    proj = _proj(name, marker, targets={'production': {'id': 'dpl_orig'}})
    a.transport = CannedTransport([
        (200, proj),
        (200, _dep('dpl_orig', 'myproject-dpl.vercel.app', meta=wrong_meta)),
    ])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert not r.success
    assert r.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_alias_job_failed_is_unproven():
    """lastAliasRequest.jobStatus == 'failed' is a job failure, not a
    confirmed negative: the outcome stays ambiguous -> PROMOTED_UNPROVEN."""
    a, name, marker = _adapter([])
    proj = _proj(
        name, marker,
        targets={'production': {'id': 'dpl_new'}},
        last_alias={
            'jobStatus': 'failed', 'type': 'promote',
            'fromDeploymentId': 'dpl_orig', 'toDeploymentId': 'dpl_new',
            'requestedAt': 123,
        },
    )
    a.transport = CannedTransport([(200, proj)])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert r.success
    assert r.data['status'] == 'PROMOTED_UNPROVEN'


def test_stale_deployment_not_ready_fails_closed():
    """Binding matches DIRECT but the bound deployment is no longer READY ->
    the evidence is stale and must not be trusted."""
    a, name, marker = _adapter([])
    meta = a._meta(APP, 'op-ext', 1, ARTIFACT)
    proj = _proj(name, marker, targets={'production': {'id': 'dpl_orig'}})
    a.transport = CannedTransport([
        (200, proj),
        (200, _with_name(_dep('dpl_orig', 'myproject-dpl.vercel.app', ready=False, alias=False, meta=meta), name)),
    ])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert not r.success
    assert r.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_unreadable_promotion_state_fails_closed():
    """A malformed targets shape in the provider read must fail closed,
    never be read as 'not live'."""
    a, name, marker = _adapter([])
    proj = _proj(name, marker, targets='not-a-dict')
    a.transport = CannedTransport([(200, proj)])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert not r.success
    assert r.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_no_production_binding_is_unproven_never_negative():
    """No production deployment is NOT proof of 'not promoted'; it is
    PROMOTED_UNPROVEN and the caller must fail closed on recovery paths."""
    a, name, marker = _adapter([])
    proj = _proj(name, marker, targets=None)
    a.transport = CannedTransport([(200, proj)])
    r = a.reconcile_external_promotion(APP, proj, _identity())
    assert r.success
    assert r.data['status'] == 'PROMOTED_UNPROVEN'
    assert r.data['deployment_id'] is None
