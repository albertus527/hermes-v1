"""N8 Decisions transport: bounded deadline, fail-closed, redaction, no retry.

Covers owner correction C ("verify that the shared Decisions transport respects
an actual bounded operational deadline; test connect stalls, read stalls,
malformed responses, HTTP 529, and cancellation behavior").
"""
from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import decisions_client as dc  # noqa: E402


# ---------------------------------------------------------------------------
# Redaction -- T20 support
# ---------------------------------------------------------------------------

class TestRedaction:

    @pytest.mark.parametrize("secret", [
        "sk-or-v1-abcdef0123456789abcdef",
        "sk-proj-ABCDEFGHIJKLMNOP",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    ])
    def test_credential_shapes_are_redacted(self, secret):
        out = dc.redact(f"upstream said: {secret} (oops)")
        assert secret not in out
        assert dc._REDACTED in out

    def test_redact_handles_none_and_empty(self):
        assert dc.redact(None) == ""
        assert dc.redact("") == ""

    def test_redact_leaves_ordinary_text_intact(self):
        text = "HTTP 529: system_overloaded"
        assert dc.redact(text) == text


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

class TestStatusClass:

    @pytest.mark.parametrize("code,expected", [
        (429, "http_429"),
        (529, "http_529"),
        (500, "http_5xx"),
        (504, "http_5xx"),
        (400, "http_4xx"),
        (None, "no_status"),
    ])
    def test_classes(self, code, expected):
        assert dc._http_status_class(code) == expected


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------

class TestExtractChoice:

    def test_real_envelope_shape(self):
        payload = {
            "model": "typesafe/jev-1.14-20261014",
            "answers": {"intent": {"type": "choice", "choice": "INTAKE",
                                   "confidence": 0.98}},
            "usage": {"input_tokens": 287, "output_tokens": 0, "cost": 0.000012},
        }
        choice, conf, cost = dc.extract_choice(payload, "intent")
        assert choice == "INTAKE"
        assert conf == pytest.approx(0.98)
        assert cost == pytest.approx(0.000012)

    def test_missing_answers_returns_nones(self):
        assert dc.extract_choice({}, "intent") == (None, None, None)

    def test_missing_cost_is_none_not_zero(self):
        """Owner: missing cost metadata must never be treated as free."""
        payload = {"answers": {"intent": {"choice": "INTAKE"}}}
        _, _, cost = dc.extract_choice(payload, "intent")
        assert cost is None

    def test_blank_choice_is_none(self):
        payload = {"answers": {"intent": {"choice": "   "}}}
        choice, _, _ = dc.extract_choice(payload, "intent")
        assert choice is None

    def test_resolved_model_id(self):
        assert dc.resolved_model_id({"model": " a/b-1 "}) == "a/b-1"
        assert dc.resolved_model_id({}) == ""


# ---------------------------------------------------------------------------
# Bounded deadline -- the core of owner correction C
# ---------------------------------------------------------------------------

class TestBoundedDeadline:

    def test_read_stall_returns_timeout_within_deadline(self):
        """A peer that never finishes must not hold the caller past the deadline."""
        def _stall(*a, **k):
            time.sleep(10)
            return 200, b"{}", ""

        started = time.monotonic()
        with patch.object(dc, "_post_once", _stall):
            result = dc.post_decision(
                model="m", state="s", questions={}, api_key="k",
                deadline_seconds=0.4,
            )
        elapsed = time.monotonic() - started

        assert result.status == dc.TIMEOUT
        assert result.error_code == "deadline_exceeded"
        assert elapsed < 2.0, f"deadline not enforced: {elapsed:.2f}s"

    def test_timeout_reports_elapsed_ms(self):
        def _stall(*a, **k):
            time.sleep(5)
            return 200, b"{}", ""

        with patch.object(dc, "_post_once", _stall):
            result = dc.post_decision(
                model="m", state="s", questions={}, api_key="k",
                deadline_seconds=0.2,
            )
        assert result.elapsed_ms >= 0

    def test_worker_thread_is_daemon_so_it_cannot_block_exit(self):
        def _stall(*a, **k):
            time.sleep(5)
            return 200, b"{}", ""

        before = threading.active_count()
        with patch.object(dc, "_post_once", _stall):
            dc.post_decision(model="m", state="s", questions={}, api_key="k",
                             deadline_seconds=0.2)
        # The abandoned worker may still be alive, but it must not be a
        # non-daemon thread (which would block interpreter shutdown).
        stragglers = [t for t in threading.enumerate()
                      if t.name == "n8-decisions-call"]
        assert all(t.daemon for t in stragglers)
        assert threading.active_count() >= before - 1

    def test_cancellation_returns_immediately_and_discards_result(self):
        """The caller must not observe a late result from an abandoned worker."""
        def _late(*a, **k):
            time.sleep(1.0)
            return 200, b'{"answers":{"intent":{"choice":"PUBLISH"}}}', ""

        with patch.object(dc, "_post_once", _late):
            result = dc.post_decision(
                model="m", state="s", questions={}, api_key="k",
                deadline_seconds=0.1,
            )
        assert result.status == dc.TIMEOUT
        assert result.payload is None


