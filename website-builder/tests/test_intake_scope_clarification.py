"""BUG 1 regression: a project can never be parked in WAITING_INPUT without
an actionable clarification question.

The p9 E2E left a project in exactly that broken combination:
``readiness = NEEDS_CLARIFICATION`` with ``clarification_question = None``.
The scope gate blocks a COMPLETE brief (NAME + WHAT + WHY all present) while
the per-field ladder has nothing left to ask, so the project moved to
WAITING_INPUT and Telegram sent nothing -- indistinguishable from a hang.

These tests pin three separate guarantees:

  1. INVARIANT -- ``NEEDS_CLARIFICATION`` always carries a non-empty question,
     for every scope and every brief-completeness combination.
  2. PERSISTENCE -- a WAITING_INPUT project always has the question durably
     recorded, and the durable question is byte-identical to the one sent.
  3. BOUNDED LOOP -- a repeated scope verdict escalates the question instead of
     re-sending the identical one, and NEVER auto-clears the scope gate.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.channels.telegram import NormalizedMessage
from app.core.authz import ProjectAccess
from app.core.intake import (
    CLARIFICATION_GENERIC,
    CLARIFICATION_MISSING_FIELD,
    CLARIFICATION_SCOPE,
    IntakeProcessor,
    IntakeResult,
    Readiness,
    Scope,
)
from app.core.state import ProjectStateStore

PRINCIPAL = "telegram:1"
CHAT = "555"

BLOCKING_SCOPES = (Scope.MIXED, Scope.OUT_OF_SCOPE, Scope.UNCLEAR)
ALL_SCOPES = (Scope.WEBSITE, Scope.WEBSITE_RELATED, *BLOCKING_SCOPES)
COMPLETE_BRIEF = {
    "name": "pokeplay",
    "what": "katalog produk",
    "why": "orang bisa lihat produk dan pilih yang mau dibeli",
}
INCOMPLETE_BRIEFS = (
    {},
    {"name": "pokeplay"},
    {"name": "pokeplay", "what": "katalog produk"},
)


def _store(tmp_path) -> ProjectStateStore:
    store = ProjectStateStore(Path(tmp_path) / "state")
    ProjectAccess(store).create(
        "tg-1", PRINCIPAL, channel="telegram", conversation_id=CHAT,
    )
    return store


def _fast(store, scope, *, question=None, needed=False, name="pokeplay",
          what="katalog produk", why="orang bisa lihat produk"):
    """An IntakeProcessor whose FAST turn returns a fixed verdict."""
    hermes = MagicMock()
    hermes.fast_interpret.return_value = {
        "scope": scope.value,
        "name": name,
        "what": what,
        "why": why,
        "why_destination": None,
        "ambiguity": None,
        # The p9 shape: the brief is complete, so FAST reports no missing
        # field and asks for nothing -- yet scope still blocks.
        "clarification_needed": needed,
        "clarification_question": question,
        "readiness": "DISCOVERY_READY",
        "source": "hermes_fast",
    }
    return IntakeProcessor(store, hermes_adapter=hermes), hermes


def _message(text: str, event_id: str) -> NormalizedMessage:
    return NormalizedMessage(
        user_id="1", conversation_id=CHAT, text=text, event_id=event_id,
    )


# ---------------------------------------------------------------------------
# 1. The invariant
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope", ALL_SCOPES)
@pytest.mark.parametrize("brief", (COMPLETE_BRIEF, *INCOMPLETE_BRIEFS),
                         ids=("complete", "empty", "name", "name+what"))
def test_never_needs_clarification_without_a_question(tmp_path, scope, brief):
    store = _store(tmp_path)
    processor, _ = _fast(store, scope, **brief)
    # Undo the fixture's own kwargs for the varying parts of the brief.
    processor.hermes_adapter.fast_interpret.return_value.update({
        "name": brief.get("name"), "what": brief.get("what"),
        "why": brief.get("why"),
    })

    result = processor.process(
        _message("bikin toko online pokeplay", "u1"), "tg-1",
    )

    if result.readiness == Readiness.NEEDS_CLARIFICATION:
        assert isinstance(result.clarification_question, str)
        assert result.clarification_question.strip(), (
            "NEEDS_CLARIFICATION must always carry an actionable question "
            f"(scope={scope.value})"
        )
        # A question without a reason is unactionable for an operator.
        assert result.clarification_reason
    else:
        assert result.clarification_question is None


@pytest.mark.parametrize("scope", BLOCKING_SCOPES)
def test_complete_brief_with_blocking_scope_asks_a_scope_question(tmp_path, scope):
    """The exact p9 case: NAME + WHAT + WHY complete, scope still blocking."""
    store = _store(tmp_path)
    processor, _ = _fast(store, scope, **COMPLETE_BRIEF)

    result = processor.process(
        _message("bikin toko online pokeplay", "u1"), "tg-1",
    )

    assert result.readiness == Readiness.NEEDS_CLARIFICATION
    assert result.clarification_reason == CLARIFICATION_SCOPE
    assert result.clarification_question
    # The per-field ladder had nothing to ask, so this cannot be a
    # missing-NAME/WHAT/WHY question.
    assert result.clarification_field is None
    # The collected business facts are preserved, not re-asked.
    assert result.brief["name"] == "pokeplay"


def test_mixed_scope_question_is_the_smallest_storefront_vs_transaction_choice(tmp_path):
    """A storefront request must be asked the storefront question -- not a
    generic "tell me more", and never an invented business fact."""
    store = _store(tmp_path)
    processor, _ = _fast(store, Scope.MIXED, **COMPLETE_BRIEF)

    question = processor.process(
        _message("bikin toko online pokeplay", "u1"), "tg-1",
    ).clarification_question

    lowered = question.lower()
    assert "katalog" in lowered
    assert "checkout" in lowered and "payment" in lowered
    assert "login" in lowered
    assert "pokeplay" in question
    # It must not assert which option the user wants, nor invent one.
    assert "kamu mau" not in lowered


def test_out_of_scope_and_unclear_ask_their_own_questions(tmp_path):
    store = _store(tmp_path)
    seen = {}
    for scope in (Scope.OUT_OF_SCOPE, Scope.UNCLEAR):
        processor, _ = _fast(store, scope, **COMPLETE_BRIEF)
        seen[scope] = processor.process(
            _message("bikin something", "u1"), "tg-1",
        ).clarification_question
    assert seen[Scope.OUT_OF_SCOPE] != seen[Scope.UNCLEAR]
    for question in seen.values():
        assert question and question.strip()


def test_fast_question_wins_over_the_scope_template(tmp_path):
    """FAST stays the semantic authority: its own question is preserved."""
    store = _store(tmp_path)
    asked = "Boleh jelasin produk apa yang mau Ditampilkan?"
    processor, _ = _fast(
        store, Scope.MIXED, question=asked, needed=True, **COMPLETE_BRIEF,
    )

    result = processor.process(_message("toko online", "u1"), "tg-1")

    assert result.readiness == Readiness.NEEDS_CLARIFICATION
    assert result.clarification_question == asked


def test_empty_message_still_asks_something(tmp_path):
    store = _store(tmp_path)
    processor = IntakeProcessor(store)

    result = processor.process(
        NormalizedMessage(
            user_id="1", conversation_id=CHAT, text="   ", event_id="blank",
        ), "tg-1",
    )

    assert result.readiness == Readiness.NEEDS_CLARIFICATION
    assert result.clarification_question.strip()
    assert result.clarification_reason == CLARIFICATION_MISSING_FIELD


def test_defensive_generic_question_covers_an_unmapped_scope(tmp_path):
    """A scope with no template must still produce a question, not silence."""
    store = _store(tmp_path)
    processor = IntakeProcessor(store)
    result = IntakeResult(
        readiness=Readiness.NEEDS_CLARIFICATION, scope=Scope.MIXED,
        brief=COMPLETE_BRIEF, clarification_question=None,
    )
    # apply_to_project refuses; process() never emits the empty combination.
    assert result.clarification_question is None
    generic = processor._generic_clarification()
    assert generic.strip()
    assert processor._scope_clarification(Scope.WEBSITE, COMPLETE_BRIEF, 1) is None


# ---------------------------------------------------------------------------
# 2. Persistence: WAITING_INPUT implies a recorded question
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope", BLOCKING_SCOPES)
def test_waiting_input_persists_the_question_it_asked(tmp_path, scope):
    store = _store(tmp_path)
    processor, _ = _fast(store, scope, **COMPLETE_BRIEF)
    result = processor.process(_message("bikin toko online", "u1"), "tg-1")

    processor.apply_to_project("tg-1", result, principal_id=PRINCIPAL, event_id="e1")

    state = store.load("tg-1")
    assert state.lifecycle == "WAITING_INPUT"
    pending = state.pending_clarification
    assert pending["question"] == result.clarification_question
    assert pending["reason"] == CLARIFICATION_SCOPE
    assert pending["scope"] == scope.value
    assert pending["attempt"] == 1
    assert pending["asked_at"] > 0


def test_waiting_input_is_refused_when_the_question_is_empty(tmp_path):
    """Defense in depth: an empty question can never write WAITING_INPUT."""
    store = _store(tmp_path)
    processor, _ = _fast(store, Scope.MIXED, **COMPLETE_BRIEF)
    broken = IntakeResult(
        readiness=Readiness.NEEDS_CLARIFICATION, scope=Scope.MIXED,
        brief=COMPLETE_BRIEF, clarification_question=None,
    )

    processor.apply_to_project("tg-1", broken, principal_id=PRINCIPAL, event_id="e1")

    state = store.load("tg-1")
    assert state.lifecycle == "DISCOVERING"
    assert not state.pending_clarification.get("question")


@pytest.mark.parametrize("question", ("", "   ", None))
def test_blank_questions_never_park_the_project(tmp_path, question):
    store = _store(tmp_path)
    processor, _ = _fast(store, Scope.MIXED, **COMPLETE_BRIEF)
    broken = IntakeResult(
        readiness=Readiness.NEEDS_CLARIFICATION, scope=Scope.MIXED,
        brief=COMPLETE_BRIEF, clarification_question=question,
    )

    processor.apply_to_project("tg-1", broken, principal_id=PRINCIPAL, event_id="e1")

    assert store.load("tg-1").lifecycle == "DISCOVERING"


def test_discovery_ready_clears_a_stale_pending_clarification(tmp_path):
    """A stale question must not survive into the next turn's FAST context."""
    store = _store(tmp_path)
    blocked, _ = _fast(store, Scope.MIXED, **COMPLETE_BRIEF)
    blocked.apply_to_project(
        "tg-1", blocked.process(_message("toko online", "u1"), "tg-1"),
        principal_id=PRINCIPAL, event_id="e1",
    )
    assert store.load("tg-1").pending_clarification

    ready, hermes = _fast(store, Scope.WEBSITE, **COMPLETE_BRIEF)
    result = ready.process(_message("cuma katalog saja", "u2"), "tg-1")
    ready.apply_to_project("tg-1", result, principal_id=PRINCIPAL, event_id="e2")

    state = store.load("tg-1")
    assert state.lifecycle == "READY"
    assert state.pending_clarification == {}
    # The answered question was handed to FAST so the follow-up could be read
    # as the ANSWER instead of a brand-new brief.
    context = hermes.fast_interpret.call_args[0][2]
    assert any("outstanding clarification" in m["content"] for m in context)


