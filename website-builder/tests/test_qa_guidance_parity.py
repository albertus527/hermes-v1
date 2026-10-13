"""N6-E — QA/VISION guidance parity is enforced end to end (fail closed).

Drives the REAL QAOrchestrator acceptance path with a recorded guidance
snapshot and proves that:

* when the documents still match the recorded snapshot, QA reaches
  PREVIEW_READY;
* when a document that WAS used changes after the FRONTEND invocation, QA fails
  closed with GUIDANCE_VERSION_MISMATCH — parity is NOT inferred from the
  current workspace.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.guidance_snapshot import record_invocation  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.requirement_creator import compile_prd  # noqa: E402
from app.core.document_store import store_documents  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.snapshot import record_checks, source_fingerprint  # noqa: E402
from app.qa.orchestrator import QAOrchestrator  # noqa: E402
from app.qa.render import RenderHandle  # noqa: E402
from app.qa.screenshot import BrowserMetrics, ScreenshotSet  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

BRIEF = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}


def _png_bytes(width: int, height: int) -> bytes:
    """Minimal valid PNG with the given dimensions (stdlib only)."""
    import struct
    import zlib

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _metrics(width: int, height: int) -> BrowserMetrics:
    return BrowserMetrics(
        inner_width=width, inner_height=height,
        document_client_width=width, document_scroll_width=width,
        body_scroll_width=width,
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace_root = root / "workspaces"
        self.store = ProjectStateStore(root / "state")
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.workspace = self.workspace_root / "proj"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "App.tsx").write_text("// real\n", encoding="utf-8")
        (self.workspace / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
        (self.workspace / "dist").mkdir(parents=True)
        (self.workspace / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")

        self.mock_renderer = MagicMock()
        self.mock_capture = MagicMock()
        self.mock_adapter = MagicMock()
        self.orchestrator = QAOrchestrator(
            self.runner, self.store, hermes_adapter=self.mock_adapter,
            renderer=self.mock_renderer, screenshot_capture=self.mock_capture,
        )

        self.store.transition_lifecycle("proj", ProjectLifecycle.READY)
        self.store.transition_lifecycle("proj", ProjectLifecycle.QUEUED)
        self.store.transition_lifecycle("proj", ProjectLifecycle.RUNNING)

    def tearDown(self):
        self._tmp.cleanup()

    def _passing_render(self):
        self.mock_renderer.start.return_value = RenderHandle(
            project_id="proj", port=5100, process=MagicMock(),
            url="http://127.0.0.1:5100/",
        )

    def _passing_screenshots(self, attempt: int):
        qa_dir = self.workspace / "qa" / f"attempt-{attempt}"
        qa_dir.mkdir(parents=True, exist_ok=True)
        desktop = qa_dir / "desktop.png"
        mobile = qa_dir / "mobile.png"
        desktop.write_bytes(_png_bytes(1440, 900))
        mobile.write_bytes(_png_bytes(390, 844))
        return ScreenshotSet(
            desktop=desktop, mobile=mobile,
            desktop_metrics=_metrics(1440, 900),
            mobile_metrics=_metrics(390, 844),
        )

    def _bind_checked(self):
        before = source_fingerprint(self.workspace)
        record_checks(self.store, "proj", self.workspace, before)


class ParityPassTests(_Base):
    def test_matching_documents_reach_preview_ready(self):
        prd = compile_prd(BRIEF, project_id="proj", source_revision=1)
        store_documents(self.workspace, prd_text=prd)
        # Record the snapshot exactly as the FRONTEND choke point would.
        record_invocation(self.store, "proj", self.workspace, invocation_id="proj:r1")
        self._bind_checked()
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        self.mock_adapter.vision_inspect.return_value = {
            "pass": True, "blocking": [], "observations": [], "summary": "ok",
        }
        result = self.orchestrator.run("proj", self.workspace, BRIEF, None)
        self.assertTrue(result.success, result.error)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)


class ParityFailClosedTests(_Base):
    def test_changed_used_document_fails_closed(self):
        prd = compile_prd(BRIEF, project_id="proj", source_revision=1)
        store_documents(self.workspace, prd_text=prd)
        record_invocation(self.store, "proj", self.workspace, invocation_id="proj:r1")
        self._bind_checked()
        # A DIFFERENT valid PRD appears after the invocation.
        other = compile_prd(
            {"name": "Northcut", "what": "changed", "why": "booking WA"},
            project_id="proj", source_revision=1,
        )
        from app.core.document_contracts import PRD_FILE_NAME
        (self.workspace / PRD_FILE_NAME).write_text(other, encoding="utf-8")
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        self.mock_adapter.vision_inspect.return_value = {
            "pass": True, "blocking": [], "observations": [], "summary": "ok",
        }
        result = self.orchestrator.run("proj", self.workspace, BRIEF, None)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "GUIDANCE_VERSION_MISMATCH")
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
