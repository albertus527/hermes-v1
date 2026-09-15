"""Local behavioral tests for Phase 14 contact-form integration.

No live HTTP calls. Real stdlib WSGI application invoked in-process.
"""
import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.authz import AuthzError
from app.core.state import ProjectStateStore
from app.deploy.adapters import HttpResponse
from app.core.contact_form import (
    ContactFormApplication,
    ContactMethodDecision,
    Web3FormsAdapter,
    compose_contact_form_instructions,
    decide_contact_method,
    enroll_contact_destination,
    fallback_contact_link,
)


class Transport:
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


# ---------------------------------------------------------------------------
# Web3FormsAdapter.submit()
# ---------------------------------------------------------------------------


def test_web3forms_real_success_shape():
    t = Transport((200, {"success": True, "message": "Submitted"}))
    a = Web3FormsAdapter("real_access_key", transport=t)
    result = a.submit({"name": "Jane", "email": "jane@example.com"})
    assert result.success
    assert result.data["message"] == "Submitted"
    payload = json.loads(t.calls[0][2]["data"])
    assert payload["access_key"] == "real_access_key"
    assert payload["name"] == "Jane"


def test_web3forms_real_failure_shape():
    t = Transport((200, {"success": False, "message": "Invalid access key"}))
    a = Web3FormsAdapter("bad_key", transport=t)
    result = a.submit({"name": "Jane"})
    assert not result.success
    assert result.error_code == "SUBMISSION_REJECTED"


def test_web3forms_non_200_with_success_true_still_rejected():
    t = Transport((500, {"success": True, "message": "weird"}))
    a = Web3FormsAdapter("key", transport=t)
    result = a.submit({"name": "Jane"})
    assert not result.success
    assert result.error_code == "SUBMISSION_REJECTED"


@pytest.mark.parametrize("body", [{}, {"message": "no success field"}, {"success": "true"}, []])
def test_web3forms_malformed_response_fails_closed(body):
    t = Transport((200, body))
    a = Web3FormsAdapter("key", transport=t)
    result = a.submit({"name": "Jane"})
    assert not result.success
    assert result.error_code in ("MALFORMED_RESPONSE", "SUBMISSION_RECONCILIATION_REQUIRED")


def test_web3forms_transport_exception_fails_closed():
    t = Transport(TimeoutError("boom"))
    a = Web3FormsAdapter("key", transport=t)
    result = a.submit({"name": "Jane"})
    assert not result.success
    assert result.error_code == "SUBMISSION_RECONCILIATION_REQUIRED"


def test_web3forms_access_key_never_caller_overridable():
    t = Transport((200, {"success": True}))
    a = Web3FormsAdapter("real_key", transport=t)
    a.submit({"access_key": "attacker_supplied"})
    payload = json.loads(t.calls[0][2]["data"])
    assert payload["access_key"] == "real_key"


def test_web3forms_requires_nonempty_access_key():
    with pytest.raises(ValueError):
        Web3FormsAdapter("")


@pytest.mark.parametrize("fields", [{}, None, {"x": 1}, {1: "x"}])
def test_web3forms_invalid_fields_rejected(fields):
    a = Web3FormsAdapter("key", transport=Transport())
    result = a.submit(fields)
    assert not result.success
    assert result.error_code == "INVALID_FORM_FIELDS"


# ---------------------------------------------------------------------------
# fallback_contact_link()
# ---------------------------------------------------------------------------


def test_fallback_link_from_phone_number():
    link = fallback_contact_link("+6281234567890")
    assert link == {"kind": "whatsapp", "link": "https://wa.me/6281234567890"}


def test_fallback_link_from_email():
    link = fallback_contact_link("jane@example.com")
    assert link["kind"] == "email"
    assert link["link"] == "mailto:jane%40example.com"


def test_fallback_link_from_generic_https_url():
    link = fallback_contact_link("https://example.com/contact")
    assert link == {"kind": "link", "link": "https://example.com/contact"}


def test_fallback_link_from_wa_url():
    link = fallback_contact_link("https://wa.me/6281234567890")
    assert link == {"kind": "whatsapp", "link": "https://wa.me/6281234567890"}


