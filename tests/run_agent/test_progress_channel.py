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
    description_advances,
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
    def _touch_activity(
        self, desc, *, provenance=None, force_persist=False, advance=None
    ) -> None:
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

                payload = make_progress_payload(
                    self._last_activity_desc, advance=advance
                )
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
    assert set(payload) == {"kind", "phase", "advance", "desc"}
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
# advance: observable activity vs. forward progress.
#
# Fail-safe direction: an unrecognised description, an empty one, or an absent
# ``advance`` all ADVANCE. Only positively-identified keep-alive text, or an
# explicit ``advance=False`` at a call site, demotes an event.
# ---------------------------------------------------------------------------

#: Every Group A keep-alive description, each emitted by a timer/heartbeat and
#: never by a genuine boundary. Encoded as a table so a new keep-alive site
#: cannot be added without a matching assertion.
GROUP_A_KEEPALIVE_DESCRIPTIONS = (
    "waiting for stream response (0s, no chunks yet)",
    "sequential tool running (30s): terminal",
    "concurrent tools running (60s, 2 remaining: read_file, terminal)",
    "retry backoff (1/3), 4s remaining",
    "error retry backoff (2/5), 8s remaining",
    "empty response retry backoff (1/2), 3s remaining",
    "stale stream detected after 180s, reconnecting",
    "stale non-streaming call killed after 900s",
    "codex stream killed after 120s with no first byte",
    "codex stream killed after 120s with no SSE events",
    "stream retry 1/3 after RemoteProtocolError",
)


@pytest.mark.parametrize("desc", GROUP_A_KEEPALIVE_DESCRIPTIONS)
def test_group_a_keepalive_descriptions_do_not_advance_by_default(desc):
    assert description_advances(desc) is False
    assert make_progress_payload(desc)["advance"] is False
    # Still observable: the kind classification is untouched by the split.
    assert make_progress_payload(desc)["kind"] in {KIND_MODEL, KIND_UNKNOWN}


@pytest.mark.parametrize(
    "desc",
    [
        "starting API call #3",
        "API call #3 completed",
        "waiting for provider response (streaming)",
        "executing tool: write_file",
        "tool completed: terminal (1.2s) ok",
        "receiving stream response",
        "",
        None,
        "a description nobody has seen yet",
    ],
)
def test_genuine_and_unrecognised_descriptions_advance_by_default(desc):
    assert description_advances(desc) is True
    assert make_progress_payload(desc)["advance"] is True


def test_ambiguous_description_is_not_classified_by_the_default_table():
    """"waiting for non-streaming API response" is a boundary AND a heartbeat.

    The identical text is the genuine request start and the 15s direct-API
    heartbeat, so it must NOT be in the keep-alive table: a description-keyed
    rule cannot tell the two call sites apart. The heartbeat call site is what
    separates them, by passing ``advance=False`` explicitly.
    """
    ambiguous = "waiting for non-streaming API response"
    assert description_advances(ambiguous) is True
    assert make_progress_payload(ambiguous)["advance"] is True
    # The genuine request boundary keeps the default.
    seen = []
    agent = _Agent(progress_callback=lambda kind, payload: seen.append(payload))
    agent._touch_activity(ambiguous)
    assert seen[-1]["advance"] is True
    # The heartbeat call site splits it.
    agent._touch_activity(ambiguous, advance=False)
    assert seen[-1]["advance"] is False


def test_explicit_advance_wins_over_the_default_table():
    desc = "waiting for stream response (0s, no chunks yet)"
    assert make_progress_payload(desc, advance=True)["advance"] is True
    assert make_progress_payload("starting API call #1", advance=False)["advance"] is False


def test_a_keepalive_still_stamps_the_activity_clock_and_notifies():
    """Demotion is about the supervisor's progress timer, not about liveness."""
    seen = []
    agent = _Agent(progress_callback=lambda kind, payload: seen.append(payload))
    before = agent._last_activity_ts
    agent._touch_activity("retry backoff (1/3), 4s remaining", advance=False)

    assert agent._last_activity_ts > before
    assert agent._last_activity_desc == "retry backoff (1/3), 4s remaining"
    assert len(seen) == 1
    assert seen[0]["advance"] is False


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


def test_emitter_writes_the_advance_bit_and_defaults_it_to_true(monkeypatch, tmp_path):
    from hermes_cli import oneshot

    path = tmp_path / "progress.jsonl"
    monkeypatch.setenv(oneshot.PROGRESS_FILE_ENV, str(path))
    monkeypatch.setenv(oneshot.PROGRESS_ID_ENV, "inv-1")

    emitter = oneshot._build_progress_emitter()
    emitter.on_progress(
        "MODEL",
        {"kind": "MODEL", "phase": "started", "desc": "d", "advance": False},
    )
    # An older producer's payload carries no key at all.
    emitter.on_progress("MODEL", {"kind": "MODEL", "phase": "completed", "desc": "d"})
    emitter.close()

    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    # The handshake is a capability announcement, not progress.
    assert lines[0]["advance"] is True
    assert lines[1]["advance"] is False
    # Absent means True so a supervisor driving an older child behaves as before.
    assert lines[2]["advance"] is True
