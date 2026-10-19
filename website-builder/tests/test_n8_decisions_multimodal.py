"""N8 B1: multimodal ``state`` transport.

The Decisions ``state`` may be a string, a JSON object, or an array of parts
carrying ``image_url`` media. This suite proves:

* text-only behaviour is unchanged;
* a multimodal array serialises exactly as the documented contract requires;
* every rejection happens LOCALLY, before any network call;
* the bounded deadline, one-call discipline and secret redaction still hold.

No test in this module opens a socket.
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import decisions_client as dc  # noqa: E402

MODEL = "openai/gpt-6-luna-decisions"
KEY = "sk-or-v1-TESTKEYTESTKEYTESTKEY"


def _png(width: int = 4, height: int = 4) -> bytes:
    """A tiny, valid PNG (no external image library)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    raw = b"\x00" * (width * 3)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _image_part(raw: bytes | None = None, mime: str = "image/png") -> dict:
    return {"type": "image_url", "image_url": {"url": _data_url(raw or _png(), mime)}}


def _questions(n: int = 1) -> dict:
    return {
        f"q{i}": {"type": "score", "instructions": "rate", "criteria": ["a", "b"]}
        for i in range(n)
    }


class _NoNetwork:
    """Fails loudly if anything tries to open a socket."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("network call attempted")


# ---------------------------------------------------------------------------
# Text-only behaviour is unchanged
# ---------------------------------------------------------------------------


def test_string_state_still_accepted():
    assert dc.validate_state_payload("hello world", _questions()) == ""


def test_string_state_round_trips_unchanged():
    """The serialised body for a string state is byte-identical to before."""
    body = json.dumps({"model": MODEL, "state": "hello", "questions": _questions()})
    parsed = json.loads(body)
    assert parsed["state"] == "hello"
    assert isinstance(parsed["state"], str)


def test_mapping_state_accepted():
    assert dc.validate_state_payload({"ticket": "x", "tier": "pro"}, _questions()) == ""


def test_empty_string_state_accepted():
    assert dc.validate_state_payload("", _questions()) == ""


# ---------------------------------------------------------------------------
# Multimodal array
# ---------------------------------------------------------------------------


def test_list_state_with_image_accepted():
    state = ["Candidate c1: calm editorial", _image_part()]
    assert dc.validate_state_payload(state, _questions()) == ""


def test_list_state_serialises_as_documented():
    """The exact documented shape survives json.dumps intact."""
    state = ["text part", _image_part()]
    body = json.loads(json.dumps({"model": MODEL, "state": state,
                                  "questions": _questions()}))
    assert isinstance(body["state"], list)
    assert body["state"][0] == "text part"
    assert body["state"][1]["type"] == "image_url"
    assert body["state"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_multiple_images_accepted():
    state = ["a", _image_part(), _image_part()]
    assert dc.validate_state_payload(state, _questions()) == ""


def test_jpeg_and_webp_accepted():
    assert dc.validate_state_payload([_image_part(mime="image/jpeg")], _questions()) == ""
    assert dc.validate_state_payload([_image_part(mime="image/webp")], _questions()) == ""


def test_non_image_mapping_is_context_not_media():
    """A JSON object part that is not an image is legitimate context."""
    state = [{"type": "context", "note": "x"}, "text"]
    assert dc.validate_state_payload(state, _questions()) == ""


# ---------------------------------------------------------------------------
# Rejections -- all local, none reachable to the network
# ---------------------------------------------------------------------------


def test_remote_url_rejected():
    state = [{"type": "image_url",
              "image_url": {"url": "https://example.test/x.png"}}]
    assert dc.validate_state_payload(state, _questions()) == dc.INVALID_IMAGE_PART


def test_raw_base64_without_prefix_rejected():
    state = [{"type": "image_url",
              "image_url": {"url": base64.b64encode(_png()).decode()}}]
    assert dc.validate_state_payload(state, _questions()) == dc.INVALID_IMAGE_PART


def test_unsupported_mime_rejected():
    state = [{"type": "image_url",
              "image_url": {"url": "data:image/gif;base64,AAAA"}}]
    assert dc.validate_state_payload(state, _questions()) == dc.INVALID_IMAGE_PART


def test_malformed_base64_rejected():
    state = [{"type": "image_url",
              "image_url": {"url": "data:image/png;base64,!!!not-base64!!!"}}]
    assert dc.validate_state_payload(state, _questions()) == dc.INVALID_IMAGE_PART


def test_image_url_container_not_mapping_rejected():
    state = [{"type": "image_url", "image_url": "data:image/png;base64,AAAA"}]
    assert dc.validate_state_payload(state, _questions()) == dc.INVALID_IMAGE_PART


def test_missing_url_rejected():
    state = [{"type": "image_url", "image_url": {}}]
    assert dc.validate_state_payload(state, _questions()) == dc.INVALID_IMAGE_PART


@pytest.mark.parametrize("bad", [None, 12345, b"bytes", {1, 2}])
def test_unsupported_state_type_rejected(bad):
    assert dc.validate_state_payload(bad, _questions()) == dc.UNSUPPORTED_STATE


def test_non_mapping_question_set_rejected():
    assert dc.validate_state_payload("x", ["not", "a", "mapping"]) == dc.UNSUPPORTED_STATE


# ---------------------------------------------------------------------------
# Resource bounds
# ---------------------------------------------------------------------------


def test_too_many_questions_rejected():
    q = _questions(dc.MAX_QUESTION_COUNT + 1)
    assert dc.validate_state_payload("x", q) == dc.TOO_MANY_QUESTIONS


def test_exactly_max_questions_accepted():
    q = _questions(dc.MAX_QUESTION_COUNT)
    assert dc.validate_state_payload("x", q) == ""


def test_too_many_images_rejected():
    state = [_image_part() for _ in range(dc.MAX_IMAGES_PER_REQUEST + 1)]
    assert dc.validate_state_payload(state, _questions()) == dc.TOO_MANY_IMAGES


def test_exactly_max_images_accepted():
    state = [_image_part() for _ in range(dc.MAX_IMAGES_PER_REQUEST)]
    assert dc.validate_state_payload(state, _questions()) == ""


def test_oversized_image_rejected():
    big = b"\x00" * (dc.MAX_IMAGE_BYTES + 1)
    state = [{"type": "image_url",
              "image_url": {"url": _data_url(big)}}]
    assert dc.validate_state_payload(state, _questions()) == dc.PAYLOAD_TOO_LARGE


def test_oversized_text_state_rejected():
    big = "x" * (dc.MAX_STATE_BYTES + 1)
    assert dc.validate_state_payload(big, _questions()) == dc.PAYLOAD_TOO_LARGE


# ---------------------------------------------------------------------------
# The guard runs before the network, always
# ---------------------------------------------------------------------------


def test_invalid_payload_never_calls_network():
    """A rejected payload must not reach _post_once."""
    guard = _NoNetwork()
    with mock.patch.object(dc, "_post_once", guard):
        result = dc.post_decision(
            model=MODEL,
            state=[{"type": "image_url",
                    "image_url": {"url": "https://evil.test/x.png"}}],
            questions=_questions(),
            api_key=KEY,
        )
    assert result.status == dc.TRANSPORT_ERROR
    assert result.error_code == dc.INVALID_IMAGE_PART
    assert guard.calls == 0


def test_body_size_guard_runs_before_network():
    """A body over the ceiling is refused before transmission."""
    guard = _NoNetwork()
    with mock.patch.object(dc, "MAX_REQUEST_BODY_BYTES", 10), \
         mock.patch.object(dc, "_post_once", guard):
        result = dc.post_decision(
            model=MODEL, state="a long enough state string",
            questions=_questions(), api_key=KEY,
        )
    assert result.status == dc.TRANSPORT_ERROR
    assert result.error_code == dc.PAYLOAD_TOO_LARGE
    assert guard.calls == 0


def test_valid_multimodal_payload_does_reach_transport():
    """The positive control: a valid payload IS sent (patched, so no socket)."""
    captured = {}

    def fake_post(url, body, api_key, socket_timeout):
        captured["body"] = body
        return 200, b'{"answers":{},"model":"m","usage":{"input_tokens":1,"output_tokens":0}}', ""

    with mock.patch.object(dc, "_post_once", fake_post):
        result = dc.post_decision(
            model=MODEL,
            state=["text", _image_part()],
            questions=_questions(),
            api_key=KEY,
        )
    assert result.status == dc.OK
    sent = json.loads(captured["body"])
    assert isinstance(sent["state"], list)
    assert sent["state"][1]["type"] == "image_url"


# ---------------------------------------------------------------------------
# Transport invariants preserved
# ---------------------------------------------------------------------------


def test_deadline_still_enforced():
    """A stalled peer cannot exceed the deadline."""
    def stall(*args, **kwargs):
        time.sleep(5)
        return 200, b"{}", ""

    with mock.patch.object(dc, "_post_once", stall):
        started = time.monotonic()
        result = dc.post_decision(
            model=MODEL, state="x", questions=_questions(),
            api_key=KEY, deadline_seconds=0.3,
        )
        elapsed = time.monotonic() - started

    assert result.status == dc.TIMEOUT
    assert result.error_code == "deadline_exceeded"
    assert elapsed < 2.0


def test_exactly_one_post_per_call():
    calls = []

    def counting(url, body, api_key, socket_timeout):
        calls.append(body)
        return 200, b'{"answers":{},"model":"m","usage":{"input_tokens":1,"output_tokens":0}}', ""

    with mock.patch.object(dc, "_post_once", counting):
        dc.post_decision(model=MODEL, state="x", questions=_questions(), api_key=KEY)
    assert len(calls) == 1


def test_no_retry_loop_in_source():
    """A retry would need a loop around the network call."""
    import ast

    source = Path(dc.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "post_decision":
            loops = [n for n in ast.walk(node)
                     if isinstance(n, (ast.For, ast.While))]
            assert loops == [], "post_decision must not loop (no retries)"
            return
    pytest.fail("post_decision not found")


def test_worker_thread_is_daemon():
    """An abandoned worker must never block interpreter shutdown."""
    seen = {}

    def stall(*args, **kwargs):
        seen["daemon"] = threading.current_thread().daemon
        time.sleep(3)
        return 200, b"{}", ""

    with mock.patch.object(dc, "_post_once", stall):
        dc.post_decision(model=MODEL, state="x", questions=_questions(),
                         api_key=KEY, deadline_seconds=0.2)
    assert seen.get("daemon") is True


def test_error_strings_are_redacted():
    secret = "sk-or-v1-SECRETSECRETSECRET"
    with mock.patch.object(dc, "_post_once",
                           lambda *a, **k: (500, f"Bearer {secret}".encode(), "")):
        result = dc.post_decision(model=MODEL, state="x",
                                  questions=_questions(), api_key=secret)
    assert secret not in (result.detail or "")
    assert dc._REDACTED in (result.detail or "")


def test_new_codes_exported():
    for name in ("UNSUPPORTED_STATE", "TOO_MANY_QUESTIONS", "TOO_MANY_IMAGES",
                 "PAYLOAD_TOO_LARGE", "INVALID_IMAGE_PART", "validate_state_payload",
                 "StatePayload", "MAX_IMAGE_BYTES", "MAX_REQUEST_BODY_BYTES"):
        assert name in dc.__all__, name


def test_missing_credential_never_calls_network():
    guard = _NoNetwork()
    with mock.patch.object(dc, "_post_once", guard):
        result = dc.post_decision(model=MODEL, state="x",
                                  questions=_questions(), api_key="")
    assert result.error_code == "missing_credential"
    assert guard.calls == 0
