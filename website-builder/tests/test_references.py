"""Phase 12 reference intake, safety, and synthesis wiring tests.

Local behavior only. No live network, DNS, or model calls. connection_factory
and resolver are injected fakes; the real PinnedHTTPSConnection class is
exercised only for the module-level shape checks (never opens a socket).
"""
from __future__ import annotations

import importlib.util
import struct
import unittest
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch
import io
import hashlib

_HAS_PIL = importlib.util.find_spec("PIL") is not None

from app.core.contracts import OperationResult
from app.core.references import (
    ReferenceItem,
    ReferenceSet,
    compose_reference_instructions,
    fetch_reference_url,
    normalized_image,
    persisted_reference_instructions,
    validate_characteristics,
    validate_reference_synthesis,
    validate_role,
    validate_upload,
)
from app.core.state import ProjectState


def _png(width=100, height=80):
    def chunk(tag, data):
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"\x00" + b"\x00" * (width * 4)
    idat = zlib.compress(raw * height)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


class TestValidateRole(unittest.TestCase):
    def test_valid_roles(self):
        for role in ("UX", "color", "Layout", "MOTION", "style", "content"):
            self.assertEqual(validate_role(role), role.upper())

    def test_invalid_role_rejected(self):
        for bad in ("", "HTML", None, 123, "ux "):
            with self.assertRaises(ValueError):
                validate_role(bad)


class TestNormalizedImage(unittest.TestCase):
    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_valid_png_accepted(self):
        data = _png()
        from PIL import Image
        clean = normalized_image(data)
        with Image.open(io.BytesIO(clean)) as image:
            image.load()
            self.assertEqual(image.size, (100, 80))
            self.assertEqual(image.info, {})

    def test_missing_decoder_fails_closed(self):
        import sys
        with patch.dict(sys.modules, {"PIL": None}):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                normalized_image(_png())

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_truncated_pixels_rejected(self):
        with self.assertRaises(ValueError):
            normalized_image(_png()[:40])

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_metadata_and_trailing_payload_removed(self):
        from PIL import Image, PngImagePlugin
        output = io.BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("comment", "untrusted payload")
        Image.new("RGB", (8, 8), "red").save(output, format="PNG", pnginfo=metadata)
        clean = normalized_image(output.getvalue() + b"trailing payload")
        self.assertNotIn(b"payload", clean)
        with Image.open(io.BytesIO(clean)) as image:
            self.assertEqual(image.info, {})
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_jpeg_reencoded_as_png(self):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 6), "blue").save(output, format="JPEG")
        clean = normalized_image(output.getvalue())
        self.assertTrue(clean.startswith(b"\x89PNG"))
        with Image.open(io.BytesIO(clean)) as image:
            self.assertEqual(image.size, (8, 6))

    def test_rejects_non_image(self):
        with self.assertRaises(ValueError):
            normalized_image(b"not an image")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            normalized_image(b"")

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_rejects_oversize_dimensions(self):
        # Claim huge IHDR dimensions without needing to allocate real pixels;
        # normalized_image parses width/height and must reject before any
        # decode step exists.
        big = _png(width=1, height=1)
        # Overwrite width field in IHDR chunk (bytes 16..20 of IHDR data start
        # at offset 8+4+4=16 in the file).
        huge = bytearray(big)
        struct.pack_into(">I", huge, 16, 20000)
        struct.pack_into(">I", huge, 20, 20000)
        with self.assertRaises(ValueError):
            normalized_image(bytes(huge))

    def test_rejects_oversize_bytes(self):
        with self.assertRaises(ValueError):
            normalized_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * (9 * 1024 * 1024))


class TestValidateUpload(unittest.TestCase):
    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_valid_upload(self):
        item = validate_upload(_png(), "ux")
        self.assertEqual(item.role, "UX")
        self.assertEqual(item.source, "upload")
        self.assertEqual(item.content_type, "image/png")

    def test_invalid_role_fails_before_decode(self):
        with self.assertRaises(ValueError):
            validate_upload(_png(), "NOT_A_ROLE")


