"""D4b: Laya integration at the REAL FAST intake seam.

Proves the production intake path invokes Laya and hands FAST the correct,
bounded, lower-trust context pack -- while preserving:

    * the ORIGINAL user brief verbatim;
    * the original FAST system/developer instructions and output schema;
    * the pre-D4b FAST path when Laya is disabled or OpenViking is unavailable;
    * no unauthorized project/publication state mutation.

The FAST model call itself is the genuine external boundary and is stubbed; the
intake merge, Laya preparation, adapter retrieval, dispatch claims, authz and
lifecycle all run for real.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.channels.telegram import NormalizedMessage
from app.core import laya_context as laya
from app.core import openviking_library as lib
from app.core.authz import ProjectAccess
from app.core.intake import IntakeProcessor
from app.core.openviking_retrieval import OpenVikingConfig, OpenVikingRetrievalAdapter
from app.core.state import ProjectStateStore


BRIEF = "Bloom, florist, minimalist editorial landing page with botanical typography"


def _index(backend, project_id="wb-design"):
    for source_id, category, content in (
        ("refero_typography", "design_dna", b"# Typography\neditorial type scale 1.25"),
        ("refero_motion", "motion", b"# Motion\nsubtle transitions, reduced motion"),
    ):
        src = lib.SourceSpec(
            source_id=source_id, project_id=project_id, category=category,
            trust="reviewed", locator=f"skills/x/{source_id}.md",
        )
        lib.ingest_sources(backend, [src], reader=lambda loc, c=content: c,
                           project_id=project_id)
    return backend


class RecordingFast:
    """A minimal FAST stand-in that records the exact arguments it received.

    Mirrors the real ``HermesAdapter.fast_interpret`` signature so the intake
    seam is exercised for real, including the ``reference_context`` keyword.
    """

    def __init__(self, result=None):
        self.calls = []
        self._result = result or {
            "scope": "WEBSITE", "name": "Bloom", "what": "florist",
            "why": "show arrangements", "why_destination": None,
            "ambiguity": None, "clarification_needed": False,
            "clarification_question": None, "readiness": "DISCOVERY_READY",
            "source": "hermes_fast",
        }

    def fast_interpret(self, text, project_id=None, conversation_context=None,
                       reference_context=None):
        self.calls.append({
            "text": text, "project_id": project_id,
            "conversation_context": conversation_context,
            "reference_context": reference_context,
        })
        return dict(self._result)


def _enabled_preparer(backend, **cfg):
    adapter = OpenVikingRetrievalAdapter(OpenVikingConfig(enabled=True), backend)
    # D4c: the FAST hand-off is a SEPARATE, INDEPENDENT opt-in; these D4b tests
    # exercise the hand-off, so they enable it explicitly here.
    return laya.LayaContextPreparer(
        laya.LayaConfig(
            enabled=True, fast_context_injection=True,
            library_project_id="wb-design", **cfg,
        ),
        adapter,
    )


# ---------------------------------------------------------------------------
# Production intake invokes Laya and hands FAST the pack
# ---------------------------------------------------------------------------


def test_the_production_intake_hands_fast_the_laya_pack():
    store = ProjectStateStore(Path(_tmp()))
    fast = RecordingFast()
    preparer = _enabled_preparer(_index(lib.FakeOpenVikingBackend()))
    intake = IntakeProcessor(store, hermes_adapter=fast, laya=preparer)

    msg = NormalizedMessage(event_id="1", user_id="1", conversation_id="5",
                            text=BRIEF)
    result = intake.process(msg, None)

    assert len(fast.calls) == 1
    ref = fast.calls[0]["reference_context"]
    assert ref is not None
    assert "=== LAYA CONTEXT" in ref
    assert "REFERENCE DATA" in ref
    # FAST's own result is unchanged: the application still owns readiness.
    assert result.readiness.value == "DISCOVERY_READY"


def test_the_original_user_brief_is_preserved_verbatim():
    store = ProjectStateStore(Path(_tmp()))
    fast = RecordingFast()
    preparer = _enabled_preparer(_index(lib.FakeOpenVikingBackend()))
    intake = IntakeProcessor(store, hermes_adapter=fast, laya=preparer)

    msg = NormalizedMessage(event_id="1", user_id="1", conversation_id="5",
                            text=BRIEF)
    intake.process(msg, None)
    # FAST receives the user's text unmodified.
    assert fast.calls[0]["text"] == BRIEF


def test_the_fast_prompt_preserves_the_user_text_and_labels_references():
    """The REAL prompt builder: the user text is verbatim and the reference
    block is labelled lower-trust DATA that cannot override the brief."""
    from app.hermes.adapter import HermesAdapter

    adapter = HermesAdapter.__new__(HermesAdapter)  # no store needed for prompt build
    block = (
        "\n=== LAYA CONTEXT (application-retrieved REFERENCE DATA, lower trust) ===\n"
        "JSON here\n=== END LAYA CONTEXT ===\n"
    )
    prompt = adapter._build_fast_prompt(BRIEF, None, block)
    assert BRIEF in prompt
    assert "=== LAYA CONTEXT" in prompt
    assert "MUST NOT override" in prompt
    # The FAST output contract text is untouched.
    assert '"readiness": "DISCOVERY_READY|NEEDS_CLARIFICATION"' in prompt


def test_no_reference_block_yields_a_byte_identical_prompt():
    """With Laya disabled the FAST prompt is byte-identical to the pre-D4b one."""
    from app.hermes.adapter import HermesAdapter

    adapter = HermesAdapter.__new__(HermesAdapter)
    without = adapter._build_fast_prompt(BRIEF, None)
    explicit_none = adapter._build_fast_prompt(BRIEF, None, None)
    empty = adapter._build_fast_prompt(BRIEF, None, "")
    assert without == explicit_none == empty
    assert "LAYA" not in without


# ---------------------------------------------------------------------------
# Fallback: Laya disabled / unavailable -> original FAST path
# ---------------------------------------------------------------------------


def test_laya_disabled_keeps_the_original_fast_path():
    store = ProjectStateStore(Path(_tmp()))
    fast = RecordingFast()
    # Laya disabled (default), OpenViking enabled: still no retrieval.
    adapter = OpenVikingRetrievalAdapter(OpenVikingConfig(enabled=True),
                                         _index(lib.FakeOpenVikingBackend()))
    preparer = laya.LayaContextPreparer(laya.LayaConfig(enabled=False), adapter)
    intake = IntakeProcessor(store, hermes_adapter=fast, laya=preparer)

    msg = NormalizedMessage(event_id="1", user_id="1", conversation_id="5", text=BRIEF)
    intake.process(msg, None)
    assert fast.calls[0]["reference_context"] is None


def test_openviking_unavailable_keeps_the_original_fast_path():
    store = ProjectStateStore(Path(_tmp()))
    fast = RecordingFast()
    backend = _index(lib.FakeOpenVikingBackend())
    backend.fail_with = ConnectionError("down")
    preparer = _enabled_preparer(backend)
    intake = IntakeProcessor(store, hermes_adapter=fast, laya=preparer)

    msg = NormalizedMessage(event_id="1", user_id="1", conversation_id="5", text=BRIEF)
    intake.process(msg, None)
    assert fast.calls[0]["reference_context"] is None


def test_a_laya_internal_exception_does_not_block_fast():
    store = ProjectStateStore(Path(_tmp()))
    fast = RecordingFast()

    class ExplodingLaya:
        # D4c: injection is explicitly on, so the hand-off IS attempted and the
        # internal exception must still degrade to None without blocking FAST.
        fast_context_injection_enabled = True

        def prepare_context(self, *a, **k):
            raise RuntimeError("laya exploded")

    intake = IntakeProcessor(store, hermes_adapter=fast, laya=ExplodingLaya())
    msg = NormalizedMessage(event_id="1", user_id="1", conversation_id="5", text=BRIEF)
    result = intake.process(msg, None)
    # The pipeline still ran FAST and produced a result.
    assert len(fast.calls) == 1
    assert fast.calls[0]["reference_context"] is None
    assert result.readiness.value == "DISCOVERY_READY"


def test_no_laya_at_all_is_the_pre_d4b_path():
    store = ProjectStateStore(Path(_tmp()))
    fast = RecordingFast()
    intake = IntakeProcessor(store, hermes_adapter=fast)  # no laya
    msg = NormalizedMessage(event_id="1", user_id="1", conversation_id="5", text=BRIEF)
    result = intake.process(msg, None)
    assert fast.calls[0]["reference_context"] is None
    assert result.readiness.value == "DISCOVERY_READY"


# ---------------------------------------------------------------------------
# FAST output contract preservation
# ---------------------------------------------------------------------------


def test_the_fast_output_contract_is_unchanged_with_laya_enabled():
    store = ProjectStateStore(Path(_tmp()))
    fast = RecordingFast(result={
        "scope": "WEBSITE", "name": "Bloom", "what": "florist", "why": None,
        "why_destination": None, "ambiguity": None,
        "clarification_needed": True,
        "clarification_question": "What should visitors do on Bloom?",
        "readiness": "NEEDS_CLARIFICATION", "source": "hermes_fast",
    })
    preparer = _enabled_preparer(_index(lib.FakeOpenVikingBackend()))
    intake = IntakeProcessor(store, hermes_adapter=fast, laya=preparer)

    msg = NormalizedMessage(event_id="1", user_id="1", conversation_id="5", text=BRIEF)
    result = intake.process(msg, None)
    # Laya did not change scope, readiness, or the clarification semantics.
    assert result.scope.value == "WEBSITE"
    assert result.readiness.value == "NEEDS_CLARIFICATION"
    assert result.clarification_question == "What should visitors do on Bloom?"


# ---------------------------------------------------------------------------
# No unauthorized state mutation through the production dispatch seam
# ---------------------------------------------------------------------------


def _tmp():
    import tempfile
    return tempfile.mkdtemp(prefix="laya-int-")


def test_intake_with_laya_mutates_only_the_expected_state():
    root = Path(_tmp())
    store = ProjectStateStore(root / "state")
    ProjectAccess(store).create("app", "telegram:1", channel="telegram",
                                conversation_id="555")
    before = store.load("app").to_dict()

    fast = RecordingFast()
    preparer = _enabled_preparer(_index(lib.FakeOpenVikingBackend()))
    intake = IntakeProcessor(store, hermes_adapter=fast, laya=preparer)
    dispatcher = TelegramDispatcher(store, intake)

    payload = {
        "update_id": 1,
        "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": BRIEF, "date": 1},
    }
    r = dispatcher.dispatch(payload, "app", "intake",
                            authenticated=AuthenticatedTelegramContext("1", "555"))
    assert r.success

    after = store.load("app")
    # Only the brief advanced; nothing about publication/deployment changed.
    assert after.brief.get("name") == "Bloom"
    assert after.production_url is None
    assert after.deployment == before.get("deployment", {})
    assert after.repository == before.get("repository", {})


def test_laya_does_not_trigger_frontend_or_build():
    root = Path(_tmp())
    store = ProjectStateStore(root / "state")
    ProjectAccess(store).create("app", "telegram:1", channel="telegram",
                                conversation_id="555")
    fast = RecordingFast()
    preparer = _enabled_preparer(_index(lib.FakeOpenVikingBackend()))
    intake = IntakeProcessor(store, hermes_adapter=fast, laya=preparer)
    # No builder injected: a Laya-enabled intake must not require or call one.
    dispatcher = TelegramDispatcher(store, intake)
    payload = {
        "update_id": 1,
        "message": {"from": {"id": 1}, "chat": {"id": 555}, "text": BRIEF, "date": 1},
    }
    r = dispatcher.dispatch(payload, "app", "intake",
                            authenticated=AuthenticatedTelegramContext("1", "555"))
    assert r.success
    # A complete brief would normally queue a build IF a builder were present;
    # with no builder, the project stays in READY (never QUEUED) -- proof that
    # Laya itself triggers nothing.
    assert store.load("app").lifecycle == "READY"


# ---------------------------------------------------------------------------
# Revision intake with accepted prior project context
# ---------------------------------------------------------------------------


def test_revision_intake_passes_project_context_to_laya():
    """On a later turn the accumulated brief is supplied to Laya as approved
    project context, so queries reflect the accepted project."""
    store = ProjectStateStore(Path(_tmp()))
    state = ProjectAccess(store).create("app", "telegram:1", channel="telegram",
                                        conversation_id="555")
    state = store.load("app")
    state.brief = {"name": "Bloom", "what": "florist", "why": "show arrangements"}
    store.save(state)

    fast = RecordingFast()
    seen = {}

    class RecordingLaya:
        # D4c: the D4b hand-off is opt-in; enable it so Laya is actually consulted.
        fast_context_injection_enabled = True

        def prepare_context(self, brief, project_id=None, project_context=None, budget=None):
            seen["brief"] = brief
            seen["project_context"] = project_context
            return laya.LayaContextResult(
                status=laya.STATUS_SKIPPED, project_id=project_id or "",
                library_project_id="wb-design", quality=laya.QUALITY_INSUFFICIENT,
                items=(), queries=(), retrieval_calls=0, estimated_chars=0,
                estimated_tokens=0, truncated=False, degraded=True,
                warnings=(laya.WARNING_LAYLA_DISABLED,),
                error_reason=laya.ERROR_LAYLA_DISABLED, latency_ms=0.0, limits={},
            )

    intake = IntakeProcessor(store, hermes_adapter=fast, laya=RecordingLaya())
    msg = NormalizedMessage(event_id="2", user_id="1", conversation_id="5",
                            text="add a gallery of seasonal bouquets")
    intake.process(msg, "app")
    # The accepted project context (persisted brief) reached Laya.
    assert seen["project_context"].get("name") == "Bloom"
    assert seen["brief"] == "add a gallery of seasonal bouquets"