@pytest.mark.parametrize("dest", [None, "", "   ", "not a destination", "just some text"])
def test_fallback_link_none_when_no_verified_destination(dest):
    assert fallback_contact_link(dest) is None


# ---------------------------------------------------------------------------
# decide_contact_method(): enrollment is required, a key alone is not enough
# ---------------------------------------------------------------------------


def _enrollment_for(adapter, endpoint="https://example.com/api/contact"):
    import hashlib
    return {
        "provider": "web3forms",
        "destination": "+6281234567890",
        "verification_evidence": "confirmed via Web3Forms dashboard email receipt #123",
        "verified_by": "owner-1",
        "endpoint": endpoint,
        "key_sha256": hashlib.sha256(adapter.access_key.encode()).hexdigest(),
    }


def test_decide_contact_method_requires_enrollment_not_just_key():
    """A configured key WITHOUT an enrollment record must not select web3forms."""
    decision = decide_contact_method("real_key", "+6281234567890", enrollment=None)
    assert decision.method == "fallback_link"


def test_decide_contact_method_uses_web3forms_when_enrolled():
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    enrollment = _enrollment_for(adapter)
    decision = decide_contact_method("real_key", "+6281234567890", enrollment=enrollment)
    assert decision.method == "web3forms"
    assert decision.endpoint == enrollment["endpoint"]


def test_decide_contact_method_enrollment_key_mismatch_falls_back():
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    enrollment = _enrollment_for(adapter)
    decision = decide_contact_method("different_key", "+6281234567890", enrollment=enrollment)
    assert decision.method == "fallback_link"


def test_decide_contact_method_falls_back_without_key():
    decision = decide_contact_method(None, "+6281234567890")
    assert decision.method == "fallback_link"
    assert decision.fallback_link == "https://wa.me/6281234567890"
    assert decision.fallback_kind == "whatsapp"


def test_decide_contact_method_no_key_no_destination():
    decision = decide_contact_method(None, None)
    assert decision.method == "fallback_link"
    assert decision.fallback_link is None


def test_decide_contact_method_blank_key_treated_as_absent():
    decision = decide_contact_method("   ", "jane@example.com")
    assert decision.method == "fallback_link"
    assert decision.fallback_kind == "email"


# ---------------------------------------------------------------------------
# enroll_contact_destination(): explicit verification receipt required
# ---------------------------------------------------------------------------


def _project_with_owner(tmp_path, project_id="proj-enroll"):
    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer(project_id) as state:
        state.roles["owner"] = "owner-1"
        store.save(state)
    return store


def test_enroll_requires_owner_or_reviewer(tmp_path):
    store = _project_with_owner(tmp_path)
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    with pytest.raises(AuthzError):
        enroll_contact_destination(
            store, "proj-enroll", principal_id="stranger", destination="+6281234567890",
            verification_evidence="receipt #1", endpoint="https://example.com/api/contact",
            adapter=adapter,
        )


def test_enroll_rejects_missing_evidence(tmp_path):
    store = _project_with_owner(tmp_path)
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    with pytest.raises(ValueError):
        enroll_contact_destination(
            store, "proj-enroll", principal_id="owner-1", destination="+6281234567890",
            verification_evidence="   ", endpoint="https://example.com/api/contact",
            adapter=adapter,
        )


def test_enroll_rejects_invalid_endpoint(tmp_path):
    store = _project_with_owner(tmp_path)
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    with pytest.raises(ValueError):
        enroll_contact_destination(
            store, "proj-enroll", principal_id="owner-1", destination="+6281234567890",
            verification_evidence="receipt #1", endpoint="not a url",
            adapter=adapter,
        )


def test_enroll_persists_and_selects_web3forms(tmp_path):
    store = _project_with_owner(tmp_path)
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    enroll_contact_destination(
        store, "proj-enroll", principal_id="owner-1", destination="+6281234567890",
        verification_evidence="confirmed via provider dashboard", endpoint="https://example.com/api/contact",
        adapter=adapter,
    )
    state = store.load("proj-enroll")
    decision = decide_contact_method("real_key", "+6281234567890", enrollment=state.deployment.get("contact"))
    assert decision.method == "web3forms"


# ---------------------------------------------------------------------------
# compose_contact_form_instructions() -- never fabricates success, never
# emits any secret/placeholder token for FRONTEND to embed
# ---------------------------------------------------------------------------


