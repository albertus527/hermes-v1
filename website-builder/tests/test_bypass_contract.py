"""Live Vercel protection-bypass API CONTRACT tests (adapter level).

These pin the adapter to the REAL documented request/response shapes of:

    PATCH /v1/projects/{idOrName}/protection-bypass

Documented contract (Vercel REST API — update-protection-bypass-for-automation):

  Request body:
      {"generate": {"secret"?: string, "note"?: string}}
    ``generate`` is an OBJECT, not a boolean. The empty documented shape is
    ``{"generate": {}}`` and asks Vercel to generate a random SERVER-SIDE
    secret. We must NEVER fabricate a secret locally.

  200 response (documented):
      {"protectionBypass": {"<createdBy>": {
          "scope": ..., "createdAt": ..., "createdBy": ...,
          "secret": "<generated>", ...}}}
    plus the live endpoint returning the generated secret directly as a
    top-level ``secret`` string field.

Status classification:
  400 / 422 -> BYPASS_PROVISION_REJECTED   (deterministic, NOT ambiguous)
  401 / 403 -> BYPASS_PROVISION_FORBIDDEN
  404 / 405 / 501 -> BYPASS_PROVISION_UNSUPPORTED (explicit, sanitized)
  429 / 5xx / transport exception -> AMBIGUOUS_BYPASS_PROVISION

No real Vercel, no browser, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.contracts import OperationResult  # noqa: E402
from app.core.secrets import BypassSecretStore  # noqa: E402
from app.deploy.adapters import HttpResponse, VercelAdapter  # noqa: E402
from app.deploy.bypass import BypassProvisioner  # noqa: E402

SECRET = "abcdef0123456789abcdef0123456789"  # matches documented pattern


class FakeVercelBypass:
    """Provisioner-level double with NO read-only reconciliation surface.

    Used by the tests that pin the BypassProvisioner's storage semantics and
    the adapter's ensure_protection_bypass parse/status classification. The
    read-first reconciliation path is exercised against the REAL adapter (and
    the harness FakeVercel, which does expose read_protection_bypass).
    """

    def __init__(self, *responses):
        self.adapter, self.transport = adapter(*responses)

    def project_name_for(self, app_id):
        return self.adapter.project_name_for(app_id)

    def _marker(self, app_id):
        return self.adapter._marker(app_id)

    def ensure_protection_bypass(self, app_id, project, *, expected_name=None):
        return self.adapter.ensure_protection_bypass(
            app_id, project, expected_name=expected_name
        )


class Transport:
    """Scripted HTTP boundary. Records every request; never touches network."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, body = value
        return HttpResponse(status, json.dumps(body).encode())


def adapter(*responses):
    transport = Transport(*responses)
    return VercelAdapter("token", "team_1", "installation", transport), transport


def project(a, pid="prj_1"):
    return {
        "id": pid,
        "name": a.project_name_for("app"),
        "accountId": "team_1",
        "env": [
            {"key": "WEBSITE_BUILDER_OWNER", "value": a._marker("app"), "type": "plain"}
        ],
    }


def documented_record(secret=SECRET):
    """The documented 200 shape: a map of per-actor records."""
    return {"protectionBypass": {"user_owner": {
        "scope": "automation-bypass",
        "createdAt": 1700000000,
        "createdBy": "user_owner",
        "secret": secret,
    }}}


LIVE_SECRET = "abcdefghijklmnopqrstuvwxyz123456"  # ^[A-Za-z0-9]{32}$


def live_map_record(secret=LIVE_SECRET):
    """The LIVE-EVIDENCE 200/GET shape: the protectionBypass MAP KEY IS the
    secret and the value is metadata with NO inner ``secret`` field."""
    return {"protectionBypass": {secret: {
        "createdAt": 1,
        "createdBy": "user",
        "isEnvVar": False,
        "scope": "project",
    }}}


def owned_project_body(a, *, pid="prj_1", name=None, bypass=None):
    """A well-formed owned project READ body (marker present), plus the
    optional ``protectionBypass`` map -- exactly what GET /v9/projects
    returns. No secret is ever added unless the caller asks for one."""
    body = {
        "id": pid,
        "name": name or a.project_name_for("app"),
        "accountId": "team_1",
        "env": [
            {"key": "WEBSITE_BUILDER_OWNER", "value": a._marker("app"), "type": "plain"}
        ],
    }
    if bypass is not None:
        body["protectionBypass"] = bypass
    return body


