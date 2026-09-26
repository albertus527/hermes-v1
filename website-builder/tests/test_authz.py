"""Regression coverage for the shared local ACL (app.core.authz) and the
authenticated Telegram dispatch seam (app.channels.dispatch).

All tests are local; no network/model/browser calls.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.core.authz import (
    AuthzError, ProjectAccess, require_mutating_role, require_owner_role,
    resolve_role, can_view, ROLE_OWNER, ROLE_REVIEWER, ROLE_VIEWER,
)
from app.core.intake import IntakeProcessor
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore


def _store(tmp_path):
    return ProjectStateStore(tmp_path / "state")


def _owned(store, project_id, owner="owner-1", reviewers=None, viewers=None):
    with store.acquire_writer(project_id) as state:
        state.roles["owner"] = owner
        state.roles["reviewers"] = reviewers or []
        state.roles["viewers"] = viewers or []
        store.save(state)


def test_legacy_unowned_state_has_no_owner_and_denies_mutation(tmp_path):
    store = _store(tmp_path)
    with store.acquire_writer("legacy") as state:
        # Simulate an old on-disk row written before roles existed: no
        # "roles" key at all triggers the inherit-from-owner_id path.
        state.owner_id = None
        store.save(state)
    state = store.load("legacy")
    assert state.roles == {"owner": None, "reviewers": [], "viewers": []}
    assert resolve_role(state, principal_id="anyone") is None
    with pytest.raises(AuthzError):
        require_mutating_role(state, principal_id="anyone")


def test_wrong_project_principal_rejected(tmp_path):
    store = _store(tmp_path)
    _owned(store, "proj-a", owner="owner-a")
    _owned(store, "proj-b", owner="owner-b")
    state_a = store.load("proj-a")
    assert resolve_role(state_a, principal_id="owner-b") is None
    with pytest.raises(AuthzError):
        require_mutating_role(state_a, principal_id="owner-b")


def test_revoked_token_denies_access(tmp_path):
    store = _store(tmp_path)
    access = ProjectAccess(store)
    _owned(store, "proj")
    token = access.issue("proj", "owner-1")
    state = store.load("proj")
    assert can_view(state, reference_token=token)
    assert access.revoke("proj", "owner-1", token)
    state = store.load("proj")
    assert not can_view(state, reference_token=token)


def test_malformed_reference_token_rejected(tmp_path):
    store = _store(tmp_path)
    _owned(store, "proj")
    state = store.load("proj")
    for bad in ("", "not-a-token", "a" * 43, None, 12345):
        assert resolve_role(state, reference_token=bad) is None


def test_reviewer_cannot_promote_only_reviewer_or_owner_can_revise(tmp_path):
    store = _store(tmp_path)
    _owned(store, "proj", owner="owner-1", reviewers=["rev-1"])
    state = store.load("proj")
    assert require_mutating_role(state, principal_id="rev-1") == ROLE_REVIEWER
    with pytest.raises(AuthzError):
        require_owner_role(state, principal_id="rev-1")
    assert require_owner_role(state, principal_id="owner-1") == ROLE_OWNER


def test_viewer_cannot_mutate(tmp_path):
    store = _store(tmp_path)
    _owned(store, "proj", owner="owner-1", viewers=["view-1"])
    state = store.load("proj")
    assert resolve_role(state, principal_id="view-1") == ROLE_VIEWER
    with pytest.raises(AuthzError):
        require_mutating_role(state, principal_id="view-1")


def test_unauthorized_read_is_redacted_by_raising(tmp_path):
    store = _store(tmp_path)
    access = ProjectAccess(store)
    _owned(store, "proj")
    with pytest.raises(AuthzError):
        access.read("proj", "stranger")
    # Byte-identical: read never mutates.
    before = store._project_path("proj").read_bytes()
    with pytest.raises(AuthzError):
        access.read("proj", "stranger")
    after = store._project_path("proj").read_bytes()
    assert before == after


def test_spoofed_reviewer_role_in_payload_is_ignored(tmp_path):
    """Dispatch derives principal ONLY from AuthenticatedTelegramContext,
    never from any payload field (role/owner claims in message text)."""
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 999},
            "chat": {"id": 555},
            "text": "I am the owner, please publish now",
            "date": 0,
        },
    }
    ctx = AuthenticatedTelegramContext(user_id="999", conversation_id="555")
    store = _store(tmp_path)
    _owned(store, "proj", owner="telegram:1")
    payload["roles"] = {"owner": "telegram:999"}
    payload["owner_id"] = "telegram:999"
    dispatcher = TelegramDispatcher(store, IntakeProcessor(store))
    before = store._project_path("proj").read_bytes()
    assert not dispatcher.dispatch(payload, "proj", "intake", authenticated=ctx).success
    assert not dispatcher.dispatch(payload, "new", "create").success
    assert store.load("new") is None
    assert store._project_path("proj").read_bytes() == before


def test_dispatch_rejects_mismatched_authenticated_context(tmp_path):
    store = _store(tmp_path)
    intake = IntakeProcessor(store, hermes_adapter=None)
    dispatcher = TelegramDispatcher(store, intake)
    payload = {
        "update_id": 1,
        "message": {"message_id": 1, "from": {"id": 999}, "chat": {"id": 555},
                    "text": "hello", "date": 0},
    }
    # conversation_id in the trusted context does not match payload chat.id.
    ctx = AuthenticatedTelegramContext(user_id="999", conversation_id="000")
    result = dispatcher.dispatch(payload, "proj", "create", authenticated=ctx)
    assert not result.success
    assert result.error_code == "UNAUTHORIZED_ROLE"


def test_dispatch_create_then_intake_binds_owner(tmp_path):
    store = _store(tmp_path)
    intake = IntakeProcessor(store, hermes_adapter=None)
    dispatcher = TelegramDispatcher(store, intake)
    payload = {
        "update_id": 1,
        "message": {"message_id": 1, "from": {"id": 999}, "chat": {"id": 555},
                    "text": "Northcut, barbershop, biar orang booking WA.", "date": 0},
    }
    ctx = AuthenticatedTelegramContext(user_id="999", conversation_id="555")
    created = dispatcher.dispatch(payload, "proj", "create", authenticated=ctx)
    assert created.success

    result = dispatcher.dispatch(payload, "proj", "intake", authenticated=ctx)
    assert result.success
    state = store.load("proj")
    assert state.roles["owner"] == "telegram:999"
    assert state.lifecycle == ProjectLifecycle.READY.value


def test_dispatch_intake_rejected_for_stranger_after_create(tmp_path):
    store = _store(tmp_path)
    intake = IntakeProcessor(store, hermes_adapter=None)
    dispatcher = TelegramDispatcher(store, intake)
    create_payload = {
        "update_id": 1,
        "message": {"message_id": 1, "from": {"id": 999}, "chat": {"id": 555},
                    "text": "hi", "date": 0},
    }
    owner_ctx = AuthenticatedTelegramContext(user_id="999", conversation_id="555")
    assert dispatcher.dispatch(create_payload, "proj", "create", authenticated=owner_ctx).success

    stranger_payload = {
        "update_id": 2,
        "message": {"message_id": 2, "from": {"id": 111}, "chat": {"id": 222},
                    "text": "hijack", "date": 0},
    }
    stranger_ctx = AuthenticatedTelegramContext(user_id="111", conversation_id="222")
    before = store._project_path("proj").read_bytes()
    result = dispatcher.dispatch(stranger_payload, "proj", "intake", authenticated=stranger_ctx)
    assert not result.success
    assert result.error_code == "UNAUTHORIZED_ROLE"
    after = store._project_path("proj").read_bytes()
    assert before == after


def test_dispatch_revise_reserve_apply_authorized(tmp_path):
    from unittest.mock import MagicMock, patch
    from app.core.contracts import OperationResult
    from app.projects.revise import RevisionOrchestrator
    from app.sandbox.runner import ProjectRunner

    store = _store(tmp_path)
    runner = ProjectRunner(tmp_path / "workspaces", store)
    mock_adapter = MagicMock()
    mock_adapter.frontend_build.return_value = {
        "success": True,
        "design_dna": {"version": 2, "typography": {"heading_font": "Inter", "body_font": "Inter"}},
    }
    mock_preview = MagicMock()
    mock_preview.run_owned.return_value = OperationResult.ok({})
    revise = RevisionOrchestrator(runner, store, hermes_adapter=mock_adapter, preview_orchestrator=mock_preview)

    intake = IntakeProcessor(store, hermes_adapter=None)
    dispatcher = TelegramDispatcher(store, intake, revise=revise)

    ws = tmp_path / "workspaces" / "proj"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "App.tsx").write_text("x", encoding="utf-8")
    (ws / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
    (ws / "dist").mkdir(exist_ok=True)
    (ws / "dist" / "index.html").write_text("<html>x</html>", encoding="utf-8")

    with store.acquire_writer("proj") as state:
        state.roles["owner"] = "telegram:999"
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        store.save(state)

    payload = {
        "update_id": 1,
        "message": {"message_id": 1, "from": {"id": 999}, "chat": {"id": 555},
                    "text": "make header red", "date": 0},
    }
    ctx = AuthenticatedTelegramContext(user_id="999", conversation_id="555")
    with patch(
        "app.projects.revise.QAOrchestrator",
        return_value=MagicMock(run=MagicMock(return_value=MagicMock(success=True, error=None))),
    ), patch(
        "app.projects.revise.run_fixed_checks",
        return_value={"npm_ci": {"success": True},
                      "npm_build": {"success": True},
                      "npm_typecheck": {"success": True}},
    ):
        result = dispatcher.dispatch(payload, "proj", "revise", authenticated=ctx, seq=1)
    assert result.success, result.error
    state = store.load("proj")
    assert state.revisions.revision_seq == 1


def test_unauthorized_promote_leaves_state_byte_identical_via_dispatch(tmp_path):
    from app.projects.promote import PromoteDeps, PromotionOrchestrator

    store = _store(tmp_path)
    from app.sandbox.runner import ProjectRunner
    runner = ProjectRunner(tmp_path / "workspaces", store)

    class Vercel:
        def lookup_project(self, app_id):
            return None
    deps = PromoteDeps(vercel=Vercel(), telegram=None, smoke=None, chat_id_for=lambda *a: None)
    promote = PromotionOrchestrator(runner, store, deps)
    intake = IntakeProcessor(store, hermes_adapter=None)
    # Approving now publishes, so the dispatcher needs a workspace to promote
    # into; the refusal under test is the role check, not the wiring.
    dispatcher = TelegramDispatcher(store, intake, promote=promote,
                                    workspace_for=lambda pid: tmp_path / "ws" / pid)

    with store.acquire_writer("proj") as state:
        state.roles["owner"] = "telegram:999"
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.deployment["latest_shown_preview"] = {
            "operation_id": "op-1", "source_revision": 0,
            "deployment_id": "d1", "source_sha256": "a" * 64, "artifact_sha256": "b" * 64,
        }
        store.save(state)
    before = store._project_path("proj").read_bytes()

    payload = {
        "update_id": 1,
        "message": {"message_id": 1, "from": {"id": 111}, "chat": {"id": 222},
                    "text": "publish", "date": 0},
    }
    stranger_ctx = AuthenticatedTelegramContext(user_id="111", conversation_id="222")
    result = dispatcher.dispatch(payload, "proj", "approve", authenticated=stranger_ctx)
    assert not result.success
    assert result.error_code == "UNAUTHORIZED_ROLE"
    after = store._project_path("proj").read_bytes()
    assert before == after


@pytest.mark.parametrize("field,value", [
    ("roles", None), ("roles", []), ("roles", {"owner": "owner-1"}),
    ("roles", {"owner": "owner-1", "reviewers": "attacker", "viewers": []}),
    ("reference_tokens", None), ("reference_tokens", []),
    ("reference_tokens", {"x": {"role": "viewer"}}),
    ("reference_tokens", {"a" * 64: {"role": "reviewer", "created_at": 1}}),
])
def test_malformed_acl_fails_closed_after_restart(tmp_path, field, value):
    store = _store(tmp_path)
    _owned(store, "proj")
    with store.acquire_writer("proj") as state:
        setattr(state, field, value)
        store.save(state)
    restarted = ProjectStateStore(store.root)
    before = store._project_path("proj").read_bytes()
    assert resolve_role(restarted.load("proj"), "owner-1") is None
    with pytest.raises(AuthzError):
        ProjectAccess(restarted).issue("proj", "owner-1")
    assert store._project_path("proj").read_bytes() == before


def test_legacy_owner_and_authoritative_roles(tmp_path):
    import json
    store = _store(tmp_path)
    path = store._project_path("legacy")
    path.write_text(json.dumps({"project_id": "legacy", "owner_id": "old"}))
    assert resolve_role(store.load("legacy"), "old") == ROLE_OWNER
    data = json.loads(path.read_text())
    data["roles"] = {"owner": None, "reviewers": [], "viewers": []}
    path.write_text(json.dumps(data))
    assert resolve_role(store.load("legacy"), "old") is None
    before = path.read_bytes()
    with pytest.raises(AuthzError):
        ProjectAccess(store).create("legacy", "new")
    result = IntakeProcessor(store).process(
        __import__("app.channels.telegram", fromlist=["NormalizedMessage"]).NormalizedMessage(
            event_id="e", text="pause"))
    with pytest.raises(AuthzError):
        IntakeProcessor(store).apply_to_project("legacy", result, principal_id="new")
    assert path.read_bytes() == before


def test_token_restart_cross_project_redaction_and_owner_gates(tmp_path):
    import base64
    import hashlib
    store = _store(tmp_path)
    access = ProjectAccess(store)
    _owned(store, "a", reviewers=["reviewer"], viewers=["viewer"])
    _owned(store, "b")
    token = access.issue("a", "owner-1")
    assert len(base64.urlsafe_b64decode(token + "=")) == 32
    raw = store._project_path("a").read_text()
    assert token not in raw
    assert hashlib.sha256(token.encode()).hexdigest() in raw
    access = ProjectAccess(ProjectStateStore(store.root))
    public = access.read("a", reference_token=token)
    assert set(public) == {"project_id", "lifecycle", "source_revision"}
    with pytest.raises(AuthzError):
        access.read("b", reference_token=token)
    for principal in (None, "reviewer", "viewer", "stranger"):
        with pytest.raises(AuthzError):
            access.issue("a", principal)
        with pytest.raises(AuthzError):
            access.revoke("a", principal, token)
    with pytest.raises(AuthzError):
        require_mutating_role(store.load("a"), reference_token=token)


@pytest.mark.parametrize("operation", ["revise", "promote"])
def test_revocation_wins_writer_before_effects(tmp_path, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from unittest.mock import MagicMock
    from app.projects.revise import RevisionOrchestrator
    from app.projects.promote import PromotionOrchestrator, PromoteDeps
    from app.sandbox.runner import ProjectRunner
    store = _store(tmp_path)
    _owned(store, "proj")
    with store.acquire_writer("proj") as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        store.save(state)
    runner = ProjectRunner(tmp_path / "workspaces", store)
    adapter = MagicMock()
    revise = RevisionOrchestrator(runner, store, hermes_adapter=adapter)
    promote = PromotionOrchestrator(runner, store, PromoteDeps(adapter, adapter, adapter, adapter))
    if operation == "revise":
        assert revise.reserve("proj", 1, principal_id="owner-1").success
    reached = Event()
    original = runner.acquire_project
    def acquire(pid):
        acquired = original(pid)
        reached.set()
        return acquired
    runner.acquire_project = acquire
    with ThreadPoolExecutor() as pool:
        with store.acquire_writer("proj") as state:
            call = (lambda: revise.apply("proj", 1, "change", principal_id="owner-1")) if operation == "revise" else (
                lambda: promote.promote("proj", tmp_path / "workspace", principal_id="owner-1"))
            future = pool.submit(call)
            assert reached.wait(5)
            state.roles["owner"] = "replacement"
            store.save(state)
            before = store._project_path("proj").read_bytes()
        result = future.result(timeout=5)
    assert result.error_code == "UNAUTHORIZED_ROLE"
    assert store._project_path("proj").read_bytes() == before
    assert not adapter.mock_calls
    assert not (tmp_path / "workspaces" / "proj").exists()


@pytest.mark.parametrize("principal", [None, "viewer", "stranger"])
def test_unauthorized_mutations_have_no_effects(tmp_path, principal):
    from unittest.mock import MagicMock
    from app.projects.revise import RevisionOrchestrator
    from app.projects.promote import PromotionOrchestrator, PromoteDeps
    from app.sandbox.runner import ProjectRunner
    store = _store(tmp_path)
    _owned(store, "proj", viewers=["viewer"])
    runner = ProjectRunner(tmp_path / "ws", store)
    adapter = MagicMock()
    revision = RevisionOrchestrator(runner, store, hermes_adapter=adapter)
    promotion = PromotionOrchestrator(runner, store, PromoteDeps(adapter, adapter, adapter, adapter))
    before = store._project_path("proj").read_bytes()
    assert not revision.reserve("proj", 1, principal_id=principal).success
    assert not revision.apply("proj", 1, "edit", principal_id=principal).success
    assert not promotion.approve("proj", approved_by="owner-1", principal_id=principal).success
    assert not promotion.promote("proj", tmp_path, principal_id=principal).success
    assert not promotion._promote("proj", tmp_path, principal_id=principal).success
    assert not promotion._promote_authorized("proj", tmp_path, principal_id=principal).success
    assert store._project_path("proj").read_bytes() == before
    assert not adapter.mock_calls
    assert not (tmp_path / "ws" / "proj").exists()


def test_reviewer_approval_audit_ignores_claimed_owner(tmp_path):
    from unittest.mock import MagicMock
    from app.projects.promote import PromotionOrchestrator, PromoteDeps
    from app.sandbox.runner import ProjectRunner
    store = _store(tmp_path)
    _owned(store, "proj", reviewers=["reviewer"])
    with store.acquire_writer("proj") as state:
        state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
        state.revisions.source_revision = state.revisions.qa_revision = state.revisions.preview_revision = 1
        state.deployment["latest_shown_preview"] = {
            "operation_id": "op", "source_revision": 1, "deployment_id": "dep",
            "source_sha256": "a" * 64, "artifact_sha256": "b" * 64,
        }
        store.save(state)
    adapter = MagicMock()
    promotion = PromotionOrchestrator(ProjectRunner(tmp_path / "ws", store), store,
                                     PromoteDeps(adapter, adapter, adapter, adapter))
    assert promotion.approve("proj", approved_by="owner-1", principal_id="reviewer").success
    assert store.load("proj").deployment["approval"]["approved_by"] == "reviewer"
    assert not promotion.promote("proj", tmp_path, principal_id="reviewer").success
    assert not adapter.mock_calls


def _dispatch_payload(event=50, user=1):
    return {"update_id": event, "message": {"message_id": event,
            "from": {"id": user}, "chat": {"id": 555}, "text": "content", "date": 0},
            "principal_id": "telegram:1", "reference_token": "forged", "ownership_claim": True}


@pytest.mark.parametrize("action,method", [
    ("reference_upload", "add_upload"), ("reference_url", "add_url"),
    ("directions_propose", "propose"), ("directions_choose", "choose_direction"),
    ("domain_connect", "connect"),
])
def test_new_dispatch_actions_authenticated_and_deduplicated(tmp_path, action, method):
    from unittest.mock import MagicMock
    from app.core.contracts import OperationResult
    store = _store(tmp_path)
    _owned(store, "proj", owner="telegram:1")
    collaborator = MagicMock()
    getattr(collaborator, method).return_value = OperationResult.ok()
    kwargs = dict(reference_intake=collaborator, directions=collaborator, domain=collaborator,
                  workspace_for=lambda pid: tmp_path / pid)
    dispatcher = TelegramDispatcher(store, None, **kwargs)
    args = dict(data=b"image", role="UX", url="https://example.org/image.png", brief={"name": "N"},
                index=0, hostname="example.org", ownership_claim=True)
    ctx = AuthenticatedTelegramContext("1", "555")
    assert dispatcher.dispatch(_dispatch_payload(), "proj", action, authenticated=ctx, **args).success
    call = getattr(collaborator, method)
    assert call.call_args.kwargs["principal_id"] == "telegram:1"
    assert call.call_args.kwargs["reference_token"] is None
    restarted = TelegramDispatcher(ProjectStateStore(store.root), None, **kwargs)
    duplicate = restarted.dispatch(_dispatch_payload(), "proj", action, authenticated=ctx, **args)
    assert duplicate.success and duplicate.data["duplicate"]
    assert call.call_count == 1
    denied = dispatcher.dispatch(_dispatch_payload(51, 2), "proj", action,
                                 authenticated=AuthenticatedTelegramContext("2", "555"), **args)
    assert denied.error_code == "UNAUTHORIZED_ROLE"
    assert call.call_count == 1
    assert TelegramDispatcher(store, None).dispatch(
        _dispatch_payload(52), "proj", action, authenticated=ctx, **args).error_code == "UNSUPPORTED_ACTION"


@pytest.mark.parametrize("claim", [None, False, 1, "true"])
def test_domain_dispatch_requires_explicit_boolean(tmp_path, claim):
    from unittest.mock import MagicMock
    store = _store(tmp_path)
    _owned(store, "proj", owner="telegram:1")
    domain = MagicMock()
    dispatcher = TelegramDispatcher(store, None, domain=domain, workspace_for=lambda pid: tmp_path)
    result = dispatcher.dispatch(_dispatch_payload(), "proj", "domain_connect",
                                 authenticated=AuthenticatedTelegramContext("1", "555"),
                                 hostname="example.org", ownership_claim=claim)
    assert result.error_code == "OWNERSHIP_CLAIM_REQUIRED"
    domain.connect.assert_not_called()


def test_failed_dispatch_event_requires_reconciliation(tmp_path):
    from unittest.mock import MagicMock
    from app.core.contracts import OperationResult
    store = _store(tmp_path)
    _owned(store, "proj", owner="telegram:1")
    refs = MagicMock()
    refs.add_upload.side_effect = [OperationResult.fail("temporary"), OperationResult.ok()]
    dispatcher = TelegramDispatcher(store, None, reference_intake=refs)
    args = dict(authenticated=AuthenticatedTelegramContext("1", "555"), data=b"image", role="UX")
    assert not dispatcher.dispatch(_dispatch_payload(), "proj", "reference_upload", **args).success
    restarted = TelegramDispatcher(ProjectStateStore(store.root), None, reference_intake=refs)
    assert restarted.dispatch(_dispatch_payload(), "proj", "reference_upload", **args).error_code == "EVENT_RECONCILIATION_REQUIRED"
    assert refs.add_upload.call_count == 1


@pytest.mark.parametrize("action", ["reference_upload", "reference_url", "directions_propose", "directions_choose"])
@pytest.mark.parametrize("lifecycle,revision", [("READY", 1)])
def test_prebuild_mutations_fail_closed(tmp_path, action, lifecycle, revision):
    from unittest.mock import MagicMock
    from app.projects.references import ReferenceIntake
    from app.projects.directions import DirectionsOrchestrator
    store = _store(tmp_path)
    _owned(store, "proj", owner="telegram:1")
    with store.acquire_writer("proj") as state:
        state.lifecycle = lifecycle
        state.revisions.source_revision = revision
        store.save(state)
    adapter = MagicMock()
    dispatcher = TelegramDispatcher(store, None, reference_intake=ReferenceIntake(store, adapter),
        directions=DirectionsOrchestrator(store, adapter), workspace_for=lambda pid: tmp_path)
    before = store._project_path("proj").read_bytes()
    result = dispatcher.dispatch(_dispatch_payload(), "proj", action,
        authenticated=AuthenticatedTelegramContext("1", "555"), data=b"image", role="UX",
        url="https://example.org/image.png", brief={}, index=0)
    prefix = "REFERENCE" if action.startswith("reference") else "DIRECTIONS"
    assert result.error_code == prefix + "_NOT_ALLOWED_IN_LIFECYCLE"
    assert store._project_path("proj").read_bytes() == before
    assert not adapter.mock_calls


def test_build_dispatch_blocks_pending_then_builds_selected_once(tmp_path):
    from unittest.mock import MagicMock
    from app.core.contracts import OperationResult
    from app.projects.directions import DirectionsOrchestrator
    store = _store(tmp_path)
    _owned(store, "proj", owner="telegram:1")
    with store.acquire_writer("proj") as state:
        state.lifecycle = "READY"
        state.brief = {"name": "Persisted"}
        state.design_directions = [{"label": "A"}, {"label": "B"}]
        store.save(state)
    builder = MagicMock()
    builder.build.return_value = OperationResult.ok()
    dispatcher = TelegramDispatcher(store, None, builder=builder, directions=DirectionsOrchestrator(store))
    ctx = AuthenticatedTelegramContext("1", "555")
    assert dispatcher.dispatch(_dispatch_payload(), "proj", "build", authenticated=ctx).error_code == "DIRECTION_CHOICE_PENDING"
    builder.build.assert_not_called()
    assert store.load("proj").lifecycle == "READY"
    assert dispatcher.dispatch(_dispatch_payload(51), "proj", "directions_choose", authenticated=ctx, index=1).success
    assert dispatcher.dispatch(_dispatch_payload(), "proj", "build", authenticated=ctx, brief={"name": "forged"}).success
    # H-6: the explicit build path now receives the SAME remote-boundary
    # callback the auto-build path uses (durable reached_remote=True before
    # the first possible Vercel effect); the persisted brief is authoritative.
    builder.build.assert_called_once()
    call_args = builder.build.call_args
    assert call_args.args == ("proj", {"name": "Persisted"})
    assert "on_remote_boundary" in call_args.kwargs
    assert callable(call_args.kwargs["on_remote_boundary"])
    assert store.load("proj").lifecycle == "QUEUED"
    assert dispatcher.dispatch(_dispatch_payload(), "proj", "build", authenticated=ctx).data["duplicate"]


if __name__ == "__main__":
    import unittest
    unittest.main()