def test_compose_instructions_web3forms_never_embeds_secret():
    decision = ContactMethodDecision(method="web3forms", endpoint="https://example.com/api/contact")
    text = compose_contact_form_instructions(decision)
    assert "https://example.com/api/contact" in text
    assert "access key" in text.lower() or "secret" in text.lower()
    assert "real_key" not in text


def test_compose_instructions_fallback_link_forbids_fake_form():
    decision = ContactMethodDecision(
        method="fallback_link", fallback_link="https://wa.me/6281234567890", fallback_kind="whatsapp",
    )
    text = compose_contact_form_instructions(decision)
    assert "https://wa.me/6281234567890" in text
    assert "fake" in text.lower()


def test_compose_instructions_no_method_forbids_any_form():
    decision = ContactMethodDecision(method="fallback_link", fallback_link=None)
    text = compose_contact_form_instructions(decision)
    assert "Do NOT add any contact form" in text


# ---------------------------------------------------------------------------
# ContactFormApplication: real stdlib WSGI application, in-process
# ---------------------------------------------------------------------------


def _wsgi_call(app, method, path, body=b"", content_type="application/json", origin=None):
    environ = {
        "REQUEST_METHOD": method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    result = b"".join(app(environ, start_response))
    return captured["status"], dict(captured["headers"]), json.loads(result)


def test_wsgi_app_rejects_when_not_enrolled(tmp_path):
    store = _project_with_owner(tmp_path)
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    app = ContactFormApplication(store, "proj-enroll", adapter, allowed_origins=("https://site.example",))
    status, _, body = _wsgi_call(app, "POST", "/api/contact",
                                 json.dumps({"name": "A", "email": "a@example.com", "message": "hi"}).encode())
    assert status.startswith("503")
    assert body["success"] is False


def test_wsgi_app_submits_and_reports_success(tmp_path):
    store = _project_with_owner(tmp_path)
    transport = Transport((200, {"success": True, "message": "ok"}))
    adapter = Web3FormsAdapter("real_key", transport=transport)
    enroll_contact_destination(
        store, "proj-enroll", principal_id="owner-1", destination="+6281234567890",
        verification_evidence="confirmed via provider dashboard", endpoint="https://example.com/api/contact",
        adapter=adapter,
    )
    app = ContactFormApplication(store, "proj-enroll", adapter, allowed_origins=("https://site.example",))
    status, _, body = _wsgi_call(
        app, "POST", "/api/contact",
        json.dumps({"name": "A", "email": "a@example.com", "message": "hi"}).encode(),
        origin="https://site.example",
    )
    assert status.startswith("200")
    assert body["success"] is True


def test_wsgi_app_reports_failure_never_fakes_success(tmp_path):
    store = _project_with_owner(tmp_path)
    transport = Transport((200, {"success": False, "message": "bad"}))
    adapter = Web3FormsAdapter("real_key", transport=transport)
    enroll_contact_destination(
        store, "proj-enroll", principal_id="owner-1", destination="+6281234567890",
        verification_evidence="confirmed via provider dashboard", endpoint="https://example.com/api/contact",
        adapter=adapter,
    )
    app = ContactFormApplication(store, "proj-enroll", adapter)
    status, _, body = _wsgi_call(
        app, "POST", "/api/contact",
        json.dumps({"name": "A", "email": "a@example.com", "message": "hi"}).encode(),
    )
    assert status.startswith("502")
    assert body["success"] is False


def test_wsgi_app_rejects_disallowed_origin(tmp_path):
    store = _project_with_owner(tmp_path)
    adapter = Web3FormsAdapter("real_key", transport=Transport((200, {"success": True})))
    enroll_contact_destination(
        store, "proj-enroll", principal_id="owner-1", destination="+6281234567890",
        verification_evidence="confirmed via provider dashboard", endpoint="https://example.com/api/contact",
        adapter=adapter,
    )
    app = ContactFormApplication(store, "proj-enroll", adapter, allowed_origins=("https://good.example",))
    status, _, body = _wsgi_call(
        app, "POST", "/api/contact",
        json.dumps({"name": "A", "email": "a@example.com", "message": "hi"}).encode(),
        origin="https://evil.example",
    )
    assert status.startswith("403")


def test_wsgi_app_rejects_invalid_fields(tmp_path):
    store = _project_with_owner(tmp_path)
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    enroll_contact_destination(
        store, "proj-enroll", principal_id="owner-1", destination="+6281234567890",
        verification_evidence="confirmed via provider dashboard", endpoint="https://example.com/api/contact",
        adapter=adapter,
    )
    app = ContactFormApplication(store, "proj-enroll", adapter)
    status, _, body = _wsgi_call(app, "POST", "/api/contact", json.dumps({"name": "A"}).encode())
    assert status.startswith("400")


def test_wsgi_app_rejects_non_post(tmp_path):
    store = ProjectStateStore(tmp_path / "state")
    adapter = Web3FormsAdapter("real_key", transport=Transport())
    app = ContactFormApplication(store, "proj-enroll", adapter)
    status, _, _ = _wsgi_call(app, "GET", "/api/contact")
    assert status.startswith("405") or status.startswith("503")


# ---------------------------------------------------------------------------
# Build instruction composition (mirrors test_build.py conventions)
# ---------------------------------------------------------------------------


def _prepare_build_fixture(tmp_path, project_id, web3forms_access_key, enrolled=False):
    from app.core.lifecycle import ProjectLifecycle
    from app.projects.build import FrontendBuilder
    from app.sandbox.runner import ProjectRunner

    store = ProjectStateStore(tmp_path / "state")
    with store.acquire_writer(project_id) as state:
        state.roles["owner"] = "owner-1"
        state.brief = {"name": "N", "what": "W", "why": "Y"}
        state.domain.deferred = True
        store.save(state)
    store.transition_lifecycle(project_id, ProjectLifecycle.READY)
    store.transition_lifecycle(project_id, ProjectLifecycle.QUEUED)

    if enrolled and web3forms_access_key:
        adapter = Web3FormsAdapter(web3forms_access_key, transport=Transport())
        enroll_contact_destination(
            store, project_id, principal_id="owner-1", destination="+6281234567890",
            verification_evidence="confirmed via provider dashboard", endpoint="https://example.com/api/contact",
            adapter=adapter,
        )

    starter = tmp_path / "starter"
    starter.mkdir()
    (starter / "index.html").write_text("<html></html>")
    (starter / "dist").mkdir()
    (starter / "dist" / "index.html").write_text("<html>built</html>")

    runner = ProjectRunner(tmp_path / "workspaces", store)

    hermes_adapter = Mock()
    hermes_adapter.frontend_build.return_value = {
        "success": True,
        "design_dna": {"version": 1},
    }

    def fake_run_command(*args, **kwargs):
        return Mock(returncode=0, stdout="", stderr="")

    runner.run_command = fake_run_command

    builder = FrontendBuilder(
        runner, store, hermes_adapter=hermes_adapter, starter_path=starter,
        web3forms_access_key=web3forms_access_key,
    )
    return builder, hermes_adapter, store


def test_build_composes_web3forms_instructions_when_enrolled(tmp_path):
    builder, hermes_adapter, store = _prepare_build_fixture(
        tmp_path, "proj-contact-1", "real_access_key", enrolled=True
    )
    with store.acquire_writer("proj-contact-1") as state:
        state.brief["why_destination"] = "+6281234567890"
        store.save(state)
    builder.build("proj-contact-1", brief={"name": "N", "what": "W", "why": "Y"})

    call_kwargs = hermes_adapter.frontend_build.call_args.kwargs
    instructions = call_kwargs.get("design_dna_instructions") or ""
    assert "https://example.com/api/contact" in instructions
    assert "real_access_key" not in instructions


def test_build_composes_fallback_link_instructions_without_enrollment(tmp_path):
    builder, hermes_adapter, store = _prepare_build_fixture(
        tmp_path, "proj-contact-2", None
    )
    with store.acquire_writer("proj-contact-2") as state:
        state.brief["why_destination"] = "+6281234567890"
        store.save(state)
    builder.build("proj-contact-2", brief={"name": "N", "what": "W", "why": "Y"})

    call_kwargs = hermes_adapter.frontend_build.call_args.kwargs
    instructions = call_kwargs.get("design_dna_instructions") or ""
    assert "https://wa.me/6281234567890" in instructions