def owned_project_body_without_bypass_key(a):
    """A well-formed owned project READ body where ``protectionBypass`` is
    GENUINELY ABSENT (legacy project)."""
    return {
        "id": "prj_1",
        "name": a.project_name_for("app"),
        "accountId": "team_1",
        "env": [
            {"key": "WEBSITE_BUILDER_OWNER", "value": a._marker("app"), "type": "plain"}
        ],
    }


# ---------------------------------------------------------------------------
# A. exact request body
# ---------------------------------------------------------------------------

def test_A_patch_body_is_documented_empty_generate_object():
    a, t = adapter((200, documented_record()))
    result = a.ensure_protection_bypass("app", project(a))
    assert result.success
    method, url, kwargs = t.calls[0]
    assert method == "PATCH"
    assert "/v1/projects/prj_1/protection-bypass?" in url
    # EXACT documented empty-generate shape (never {"generate": true}).
    assert json.loads(kwargs["data"]) == {"generate": {}}
    # And we never fabricate a secret locally.
    assert "secret" not in json.loads(kwargs["data"])["generate"]


# ---------------------------------------------------------------------------
# B. documented success -> secret parsed + persisted by the provisioner
# ---------------------------------------------------------------------------

def test_B_documented_success_parsed_and_persisted(tmp_path):
    a = FakeVercelBypass((200, documented_record()))
    store = BypassSecretStore(tmp_path / "hermes-home" / "vercel-bypass")
    prov = BypassProvisioner(a, store)

    result = prov.ensure("app", project(a))
    assert result.success, result.error
    assert result.data["secret"] == SECRET
    assert result.data["source"] == "generated"
    # Persisted securely, keyed by immutable Vercel project id.
    assert store.get("prj_1") == SECRET


def test_B2_live_top_level_secret_string_shape_is_supported():
    """The live endpoint returns the generated secret directly as a string."""
    a, _ = adapter((200, {"secret": SECRET}))
    result = a.ensure_protection_bypass("app", project(a))
    assert result.success
    assert result.data["secret"] == SECRET


def test_B3_secret_only_in_success_data_never_in_error_paths():
    # Success legitimately carries the secret in ``data`` (the caller persists
    # it); every sanitized error path must NOT carry it.
    a, _ = adapter((200, documented_record()))
    result = a.ensure_protection_bypass("app", project(a))
    assert result.data["secret"] == SECRET

    # A rejected response body that echoes the secret must never surface it
    # in the sanitized error string.
    a2, _ = adapter((400, {"error": {"message": "invalid"}, "secret": SECRET}))
    bad = a2.ensure_protection_bypass("app", project(a2))
    assert not bad.success
    assert bad.error_code == "BYPASS_PROVISION_REJECTED"
    assert SECRET not in str(bad)


# ---------------------------------------------------------------------------
# C. deterministic validation rejection -> REJECTED (NOT ambiguous)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 422])
def test_C_deterministic_validation_is_rejected_not_ambiguous(status):
    a, _ = adapter((status, {"error": {"message": "seed"}}))
    result = a.ensure_protection_bypass("app", project(a))
    assert not result.success
    assert result.error_code == "BYPASS_PROVISION_REJECTED"
    assert result.error_code != "AMBIGUOUS_BYPASS_PROVISION"


def test_C_rejected_does_not_persist_secret(tmp_path):
    a = FakeVercelBypass((400, {"error": {}}))
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    prov = BypassProvisioner(a, store)
    result = prov.ensure("app", project(a))
    assert not result.success
    assert result.error_code == "BYPASS_PROVISION_REJECTED"
    assert store.get("prj_1") is None


# ---------------------------------------------------------------------------
# D. authz forbidden
# ---------------------------------------------------------------------------

def test_D_403_is_forbidden():
    a, _ = adapter((403, {"error": {"message": "nope"}}))
    result = a.ensure_protection_bypass("app", project(a))
    assert not result.success
    assert result.error_code == "BYPASS_PROVISION_FORBIDDEN"


def test_D_401_is_forbidden():
    a, _ = adapter((401, {"error": {"message": "unauth"}}))
    result = a.ensure_protection_bypass("app", project(a))
    assert not result.success
    assert result.error_code == "BYPASS_PROVISION_FORBIDDEN"


# ---------------------------------------------------------------------------
# E. ambiguous: 429 / 500 / transport exception
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 503])
def test_E_retryable_or_server_error_is_ambiguous(status):
    a, _ = adapter((status, {"error": {"message": "slow down"}}))
    result = a.ensure_protection_bypass("app", project(a))
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"


def test_E_transport_exception_is_ambiguous():
    a, _ = adapter(TimeoutError("connection reset"))
    result = a.ensure_protection_bypass("app", project(a))
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"


