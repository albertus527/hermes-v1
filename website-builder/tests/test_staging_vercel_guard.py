"""Phase C negative tests: staging Vercel allowlist guard (broker).

These tests exercise the guard with a RECORDING transport so we can assert
both policy (deny before I/O) and ownership (canonical team + pinned project).

Run:
    cd website-builder
    python -m pytest tests/test_staging_vercel_guard.py -q
"""
import json
import urllib.error

import pytest

from app.deploy.adapters import HttpResponse
from app.deploy.staging_guard import (
    StagingVercelGuard,
    build_vercel_adapter,
    resolve_canonical_team_id,
    staging_guard_enabled,
)

TEAM_ID = "team_E50r6RSMGypiUpWGdMHEK1DG"
SLUG = "albert-a121"
TOKEN = "tok_staging_not_a_real_secret"
NS = "wb-e2e-staging"
APP_ID = "tg-12345"
PROJECT_NAME = "wb-" + __import__("hashlib").sha256(
    (NS + "\0" + APP_ID).encode()).hexdigest()[:40]


class RecordingTransport:
    """Records (method, url) and returns scripted responses."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, headers, data=None, timeout=30):
        self.calls.append((method, url, data))
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                if isinstance(resp, Exception):
                    raise resp
                status, body = resp
                return HttpResponse(status, json.dumps(body).encode())
        # default: 404 not found
        return HttpResponse(404, b'{"error":{"code":"not_found"}}')


def _project(pid="prj_staging", name=PROJECT_NAME, account=TEAM_ID, marker=None):
    import hashlib
    if marker is None:
        marker = hashlib.sha256((NS + "\0" + APP_ID).encode()).hexdigest()
    return {"id": pid, "name": name, "accountId": account,
            "env": [{"key": "WEBSITE_BUILDER_OWNER", "value": marker,
                     "type": "plain", "target": ["preview"]}]}


def _guard(transport, pinned=None):
    return StagingVercelGuard(token=TOKEN, team_id=TEAM_ID,
                              ownership_namespace=NS,
                              pinned_project_id=pinned, transport=transport)


# --- 1. slug -> canonical team id resolution -------------------------------

def test_slug_resolves_to_canonical_team_id():
    tr = RecordingTransport({("GET", "/v2/teams"): (200, {"teams": [
        {"id": TEAM_ID, "slug": SLUG}, {"id": "team_OTHER000000000000", "slug": "x"}]})})
    assert resolve_canonical_team_id(token=TOKEN, team_id=SLUG, transport=tr) == TEAM_ID


def test_canonical_id_passthrough_no_network():
    tr = RecordingTransport()
    assert resolve_canonical_team_id(token=TOKEN, team_id=TEAM_ID, transport=tr) == TEAM_ID
    assert tr.calls == []          # already canonical -> no lookup


def test_ambiguous_or_missing_slug_fails_closed():
    tr = RecordingTransport({("GET", "/v2/teams"): (200, {"teams": []})})
    assert resolve_canonical_team_id(token=TOKEN, team_id=SLUG, transport=tr) is None


# --- 2. build seam: inert unless enabled -----------------------------------

def test_build_adapter_inert_without_flag():
    from app.deploy.adapters import VercelAdapter
    a = build_vercel_adapter(token=TOKEN, team_id=SLUG, ownership_namespace=NS, env={})
    assert type(a) is VercelAdapter          # production behaviour unchanged


def test_build_adapter_requires_canonical_team_when_enabled():
    with pytest.raises(ValueError):
        build_vercel_adapter(token=TOKEN, team_id=SLUG, ownership_namespace=NS,
                             env={"WEBSITE_BUILDER_STAGING_GUARD": "1"},
                             transport=RecordingTransport(
                                 {("GET", "/v2/teams"): (200, {"teams": []})}))


def test_guard_constructor_rejects_slug():
    with pytest.raises(ValueError):
        StagingVercelGuard(token=TOKEN, team_id=SLUG, ownership_namespace=NS)


# --- 3. allowlisted operations pass ----------------------------------------

def test_project_lookup_permitted():
    tr = RecordingTransport({("GET", "/v9/projects/" + PROJECT_NAME): (200, _project())})
    res = _guard(tr).lookup_project(APP_ID, expected_name=PROJECT_NAME)
    assert res.success and res.data["project"]["id"] == "prj_staging"


def test_preview_deployment_creation_permitted():
    """The guard permits POST /v13/deployments (preview) and lets it through."""
    tr = RecordingTransport({("POST", "/v13/deployments"): (200, {"id": "dpl_1"})})
    g = _guard(tr)
    status, _ = g._call("POST", "/v13/deployments",
                        {"name": "wb-x", "project": "prj_staging", "files": []})
    assert status == 200
    assert any(m == "POST" and "/v13/deployments" in u for m, u, _ in tr.calls)


# --- 4. production project id rejected (ownership) --------------------------

def test_foreign_project_id_rejected_when_pinned():
    tr = RecordingTransport({("GET", "/v9/projects/" + PROJECT_NAME): (200, _project(pid="prj_PROD"))})
    res = _guard(tr, pinned="prj_staging").lookup_project(APP_ID, expected_name=PROJECT_NAME)
    assert not res.success and res.error_code == "PROJECT_RECONCILIATION_REQUIRED"


def test_foreign_accountid_rejected():
    tr = RecordingTransport({("GET", "/v9/projects/" + PROJECT_NAME): (200, _project(account="team_OTHER000000000000"))})
    res = _guard(tr).lookup_project(APP_ID, expected_name=PROJECT_NAME)
    assert not res.success


# --- 4b. project creation confined to the staging namespace -----------------

def test_project_creation_in_staging_namespace_permitted():
    tr = RecordingTransport({("POST", "/v11/projects"): (200, {"id": "prj_new"})})
    g = _guard(tr)
    status, _ = g._call("POST", "/v11/projects",
                        {"name": PROJECT_NAME, "framework": None})
    assert status == 200
    assert any(m == "POST" and "/v11/projects" in u for m, u, _ in tr.calls)


def test_project_creation_foreign_name_rejected():
    """A name outside the derived staging-namespace shape is refused pre-I/O."""
    tr = RecordingTransport({("POST", "/v11/projects"): (200, {"id": "prj"})})
    g = _guard(tr)
    status, body = g._call("POST", "/v11/projects", {"name": "my-production-app"})
    assert status == 403
    assert body["error"]["reason"] == "project_name_not_in_staging_namespace"
    assert tr.calls == []          # denied BEFORE any network I/O


def test_project_creation_wrong_staging_name_rejected_when_pinned_name():
    from app.deploy.staging_guard import StagingVercelGuard
    tr = RecordingTransport({("POST", "/v11/projects"): (200, {"id": "prj"})})
    g = StagingVercelGuard(token="t", team_id=TEAM_ID,
                           ownership_namespace="wb-e2e-staging",
                           staging_project_name=PROJECT_NAME, transport=tr)
    other = "wb-" + "0" * 40
    status, body = g._call("POST", "/v11/projects", {"name": other})
    assert status == 403 and body["error"]["reason"] == "project_name_not_allowlisted"
    assert tr.calls == []


# --- 5. production deployment target rejected -------------------------------

def test_production_target_rejected():
    tr = RecordingTransport({("POST", "/v13/deployments"): (200, {"id": "dpl"})})
    g = _guard(tr)
    status, body = g._call("POST", "/v13/deployments", {"name": "x", "target": "production"})
    assert status == 403 and body["error"]["reason"] == "production_target"
    assert tr.calls == []          # denied BEFORE any network I/O


# --- 6. promote / domain / delete rejected ----------------------------------

@pytest.mark.parametrize("method,path,reason", [
    ("POST", "/v10/projects/prj_1/promote/dpl_1", "denied_operation"),
    ("POST", "/v10/projects/prj_1/domains", "denied_operation"),
    ("POST", "/v9/projects/prj_1/domains/example.com/verify", "denied_operation"),
    ("DELETE", "/v9/projects/prj_1", "denied_operation"),
    ("PATCH", "/v9/projects/prj_1/env", "denied_operation"),
    ("GET", "/v2/user", "not_allowlisted"),
])
def test_denied_operations(method, path, reason):
    tr = RecordingTransport()
    status, body = _guard(tr)._call(method, path, {"a": 1})
    assert status == 403 and body["error"]["reason"] == reason
    assert tr.calls == []


def test_alias_remap_payload_rejected():
    tr = RecordingTransport()
    status, body = _guard(tr)._call("POST", "/v13/deployments", {"alias": ["prod.com"]})
    assert status == 403 and body["error"]["reason"] == "alias_remap"


# --- 7. direct bypass attempts fail -----------------------------------------

def test_direct_https_bypass_is_denied_by_guard():
    """A direct provider call (not via a helper) still hits the seam."""
    tr = RecordingTransport()
    g = _guard(tr)
    assert g._call("POST", "/v10/projects/prj_1/promote/dpl_1", {})[0] == 403
    assert g._call("DELETE", "/v9/projects/prj_1", {})[0] == 403
    assert tr.calls == []


def test_guard_failure_prevents_deployment(monkeypatch):
    """If the canonical team cannot be resolved, build fails closed."""
    with pytest.raises(ValueError):
        build_vercel_adapter(
            token=TOKEN, team_id=SLUG, ownership_namespace=NS,
            env={"WEBSITE_BUILDER_STAGING_GUARD": "1"},
            transport=RecordingTransport({("GET", "/v2/teams"): (500, {})}))


# --- 8. token never reaches the environment ---------------------------------

def test_vercel_token_never_reaches_untrusted_contexts():
    """The deploy token must never reach FRONTEND / generated code / browser.

    ``vercel_env()`` is the adapter seam and carries Vercel's OWN token (that
    is its contract); it is never called by ``app/``. The untrusted contexts
    are the generation roles (``agent_env``) and the generated-project /
    browser shells (``build_env``, ``shell_env``) -- all three must be free of
    every privileged deployment credential.
    """
    from app.core import credentials
    src = {"VERCEL_TOKEN": TOKEN, "VERCEL_TEAM_ID": "team_x", "PATH": "/bin"}
    for role in ("FAST", "FRONTEND", "VISION"):
        credentials.assert_no_privileged(credentials.agent_env(role, source=src))
    for builder in (credentials.build_env("p1", "/ws", source=src),
                    credentials.shell_env(source=src)):
        credentials.assert_no_privileged(builder)
        assert "VERCEL_TOKEN" not in builder