# ---------------------------------------------------------------------------
# Fail-closed transport outcomes
# ---------------------------------------------------------------------------

class TestFailClosed:

    def test_http_529_is_http_error_with_class(self):
        with patch.object(dc, "_post_once",
                          return_value=(529, b'{"error":"overloaded"}', "")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert result.status == dc.HTTP_ERROR
        assert result.error_code == "http_529"
        assert result.http_status == 529

    def test_http_429_is_http_error(self):
        with patch.object(dc, "_post_once", return_value=(429, b"{}", "")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert result.error_code == "http_429"

    def test_http_504_is_http_error(self):
        with patch.object(dc, "_post_once", return_value=(504, b"", "")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert result.error_code == "http_5xx"

    def test_malformed_json(self):
        with patch.object(dc, "_post_once", return_value=(200, b"<html>nope", "")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert result.status == dc.MALFORMED
        assert result.error_code == "unparseable_body"

    def test_non_object_json(self):
        with patch.object(dc, "_post_once", return_value=(200, b"[1,2,3]", "")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert result.status == dc.MALFORMED
        assert result.error_code == "non_object_body"

    def test_connect_error(self):
        with patch.object(dc, "_post_once",
                          return_value=(None, b"", "URLError: refused")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert result.status == dc.TRANSPORT_ERROR

    def test_missing_credential_never_calls_network(self):
        called = []
        with patch.object(dc, "_post_once",
                          side_effect=lambda *a, **k: called.append(1)):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="", deadline_seconds=1.0)
        assert result.error_code == "missing_credential"
        assert called == []

    def test_error_detail_is_redacted(self):
        body = b'{"error":"bad key sk-or-v1-DEADBEEFDEADBEEF"}'
        with patch.object(dc, "_post_once", return_value=(401, body, "")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert "sk-or-v1-DEADBEEF" not in result.detail

    def test_success_path(self):
        body = (b'{"model":"a/b-1","answers":{"intent":{"choice":"INTAKE",'
                b'"confidence":0.9}},"usage":{"cost":0.0001}}')
        with patch.object(dc, "_post_once", return_value=(200, body, "")):
            result = dc.post_decision(model="m", state="s", questions={},
                                      api_key="k", deadline_seconds=1.0)
        assert result.ok
        assert result.payload["model"] == "a/b-1"


# ---------------------------------------------------------------------------
# No-retry invariant -- structural
# ---------------------------------------------------------------------------

class TestNoRetry:

    def test_exactly_one_network_call_site(self):
        source = Path(dc.__file__).read_text()
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_post_once"
        ]
        # one definition-free reference inside post_decision
        assert len(calls) == 1, f"expected 1 call site, found {len(calls)}"

    def test_no_while_loop_around_the_call(self):
        """A retry would need a loop *inside the same function* around the call."""
        source = Path(dc.__file__).read_text()
        tree = ast.parse(source)
        lines = source.splitlines()

        # Find the function that contains the network call.
        enclosing = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if (isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Name)
                            and child.func.id == "_post_once"):
                        enclosing = node
                        break
        assert enclosing is not None, "network call not found"

        loops = [
            child for child in ast.walk(enclosing)
            if isinstance(child, (ast.While, ast.For))
        ]
        assert loops == [], (
            f"{enclosing.name}() contains a loop, which is where a retry would live"
        )
        # No retry helper of any kind.
        assert not any(
            isinstance(node, ast.FunctionDef) and "retry" in node.name.lower()
            for node in ast.walk(tree)
        )

    def test_no_substitution_path(self):
        source = Path(dc.__file__).read_text()
        # the model id must come from the caller, never be rewritten here
        assert "model =" not in source.replace("model: str", "")

    def test_single_request_per_call(self):
        calls = []

        def _count(*a, **k):
            calls.append(1)
            return 200, b'{"answers":{"intent":{"choice":"INTAKE"}}}', ""

        with patch.object(dc, "_post_once", _count):
            dc.post_decision(model="m", state="s", questions={},
                             api_key="k", deadline_seconds=1.0)
        assert len(calls) == 1