# ---------------------------------------------------------------------------
# F. malformed 200 -> fail closed, no secret persisted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {},                                                    # nothing at all
    {"protectionBypass": {"user_1": {}}},                  # record w/o secret
    {"protectionBypass": {"user_1": {"secret": ""}}},      # empty secret
    {"protectionBypass": {"user_1": {"secret": 123}}},     # wrong type
    {"protectionBypass": "not-an-object"},                 # wrong shape
    {"protectionBypass": {}},                              # empty map, no secret
    # Multi-entry map: pre-existing bypass we did NOT rotate -> never mined.
    {"protectionBypass": {"old": {"secret": "old-secret"},
                          "user_1": {"secret": SECRET}}},
])
def test_F_malformed_200_fails_closed_no_persist(tmp_path, body):
    a = FakeVercelBypass((200, body))
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    prov = BypassProvisioner(a, store)
    result = prov.ensure("app", project(a))
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"
    assert store.get("prj_1") is None


# ---------------------------------------------------------------------------
# G. retry with an already stored secret -> NO PATCH at all
# ---------------------------------------------------------------------------

def test_G_retry_with_stored_secret_sends_no_patch(tmp_path):
    # Transport has NO scripted response: if a PATCH were sent, pop() would
    # raise -> the call would be ambiguous. Success proves no PATCH happened.
    a = FakeVercelBypass()
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    store.set("prj_1", SECRET)
    prov = BypassProvisioner(a, store)

    result = prov.ensure("app", project(a))
    assert result.success
    assert result.data["source"] == "stored"
    assert result.data["secret"] == SECRET
    assert a.transport.calls == []  # no provider call whatsoever
# ---------------------------------------------------------------------------
# unsupported / not-found endpoint as documented -> explicit sanitized failure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [404, 405, 501])
def test_unsupported_endpoint_is_explicit_and_sanitized(status):
    a, _ = adapter((status, {"error": {"message": "not found"}}))
    result = a.ensure_protection_bypass("app", project(a))
    assert not result.success
    assert result.error_code == "BYPASS_PROVISION_UNSUPPORTED"
    assert result.error_code not in ("AMBIGUOUS_BYPASS_PROVISION",)


# ---------------------------------------------------------------------------
# identity guard is preserved (never touched by the contract fix)
# ---------------------------------------------------------------------------

def test_identity_mismatch_still_fails_closed():
    a, t = adapter((200, documented_record()))
    bad = {"id": "prj_1", "name": "someone-elses", "accountId": "team_1", "env": []}
    result = a.ensure_protection_bypass("app", bad)
    assert not result.success
    assert result.error_code == "PROJECT_IDENTITY_MISMATCH"
    assert t.calls == []  # never even sent the PATCH


# ---------------------------------------------------------------------------
# H. LIVE-EVIDENCE shape: the protectionBypass MAP KEY is the secret
# ---------------------------------------------------------------------------

def test_H_live_map_key_shape_is_parsed_and_is_authoritative():
    """REAL LIVE EVIDENCE: GET/PATCH protectionBypass = {<secret>: {metadata}}
    with NO inner ``secret`` field. The MAP KEY is the secret."""
    a, _ = adapter((200, live_map_record()))
    result = a.ensure_protection_bypass("app", project(a))
    assert result.success
    assert result.data["secret"] == LIVE_SECRET


def test_H_live_map_key_shape_persisted_by_provisioner(tmp_path):
    """The reconciled/generated secret is persisted once by the provisioner."""
    a = FakeVercelBypass((200, live_map_record()))
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    prov = BypassProvisioner(a, store)
    result = prov.ensure("app", project(a))
    assert result.success, result.error
    assert result.data["source"] == "generated"
    assert store.get("prj_1") == LIVE_SECRET


@pytest.mark.parametrize("key,value", [
    # A key that is not a 32-alphanumeric secret -> never mined.
    ("user_owner", {"createdAt": 1, "createdBy": "user", "isEnvVar": False,
                    "scope": "project"}),
    # Value is not a metadata object.
    (LIVE_SECRET, "just-a-string"),
    (LIVE_SECRET, []),
    # An opaque/empty metadata object is malformed, never mined.
    (LIVE_SECRET, {}),
])
def test_H_malformed_single_entry_fails_closed(tmp_path, key, value):
    a = FakeVercelBypass((200, {"protectionBypass": {key: value}}))
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    prov = BypassProvisioner(a, store)
    result = prov.ensure("app", project(a))
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"
    assert store.get("prj_1") is None


