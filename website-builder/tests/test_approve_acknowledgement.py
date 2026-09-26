"""BUG 3 regression: a successful approval is acknowledged, exactly once.

The p9 E2E approved a preview and the bot went silent: the approval binding
succeeded (approved_revision advanced, every identity field matched) and
``_handle_approve`` only surfaced errors, so a successful approve produced no
user-visible outcome at all and read as "still thinking".

The acknowledgement must be:

  * sent on success, naming the next step ("publish"), and publishing
    nothing itself,
  * at-most-once per approved identity -- a replayed Telegram update AND a
    brand-new update that re-approves the SAME preview both stay silent,
  * still allowed for a genuinely NEW approved identity (a newly shown
    preview), because that is a different thing the user did,
  * never sent on a failed approve, which keeps its existing error reply,
  * fail-closed on an ambiguous send: the durable outcome stays PENDING so a
    possibly-delivered acknowledgement is never repeated.

Exercised through the REAL TelegramReceiveLoop -> TelegramDispatcher ->
PromotionOrchestrator -> ProjectStateStore chain. Only the Telegram transport
is faked; the Vercel/deploy/smoke boundaries use the real test doubles.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.channels.dispatch import TelegramDispatcher  # noqa: E402
from app.core.authz import ProjectAccess  # noqa: E402
from app.core.contracts import OperationResult  # noqa: E402
from app.core.intake import IntakeProcessor  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.projects.promote import PromotionOrchestrator  # noqa: E402
from app.runtime import TelegramReceiveLoop  # noqa: E402
from test_promote import (  # noqa: E402
    OWNER,
    FakeSmoke,
    FakeTelegram,
    FakeVercel,
    _approved_state,
    _make_workspace,
)

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
CHAT = "555"
PROJECT_ID = "tg-555"
ACK_MARKER = "Preview approved"


def _payload(event_id, text, user="1", chat=CHAT):
    return {
        "update_id": event_id,
        "message": {
            "from": {"id": int(user)},
            "chat": {"id": int(chat)},
            "text": text,
            "date": event_id,
        },
    }


class RecordingTelegram(FakeTelegram):
    """Telegram boundary that records text sends and can fail on demand."""

    def __init__(self):
        super().__init__()
        self.raise_on_text = None
        self.reject_text = False
        self.message_id = 4242

    def send_text(self, chat_id, text):
        if self.raise_on_text is not None:
            raise self.raise_on_text
        self.sent.append((str(chat_id), text))
        if self.reject_text:
            return OperationResult.fail("TELEGRAM_REJECTED",
                                        error_code="TELEGRAM_REJECTED")
        return OperationResult.ok({"message_id": self.message_id})


def _make(tmp_path, *, intent="APPROVE"):
    store = ProjectStateStore(tmp_path / 'state')
    ProjectAccess(store).create(PROJECT_ID, "telegram:1", channel="telegram",
                                conversation_id=CHAT)
    hermes = MagicMock()
    # The intent classifier runs FAST and parses one of the bounded verbs.
    hermes._run_fast_programmatic.return_value = MagicMock(
        success=True, response=intent,
    )
    intake = IntakeProcessor(store, hermes_adapter=hermes)
    builder = MagicMock()
    builder.build.return_value = OperationResult.ok({})
    out = RecordingTelegram()
    vercel = FakeVercel()
    promote = PromotionOrchestrator(
        MagicMock(), store,
        _promote_deps(vercel, out, chat_id=CHAT),
    )
    dispatcher = TelegramDispatcher(
        store, intake, builder=builder, promote=promote,
        workspace_for=lambda pid: _make_workspace(tmp_path),
    )
    loop = TelegramReceiveLoop(bot_token=BOT_TOKEN, dispatcher=dispatcher,
                               telegram_out=out, hermes=hermes)
    return loop, store, out, promote, vercel, hermes


def _promote_deps(vercel, telegram, smoke=None, chat_id=CHAT):
    from app.projects.promote import PromoteDeps
    return PromoteDeps(
        vercel=vercel,
        telegram=telegram,
        smoke=smoke or FakeSmoke(),
        chat_id_for=lambda pid, state: chat_id,
    )


def _ack_texts(out):
    return [text for _chat, text in out.sent if ACK_MARKER in text]


def _preview_ready_project(store, source_revision=1):
    _approved_state(store, PROJECT_ID, source_revision=source_revision)
    with store.acquire_writer(PROJECT_ID) as state:
        state.roles['owner'] = "telegram:1"
        store.save(state)


# ---------------------------------------------------------------------------
# The happy path: one acknowledgement, no publish
# ---------------------------------------------------------------------------

def test_successful_approve_acknowledges_exactly_once(tmp_path):
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)

    loop._process_update(_payload(301, "approve"))

    acks = _ack_texts(out)
    assert len(acks) == 1, "a successful approval must be acknowledged once"
    assert "publish" in acks[0]
    assert acks[0].startswith("✅")
    # Bound to the exact shown preview identity...
    state = store.load(PROJECT_ID)
    assert state.revisions.approved_revision == 1
    assert state.deployment["approval"]["operation_id"] == "op-1"
    assert state.lifecycle == "PREVIEW_READY"
    # ...and approve must NOT publish.
    assert not state.production_url
    assert state.revisions.live_revision == 0
    assert state.deployment.get("promotion_intent") is None
    ack = state.deployment["approval_ack"]
    assert ack["outcome"] == "SENT"
    assert ack["message_id"] == 4242
    assert ack["identity"] == {
        "operation_id": "op-1",
        "deployment_id": "dpl_1",
        "source_revision": 1,
    }


def test_replayed_update_sends_no_second_acknowledgement(tmp_path):
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)

    loop._process_update(_payload(302, "approve"))
    assert len(_ack_texts(out)) == 1

    # The same Telegram update, replayed.
    loop._process_update(_payload(302, "approve"))
    assert len(_ack_texts(out)) == 1


def test_a_distinct_update_re_approving_the_same_identity_is_silent(tmp_path):
    """The user repeating "approve" is a duplicate approval, not a new one."""
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)

    loop._process_update(_payload(303, "approve"))
    assert len(_ack_texts(out)) == 1

    # A genuinely different update id, same approved identity.
    loop._process_update(_payload(304, "oke approve"))
    assert len(_ack_texts(out)) == 1
    assert store.load(PROJECT_ID).deployment["approval"]["operation_id"] == "op-1"


def test_a_new_shown_preview_gets_its_own_acknowledgement(tmp_path):
    """A different approved identity is a different action, and is allowed."""
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)
    loop._process_update(_payload(305, "approve"))
    assert len(_ack_texts(out)) == 1

    # A revision lands a new shown preview, which the user approves.
    with store.acquire_writer(PROJECT_ID) as state:
        state.revisions.revision_seq = 1
        state.revisions.queued_revision_seq = 0
        store.save(state)
    _preview_ready_project(store, source_revision=2)

    loop._process_update(_payload(306, "approve"))

    assert len(_ack_texts(out)) == 2
    state = store.load(PROJECT_ID)
    assert state.revisions.approved_revision == 2
    assert state.deployment["approval_ack"]["identity"]["source_revision"] == 2


# ---------------------------------------------------------------------------
# Fail-closed acknowledgement delivery
# ---------------------------------------------------------------------------

def test_ambiguous_acknowledgement_is_never_resent(tmp_path):
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)
    out.raise_on_text = TimeoutError("telegram timeout after send")

    loop._process_update(_payload(307, "approve"))

    state = store.load(PROJECT_ID)
    assert state.deployment["approval_ack"]["outcome"] == "PENDING"
    # A later duplicate approval must not re-send a possibly-delivered ack.
    out.raise_on_text = None
    loop._process_update(_payload(308, "approve"))
    assert _ack_texts(out) == []
    assert store.load(PROJECT_ID).deployment["approval_ack"]["outcome"] == "PENDING"


def test_proved_rejection_re_arms_the_acknowledgement(tmp_path):
    """A definite non-delivery is not a duplicate risk, so a later approval
    may re-drive it."""
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)
    out.reject_text = True

    loop._process_update(_payload(309, "approve"))

    # One attempt, proved undelivered, so the acknowledgement is re-armed.
    assert len(out.sent) == 1
    assert store.load(PROJECT_ID).deployment["approval_ack"]["outcome"] == "NOT_SENT"

    out.reject_text = False
    loop._process_update(_payload(310, "approve"))
    assert len(_ack_texts(out)) == 2
    assert store.load(PROJECT_ID).deployment["approval_ack"]["outcome"] == "SENT"


# ---------------------------------------------------------------------------
# The failure path is untouched
# ---------------------------------------------------------------------------

def test_failed_approve_keeps_the_error_path_and_sends_no_ack(tmp_path):
    """No shown preview -> approve fails closed: the user still gets the
    existing error reply, and no acknowledgement is written or sent."""
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    with store.acquire_writer(PROJECT_ID) as state:
        state.roles['owner'] = "telegram:1"
        state.lifecycle = "PREVIEW_READY"
        store.save(state)

    loop._process_update(_payload(311, "approve"))

    assert _ack_texts(out) == []
    assert out.sent, "a failed approval still explains itself"
    state = store.load(PROJECT_ID)
    assert state.revisions.approved_revision == 0
    assert state.deployment.get("approval") is None
    assert state.deployment.get("approval_ack") is None


def test_unauthorized_approve_sends_no_ack(tmp_path):
    """A non-owner cannot approve, so there is nothing to acknowledge."""
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)

    loop._process_update(_payload(312, "approve", user="2", chat=CHAT))

    assert _ack_texts(out) == []
    assert store.load(PROJECT_ID).deployment.get("approval_ack") is None


def test_publish_dispatch_does_not_emit_an_approve_acknowledgement(tmp_path):
    """PUBLISH is a separate verb. The publish flow approves internally, and
    must not double-message the user with an approval acknowledgement."""
    loop, store, out, promote, vercel, hermes = _make(tmp_path, intent="PUBLISH")
    _preview_ready_project(store)

    loop._process_update(_payload(313, "publish"))

    assert _ack_texts(out) == []
    state = store.load(PROJECT_ID)
    # The live notification is a different message.
    assert any("Live" in text or "live" in text for _chat, text in out.sent)
    assert state.lifecycle == "LIVE"
    assert state.production_url == "https://prod.vercel.app"


def test_ack_never_contains_the_preview_url(tmp_path):
    """The protected preview URL stays internal in every message we send."""
    loop, store, out, promote, _vercel, _hermes = _make(tmp_path)
    _preview_ready_project(store)

    loop._process_update(_payload(314, "approve"))

    for _chat, text in out.sent:
        assert "vercel.app" not in text
        assert "dpl_1" not in text
        assert "op-1" not in text
