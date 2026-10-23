"""Deploy failure observability: classification, sanitization, timeouts.

No real network. Every transport is a local fake that either raises or returns
a canned response, so no Vercel POST is ever made by this suite.

The behaviour under test is deliberately narrow: the adapter must keep
returning its existing fail-closed result while ALSO recording enough
information to tell a timeout from a rejection. Nothing here weakens the
retryable=False contract.
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deploy.adapters import (
    HttpResponse,
    UrllibHttpTransport,
    VercelAdapter,
    classify_transport_failure,
    log_transport_failure,
)

TEAM = "team_1"
NS = "installation"
APP = "app"
SHA = "a" * 64
SECRET = "sk-or-v1-SUPERSECRETTOKENVALUE0123456789"


class FakeTransport:
    """Raises or returns canned responses; records every call's kwargs."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": headers or {}, "timeout": timeout,
                           "data": data, "bytes": len(data or b"")})
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        status, body = value
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return HttpResponse(status, raw)


def adapter(*responses, **kw):
    """Adapter over a project whose production slot is ALREADY consumed.

    The first-deployment guard reads the project's production binding before
    any POST, so a canned GET is prepended automatically. Pass
    ``bootstrapped=False`` to model a brand-new project instead (no binding).
    """
    bootstrapped = kw.pop("bootstrapped", True)
    t = FakeTransport(*responses)
    a = VercelAdapter("secret-token", TEAM, NS, t, **kw)
    guard_body = {"id": "prj_1", "name": "dorma-atelier", "accountId": TEAM,
                  "env": [{"key": "WEBSITE_BUILDER_OWNER",
                           "value": a._marker(APP), "type": "plain"}]}
    if bootstrapped:
        guard_body["targets"] = {"production": {"id": "dpl_bootstrap"}}
    else:
        guard_body["targets"] = {}
    t.responses.insert(0, (200, guard_body))
    return a, t


def project(a):
    return {"id": "prj_1", "name": "dorma-atelier", "accountId": TEAM,
            "env": [{"key": "WEBSITE_BUILDER_OWNER",
                     "value": a._marker(APP), "type": "plain"}]}


FILES = {"index.html": b"<html>ok</html>"}


def deploy(a, files=None):
    return a.deploy_static_files(APP, project(a), files or FILES, "op-1", 1, SHA,
                                 expected_name="dorma-atelier")


# ---------------------------------------------------------------------------
# 1. Classification of the six distinguishable situations
# ---------------------------------------------------------------------------

class TestClassification:
    def test_socket_timeout_is_timeout(self):
        assert classify_transport_failure(socket.timeout("timed out"))[0] == "timeout"

    def test_builtin_timeout_is_timeout(self):
        assert classify_transport_failure(TimeoutError("t"))[0] == "timeout"

    def test_url_error_wrapping_timeout_is_timeout(self):
        exc = urllib.error.URLError(socket.timeout("timed out"))
        assert classify_transport_failure(exc)[0] == "timeout"

    def test_connection_refused_is_connection(self):
        assert classify_transport_failure(ConnectionRefusedError("no"))[0] == "connection"

    def test_dns_failure_is_connection(self):
        exc = urllib.error.URLError("Name or service not known")
        assert classify_transport_failure(exc)[0] == "connection"

    def test_connection_reset_is_connection(self):
        assert classify_transport_failure(ConnectionResetError("reset"))[0] == "connection"

    def test_json_decode_is_parse_error(self):
        exc = json.JSONDecodeError("bad", "doc", 0)
        assert classify_transport_failure(exc)[0] == "parse_error"

    def test_unexpected_exception_is_application(self):
        assert classify_transport_failure(RuntimeError("boom"))[0] == "application"

    def test_http_error_carries_status(self):
        exc = urllib.error.HTTPError(
            "https://api.vercel.com/v13/deployments", 400, "Bad Request", {},
            __import__("io").BytesIO(b'{"error":{"code":"invalid_request"}}'))
        category, status, code = classify_transport_failure(exc)
        assert category == "http_error"
        assert status == 400
        assert code == "invalid_request"

    def test_http_403_maps_forbidden(self):
        exc = urllib.error.HTTPError(
            "u", 403, "Forbidden", {},
            __import__("io").BytesIO(b'{"error":{"code":"forbidden"}}'))
        assert classify_transport_failure(exc)[1:] == (403, "forbidden")

    def test_http_error_with_html_body_yields_no_code(self):
        exc = urllib.error.HTTPError(
            "u", 502, "Bad Gateway", {},
            __import__("io").BytesIO(b"<html>gateway</html>"))
        category, status, code = classify_transport_failure(exc)
        assert (category, status, code) == ("http_error", 502, "")

    def test_classifier_never_raises_on_weird_input(self):
        for weird in (None, 42, object(), "", b"bytes"):
            classify_transport_failure(weird)  # must not raise