class TestFetchReferenceUrl(unittest.TestCase):
    def _fake_connection(self, status=200, content_type="image/png", body=None, length=None):
        body = body if body is not None else _png()

        class FakeResponse:
            def __init__(self):
                self.status = status
                self._sent = False
                self.sock = None

            def getheader(self, name, default=None):
                headers = {
                    "Content-Encoding": "identity",
                    "Content-Type": content_type,
                }
                if length is not None:
                    headers["Content-Length"] = str(length)
                return headers.get(name, default)

            def read1(self, n):
                if self._sent:
                    return b""
                self._sent = True
                return body

        class FakeConn:
            def __init__(self, *a, **kw):
                self.sock = None

            def request(self, *a, **kw):
                pass

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        return FakeConn

    def test_rejects_non_https(self):
        result = fetch_reference_url("http://example.com/a.png", "UX")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_REFERENCE_URL")

    def test_rejects_url_with_credentials(self):
        result = fetch_reference_url("https://user:pass@example.com/a.png", "UX")
        self.assertFalse(result.success)

    def test_rejects_query_string(self):
        result = fetch_reference_url("https://example.com/a.png?x=1", "UX")
        self.assertFalse(result.success)

    def test_rejects_private_address(self):
        result = fetch_reference_url(
            "https://internal.example/a.png", "UX",
            resolver=lambda host: ["10.0.0.5"],
            connection_factory=self._fake_connection(),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_REFERENCE_URL")

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_successful_fetch(self):
        result = fetch_reference_url(
            "https://example.com/a.png", "COLOR",
            resolver=lambda host: ["93.184.216.34"],
            connection_factory=self._fake_connection(),
        )
        self.assertTrue(result.success)
        item = result.data["item"]
        self.assertEqual(item.role, "COLOR")
        self.assertEqual(item.source, "url")
        self.assertEqual(item.origin_url, "https://example.com/a.png")

    def test_rejects_wrong_content_type(self):
        result = fetch_reference_url(
            "https://example.com/a.png", "UX",
            resolver=lambda host: ["93.184.216.34"],
            connection_factory=self._fake_connection(content_type="text/html"),
        )
        self.assertFalse(result.success)

    def test_rejects_oversized_declared_length(self):
        result = fetch_reference_url(
            "https://example.com/a.png", "UX",
            resolver=lambda host: ["93.184.216.34"],
            connection_factory=self._fake_connection(length=999999999),
        )
        self.assertFalse(result.success)

    def test_rejects_non_200_status(self):
        result = fetch_reference_url(
            "https://example.com/a.png", "UX",
            resolver=lambda host: ["93.184.216.34"],
            connection_factory=self._fake_connection(status=302),
        )
        self.assertFalse(result.success)


class TestReferenceSet(unittest.TestCase):
    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_add_and_to_dict(self):
        rs = ReferenceSet()
        item = validate_upload(_png(), "layout")
        rs.add(item)
        self.assertIn("LAYOUT", rs.to_dict())

    def test_add_invalid_role_rejected(self):
        rs = ReferenceSet()
        bad = ReferenceItem("NOTAROLE", "upload", "a" * 64, "image/png", 10)
        with self.assertRaises(ValueError):
            rs.add(bad)


class TestValidateCharacteristics(unittest.TestCase):
    def test_valid(self):
        validate_characteristics({"UX": "clean nav"}, {"UX": object()})

    def test_missing_role_rejected(self):
        with self.assertRaises(ValueError):
            validate_characteristics({}, {"UX": object()})

    def test_empty_note_rejected(self):
        with self.assertRaises(ValueError):
            validate_characteristics({"UX": "  "}, {"UX": object()})

    def test_extra_role_rejected(self):
        with self.assertRaises(ValueError):
            validate_characteristics({"UX": "a", "COLOR": "b"}, {"UX": object()})


class TestComposeReferenceInstructions(unittest.TestCase):
    def test_empty_set_returns_empty_string(self):
        self.assertEqual(compose_reference_instructions(ReferenceSet()), "")

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_requires_evidence_for_every_role(self):
        rs = ReferenceSet()
        rs.add(validate_upload(_png(), "ux"))
        with self.assertRaises(ValueError):
            compose_reference_instructions(rs, {})

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_composes_with_evidence(self):
        rs = ReferenceSet()
        rs.add(validate_upload(_png(), "ux"))
        text = compose_reference_instructions(rs, {"UX": "calm, spacious navigation"})
        self.assertIn("untrusted observations", text)
        self.assertIn("calm, spacious navigation", text)
        self.assertIn("reference_synthesis", text)


class TestPersistedReferenceInstructions(unittest.TestCase):
    def test_no_references_returns_empty(self):
        state = ProjectState(project_id="p1")
        self.assertEqual(persisted_reference_instructions(state), "")

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_persisted_roundtrip(self):
        state = ProjectState(project_id="p1")
        item = validate_upload(_png(), "ux")
        state.design_references["UX"] = {
            "item": item.to_dict(),
            "evidence": "clean, minimal navigation",
        }
        text = persisted_reference_instructions(state)
        self.assertIn("clean, minimal navigation", text)

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_role_mismatch_rejected(self):
        state = ProjectState(project_id="p1")
        item = validate_upload(_png(), "ux")
        record = item.to_dict()
        record["role"] = "COLOR"
        state.design_references["UX"] = {"item": record, "evidence": "x"}
        with self.assertRaises(ValueError):
            persisted_reference_instructions(state)


class TestValidateReferenceSynthesis(unittest.TestCase):
    def test_no_references_no_requirement(self):
        validate_reference_synthesis({}, {})

    def test_missing_synthesis_fails_closed(self):
        with self.assertRaises(ValueError):
            validate_reference_synthesis({}, {"UX": object()})

    def test_valid_synthesis_passes(self):
        dna = {"reference_synthesis": {"UX": "interpreted as calm spacing"}}
        validate_reference_synthesis(dna, {"UX": object()})


class TestReferenceIntakeWorkspace(unittest.TestCase):
    """Exercises app.projects.references.ReferenceIntake end-to-end locally."""

    def setUp(self):
        import tempfile
        from app.core.state import ProjectStateStore
        from app.projects.references import ReferenceIntake

        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir) / "state")
        with self.store.acquire_writer("proj-ref-1") as state:
            state.roles["owner"] = "telegram:owner1"
            self.store.save(state)
        self.mock_adapter = MagicMock()
        self.intake = ReferenceIntake(self.store, hermes_adapter=self.mock_adapter)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_upload_stages_normalized_bytes_and_invalidates_directions(self):
        raw = _png() + b"untrusted trailer"
        staged = []
        def extract(files):
            staged.append(files["UX"].read_bytes())
            return {"success": True, "characteristics": {"UX": "spacing"}}
        self.mock_adapter.vision_extract_references.side_effect = extract
        with self.store.acquire_writer("proj-ref-1") as state:
            state.design_directions = [{"label": "old"}]
            state.selected_direction = {"label": "old"}
            self.store.save(state)
        result = self.intake.add_upload("proj-ref-1", raw, "UX", principal_id="telegram:owner1")
        self.assertTrue(result.success)
        state = self.store.load("proj-ref-1")
        self.assertEqual(staged, [normalized_image(raw)])
        self.assertEqual(state.design_references["UX"]["item"]["sha256"], hashlib.sha256(staged[0]).hexdigest())
        self.assertEqual(state.design_directions, [])
        self.assertIsNone(state.selected_direction)

    def test_built_or_active_project_rejected_before_vision(self):
        for lifecycle, revision in [("READY", 1), ("RUNNING", 0), ("PAUSED", 0), ("CANCELED", 0)]:
            with self.store.acquire_writer("proj-ref-1") as state:
                state.lifecycle = lifecycle
                state.revisions.source_revision = revision
                self.store.save(state)
            result = self.intake.add_upload("proj-ref-1", _png(), "UX", principal_id="telegram:owner1")
            self.assertFalse(result.success)
        self.mock_adapter.vision_extract_references.assert_not_called()

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_snapshot_drift_during_vision_rejects_persistence(self):
        def extract(files):
            with self.store.acquire_writer("proj-ref-1") as state:
                state.brief["name"] = "new brief"
                self.store.save(state)
            return {"success": True, "characteristics": {"UX": "spacing"}}
        self.mock_adapter.vision_extract_references.side_effect = extract
        result = self.intake.add_upload("proj-ref-1", _png(), "UX", principal_id="telegram:owner1")
        self.assertEqual(result.error_code, "STALE_REFERENCE_INPUT")
        self.assertEqual(self.store.load("proj-ref-1").design_references, {})

    @unittest.skipUnless(_HAS_PIL, "Pillow not installed in this environment")
    def test_add_upload_persists_evidence(self):
        self.mock_adapter.vision_extract_references.return_value = {
            "success": True,
            "characteristics": {"UX": "simple top nav, generous whitespace"},
        }
        result = self.intake.add_upload(
            "proj-ref-1", _png(), "ux", principal_id="telegram:owner1",
        )
        self.assertTrue(result.success)
        state = self.store.load("proj-ref-1")
        self.assertIn("UX", state.design_references)
        self.assertEqual(
            state.design_references["UX"]["evidence"],
            "simple top nav, generous whitespace",
        )

    def test_add_upload_unauthorized_rejected(self):
        result = self.intake.add_upload(
            "proj-ref-1", _png(), "ux", principal_id="telegram:stranger",
        )
        self.assertFalse(result.success)
        state = self.store.load("proj-ref-1")
        self.assertNotIn("UX", state.design_references)
        self.mock_adapter.vision_extract_references.assert_not_called()

    def test_add_upload_vision_failure_not_persisted(self):
        self.mock_adapter.vision_extract_references.return_value = {
            "success": False, "characteristics": {}, "error": "boom",
        }
        result = self.intake.add_upload(
            "proj-ref-1", _png(), "ux", principal_id="telegram:owner1",
        )
        self.assertFalse(result.success)
        state = self.store.load("proj-ref-1")
        self.assertNotIn("UX", state.design_references)

    def test_add_upload_no_evidence_fails_closed(self):
        self.mock_adapter.vision_extract_references.return_value = {
            "success": True, "characteristics": {},
        }
        result = self.intake.add_upload(
            "proj-ref-1", _png(), "ux", principal_id="telegram:owner1",
        )
        self.assertFalse(result.success)

    def test_add_url_delegates_to_fetch_and_persists(self):
        self.mock_adapter.vision_extract_references.return_value = {
            "success": True,
            "characteristics": {"COLOR": "warm earth tones, high contrast"},
        }
        import app.projects.references as refs_module

        fake_result = OperationResult.ok({
            "item": ReferenceItem("COLOR", "url", "a" * 64, "image/png", 100, "https://example.com/a.png"),
            "bytes": _png(),
        })
        original = refs_module.fetch_reference_url
        refs_module.fetch_reference_url = MagicMock(return_value=fake_result)
        try:
            result = self.intake.add_url(
                "proj-ref-1", "https://example.com/a.png", "color",
                principal_id="telegram:owner1",
            )
        finally:
            refs_module.fetch_reference_url = original
        self.assertTrue(result.success)
        state = self.store.load("proj-ref-1")
        self.assertIn("COLOR", state.design_references)

    def test_add_url_unauthorized_never_fetches(self):
        import app.projects.references as refs_module

        original = refs_module.fetch_reference_url
        refs_module.fetch_reference_url = MagicMock()
        mocked = refs_module.fetch_reference_url
        try:
            result = self.intake.add_url(
                "proj-ref-1", "https://example.com/a.png", "color",
                principal_id="telegram:stranger",
            )
        finally:
            refs_module.fetch_reference_url = original
        self.assertFalse(result.success)
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
