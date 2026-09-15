"""Verified Web3Forms enrollment, server-side WSGI submission, frontend policy.

Enrollment is an explicit operator attestation of destination verification, not
proof inferred from possession of an API key. Secrets stay in the server process.
Mount ContactFormApplication behind the enrolled endpoint; no generated backend
or build-time secret substitution is required.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import quote, urlsplit

from app.core.authz import require_mutating_role
from app.core.contracts import OperationResult
from app.deploy.adapters import UrllibHttpTransport, _json_call

WEB3FORMS_ENDPOINT = "https://api.web3forms.com/submit"
MAX_BODY_BYTES = 16384


def _fail(code: str) -> OperationResult:
    return OperationResult.fail(code, error_code=code, retryable=False)


class Web3FormsAdapter:
    """Real provider call; an HTTP 200 alone never constitutes success."""

    def __init__(self, access_key: str, transport=None):
        if not isinstance(access_key, str) or not access_key.strip():
            raise ValueError("access_key required")
        self.access_key = access_key
        self.transport = transport or UrllibHttpTransport()

    def submit(self, fields: Dict[str, str]) -> OperationResult:
        if not isinstance(fields, dict) or not fields or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in fields.items()
        ):
            return _fail("INVALID_FORM_FIELDS")
        payload = dict(fields)
        payload["access_key"] = self.access_key
        payload.setdefault("subject", "New contact form submission")
        try:
            status, body = _json_call(
                self.transport, "POST", WEB3FORMS_ENDPOINT,
                {"Content-Type": "application/json", "Accept": "application/json"}, payload,
            )
        except Exception:
            return _fail("SUBMISSION_RECONCILIATION_REQUIRED")
        if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
            return _fail("MALFORMED_RESPONSE")
        if status != 200 or body["success"] is not True:
            return _fail("SUBMISSION_REJECTED")
        return OperationResult.ok({"message": body.get("message", "")})


def _endpoint_valid(endpoint):
    try:
        if not isinstance(endpoint, str) or any(ord(c) <= 32 for c in endpoint):
            return False
        parts = urlsplit(endpoint)
        return bool(parts.hostname and parts.path and parts.path != "/"
                    and not parts.username and not parts.password
                    and not parts.query and not parts.fragment and "\\" not in endpoint
                    and "{" not in endpoint and "}" not in endpoint
                    and parts.port != 0
                    and (parts.scheme == "https" or
                         (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))))
    except ValueError:
        return False


def _key_digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def enroll_contact_destination(store, project_id, *, principal_id, destination,
                               verification_evidence, endpoint, adapter):
    """Persist an authorized operator's receipt of destination verification.

    The caller must verify the provider account's recipient out of band, then
    supply its receipt/evidence identifier. Merely configuring a key never enrolls
    a destination. This function does not send a verification email or claim to.
    """
    if (not isinstance(adapter, Web3FormsAdapter) or not _endpoint_valid(endpoint)
            or not fallback_contact_link(destination)
            or not isinstance(verification_evidence, str)
            or not verification_evidence.strip() or len(verification_evidence) > 2000):
        raise ValueError("INVALID_CONTACT_ENROLLMENT")
    with store.acquire_writer(project_id) as state:
        require_mutating_role(state, principal_id)
        state.deployment["contact"] = {
            "provider": "web3forms", "destination": destination,
            "verification_evidence": verification_evidence.strip(),
            "verified_by": principal_id, "endpoint": endpoint,
            "key_sha256": _key_digest(adapter.access_key),
        }
        state.revisions.approved_revision = 0
        state.revisions.qa_revision = 0
        state.revisions.preview_revision = 0
        for key in ("approval", "qa", "checked", "tested_snapshot", "latest_shown_preview"):
            state.deployment.pop(key, None)
        store.save(state)


def _enrollment_valid(enrollment, access_key):
    return bool(isinstance(enrollment, dict)
                and isinstance(access_key, str) and access_key.strip()
                and enrollment.get("provider") == "web3forms"
                and _endpoint_valid(enrollment.get("endpoint"))
                and fallback_contact_link(enrollment.get("destination"))
                and isinstance(enrollment.get("verification_evidence"), str)
                and enrollment["verification_evidence"].strip()
                and enrollment.get("verified_by")
                and enrollment.get("key_sha256") == _key_digest(access_key))


@dataclass(frozen=True)
class ContactMethodDecision:
    method: str
    endpoint: Optional[str] = None
    fallback_link: Optional[str] = None
    fallback_kind: Optional[str] = None


def fallback_contact_link(why_destination: Optional[str]) -> Optional[Dict[str, str]]:
    """Only derive links from explicit contact details; never invent a recipient."""
    if not isinstance(why_destination, str) or not why_destination.strip():
        return None
    dest = why_destination.strip()
    if any(ord(c) < 32 for c in dest):
        return None
    if re.fullmatch(r"\+?\d{10,15}", dest):
        return {"kind": "whatsapp", "link": f"https://wa.me/{dest.lstrip('+')}"}
    if re.fullmatch(r"[^\s@:/?#]+@[^\s@:/?#]+\.[^\s@:/?#]+", dest):
        return {"kind": "email", "link": f"mailto:{quote(dest)}"}
    try:
        parts = urlsplit(dest)
        if (parts.scheme == "https" and parts.hostname and not parts.username
                and not parts.password and not re.search(r"\s|\\", dest)):
            kind = "whatsapp" if parts.hostname.lower() in ("wa.me", "whatsapp.com", "www.whatsapp.com") else "link"
            return {"kind": kind, "link": dest}
    except ValueError:
        pass
    return None


def decide_contact_method(access_key, why_destination, *, enrollment=None):
    if _enrollment_valid(enrollment, access_key):
        return ContactMethodDecision("web3forms", endpoint=enrollment["endpoint"])
    fallback = fallback_contact_link(why_destination)
    return ContactMethodDecision("fallback_link", fallback_link=fallback["link"] if fallback else None,
                                 fallback_kind=fallback["kind"] if fallback else None)


def compose_contact_form_instructions(decision):
    if decision.method == "web3forms" and _endpoint_valid(decision.endpoint):
        return f'''CONTACT FORM (Phase 14 -- server-side Web3Forms):
POST JSON containing only name, email, message to {decision.endpoint}.
The endpoint is configured by the operator. Never send directly to Web3Forms.
Never embed an access key, secret, or build-time placeholder in frontend source.
Show success ONLY after HTTP 200 and the actual response body reports "success": true.
Show clear failure for non-200, "success": false, malformed JSON, or network failure.
Do NOT automatically retry ambiguous submissions. Do NOT fake submission behavior.'''
    if decision.fallback_link:
        return f'''CONTACT FORM (Phase 14 -- no verified form backend configured):
Do NOT build a contact form: fake submission behavior is forbidden.
Provide a working {decision.fallback_kind or 'contact'} link to the explicit destination:
{decision.fallback_link}'''
    return '''CONTACT FORM (Phase 14 -- no verified destination):
Do NOT add any contact form or contact link. No verified submission backend or
explicit fallback contact detail exists. Never fabricate a destination or success.'''


class ContactFormApplication:
    """Stdlib WSGI callable. Mount on the enrolled path; inject server credentials.

    Enrollment is reloaded per request (revocation/key rotation fail closed).
    allowed_origins must name the actual site origins; browser cross-origin POST
    and preflight are supported. No request controls the destination/provider key.
    """

    def __init__(self, store, project_id, adapter, *, allowed_origins=()):
        if not isinstance(adapter, Web3FormsAdapter):
            raise ValueError("Web3FormsAdapter required")
        self.store, self.project_id, self.adapter = store, project_id, adapter
        self.allowed_origins = frozenset(allowed_origins)

    def __call__(self, environ, start_response):
        headers = [("Content-Type", "application/json"), ("Cache-Control", "no-store")]

        def respond(status, success=False, error=None):
            body = json.dumps({"success": success, "error": error}).encode()
            start_response(status, headers + [("Content-Length", str(len(body)))])
            return [body]

        state = self.store.load(self.project_id)
        enrollment = state.deployment.get("contact") if state else None
        if not _enrollment_valid(enrollment, self.adapter.access_key):
            return respond("503 Service Unavailable", error="CONTACT_UNAVAILABLE")
        path = environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")
        if path != urlsplit(enrollment["endpoint"]).path:
            return respond("404 Not Found", error="NOT_FOUND")
        origin = environ.get("HTTP_ORIGIN")
        if origin and origin not in self.allowed_origins:
            return respond("403 Forbidden", error="ORIGIN_REJECTED")
        if origin:
            headers.extend([("Access-Control-Allow-Origin", origin), ("Vary", "Origin")])
        method = environ.get("REQUEST_METHOD")
        if method == "OPTIONS":
            headers.extend([("Access-Control-Allow-Methods", "POST"),
                            ("Access-Control-Allow-Headers", "Content-Type")])
            return respond("200 OK")
        if method != "POST":
            headers.append(("Allow", "POST, OPTIONS"))
            return respond("405 Method Not Allowed", error="METHOD_NOT_ALLOWED")
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            return respond("415 Unsupported Media Type", error="JSON_REQUIRED")
        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
            if not 0 < length <= MAX_BODY_BYTES:
                return respond("413 Payload Too Large", error="INVALID_BODY_SIZE")
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                raise ValueError("Truncated body")
            fields = json.loads(raw)
            if (not isinstance(fields, dict) or set(fields) != {"name", "email", "message"}
                    or any(not isinstance(v, str) or not v.strip() for v in fields.values())
                    or len(fields["name"]) > 200 or len(fields["message"]) > 8000
                    or len(fields["email"]) > 254
                    or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", fields["email"])):
                raise ValueError("Invalid fields")
        except (ValueError, TypeError, KeyError, UnicodeError):
            return respond("400 Bad Request", error="INVALID_FORM_FIELDS")
        result = self.adapter.submit(fields)
        if not result.success:
            return respond("502 Bad Gateway", error=result.error_code)
        # Never expose provider response text: it can echo submitted secrets/PII.
        return respond("200 OK", success=True)
