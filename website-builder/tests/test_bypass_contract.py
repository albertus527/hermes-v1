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
    a, _ = adapter((200, documented_record()))
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
    a, _ = adapter((400, {"error": {}}))
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
    a, _ = adapter((200, body))
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
    a, t = adapter()
    store = BypassSecretStore(tmp_path / "hh" / "vercel-bypass")
    store.set("prj_1", SECRET)
    prov = BypassProvisioner(a, store)

    result = prov.ensure("app", project(a))
    assert result.success
    assert result.data["source"] == "stored"
    assert result.data["secret"] == SECRET
    assert t.calls == []  # no provider call whatsoever


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
