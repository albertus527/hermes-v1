"""Phase 3 tests: Telegram normalization, debounce, pause/resume, scope, NAME/WHAT/WHY.

FAST uses Hermes when available; deterministic fallback is bounded and safe.
Application code owns all state transitions.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.channels.telegram import NormalizedMessage, TelegramNormalizer
from app.core.buffer import MessageBuffer
from app.core.intake import IntakeProcessor, Readiness, Scope
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore


class TestTelegramNormalizer(unittest.TestCase):
    def test_normalize_text_message(self):
        payload = {
            "update_id": 12345,
            "message": {
                "message_id": 1,
                "from": {"id": 67890, "first_name": "Test"},
                "chat": {"id": 11122, "type": "private"},
                "date": 1694500000,
                "text": "Bikin Northcut, barbershop, biar orang booking WA.",
            },
        }
        msg = TelegramNormalizer.normalize(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.event_id, "12345")
        self.assertEqual(msg.channel, "telegram")
        self.assertEqual(msg.user_id, "67890")
        self.assertEqual(msg.conversation_id, "11122")
        self.assertEqual(msg.text, "Bikin Northcut, barbershop, biar orang booking WA.")
        self.assertEqual(msg.timestamp, 1694500000.0)

    def test_normalize_photo_message(self):
        payload = {
            "update_id": 12346,
            "message": {
                "message_id": 2,
                "from": {"id": 67890},
                "chat": {"id": 11122},
                "date": 1694500001,
                "caption": "Reference design",
                "photo": [
                    {"file_id": "photo_1", "width": 800, "height": 600},
                    {"file_id": "photo_2", "width": 1280, "height": 960},
                ],
            },
        }
        msg = TelegramNormalizer.normalize(payload)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.text, "Reference design")
        self.assertEqual(len(msg.attachments), 2)
        self.assertEqual(msg.attachments[0]["type"], "photo")

    def test_normalize_reply(self):
        payload = {
            "update_id": 12347,
            "message": {
                "message_id": 3,
                "from": {"id": 67890},
                "chat": {"id": 11122},
                "date": 1694500002,
                "text": "Yes, that one",
                "reply_to_message": {
                    "message_id": 2,
                    "text": "Which design?",
                    "from": {"id": 99999},
                },
            },
        }
        msg = TelegramNormalizer.normalize(payload)
        self.assertIsNotNone(msg)
        self.assertIsNotNone(msg.reply_to)
        self.assertEqual(msg.reply_to["message_id"], 2)

    def test_normalize_non_message_returns_none(self):
        payload = {"update_id": 12348, "callback_query": {"id": "abc"}}
        msg = TelegramNormalizer.normalize(payload)
        self.assertIsNone(msg)


class TestMessageBuffer(unittest.TestCase):
    def test_debounce_flush(self):
        flushed = []

        def on_flush(conv_id, messages):
            flushed.append((conv_id, messages))

        buffer = MessageBuffer(debounce_seconds=0.1, on_flush=on_flush)
        msg1 = NormalizedMessage(event_id="e1", text="Hello")
        msg2 = NormalizedMessage(event_id="e2", text="World")

        buffer.add("conv-1", msg1)
        buffer.add("conv-1", msg2)
        self.assertEqual(buffer.pending_count("conv-1"), 2)

        time.sleep(0.2)
        self.assertEqual(len(flushed), 1)
        self.assertEqual(flushed[0][0], "conv-1")
        self.assertEqual(len(flushed[0][1]), 2)

    def test_flush_now(self):
        buffer = MessageBuffer(debounce_seconds=10.0)
        msg = NormalizedMessage(event_id="e1", text="Test")
        buffer.add("conv-2", msg)
        messages = buffer.flush_now("conv-2")
        self.assertEqual(len(messages), 1)
        self.assertEqual(buffer.pending_count("conv-2"), 0)


class TestIntakeProcessorWithHermes(unittest.TestCase):
    """Test intake when Hermes FAST adapter is available."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir))
        self.mock_adapter = MagicMock()
        self.processor = IntakeProcessor(self.store, hermes_adapter=self.mock_adapter)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fast_interpretation_used(self):
        """FAST result contract is consumed deterministically."""
        self.mock_adapter.fast_interpret.return_value = {
            "scope": "WEBSITE",
            "name": "Northcut",
            "what": "barbershop",
            "why": "booking WA",
            "why_destination": None,
            "ambiguity": None,
            "clarification_needed": False,
            "clarification_question": None,
            "readiness": "DISCOVERY_READY",
        }

        msg = NormalizedMessage(event_id="e1", text="Bikin Northcut, barbershop, biar orang booking WA.")
        result = self.processor.process(msg)

        self.assertEqual(result.scope, Scope.WEBSITE)
        self.assertEqual(result.brief["name"], "Northcut")
        self.assertEqual(result.brief["what"], "barbershop")
        self.assertEqual(result.brief["why"], "booking WA")
        self.assertIsNone(result.brief["why_destination"])
        self.assertEqual(result.readiness, Readiness.DISCOVERY_READY)
        self.mock_adapter.fast_interpret.assert_called_once()

    def test_pause_overrides_fast(self):
        """Application pause detection overrides FAST readiness."""
        self.mock_adapter.fast_interpret.return_value = {
            "scope": "WEBSITE",
            "name": "Northcut",
            "what": "barbershop",
            "why": "booking WA",
            "why_destination": None,
            "ambiguity": None,
            "clarification_needed": False,
            "clarification_question": None,
            "readiness": "DISCOVERY_READY",
        }

        msg = NormalizedMessage(event_id="e2", text="eh bentar")
        result = self.processor.process(msg)

        self.assertTrue(result.pause_detected)
        self.assertEqual(result.readiness, Readiness.PAUSED)

    def test_intake_executes_deterministic_fallback_after_fast_failure(self):
        """IntakeProcessor executes deterministic fallback after FAST failure.

        When the FAST adapter raises (Hermes unavailable / provider error /
        construction failure surfaced as an exception), the application must
        still produce a deterministic interpretation rather than crashing.
        """
        self.mock_adapter.fast_interpret.side_effect = RuntimeError(
            "FAST backend unavailable"
        )

        msg = NormalizedMessage(
            event_id="e9f", text="Northcut, barbershop, biar orang booking WA."
        )
        result = self.processor.process(msg)

        # Deterministic fallback result, not an exception.
        self.assertEqual(result.scope, Scope.WEBSITE)
        self.assertEqual(result.brief.get("name"), "Northcut")
        self.assertEqual(result.brief.get("what"), "barbershop")
        self.assertEqual(result.readiness, Readiness.DISCOVERY_READY)
        # Never fabricate a CTA destination from WHY text.
        self.assertIsNone(result.brief.get("why_destination"))

    def test_apply_to_project_uses_lifecycle_authority(self):
        """All lifecycle transitions go through the deterministic authority."""
        self.mock_adapter.fast_interpret.return_value = {
            "scope": "WEBSITE",
            "name": "Northcut",
            "what": "barbershop",
            "why": "booking WA",
            "why_destination": None,
            "ambiguity": None,
            "clarification_needed": False,
            "clarification_question": None,
            "readiness": "DISCOVERY_READY",
        }

        msg = NormalizedMessage(event_id="e3", text="Bikin Northcut, barbershop, biar orang booking WA.")
        result = self.processor.process(msg)
        from app.core.authz import ProjectAccess
        ProjectAccess(self.store).create("proj-fast", "owner")
        self.processor.apply_to_project("proj-fast", result, principal_id="owner")

        state = self.store.load("proj-fast")
        self.assertEqual(state.lifecycle, ProjectLifecycle.READY.value)
        self.assertEqual(state.revisions.requirements_version, 1)


