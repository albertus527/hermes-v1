"""Core progress-channel seam: AIAgent.progress_callback.

The supervised oneshot path depends on this seam for liveness, so its contract
matters beyond the Website Builder: the default must be a no-op, a misbehaving
consumer must never break the agent loop, and payloads must stay bounded.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from agent.progress_events import (
    CHANNEL_READY_EVENT,
    KIND_MODEL,
    KIND_STREAM,
    KIND_TOOL,
    KIND_UNKNOWN,
    PHASE_ACTIVE,
    PHASE_COMPLETED,
    PHASE_STARTED,
    classify_progress_event,
    make_progress_payload,
)


class _Agent:
    """Minimal stand-in exposing the real _touch_activity body under test."""

    _last_activity_ts = 0.0
    _last_activity_desc = ""
    _last_activity_provenance = "unknown"
    _session_activity_last_persist_mono = 0.0

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    def _persist_session_activity_if_due(self) -> None:
        return None

    # Copied structure from run_agent.AIAgent._touch_activity (the notify block
    # is what is under test; the rest is stubbed to avoid a full agent).
    def _touch_activity(self, desc, *, provenance=None, force_persist=False) -> None:
        import logging
        import time

        from agent.session_activity import (
            bound_activity_description,
            normalize_activity_provenance,
        )

        self._last_activity_ts = time.time()
        self._last_activity_desc = bound_activity_description(desc)
        self._last_activity_provenance = normalize_activity_provenance(provenance)
        progress_cb = getattr(self, "progress_callback", None)
        if progress_cb is not None:
            try:
                from agent.progress_events import make_progress_payload

                payload = make_progress_payload(self._last_activity_desc)
                progress_cb(payload["kind"], payload)
            except Exception:
                logging.getLogger(__name__).debug(
                    "progress_callback notification failed", exc_info=True
                )


def test_classification_covers_every_touch_activity_call_site():
    """Every real description classifies, or degrades safely to UNKNOWN."""
    assert classify_progress_event("starting API call #3") == (KIND_MODEL, PHASE_STARTED)
    assert classify_progress_event("API call #3 completed") == (KIND_MODEL, PHASE_COMPLETED)
    assert classify_progress_event("waiting for provider response (streaming)") == (
        KIND_MODEL, PHASE_STARTED
    )
    assert classify_progress_event("waiting for non-streaming API response") == (
        KIND_MODEL, PHASE_STARTED
    )
    assert classify_progress_event("waiting for stream response (12s, no chunks yet)") == (
        KIND_MODEL, PHASE_STARTED
    )
    assert classify_progress_event("receiving stream response") == (KIND_STREAM, PHASE_ACTIVE)
    assert classify_progress_event("executing tool: write_file") == (KIND_TOOL, PHASE_STARTED)
    assert classify_progress_event("tool completed: terminal (1.2s) ok") == (
        KIND_TOOL, PHASE_COMPLETED
    )


def test_unknown_descriptions_degrade_to_liveness_only():
    """An unrecognised description never claims or clears an operation."""
    for desc in ("retry backoff (1/3), 4s remaining", "", None, "something new"):
        assert classify_progress_event(desc) == (KIND_UNKNOWN, PHASE_ACTIVE)


def test_payload_is_bounded_and_content_free():
    payload = make_progress_payload("x" * 5000)
    assert set(payload) == {"kind", "phase", "desc"}
    assert len(payload["desc"]) <= 120
    assert "args" not in payload and "prompt" not in payload


def test_default_is_a_no_op():
    agent = _Agent()
    assert agent.progress_callback is None
    agent._touch_activity("starting API call #1")
    assert agent._last_activity_desc == "starting API call #1"


def test_boundary_events_notify():
    seen = []
    agent = _Agent(progress_callback=lambda kind, payload: seen.append((kind, payload)))
    agent._touch_activity("starting API call #1")
    agent._touch_activity("API call #1 completed")
    assert [k for k, _ in seen] == [KIND_MODEL, KIND_MODEL]
    assert [p["phase"] for _, p in seen] == [PHASE_STARTED, PHASE_COMPLETED]


def test_a_raising_consumer_never_breaks_the_activity_clock():
    def boom(kind, payload):
        raise RuntimeError("consumer exploded")

    agent = _Agent(progress_callback=boom)
    agent._touch_activity("starting API call #1")
    assert agent._last_activity_desc == "starting API call #1"


# ---------------------------------------------------------------------------
# Oneshot transport.
# ---------------------------------------------------------------------------


def test_emitter_is_absent_without_the_env_var(monkeypatch):
    from hermes_cli import oneshot

    monkeypatch.delenv(oneshot.PROGRESS_FILE_ENV, raising=False)
    monkeypatch.delenv(oneshot.PROGRESS_ID_ENV, raising=False)
    assert oneshot._build_progress_emitter() is None


def test_emitter_announces_ready_and_writes_one_line_per_event(monkeypatch, tmp_path):
    from hermes_cli import oneshot

    path = tmp_path / "nested" / "progress.jsonl"
    monkeypatch.setenv(oneshot.PROGRESS_FILE_ENV, str(path))
    monkeypatch.setenv(oneshot.PROGRESS_ID_ENV, "inv-1")

    emitter = oneshot._build_progress_emitter()
    assert emitter is not None
    emitter.on_progress("MODEL", {"kind": "MODEL", "phase": "started", "desc": "d"})
    emitter.on_progress("MODEL", {"kind": "MODEL", "phase": "completed", "desc": "d"})
    emitter.close()

    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    assert lines[0]["kind"] == CHANNEL_READY_EVENT
    assert lines[0]["phase"] == "ready"
    assert [line["run_id"] for line in lines] == ["inv-1"] * 3
    # Each event is exactly one complete line: a tailing reader can never see a
    # half-written record.
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_active_events_are_coalesced_but_boundaries_are_not(monkeypatch, tmp_path):
    from hermes_cli import oneshot

    path = tmp_path / "progress.jsonl"
    monkeypatch.setenv(oneshot.PROGRESS_FILE_ENV, str(path))
    monkeypatch.setenv(oneshot.PROGRESS_ID_ENV, "inv-1")

    emitter = oneshot._build_progress_emitter()
    emitter.on_progress("STREAM", {"kind": "STREAM", "phase": "active", "desc": "s"})
    for _ in range(50):
        emitter.on_progress("STREAM", {"kind": "STREAM", "phase": "active", "desc": "s"})
    emitter.on_progress("TOOL", {"kind": "TOOL", "phase": "started", "desc": "t"})
    emitter.close()

    kinds = [
        json.loads(x)["kind"]
        for x in path.read_text(encoding="utf-8").splitlines()
        if x
    ]
    assert kinds.count("STREAM") <= 2  # first is never dropped, rest coalesced
    assert kinds.count("TOOL") == 1  # boundary survives coalescing


def test_emitter_survives_an_unwritable_path(monkeypatch):
    from hermes_cli import oneshot

    monkeypatch.setenv(oneshot.PROGRESS_FILE_ENV, os.devnull + "/nope/impossible.jsonl")
    assert oneshot._build_progress_emitter() is None