def test_H_multi_entry_live_map_never_mined(tmp_path):
    a = FakeVercelBypass((200, {"protectionBypass": {
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": {
            "createdAt": 1, "createdBy": "a", "isEnvVar": False, "scope": "project"},
        LIVE_SECRET: {
            "createdAt": 2, "createdBy": "b", "isEnvVar": False, "scope": "project"},
    }}))
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    prov = BypassProvisioner(a, store)
    result = prov.ensure("app", project(a))
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"
    assert store.get("prj_1") is None


# ---------------------------------------------------------------------------
# I. read_protection_bypass — read-only recovery, no PATCH
# ---------------------------------------------------------------------------

def test_I_read_only_recovery_returns_map_key_secret_without_patch():
    a0, _ = adapter()
    a, t = adapter((200, owned_project_body(a0, bypass=live_map_record()["protectionBypass"])))
    result = a.read_protection_bypass("app", project(a))
    assert result.success
    assert result.data["secret"] == LIVE_SECRET
    # Read-only: exactly one GET, never a PATCH.
    assert [c[0] for c in t.calls] == ["GET"]


@pytest.mark.parametrize("bypass", ["__ABSENT__", {}])
def test_I_read_only_reports_absence(bypass):
    """A proven-absent key is reported as a real absence (data=={}); the
    provisioner layer (not the adapter) owns the generate/fail-closed
    decision, since only a recoverable-scan surface is authoritative."""
    a0, _ = adapter()
    if bypass == "__ABSENT__":
        # The key is genuinely absent from the READ body (legacy projects).
        body = owned_project_body_without_bypass_key(a0)
    else:
        body = owned_project_body(a0, bypass={})
    a, t = adapter((200, body))
    result = a.read_protection_bypass("app", project(a))
    assert result.success
    assert result.data == {}
    assert [c[0] for c in t.calls] == ["GET"]


def test_I_provisioner_never_generates_from_a_non_authoritative_read(tmp_path):
    """A bare ``read_protection_bypass`` absence must NOT trigger generation:
    only an authoritative recoverable scan may prove "no remote bypass"."""
    a0, _ = adapter()
    body = owned_project_body(a0, bypass={})
    a, t = adapter((200, body))
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    prov = BypassProvisioner(a, store)
    result = prov.ensure("app", project(a))
    assert not result.success
    assert result.error_code == "BYPASS_RECONCILIATION_REQUIRED"
    # Exactly one read-only GET; never a generation PATCH.
    assert [c[0] for c in t.calls] == ["GET"]
    assert store.get("prj_1") is None


@pytest.mark.parametrize("bypass", [
    {LIVE_SECRET: {"createdAt": 1, "createdBy": "u", "isEnvVar": False, "scope": "p"},
     "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": {"createdAt": 2, "createdBy": "v",
                                          "isEnvVar": False, "scope": "p"}},
    {LIVE_SECRET: {}},                       # malformed metadata
    {LIVE_SECRET: "opaque"},                 # malformed value
    "not-an-object",                         # malformed map
])
def test_I_read_only_multiple_or_malformed_fails_closed(bypass):
    a0, _ = adapter()
    a, t = adapter((200, owned_project_body(a0, bypass=bypass)))
    result = a.read_protection_bypass("app", project(a))
    assert not result.success
    assert result.error_code == "BYPASS_RECONCILIATION_REQUIRED"
    assert [c[0] for c in t.calls] == ["GET"]  # never guessed by PATCHing


def test_I_read_only_identity_guard_never_calls_provider():
    a0, _ = adapter()
    a, t = adapter((200, owned_project_body(a0, bypass=live_map_record()["protectionBypass"])))
    bad = {"id": "prj_1", "name": "someone-elses", "accountId": "team_1", "env": []}
    result = a.read_protection_bypass("app", bad)
    assert not result.success
    assert result.error_code == "PROJECT_IDENTITY_MISMATCH"
    assert t.calls == []


def test_I_read_only_unreadable_response_is_ambiguous():
    a0, _ = adapter()
    a, _ = adapter((503, {"error": {"message": "unavailable"}}))
    result = a.read_protection_bypass("app", project(a0))
    assert not result.success
    assert result.error_code == "AMBIGUOUS_BYPASS_PROVISION"


def test_I_read_only_secret_absent_from_error_and_repr():
    a0, _ = adapter()
    a, _ = adapter((503, {"error": {"message": "unavailable"}}))
    result = a.read_protection_bypass("app", project(a0))
    assert LIVE_SECRET not in str(result)
    assert LIVE_SECRET not in repr(result)

