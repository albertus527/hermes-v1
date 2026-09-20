"""Regression tests for the pre-E2E reliability audit findings.

Each test pins the CORRECT behavior for one audited bug class so a future
refactor cannot silently reintroduce it. Offline only; no network, no Hermes.

Findings covered:
  F1  resume false-positive on a non-paused project + resume must return to
      the pre-pause lifecycle (not unconditionally DISCOVERING).
  F2  a DISCOVERY_READY transition must clear a stale pause flag so the
      auto-build gate (lifecycle READY + source_revision 0 + not paused) opens.
  F3  the scope fallback must not match "app" inside "WhatsApp" (word boundary).
  F4  pause/resume phrase detection must not fire on a larger containing word
      ("waiting", "gasifikasi").
  F5  when the Hermes adapter returns its internal deterministic fallback dict
      (source="fallback_heuristic") instead of raising, intake must treat the
      turn as a fallback turn so a lone token shifts into the next missing
      field instead of clobbering an established NAME.
  F6  the pre-turn reconcile_preview dispatch must use a distinct claim sub-key
      so it never collides with the same event's intake/turn claim.
  F7  a failed telegram_out.send_text (OperationResult.fail, never raises) must
      be logged, not silently swallowed by a dead except block.
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.core.contracts import OperationResult
from app.core.intake import (
    IntakeProcessor,
    Readiness,
    Scope,
    _contains_phrase,
    _PAUSE_PHRASES,
    _RESUME_PHRASES,
)
from app.core.state import ProjectStateStore
from app.runtime import TelegramReceiveLoop


class _Msg:
    def __init__(self, text):
        self.text = text


def _owned_store(root, project_id="tg-1", lifecycle="DISCOVERING", brief=None):
    store = ProjectStateStore(root / "state")
    with store.acquire_writer(project_id) as state:
        state.lifecycle = lifecycle
        state.roles = {"owner": "telegram:1", "reviewers": [], "viewers": []}
        state.brief = brief or {}
        state.conversation_id = "555"
        store.save(state)
    return store


class TestResumeGating(unittest.TestCase):
    """F1: resume phrase on a non-paused project must not suppress clarification."""

    def test_resume_phrase_on_non_paused_project_still_clarifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _owned_store(Path(tmp), brief={"name": "toko kue"})
            intake = IntakeProcessor(store)
            result = intake.process(_Msg("gas"), "tg-1")
            self.assertFalse(result.resume_detected)
            self.assertNotEqual(result.readiness, Readiness.RESUMED)
            self.assertIsNotNone(result.clarification_question)

    def test_resume_returns_to_pre_pause_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _owned_store(
                Path(tmp), lifecycle="READY",
                brief={"name": "x", "what": "y", "why": "z"},
            )
            intake = IntakeProcessor(store)
            r_pause = intake.process(_Msg("wait"), "tg-1")
            intake.apply_to_project("tg-1", r_pause, principal_id="telegram:1", event_id="e1")
            self.assertEqual(store.load("tg-1").lifecycle, "PAUSED")

            r_resume = intake.process(_Msg("lanjut"), "tg-1")
            self.assertTrue(r_resume.resume_detected)
            intake.apply_to_project("tg-1", r_resume, principal_id="telegram:1", event_id="e2")
            self.assertEqual(store.load("tg-1").lifecycle, "READY")


class TestReadyClearsPause(unittest.TestCase):
    """F2: DISCOVERY_READY must clear a stale pause flag so auto-build can fire."""

    def test_discovery_ready_clears_pause_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _owned_store(Path(tmp), lifecycle="PAUSED", brief={"name": "toko kue"})
            with store.acquire_writer("tg-1") as state:
                state.pause_state = {
                    "paused": True,
                    "paused_at": 1.0,
                    "pre_pause_lifecycle": "DISCOVERING",
                }
                store.save(state)
            intake = IntakeProcessor(store)
            result = intake.process(
                _Msg("website toko kue, jualan kue online, biar orang bisa pesan"), "tg-1"
            )
            self.assertEqual(result.readiness, Readiness.DISCOVERY_READY)
            intake.apply_to_project("tg-1", result, principal_id="telegram:1", event_id="e1")
            state = store.load("tg-1")
            self.assertEqual(state.lifecycle, "READY")
            self.assertFalse(state.pause_state.get("paused"))
            # The auto-build gate condition must now be satisfiable.
            self.assertEqual(state.revisions.source_revision, 0)


class TestScopeWordBoundary(unittest.TestCase):
    """F3: 'app' inside 'WhatsApp' must not flip scope to MIXED/OUT_OF_SCOPE."""

    def test_whatsapp_does_not_flip_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake = IntakeProcessor(ProjectStateStore(Path(tmp) / "state"))
            self.assertEqual(
                intake._fallback_scope("website toko kue, jualan online, biar orang pesan via WhatsApp"),
                Scope.WEBSITE,
            )
            self.assertEqual(
                intake._fallback_scope("website dengan tombol WhatsApp"),
                Scope.WEBSITE,
            )

    def test_real_app_keyword_still_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            intake = IntakeProcessor(ProjectStateStore(Path(tmp) / "state"))
            self.assertEqual(
                intake._fallback_scope("buat mobile app untuk tracking"),
                Scope.OUT_OF_SCOPE,
            )


class TestPhraseWordBoundary(unittest.TestCase):
    """F4: pause/resume phrases must not fire on a larger containing word."""

    def test_waiting_does_not_pause(self):
        self.assertFalse(_contains_phrase("website untuk waiting list pendaftaran", _PAUSE_PHRASES))
        self.assertFalse(_contains_phrase("fitur waiting room", _PAUSE_PHRASES))

    def test_gasifikasi_does_not_resume(self):
        self.assertFalse(_contains_phrase("gasifikasi proyek", _RESUME_PHRASES))

    def test_genuine_phrases_still_fire(self):
        self.assertTrue(_contains_phrase("wait", _PAUSE_PHRASES))
        self.assertTrue(_contains_phrase("tolong tunggu dulu", _PAUSE_PHRASES))
        self.assertTrue(_contains_phrase("gas", _RESUME_PHRASES))
        self.assertTrue(_contains_phrase("lanjut", _RESUME_PHRASES))


class TestAdapterFallbackSetsUsedFallback(unittest.TestCase):
    """F5: adapter's internal fallback dict must be treated as a fallback turn."""

    def test_fallback_dict_preserves_established_name(self):
        adapter = MagicMock()
        adapter.fast_interpret.return_value = {
            "scope": "UNCLEAR",
            "name": "jualan kue online",  # fallback maps lone segment to name
            "what": None,
            "why": None,
            "why_destination": None,
            "ambiguity": None,
            "clarification_needed": True,
            "clarification_question": "What should visitors do?",
            "readiness": "NEEDS_CLARIFICATION",
            "source": "fallback_heuristic",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = _owned_store(Path(tmp), lifecycle="WAITING_INPUT", brief={"name": "toko kue"})
            intake = IntakeProcessor(store, hermes_adapter=adapter)
            result = intake.process(_Msg("jualan kue online"), "tg-1")
            self.assertEqual(result.brief.get("name"), "toko kue")
            self.assertEqual(result.brief.get("what"), "jualan kue online")


class _StubPreview:
    def run_owned(self, project_id, workspace, slot_held=False):
        return OperationResult.fail("NO_DELIVERY_TARGET", error_code="NO_DELIVERY_TARGET")

class _SucceedingPreview:
    """Reconcile stub that succeeds so the turn continues past the gate.

    F6 pins the distinct-claim invariant (reconcile vs intake sub-keys). Under
    the fail-closed recovery gate a FAILED reconcile stops the turn before
    intake, so this stub must SUCCEED for intake to be claimed at all.
    """
    def run_owned(self, project_id, workspace, slot_held=False):
        return OperationResult.ok({"preview_url": "https://x.vercel.app"})


class _RecordingOut:
    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self._fail:
            return OperationResult.fail("SEND_FAILED", error_code="SEND_FAILED")
        return OperationResult.ok({"message_id": 1})


class TestReconcileClaimSubkey(unittest.TestCase):
    """F6: reconcile_preview must not consume the event-derived claim key."""

    def test_reconcile_and_turn_use_distinct_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProjectStateStore(root / "state")
            workdir = root / "ws"
            workdir.mkdir(parents=True, exist_ok=True)
            with store.acquire_writer("tg-555") as state:
                state.lifecycle = "PREVIEW_READY"
                state.roles = {"owner": "telegram:1", "reviewers": [], "viewers": []}
                state.revisions.source_revision = 4
                state.revisions.qa_revision = 4
                state.revisions.preview_revision = 0
                state.deployment = {"tested_snapshot": {"dist_hash": "abc"}}
                state.conversation_id = "555"
                store.save(state)

            dispatcher = TelegramDispatcher(
                store, IntakeProcessor(store),
                workspace_for=lambda pid: workdir,
                preview=_SucceedingPreview(),
            )
            out = _RecordingOut()
            loop = TelegramReceiveLoop(
                bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
                dispatcher=dispatcher, telegram_out=out, hermes=None,
                transport=None, conversations=None,
            )
            loop._process_update({
                "update_id": 9001,
                "message": {"from": {"id": 1}, "chat": {"id": 555},
                            "text": "warnanya tolong bikin hijau dong", "date": 1},
            })

            state = store.load("tg-555")
            actions = sorted(v["action"] for v in state.dispatch_events.values())
            # Both the reconcile and the turn's intake must be claimed, and the
            # intake must NOT have failed with an action-mismatch collision.
            self.assertIn("reconcile_preview", actions)
            self.assertIn("intake", actions)
            self.assertFalse(
                any(
                    v.get("action") == "intake" and v.get("status") == "FAILED"
                    for v in state.dispatch_events.values()
                ),
                "intake claim collided with reconcile claim",
            )


class TestSendFailureLogged(unittest.TestCase):
    """F7: a failed send_text must be logged, not silently swallowed."""

    def test_failed_clarification_send_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _owned_store(root, project_id="tg-555", lifecycle="DISCOVERING")
            workdir = root / "ws"
            workdir.mkdir(parents=True, exist_ok=True)
            dispatcher = TelegramDispatcher(
                store, IntakeProcessor(store), workspace_for=lambda pid: workdir
            )
            loop = TelegramReceiveLoop(
                bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
                dispatcher=dispatcher, telegram_out=_RecordingOut(fail=True),
                hermes=None, transport=None, conversations=None,
            )
            logger = logging.getLogger("app.runtime")
            records = []

            class _H(logging.Handler):
                def emit(self, record):
                    records.append(record)

            handler = _H()
            logger.addHandler(handler)
            try:
                loop._process_update({
                    "update_id": 9002,
                    "message": {"from": {"id": 1}, "chat": {"id": 555},
                                "text": "toko kue", "date": 1},
                })
            finally:
                logger.removeHandler(handler)

            self.assertTrue(
                any(
                    r.levelno >= logging.ERROR and "Failed to send" in r.getMessage()
                    for r in records
                ),
                "failed send was not logged at ERROR",
            )


if __name__ == "__main__":
    unittest.main()