# ---------------------------------------------------------------------------
# 3. Bounded loop: escalate, never auto-clear
# ---------------------------------------------------------------------------

def test_repeated_scope_verdict_escalates_the_question(tmp_path):
    store = _store(tmp_path)
    processor, _ = _fast(store, Scope.MIXED, **COMPLETE_BRIEF)

    first = processor.process(_message("bikin toko online", "u1"), "tg-1")
    processor.apply_to_project("tg-1", first, principal_id=PRINCIPAL, event_id="e1")
    second = processor.process(_message("cuma katalog", "u2"), "tg-1")
    processor.apply_to_project("tg-1", second, principal_id=PRINCIPAL, event_id="e2")

    assert first.clarification_attempt == 1
    assert second.clarification_attempt == 2
    assert second.clarification_question != first.clarification_question
    # The re-ask is an explicit choice, not the same open question again.
    assert "1" in second.clarification_question and "2" in second.clarification_question
    state = store.load("tg-1")
    assert state.pending_clarification["attempt"] == 2
    assert state.pending_clarification["question"] == second.clarification_question


def test_scope_gate_is_never_auto_cleared_by_repeats(tmp_path):
    """Bounded means bounded: repeats escalate the question, never the gate."""
    store = _store(tmp_path)
    processor, _ = _fast(store, Scope.OUT_OF_SCOPE, **COMPLETE_BRIEF)
    for i in range(4):
        result = processor.process(_message(f"attempt {i}", f"u{i}"), "tg-1")
        processor.apply_to_project(
            "tg-1", result, principal_id=PRINCIPAL, event_id=f"e{i}",
        )
        assert result.readiness == Readiness.NEEDS_CLARIFICATION
        assert store.load("tg-1").lifecycle == "WAITING_INPUT"
    # A distinct, re-askable question is always available.
    assert store.load("tg-1").pending_clarification["question"].strip()


def test_repeated_missing_field_repeat_count_tracks_the_same_question(tmp_path):
    store = _store(tmp_path)
    processor, _ = _fast(store, Scope.WEBSITE, name="pokeplay", what=None, why=None)
    seen = []
    for i in range(3):
        result = processor.process(_message(f"pokeplay {i}", f"u{i}"), "tg-1")
        processor.apply_to_project(
            "tg-1", result, principal_id=PRINCIPAL, event_id=f"e{i}",
        )
        seen.append(result.clarification_attempt)
    assert seen == [1, 2, 3]
    assert store.load("tg-1").pending_clarification["reason"] == \
        CLARIFICATION_MISSING_FIELD
    assert store.load("tg-1").pending_clarification["field"] == "what"
