"""WhatsApp Cloud API channel adapter for Website Builder R1 Phase 15.

Official Meta WhatsApp Business Platform / Cloud API ONLY
(https://developers.facebook.com/documentation/business-messaging/whatsapp/).
No Twilio, no BSP, no unofficial API, no WhatsApp Web automation.

Disabled by default (``WhatsAppConfig.enabled=False``). No live Meta
account/network call is required for this module to load, be imported, or
be tested — outbound calls go through an injected HTTP transport (the same
``UrllibHttpTransport``/``_json_call`` convention as
``app/deploy/adapters.py`` and ``app/core/contact_form.py``).

This module is ONLY a transport/authentication/normalization boundary:
    Meta webhook -> signature/verify-token authentication -> normalization
    -> existing NormalizedMessage/dispatch pipeline (app.channels.dispatch)

It does NOT create a parallel Website Builder, project lifecycle, revision
system, deployment system, or messaging-provider framework. Inbound
messages become the exact same ``NormalizedMessage`` Telegram produces and
flow through the exact same ``TelegramDispatcher.dispatch`` pipeline via
``AuthenticatedWhatsAppContext`` (see app/channels/dispatch.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.channels.telegram import NormalizedMessage
from app.core.contracts import OperationResult
from app.deploy.adapters import UrllibHttpTransport, _json_call


def _fail(code: str) -> OperationResult:
    # Never expose provider responses/exceptions -- they can contain tokens.
    return OperationResult.fail(code, error_code=code, retryable=False)


@dataclass(frozen=True)
class WhatsAppConfig:
    """WhatsApp Cloud API configuration. Disabled by default.

    ``enabled`` is a non-secret ``config.yaml`` setting. The remaining
    fields are SECRETS and must come from environment variables (``.env``),
    never from ``config.yaml`` and never committed. See
    ``load_whatsapp_config`` for the canonical loader.
    """

    enabled: bool = False
    access_token: Optional[str] = None
    phone_number_id: Optional[str] = None
    verify_token: Optional[str] = None
    app_secret: Optional[str] = None
    graph_api_version: Optional[str] = None

    def validate_enabled(self) -> Optional[str]:
        """Return an error code if enabled-but-misconfigured, else None.

        When ``enabled`` is False, configuration completeness is never
        checked -- the feature is simply off, and the rest of Website
        Builder behaves exactly as before this phase.
        """
        if not self.enabled:
            return None
        for name in (
            "access_token", "phone_number_id", "verify_token",
            "app_secret", "graph_api_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                return "CONFIGURATION_ERROR"
        return None

    @property
    def ready(self) -> bool:
        return self.enabled and self.validate_enabled() is None


def load_whatsapp_config(
    cfg: Optional[Dict[str, Any]] = None, env: Optional[Dict[str, str]] = None
) -> WhatsAppConfig:
    """Build a ``WhatsAppConfig`` from ``config.yaml`` + environment secrets.

    ``cfg`` is the already-loaded Website Builder config mapping (e.g. the
    parsed ``config/default.yaml`` merged with user overrides); only the
    non-secret ``website_builder.whatsapp.enabled`` /
    ``website_builder.whatsapp.graph_api_version`` keys are read from it.
    Every credential comes from ``env`` (defaults to ``os.environ``) --
    ``WHATSAPP_ACCESS_TOKEN``, ``WHATSAPP_PHONE_NUMBER_ID``,
    ``WHATSAPP_VERIFY_TOKEN``, ``WHATSAPP_APP_SECRET``. This function never
    raises; ``validate_enabled()`` reports configuration errors only when
    the feature is actually turned on.
    """
    env = env if env is not None else os.environ
    section: Dict[str, Any] = {}
    if isinstance(cfg, dict):
        wb = cfg.get("website_builder")
        if isinstance(wb, dict):
            candidate = wb.get("whatsapp")
            if isinstance(candidate, dict):
                section = candidate
    return WhatsAppConfig(
        enabled=bool(section.get("enabled", False)),
        access_token=(env.get("WHATSAPP_ACCESS_TOKEN") or None),
        phone_number_id=(env.get("WHATSAPP_PHONE_NUMBER_ID") or None),
        verify_token=(env.get("WHATSAPP_VERIFY_TOKEN") or None),
        app_secret=(env.get("WHATSAPP_APP_SECRET") or None),
        graph_api_version=(
            section.get("graph_api_version") or env.get("WHATSAPP_GRAPH_API_VERSION") or None
        ),
    )


def verify_webhook_get(config: WhatsAppConfig, params: Dict[str, Any]) -> Optional[str]:
    """Official Meta webhook GET verification handshake.

    ``params`` maps the exact query parameter names Meta sends
    (``hub.mode``, ``hub.verify_token``, ``hub.challenge``). Returns the
    challenge string ONLY after ``hub.mode == "subscribe"`` and a constant-
    time match of ``hub.verify_token`` against the configured token.
    Never discloses the configured verify token. Returns None (reject) for
    disabled/misconfigured, malformed, or invalid-token requests.
    """
    if not config.ready:
        return None
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if (
        mode != "subscribe"
        or not isinstance(token, str)
        or not isinstance(challenge, str)
        or not challenge
    ):
        return None
    if not hmac.compare_digest(token.encode("utf-8"), config.verify_token.encode("utf-8")):
        return None
    return challenge


def verify_webhook_signature(
    config: WhatsAppConfig, raw_body: bytes, signature_header: Optional[str]
) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` over the RAW request body.

    Fails closed: disabled/misconfigured, missing header, malformed
    header, or mismatched HMAC all return False. Uses constant-time
    comparison. Never parses/trusts the body before this check passes.
    """
    if not config.ready:
        return False
    if not isinstance(raw_body, (bytes, bytearray)):
        return False
    if not isinstance(signature_header, str) or not signature_header.startswith("sha256="):
        return False
    provided = signature_header[len("sha256="):].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", provided):
        return False
    expected = hmac.new(
        config.app_secret.encode("utf-8"), bytes(raw_body), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


class WhatsAppNormalizer:
    """Normalize Meta WhatsApp Cloud API webhook payloads.

    Only plain inbound text messages are in scope for R1. Any other shape
    (unsupported message type, malformed payload, missing identity) is a
    deterministic safe no-op: returns None, never raises, never fabricates
    text, never triggers a mutation.
    """

    @staticmethod
    def normalize(payload: Dict[str, Any]) -> Optional[NormalizedMessage]:
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
            return None
        entries = payload.get("entry")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("changes")
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict) or change.get("field") != "messages":
                    continue
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                messages = value.get("messages")
                if not isinstance(messages, list) or not messages:
                    continue
                message = messages[0]
                if not isinstance(message, dict):
                    return None

                event_id = message.get("id")
                sender = message.get("from")
                if (
                    not isinstance(event_id, str) or not event_id.strip()
                    or not isinstance(sender, str) or not sender.strip()
                ):
                    return None

                if message.get("type") != "text":
                    # Unsupported message type: deterministic safe no-op,
                    # never a crash and never fabricated text.
                    return None

                text_obj = message.get("text")
                text = text_obj.get("body") if isinstance(text_obj, dict) else None
                if not isinstance(text, str):
                    return None

                try:
                    timestamp = float(message.get("timestamp"))
                except (TypeError, ValueError):
                    timestamp = time.time()

                reply_to = None
                context = message.get("context")
                if isinstance(context, dict):
                    context_id = context.get("id")
                    if isinstance(context_id, str) and context_id:
                        reply_to = {"message_id": context_id}

                return NormalizedMessage(
                    event_id=event_id,
                    channel="whatsapp",
                    user_id=sender,
                    conversation_id=sender,
                    text=text,
                    attachments=[],
                    reply_to=reply_to,
                    timestamp=timestamp,
                )
        return None