# ---------------------------------------------------------------------------
# 2. Sanitization -- no provider prose, no secrets, bounded
# ---------------------------------------------------------------------------

class TestSanitization:
    def test_prose_code_is_discarded(self):
        exc = urllib.error.HTTPError(
            "u", 400, "Bad", {},
            __import__("io").BytesIO(
                json.dumps({"error": {"code": "Invalid payload: " + SECRET}}).encode()))
        _, _, code = classify_transport_failure(exc)
        assert SECRET not in (code or "")
        assert code == ""

    def test_safe_allowlisted_code_survives(self):
        exc = urllib.error.HTTPError(
            "u", 429, "Slow", {},
            __import__("io").BytesIO(b'{"error":{"code":"rate_limited"}}'))
        assert classify_transport_failure(exc)[2] == "rate_limited"

    def test_message_field_is_never_used(self):
        # A body whose ONLY error content is `message` must yield no code.
        exc = urllib.error.HTTPError(
            "u", 400, "Bad", {},
            __import__("io").BytesIO(
                json.dumps({"error": {"message": SECRET}}).encode()))
        assert classify_transport_failure(exc)[2] == ""

    def test_log_line_contains_no_secret(self, caplog):
        exc = urllib.error.HTTPError(
            "u", 400, "Bad", {},
            __import__("io").BytesIO(
                json.dumps({"error": {"code": SECRET, "message": SECRET}}).encode()))
        with caplog.at_level(logging.WARNING):
            log_transport_failure("deploy_static_files", exc)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert SECRET not in text
        assert "category=http_error" in text
        assert "http_status=400" in text

    def test_log_line_contains_no_authorization_header(self, caplog):
        with caplog.at_level(logging.WARNING):
            log_transport_failure("deploy_static_files", socket.timeout("timed out"))
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Bearer" not in text
        assert "secret-token" not in text

    def test_log_line_is_bounded(self, caplog):
        exc = RuntimeError("x" * 5000)
        with caplog.at_level(logging.WARNING):
            log_transport_failure("deploy_static_files", exc)
        for r in caplog.records:
            assert len(r.getMessage()) <= 240

    def test_log_transport_failure_never_raises(self):
        class Hostile(Exception):
            def __str__(self):
                raise RuntimeError("nope")
        assert log_transport_failure("op", Hostile()) in {
            "timeout", "connection", "http_error", "parse_error",
            "application", "unknown"}


# ---------------------------------------------------------------------------
# 3. deploy_static_files behaviour under each failure
# ---------------------------------------------------------------------------