class TestIntakeProcessorFallback(unittest.TestCase):
    """Test deterministic fallback when Hermes is unavailable."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir))
        self.processor = IntakeProcessor(self.store, hermes_adapter=None)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pause_detection(self):
        msg = NormalizedMessage(event_id="e1", text="eh bentar")
        result = self.processor.process(msg)
        self.assertTrue(result.pause_detected)
        self.assertEqual(result.readiness, Readiness.PAUSED)

    def test_resume_detection(self):
        msg = NormalizedMessage(event_id="e2", text="lanjut")
        result = self.processor.process(msg)
        self.assertTrue(result.resume_detected)
        self.assertEqual(result.readiness, Readiness.RESUMED)

    def test_scope_website(self):
        msg = NormalizedMessage(event_id="e3", text="Bikin website barbershop")
        result = self.processor.process(msg)
        self.assertEqual(result.scope, Scope.WEBSITE)

    def test_scope_out_of_scope(self):
        msg = NormalizedMessage(event_id="e4", text="Bikin mobile app")
        result = self.processor.process(msg)
        self.assertEqual(result.scope, Scope.OUT_OF_SCOPE)

    def test_name_extraction(self):
        msg = NormalizedMessage(event_id="e5", text="Bikin Northcut.")
        result = self.processor.process(msg)
        self.assertEqual(result.brief.get("name"), "Northcut")
        self.assertIsNone(result.brief.get("what"))
        self.assertIsNone(result.brief.get("why"))
        self.assertEqual(result.readiness, Readiness.NEEDS_CLARIFICATION)
        self.assertIn("What is Northcut?", result.clarification_question)

    def test_name_what_extraction(self):
        msg = NormalizedMessage(event_id="e6", text="Northcut, barbershop.")
        result = self.processor.process(msg)
        self.assertEqual(result.brief.get("name"), "Northcut")
        self.assertEqual(result.brief.get("what"), "barbershop")
        self.assertIsNone(result.brief.get("why"))
        self.assertEqual(result.readiness, Readiness.NEEDS_CLARIFICATION)
        self.assertIn("What should visitors", result.clarification_question)

    def test_discovery_ready(self):
        msg = NormalizedMessage(
            event_id="e7", text="Northcut, barbershop, biar orang booking WA."
        )
        result = self.processor.process(msg)
        self.assertEqual(result.brief.get("name"), "Northcut")
        self.assertEqual(result.brief.get("what"), "barbershop")
        self.assertEqual(result.brief.get("why"), "orang booking WA")
        self.assertEqual(result.readiness, Readiness.DISCOVERY_READY)
        self.assertIsNone(result.clarification_question)

    def test_why_never_becomes_fabricated_url(self):
        """WHY text must never be turned into a fabricated WhatsApp URL."""
        msg = NormalizedMessage(
            event_id="e8", text="Northcut, barbershop, biar orang booking WA."
        )
        result = self.processor.process(msg)
        # why_destination must be None — no explicit URL/phone was provided
        self.assertIsNone(result.brief.get("why_destination"))

    def test_explicit_url_extracted(self):
        """Explicit URL in text is extracted as why_destination."""
        msg = NormalizedMessage(
            event_id="e9",
            text="Northcut, barbershop, booking via https://wa.me/6281234567890",
        )
        result = self.processor.process(msg)
        self.assertEqual(result.brief.get("why_destination"), "https://wa.me/6281234567890")

    def test_explicit_phone_extracted(self):
        """Explicit phone number in text is extracted as why_destination."""
        msg = NormalizedMessage(
            event_id="e10",
            text="Northcut, barbershop, booking via +6281234567890",
        )
        result = self.processor.process(msg)
        self.assertEqual(result.brief.get("why_destination"), "+6281234567890")

    def test_apply_to_project_pauses(self):
        msg = NormalizedMessage(event_id="e11", text="eh bentar")
        result = self.processor.process(msg)
        from app.core.authz import ProjectAccess
        ProjectAccess(self.store).create("proj-pause", "owner")
        self.processor.apply_to_project("proj-pause", result, principal_id="owner")

        state = self.store.load("proj-pause")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PAUSED.value)
        self.assertTrue(state.pause_state.get("paused"))

    def test_apply_to_project_discovery_ready(self):
        msg = NormalizedMessage(
            event_id="e12", text="Northcut, barbershop, biar orang booking WA."
        )
        result = self.processor.process(msg)
        from app.core.authz import ProjectAccess
        ProjectAccess(self.store).create("proj-ready", "owner")
        self.processor.apply_to_project("proj-ready", result, principal_id="owner")

        state = self.store.load("proj-ready")
        self.assertEqual(state.lifecycle, ProjectLifecycle.READY.value)
        self.assertEqual(state.brief["name"], "Northcut")
        self.assertEqual(state.revisions.requirements_version, 1)

    def test_deduplication_integration(self):
        msg = NormalizedMessage(event_id="e13", text="Bikin Northcut.")
        result = self.processor.process(msg)
        from app.core.authz import ProjectAccess
        ProjectAccess(self.store).create("proj-dedup", "owner")
        self.processor.apply_to_project("proj-dedup", result, principal_id="owner")

        self.assertFalse(self.store.is_event_processed("proj-dedup", "e13"))
        self.store.mark_event_processed("proj-dedup", "e13")
        self.assertTrue(self.store.is_event_processed("proj-dedup", "e13"))


if __name__ == "__main__":
    unittest.main()