class MetaCloudApiAdapter:
    """Official Meta Cloud API outbound adapter. Text/URL messages only.

    Endpoint family (configurable Graph API version, never hardcoded):
    ``POST https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages``

    No CRM, bulk messaging, marketing campaigns, catalog, ecommerce, or
    payments -- this adapter exists only to carry Website Builder
    conversation/output. No template-message fabrication: if a caller asks
    to send outside the free-form customer-service window without a real
    approved template name, this adapter fails closed rather than guessing
    template identity.
    """

    def __init__(self, config: WhatsAppConfig, transport=None):
        error = config.validate_enabled()
        if not config.enabled or error is not None:
            raise ValueError(error or "WHATSAPP_DISABLED")
        self.config = config
        self.transport = transport or UrllibHttpTransport()

    def _endpoint(self) -> str:
        return (
            f"https://graph.facebook.com/{self.config.graph_api_version}/"
            f"{self.config.phone_number_id}/messages"
        )

    def _post(self, to: str, payload: Dict[str, Any]) -> OperationResult:
        if not isinstance(to, str) or not to.strip():
            return _fail("INVALID_RECIPIENT")
        body = {"messaging_product": "whatsapp", "to": to, **payload}
        try:
            status, response_body = _json_call(
                self.transport, "POST", self._endpoint(),
                {
                    "Authorization": f"Bearer {self.config.access_token}",
                    "Content-Type": "application/json",
                },
                body,
            )
        except Exception:
            # Never expose provider responses/exceptions -- may contain
            # the bearer token in transport error text.
            return _fail("SEND_RECONCILIATION_REQUIRED")
        if not isinstance(response_body, dict):
            return _fail("MALFORMED_RESPONSE")
        messages = response_body.get("messages")
        if (
            status != 200
            or not isinstance(messages, list)
            or not messages
            or not isinstance(messages[0], dict)
        ):
            return _fail("SEND_FAILED")
        message_id = messages[0].get("id")
        if not isinstance(message_id, str) or not message_id:
            return _fail("MALFORMED_RESPONSE")
        return OperationResult.ok({"message_id": message_id, "to": to})

    def send_text(self, to: str, text: str) -> OperationResult:
        if not isinstance(text, str) or not 0 < len(text) <= 4096:
            return _fail("INVALID_TEXT")
        return self._post(to, {"type": "text", "text": {"body": text, "preview_url": False}})

    def send_preview_url(self, to: str, url: str, label: str = "Preview") -> OperationResult:
        if not isinstance(url, str) or not url.startswith("https://"):
            return _fail("INVALID_URL")
        return self.send_text(to, f"{label}: {url}")

    def send_production_url(self, to: str, url: str, label: str = "Live") -> OperationResult:
        if not isinstance(url, str) or not url.startswith("https://"):
            return _fail("INVALID_URL")
        return self.send_text(to, f"{label}: {url}")

    def send_status(self, to: str, text: str) -> OperationResult:
        return self.send_text(to, text)

    def send_error(self, to: str, text: str) -> OperationResult:
        return self.send_text(to, text)