class TestDeployFailClosed:
    def test_timeout_still_returns_fail_closed_and_logs(self, caplog):
        a, t = adapter(socket.timeout("timed out"))
        with caplog.at_level(logging.WARNING):
            r = deploy(a)
        assert r.success is False
        assert r.error_code == "DEPLOYMENT_RECONCILIATION_REQUIRED"
        assert r.retryable is False
        text = "\n".join(x.getMessage() for x in caplog.records)
        assert "category=timeout" in text
        assert "operation=deploy_static_files" in text

    def test_connection_failure_logs_connection(self, caplog):
        a, _ = adapter(ConnectionRefusedError("refused"))
        with caplog.at_level(logging.WARNING):
            r = deploy(a)
        assert r.success is False and r.retryable is False
        assert "category=connection" in "\n".join(
            x.getMessage() for x in caplog.records)

    def test_http_500_logs_status(self, caplog):
        a, _ = adapter((500, {"error": {"code": "internal_server_error"}}))
        with caplog.at_level(logging.WARNING):
            r = deploy(a)
        assert r.success is False and r.retryable is False
        text = "\n".join(x.getMessage() for x in caplog.records)
        assert "http_status=500" in text
        assert "internal_server_error" in text

    def test_http_400_rejection_logs_no_secret(self, caplog):
        a, _ = adapter((400, {"error": {"code": "invalid_request",
                                        "message": SECRET}}))
        with caplog.at_level(logging.WARNING):
            r = deploy(a)
        assert r.success is False
        text = "\n".join(x.getMessage() for x in caplog.records)
        assert SECRET not in text
        assert "invalid_request" in text

    def test_malformed_json_logs_parse_error(self, caplog):
        a, _ = adapter((200, b"not json at all"))
        with caplog.at_level(logging.WARNING):
            r = deploy(a)
        assert r.success is False and r.retryable is False
        assert "category=parse_error" in "\n".join(
            x.getMessage() for x in caplog.records)

    def test_invalid_files_returns_without_network(self):
        a, t = adapter()  # no responses queued: a network call would IndexError
        r = deploy(a, {"../escape.html": b"x"})
        assert r.error_code == "INVALID_STATIC_FILES"
        assert t.calls == []


# ---------------------------------------------------------------------------
# 4. Timeout budgets
# ---------------------------------------------------------------------------

