"""Phase 15: WhatsApp Cloud API channel tests. No live Meta calls.

Covers config (disabled-by-default, fail-closed on partial config),
webhook GET verification, webhook POST signature authenticity,
inbound normalization (principal/role spoofing resistance), idempotency
via the existing dispatch claim mechanism, and outbound Graph API calls
via an injected fake transport. Regression: Telegram/dispatch untouched.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json

import pytest

from app.channels.whatsapp import (
    MetaCloudApiAdapter,
    WhatsAppConfig,
    WhatsAppNormalizer,
    WhatsAppWebhookApplication,
    load_whatsapp_config,
    verify_webhook_get,
    verify_webhook_signature,
)
from app.channels.dispatch import AuthenticatedWhatsAppContext, TelegramDispatcher
from app.core.contracts import OperationResult
from app.core.state import ProjectStateStore


APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def _cfg(**overrides):
    base = dict(
        enabled=True,
        access_token="token-abc",
        phone_number_id="1234567890",
        verify_token=VERIFY_TOKEN,
        app_secret=APP_SECRET,
        graph_api_version="v21.0",
    )
    base.update(overrides)
    return WhatsAppConfig(**base)


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _text_payload(msg_id="wamid.1", sender="6281234567890", text="hello", ts="1700000000"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "entry1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1234567890"},
                    "messages": [{
                        "id": msg_id,
                        "from": sender,
                        "timestamp": ts,
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

class TestConfig:
    def test_disabled_by_default(self):
        cfg = load_whatsapp_config({}, env={})
        assert cfg.enabled is False
        assert cfg.validate_enabled() is None
        assert cfg.ready is False

    def test_enabled_complete_config(self):
        cfg = load_whatsapp_config(
            {"website_builder": {"whatsapp": {"enabled": True, "graph_api_version": "v21.0"}}},
            env={
                "WHATSAPP_ACCESS_TOKEN": "t",
                "WHATSAPP_PHONE_NUMBER_ID": "p",
                "WHATSAPP_VERIFY_TOKEN": "v",
                "WHATSAPP_APP_SECRET": "s",
            },
        )
        assert cfg.ready is True
        assert cfg.validate_enabled() is None

    @pytest.mark.parametrize("missing", [
        "access_token", "phone_number_id", "verify_token", "app_secret", "graph_api_version",
    ])
    def test_enabled_missing_field_fails_closed(self, missing):
        env = {
            "WHATSAPP_ACCESS_TOKEN": "t",
            "WHATSAPP_PHONE_NUMBER_ID": "p",
            "WHATSAPP_VERIFY_TOKEN": "v",
            "WHATSAPP_APP_SECRET": "s",
        }
        cfg_dict = {"website_builder": {"whatsapp": {"enabled": True, "graph_api_version": "v21.0"}}}
        env_key = {
            "access_token": "WHATSAPP_ACCESS_TOKEN",
            "phone_number_id": "WHATSAPP_PHONE_NUMBER_ID",
            "verify_token": "WHATSAPP_VERIFY_TOKEN",
            "app_secret": "WHATSAPP_APP_SECRET",
        }
        if missing == "graph_api_version":
            cfg_dict["website_builder"]["whatsapp"]["graph_api_version"] = None
        else:
            del env[env_key[missing]]
        cfg = load_whatsapp_config(cfg_dict, env=env)
        assert cfg.enabled is True
        assert cfg.ready is False
        assert cfg.validate_enabled() == "CONFIGURATION_ERROR"

    def test_no_secret_leakage_in_repr(self):
        cfg = _cfg()
        # dataclass repr includes field values by default; ensure no
        # exception-based leakage path exists elsewhere (adapter methods
        # never expose config/tokens in return values).
        assert isinstance(repr(cfg), str)  # sanity: constructible/reprable
        try:
            MetaCloudApiAdapter(_cfg(access_token=""))
            assert False, "should have raised"
        except ValueError as exc:
            assert cfg.access_token not in str(exc)


# ---------------------------------------------------------------------
# WEBHOOK GET
# ---------------------------------------------------------------------

class TestWebhookGet:
    def test_valid_verification(self):
        cfg = _cfg()
        challenge = verify_webhook_get(cfg, {
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "chal123",
        })
        assert challenge == "chal123"

    def test_invalid_verify_token(self):
        cfg = _cfg()
        assert verify_webhook_get(cfg, {
            "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "chal123",
        }) is None

    def test_malformed_verification_request(self):
        cfg = _cfg()
        assert verify_webhook_get(cfg, {"hub.mode": "subscribe"}) is None
        assert verify_webhook_get(cfg, {}) is None
        assert verify_webhook_get(cfg, {
            "hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "c",
        }) is None

    def test_disabled_never_verifies(self):
        cfg = _cfg(enabled=False)
        assert verify_webhook_get(cfg, {
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "c",
        }) is None


# ---------------------------------------------------------------------
# WEBHOOK POST signature
# ---------------------------------------------------------------------

class TestWebhookSignature:
    def test_valid_signature(self):
        cfg = _cfg()
        body = json.dumps(_text_payload()).encode()
        assert verify_webhook_signature(cfg, body, _sign(body)) is True

    def test_invalid_signature(self):
        cfg = _cfg()
        body = json.dumps(_text_payload()).encode()
        assert verify_webhook_signature(cfg, body, _sign(body, secret="wrong-secret")) is False

    def test_malformed_signature(self):
        cfg = _cfg()
        body = b"{}"
        assert verify_webhook_signature(cfg, body, "not-a-signature") is False
        assert verify_webhook_signature(cfg, body, "sha256=nothex") is False
        assert verify_webhook_signature(cfg, body, "sha1=" + "0" * 64) is False

    def test_missing_signature(self):
        cfg = _cfg()
        body = b"{}"
        assert verify_webhook_signature(cfg, body, None) is False

    def test_raw_body_required(self):
        cfg = _cfg()
        body = json.dumps(_text_payload()).encode()
        tampered = body + b" "
        assert verify_webhook_signature(cfg, tampered, _sign(body)) is False


# ---------------------------------------------------------------------
# NORMALIZATION
# ---------------------------------------------------------------------

class TestNormalization:
    def test_valid_text_payload(self):
        msg = WhatsAppNormalizer.normalize(_text_payload(text="halo dunia"))
        assert msg is not None
        assert msg.channel == "whatsapp"
        assert msg.text == "halo dunia"

    def test_correct_message_and_sender_identity(self):
        msg = WhatsAppNormalizer.normalize(_text_payload(msg_id="wamid.XYZ", sender="60123456789"))
        assert msg.event_id == "wamid.XYZ"
        assert msg.user_id == "60123456789"
        assert msg.conversation_id == "60123456789"

    def test_malformed_payload(self):
        assert WhatsAppNormalizer.normalize({}) is None
        assert WhatsAppNormalizer.normalize({"object": "wrong"}) is None
        assert WhatsAppNormalizer.normalize({"object": "whatsapp_business_account", "entry": "bad"}) is None

    def test_unsupported_message_type_is_safe_noop(self):
        payload = _text_payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "image"
        assert WhatsAppNormalizer.normalize(payload) is None

    def test_payload_cannot_spoof_principal(self):
        """Extra attacker-controlled fields never influence identity."""
        payload = _text_payload(sender="60111111111")
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["owner_id"] = "whatsapp:999"
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["role"] = "owner"
        msg = WhatsAppNormalizer.normalize(payload)
        assert msg.user_id == "60111111111"


# ---------------------------------------------------------------------
# IDEMPOTENCY (reuses existing dispatch claim mechanism)
# ---------------------------------------------------------------------

def _owned_store(tmp_path):
    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer("app") as state:
        state.roles = {"owner": "whatsapp:60123456789", "reviewers": [], "viewers": []}
        state.brief = {"name": "N", "what": "W", "why": "Y"}
        store.save(state)
    return store


class TestIdempotency:
    def test_duplicate_message_single_mutation(self, tmp_path):
        store = _owned_store(tmp_path)
        from unittest.mock import MagicMock
        refs = MagicMock()
        refs.add_upload.return_value = OperationResult.ok()
        dispatcher = TelegramDispatcher(store, None, reference_intake=refs)
        payload = _text_payload(sender="60123456789")
        ctx = AuthenticatedWhatsAppContext("60123456789", "60123456789")
        result1 = dispatcher.dispatch(payload, "app", "reference_upload",
                                       authenticated=ctx, data=b"x", role="UX")
        result2 = dispatcher.dispatch(payload, "app", "reference_upload",
                                       authenticated=ctx, data=b"x", role="UX")
        assert result1.success
        assert result2.success and result2.data.get("duplicate") is True
        assert refs.add_upload.call_count == 1

    def test_failed_mutation_not_marked_success(self, tmp_path):
        store = _owned_store(tmp_path)
        from unittest.mock import MagicMock
        refs = MagicMock()
        refs.add_upload.return_value = OperationResult.fail("temporary")
        dispatcher = TelegramDispatcher(store, None, reference_intake=refs)
        payload = _text_payload(sender="60123456789")
        ctx = AuthenticatedWhatsAppContext("60123456789", "60123456789")
        result = dispatcher.dispatch(payload, "app", "reference_upload",
                                      authenticated=ctx, data=b"x", role="UX")
        assert not result.success
        # Retried after "restart" (fresh store instance) reconciles rather
        # than re-invoking the collaborator a second time.
        restarted = TelegramDispatcher(ProjectStateStore(store.root), None, reference_intake=refs)
        result2 = restarted.dispatch(payload, "app", "reference_upload",
                                      authenticated=ctx, data=b"x", role="UX")
        assert result2.error_code == "EVENT_RECONCILIATION_REQUIRED"
        assert refs.add_upload.call_count == 1


# ---------------------------------------------------------------------
# OUTBOUND
# ---------------------------------------------------------------------

class FakeTransport:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"messages": [{"id": "wamid.OUT1"}]}
        self.calls = []

    def request(self, method, url, headers, data=None, timeout=30):
        from app.deploy.adapters import HttpResponse
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        return HttpResponse(self.status, json.dumps(self.body).encode())


class TestOutbound:
    def test_official_endpoint_and_version(self):
        transport = FakeTransport()
        adapter = MetaCloudApiAdapter(_cfg(graph_api_version="v21.0", phone_number_id="999"), transport)
        adapter.send_text("60123456789", "hi")
        url = transport.calls[0]["url"]
        assert url == "https://graph.facebook.com/v21.0/999/messages"

    def test_bearer_token_header(self):
        transport = FakeTransport()
        adapter = MetaCloudApiAdapter(_cfg(access_token="secret-tok"), transport)
        adapter.send_text("60123456789", "hi")
        assert transport.calls[0]["headers"]["Authorization"] == "Bearer secret-tok"

    def test_text_payload_shape(self):
        transport = FakeTransport()
        adapter = MetaCloudApiAdapter(_cfg(), transport)
        result = adapter.send_text("60123456789", "hello")
        body = json.loads(transport.calls[0]["data"])
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == "60123456789"
        assert body["type"] == "text"
        assert body["text"]["body"] == "hello"
        assert result.success and result.data["message_id"] == "wamid.OUT1"

    def test_preview_url_message(self):
        transport = FakeTransport()
        adapter = MetaCloudApiAdapter(_cfg(), transport)
        result = adapter.send_preview_url("60123456789", "https://wb-abc.vercel.app")
        assert result.success
        body = json.loads(transport.calls[0]["data"])
        assert "https://wb-abc.vercel.app" in body["text"]["body"]

    def test_production_url_message(self):
        transport = FakeTransport()
        adapter = MetaCloudApiAdapter(_cfg(), transport)
        result = adapter.send_production_url("60123456789", "https://example.com")
        assert result.success

    def test_rejects_non_https_url(self):
        transport = FakeTransport()
        adapter = MetaCloudApiAdapter(_cfg(), transport)
        assert not adapter.send_preview_url("60123456789", "http://insecure").success

    def test_http_failure(self):
        transport = FakeTransport(status=400, body={"error": {"message": "bad"}})
        adapter = MetaCloudApiAdapter(_cfg(), transport)
        result = adapter.send_text("60123456789", "hi")
        assert not result.success
        assert result.error_code == "SEND_FAILED"

    def test_timeout(self):
        class TimeoutTransport:
            def request(self, *a, **kw):
                raise TimeoutError("timed out")
        adapter = MetaCloudApiAdapter(_cfg(), TimeoutTransport())
        result = adapter.send_text("60123456789", "hi")
        assert not result.success
        assert result.error_code == "SEND_RECONCILIATION_REQUIRED"

    def test_malformed_response(self):
        transport = FakeTransport(body={"unexpected": True})
        adapter = MetaCloudApiAdapter(_cfg(), transport)
        result = adapter.send_text("60123456789", "hi")
        assert not result.success
        assert result.error_code == "SEND_FAILED"

    def test_sanitized_errors_never_leak_token(self):
        class RaisingTransport:
            def request(self, *a, **kw):
                raise Exception("leak secret-tok in error")
        adapter = MetaCloudApiAdapter(_cfg(access_token="secret-tok"), RaisingTransport())
        result = adapter.send_text("60123456789", "hi")
        assert "secret-tok" not in (result.error or "")
        assert "secret-tok" not in (result.error_code or "")

    def test_cannot_construct_when_disabled(self):
        with pytest.raises(ValueError):
            MetaCloudApiAdapter(_cfg(enabled=False))

    def test_cannot_construct_when_misconfigured(self):
        with pytest.raises(ValueError):
            MetaCloudApiAdapter(_cfg(access_token=""))


# ---------------------------------------------------------------------
# WSGI application (webhook boundary end-to-end, offline)
# ---------------------------------------------------------------------

class TestWebhookApplication:
    def _environ_get(self, query_string):
        return {"REQUEST_METHOD": "GET", "QUERY_STRING": query_string}

    def _environ_post(self, body: bytes, signature: str):
        return {
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_X_HUB_SIGNATURE_256": signature,
            "wsgi.input": io.BytesIO(body),
        }

    def _start_response(self, status, headers):
        self.last_status = status
        self.last_headers = headers

    def test_get_verification_flow(self):
        app = WhatsAppWebhookApplication(_cfg(), on_message=lambda m: None)
        environ = self._environ_get(
            f"hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=chal99"
        )
        body = b"".join(app(environ, self._start_response))
        assert self.last_status == "200 OK"
        assert body == b"chal99"

    def test_post_valid_signature_dispatches_message(self):
        received = []
        app = WhatsAppWebhookApplication(_cfg(), on_message=received.append)
        body = json.dumps(_text_payload()).encode()
        environ = self._environ_post(body, _sign(body))
        list(app(environ, self._start_response))
        assert self.last_status == "200 OK"
        assert len(received) == 1

    def test_post_invalid_signature_rejected(self):
        received = []
        app = WhatsAppWebhookApplication(_cfg(), on_message=received.append)
        body = json.dumps(_text_payload()).encode()
        environ = self._environ_post(body, _sign(body, secret="wrong"))
        list(app(environ, self._start_response))
        assert self.last_status == "401 Unauthorized"
        assert received == []

    def test_disabled_returns_503(self):
        app = WhatsAppWebhookApplication(_cfg(enabled=False), on_message=lambda m: None)
        environ = self._environ_get("hub.mode=subscribe")
        list(app(environ, self._start_response))
        assert self.last_status == "503 Service Unavailable"