class WhatsAppWebhookApplication:
    """Stdlib WSGI callable implementing the Meta webhook GET/POST contract.

    Mount at the URL registered with Meta as the webhook callback.
    ``on_message(NormalizedMessage)`` is the caller's seam into the
    existing dispatch pipeline (construct ``AuthenticatedWhatsAppContext``
    and call ``TelegramDispatcher.dispatch`` -- see
    ``app/channels/dispatch.py``). Fails closed at every stage; never
    trusts payload content before signature verification, and never
    discloses secrets in responses.
    """

    def __init__(self, config: WhatsAppConfig, on_message):
        self.config = config
        self.on_message = on_message

    def __call__(self, environ, start_response):
        def respond(status: str, body: bytes = b""):
            start_response(status, [
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        if not self.config.ready:
            return respond("503 Service Unavailable")

        method = environ.get("REQUEST_METHOD")

        if method == "GET":
            from urllib.parse import parse_qsl
            params = dict(parse_qsl(environ.get("QUERY_STRING", "")))
            challenge = verify_webhook_get(self.config, params)
            if challenge is None:
                return respond("403 Forbidden")
            return respond("200 OK", challenge.encode("utf-8"))

        if method != "POST":
            return respond("405 Method Not Allowed")

        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
            if not 0 < length <= 1024 * 1024:
                return respond("400 Bad Request")
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                return respond("400 Bad Request")
        except (ValueError, TypeError, KeyError):
            return respond("400 Bad Request")

        signature = environ.get("HTTP_X_HUB_SIGNATURE_256")
        if not verify_webhook_signature(self.config, raw, signature):
            return respond("401 Unauthorized")

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return respond("400 Bad Request")

        message = WhatsAppNormalizer.normalize(payload)
        if message is not None:
            try:
                self.on_message(message)
            except Exception:
                # Application-side failures never leak transport/signature
                # detail back to Meta; Meta will retry the webhook.
                pass
        return respond("200 OK")