class TestTimeoutBudgets:
    def test_upload_uses_upload_timeout_not_api_timeout(self):
        a, t = adapter((500, {"error": {"code": "invalid_request"}}),
                       api_timeout=7, upload_timeout=99)
        deploy(a)
        # The first-deployment guard issues a metadata GET first, so select the
        # bulk POST explicitly rather than relying on position.
        posts = [c for c in t.calls if c["method"] == "POST"]
        assert len(posts) == 1
        assert posts[0]["timeout"] == 99

    def test_defaults_are_separate_and_sane(self):
        a, _ = adapter()
        assert a.api_timeout == 30
        assert a.upload_timeout == 120
        assert a.upload_timeout > a.api_timeout

    def test_api_timeout_used_for_metadata_calls(self):
        a, t = adapter((200, {"id": "d", "projectId": "prj_1",
                              "teamId": TEAM, "name": "dorma-atelier",
                              "target": None, "meta": {}}), api_timeout=11)
        a._call("GET", "/v9/projects/x")
        assert t.calls[0]["timeout"] == 11

    def test_zero_or_negative_timeout_rejected(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                VercelAdapter("t", TEAM, NS, FakeTransport(), api_timeout=bad)
            with pytest.raises(ValueError):
                VercelAdapter("t", TEAM, NS, FakeTransport(), upload_timeout=bad)


# ---------------------------------------------------------------------------
# 5. Success path unchanged
# ---------------------------------------------------------------------------

class TestSuccessUnchanged:
    def test_successful_deploy_returns_ok(self):
        body = {"id": "dpl_1", "projectId": "prj_1", "teamId": TEAM,
                "name": "dorma-atelier", "target": None,
                "url": "dorma-atelier-abc.vercel.app",
                "meta": {}}
        a, t = adapter((200, body))
        # meta must match what _deployment validates
        meta = a._meta(APP, "op-1", 1, SHA)
        body["meta"] = dict(meta)
        a, t = adapter((200, body))
        r = deploy(a)
        assert r.success is True, r.error_code
        assert r.data["deployment"]["id"] == "dpl_1"

    def test_transport_headers_never_logged_on_success(self, caplog):
        body = {"id": "dpl_1", "projectId": "prj_1", "teamId": TEAM,
                "name": "dorma-atelier", "target": None,
                "url": "u", "meta": {}}
        a, _ = adapter((200, body))
        body["meta"] = dict(a._meta(APP, "op-1", 1, SHA))
        a, _ = adapter((200, body))
        with caplog.at_level(logging.DEBUG):
            deploy(a)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "secret-token" not in text
        assert "Bearer" not in text


# ---------------------------------------------------------------------------
# 5. First-deployment / preview-only boundary (release blocker)
#
# Vercel auto-promotes a brand-new project's FIRST deployment to
# target="production", even when no production target is requested: there is
# no "preview" literal in this API, omission IS the preview request, and it is
# not honoured for deployment #1. Therefore omitting target does NOT guarantee
# preview, and the adapter cannot ask for preview-only. Its only safe move is
# to refuse real content while the one-time auto-promotion is unspent, and to
# spend it on content-free bytes via ensure_bootstrap().
# ---------------------------------------------------------------------------

class TestFirstDeploymentBoundary:

    def _ready(self, a, target=None):
        return {"id": "dpl_1", "projectId": "prj_1", "teamId": TEAM,
                "name": "dorma-atelier", "target": target,
                "url": "x.vercel.app", "readyState": "READY",
                "meta": a._meta(APP, "op-1", 1, SHA)}

    def test_newly_created_project_with_zero_deployments_refuses_content(self):
        """New project, zero deployments, empty production binding -> refuse.

        This is exactly the dorma-atelier state: the project exists
        (prj_C2lFWS2fBPlAI7GycXPVG5GS2ebb) but has never had a deployment.
        """
        a, t = adapter(bootstrapped=False)
        r = deploy(a)
        assert not r.success
        assert r.error_code == "BOOTSTRAP_REQUIRED_BEFORE_DEPLOY"
        assert not r.retryable

    def test_zero_deployments_sends_no_deployment_request(self):
        """The refusal happens BEFORE any upload -- nothing leaves the box."""
        a, t = adapter(bootstrapped=False)
        deploy(a)
        assert [c["method"] for c in t.calls] == ["GET"]
        assert not any("/v13/deployments" in c["url"] for c in t.calls)

    def test_first_deployment_request_target_is_never_production(self):
        """When a deploy IS allowed (promotion spent), the request must not ask
        for production. The payload carries no target field at all."""
        a, t = adapter((201, {"id": "dpl_1", "projectId": "prj_1",
                              "teamId": TEAM, "name": "dorma-atelier",
                              "target": None, "url": "x.vercel.app",
                              "readyState": "READY",
                              "meta": None}))
        # rebuild the canned deployment now that `a` exists
        t.responses[1] = (201, self._ready(a))
        r = deploy(a)
        assert r.success
        post = [c for c in t.calls if c["method"] == "POST"][0]
        raw = post["data"]
        body = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        assert "target" not in body, "must not request a production target"

    def test_publication_disabled_does_not_alter_preview_deploy_request(self):
        """publication_enabled=false lives on the runtime, not the adapter: it
        gates publication, never preview. The deploy request shape must be
        identical either way, and must never carry a production target."""
        a, t = adapter((201, {"id": "dpl_1", "projectId": "prj_1",
                              "teamId": TEAM, "name": "dorma-atelier",
                              "target": None, "url": "x.vercel.app",
                              "readyState": "READY", "meta": None}))
        t.responses[1] = (201, self._ready(a))
        r = deploy(a)
        assert r.success
        posts = [c for c in t.calls if c["method"] == "POST"]
        assert len(posts) == 1
        raw = posts[0]["data"]
        body = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        assert "target" not in body

    def test_no_promotion_or_alias_call_on_preview_path(self):
        """A successful preview deploy must not touch any production endpoint."""
        a, t = adapter((201, {"id": "dpl_1", "projectId": "prj_1",
                              "teamId": TEAM, "name": "dorma-atelier",
                              "target": None, "url": "x.vercel.app",
                              "readyState": "READY", "meta": None}))
        t.responses[1] = (201, self._ready(a))
        assert deploy(a).success
        urls = " ".join(c["url"] for c in t.calls)
        for forbidden in ("aliases", "promote"):
            assert forbidden not in urls, f"preview path touched {forbidden}"

    def test_ambiguous_binding_fails_closed_not_open(self):
        """An unreadable production binding must never be read as 'safe'."""
        a, t = adapter(bootstrapped=False)
        t.responses[0] = (200, {"id": "prj_1", "name": "dorma-atelier",
                                "accountId": TEAM, "env": [],
                                "targets": {"production": "not-a-dict"}})
        r = deploy(a)
        assert not r.success
        assert r.error_code in ("DEPLOYMENT_RECONCILIATION_REQUIRED",
                                "BOOTSTRAP_REQUIRED_BEFORE_DEPLOY")
        assert not any(c["method"] == "POST" for c in t.calls)
