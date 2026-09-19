"""Intake processing for Website Builder R1.

Handles pause/resume, scope enforcement, and NAME/WHAT/WHY extraction.
FAST is a logical role using existing Hermes/9Router — no new agent.
Application code remains authoritative over resulting state transitions.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from app.channels.telegram import NormalizedMessage
from app.core.authz import require_mutating_role
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore


class Scope(str, Enum):
    WEBSITE = "WEBSITE"
    WEBSITE_RELATED = "WEBSITE_RELATED"
    MIXED = "MIXED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNCLEAR = "UNCLEAR"


class Readiness(str, Enum):
    DISCOVERY_READY = "DISCOVERY_READY"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    PAUSED = "PAUSED"
    RESUMED = "RESUMED"


@dataclass
class IntakeResult:
    """Result of processing an intake message."""

    readiness: Readiness
    scope: Scope
    brief: Dict[str, Any]
    clarification_question: Optional[str] = None
    pause_detected: bool = False
    resume_detected: bool = False


# Explicit pause/resume phrases (Indonesian/English)
_PAUSE_PHRASES = {
    "eh bentar",
    "tunggu dulu",
    "bentar",
    "tunggu",
    "wait",
    "hold on",
    "pause",
    "stop dulu",
    "jangan dulu",
}

_RESUME_PHRASES = {
    "lanjut",
    "continue",
    "go ahead",
    "gas",
    "lanjutkan",
    "oke lanjut",
    "resume",
}


def _contains_phrase(text: str, phrases: set) -> bool:
    lower = text.lower().strip()
    for phrase in phrases:
        if phrase in lower:
            return True
    return False


class IntakeProcessor:
    """Processes normalized messages through scope, pause/resume, and requirement gates.

    Uses Hermes FAST for semantic interpretation when available.
    Falls back to deterministic heuristics only when Hermes is unavailable.
    Application code owns all state transitions.
    """

    def __init__(self, store: ProjectStateStore, hermes_adapter=None):
        self.store = store
        self.hermes_adapter = hermes_adapter

    # Brief fields the multi-turn merge accumulates. Order matters: the
    # smallest sufficient website brief is NAME + WHAT + WHY.
    _BRIEF_FIELDS = ("name", "what", "why", "why_destination")

    def _persisted_brief(self, project_id: Optional[str]) -> Dict[str, Any]:
        """Return the brief already collected for this project.

        Multi-turn accumulation authority is the STORED brief, never the
        current turn in isolation: NAME collected on turn 1 must survive a
        turn that only answers WHAT. Returns {} when there is no project yet.

        Only a genuinely missing/invalid project ID is treated as "no brief
        yet". A real load failure (corrupt state file, I/O error) must NOT be
        silently swallowed into an empty brief -- that would silently erase
        already-accumulated NAME/WHAT/WHY for this turn. Such failures
        propagate to the caller (the runtime's per-update try/except logs and
        skips rather than persisting on top of an unreadable state).
        """
        if not project_id:
            return {}
        state = self.store.load(project_id)
        return dict(state.brief) if state is not None else {}

    def _context_messages(self, persisted: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
        """Prior accumulated brief, handed to FAST as conversation context.

        The prompt gets the already-collected values so a follow-up turn is
        interpreted as the answer to the outstanding clarification rather
        than as a brand-new brief.
        """
        known = [f"{f}={persisted[f]}" for f in self._BRIEF_FIELDS if persisted.get(f)]
        if not known:
            return None
        return [{"role": "assistant", "content": "Known brief so far: " + ", ".join(known)}]

    def _merge_brief(
        self, persisted: Dict[str, Any], extracted: Dict[str, Any],
        used_fallback: bool = False,
    ) -> Dict[str, Any]:
        """Merge this turn's interpretation into the accumulated brief.

        Preserves already-collected values. FAST is handed the accumulated
        brief as context (see ``_context_messages``) and is trusted to label
        NAME/WHAT/WHY correctly for the turn it actually saw -- FAST's field
        assignment is semantic authority and is applied as-is. Only the
        deterministic fallback extractor (used_fallback=True), which has no
        real semantic understanding and always maps a lone segment to
        "name", needs the shift-into-next-missing-field heuristic so a bare
        one-word answer to an outstanding WHAT/WHY question doesn't clobber
        an already-established NAME with a stray token.
        """
        merged = {f: (persisted or {}).get(f) for f in self._BRIEF_FIELDS}
        only_name = (
            used_fallback
            and bool(extracted.get("name"))
            and not extracted.get("what")
            and not extracted.get("why")
        )
        if only_name and merged.get("name"):
            if not merged.get("what"):
                merged["what"] = extracted["name"]
            elif not merged.get("why"):
                merged["why"] = extracted["name"]
        else:
            for field in ("name", "what", "why"):
                if extracted.get(field):
                    merged[field] = extracted[field]
        if extracted.get("why_destination"):
            merged["why_destination"] = extracted["why_destination"]
        return merged

    def _readiness_for(self, scope: Scope, brief: Dict[str, Any]) -> Readiness:
        """Deterministic readiness from the ACCUMULATED brief + scope.

        Application code owns this gate; FAST only interprets text.
        """
        if scope in (Scope.OUT_OF_SCOPE, Scope.MIXED, Scope.UNCLEAR):
            return Readiness.NEEDS_CLARIFICATION
        if all(brief.get(f) for f in ("name", "what", "why")):
            return Readiness.DISCOVERY_READY
        return Readiness.NEEDS_CLARIFICATION

    def process(
        self, message: NormalizedMessage, project_id: Optional[str] = None
    ) -> IntakeResult:
        """Process a normalized message and return intake result.

        The application owns state transitions. FAST interprets; code enforces.
        The accumulated (persisted) brief is merged with this turn's
        interpretation, so NAME/WHAT/WHY collected across turns are preserved
        and readiness is computed on the merged brief.
        """
        text = message.text.strip()
        if not text:
            return IntakeResult(
                readiness=Readiness.NEEDS_CLARIFICATION,
                scope=Scope.UNCLEAR,
                brief={},
                clarification_question="Please describe the website you want.",
            )

        # Pause/resume detection (deterministic, application-owned)
        pause_detected = _contains_phrase(text, _PAUSE_PHRASES)
        resume_detected = _contains_phrase(text, _RESUME_PHRASES)

        persisted = self._persisted_brief(project_id)

        # Use Hermes FAST for semantic interpretation when available, handing
        # it the accumulated brief as context. If FAST fails (raises), the
        # application executes the deterministic fallback — FAST owns
        # interpretation, never state-transition authority.
        used_fallback = False
        fast_ambiguity_question: Optional[str] = None
        if self.hermes_adapter is not None:
            try:
                fast_result = self.hermes_adapter.fast_interpret(
                    text, project_id, self._context_messages(persisted)
                )
            except Exception:
                fast_result = None

            if fast_result is not None:
                scope = Scope(fast_result.get("scope", "UNCLEAR"))
                extracted = {
                    "name": fast_result.get("name"),
                    "what": fast_result.get("what"),
                    "why": fast_result.get("why"),
                    "why_destination": fast_result.get("why_destination"),
                }
                # FAST owns ambiguity/correction semantics: when it flags a
                # material clarification need with its own question, that
                # question is preserved rather than silently replaced by the
                # generic per-field fallback prompt.
                if fast_result.get("clarification_needed") and fast_result.get(
                    "clarification_question"
                ):
                    fast_ambiguity_question = fast_result["clarification_question"]
            else:
                # Deterministic fallback after FAST failure (bounded, safe)
                used_fallback = True
                scope = self._fallback_scope(text)
                extracted = self._fallback_extract(text)
        else:
            # Deterministic fallback (bounded, safe)
            used_fallback = True
            scope = self._fallback_scope(text)
            extracted = self._fallback_extract(text)

        brief = self._merge_brief(persisted, extracted, used_fallback=used_fallback)
        readiness = self._readiness_for(scope, brief)

        # The clarification question is derived from the ACCUMULATED brief —
        # the smallest question that resolves the still-missing field —
        # unless FAST flagged a specific material ambiguity/correction for
        # THIS turn, in which case FAST's own question is authoritative.
        if readiness == Readiness.DISCOVERY_READY:
            clarification_question = None
        elif fast_ambiguity_question:
            clarification_question = fast_ambiguity_question
        else:
            clarification_question = self._fallback_clarification(brief)

        # Application enforces pause/resume regardless of FAST result
        if pause_detected:
            readiness = Readiness.PAUSED
            clarification_question = None
        elif resume_detected:
            readiness = Readiness.RESUMED
            clarification_question = None

        return IntakeResult(
            readiness=readiness,
            scope=scope,
            brief=brief,
            clarification_question=clarification_question,
            pause_detected=pause_detected,
            resume_detected=resume_detected,
        )

    def _fallback_scope(self, text: str) -> Scope:
        """Deterministic scope fallback. Only used when Hermes is unavailable."""
        lower = text.lower()
        website_keywords = {"website", "web", "site", "landing", "page", "portfolio",
                           "company profile", "barbershop", "restaurant", "cafe",
                           "wedding", "event", "saas", "product", "gallery", "blog"}
        out_keywords = {"app", "mobile", "desktop", "game", "trading", "bot",
                       "erp", "crm", "database", "cms", "admin panel",
                       "shopping cart", "payment", "booking engine"}

        has_website = any(kw in lower for kw in website_keywords)
        has_out = any(kw in lower for kw in out_keywords)

        if has_website and has_out:
            return Scope.MIXED
        if has_out:
            return Scope.OUT_OF_SCOPE
        if has_website:
            return Scope.WEBSITE
        return Scope.UNCLEAR

    def _fallback_extract(self, text: str) -> Dict[str, Optional[str]]:
        """Deterministic extraction fallback. Never fabricates business facts."""
        result: Dict[str, Optional[str]] = {"name": None, "what": None, "why": None, "why_destination": None}

        parts = re.split(r"[,.]\s*", text.strip())
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) >= 1:
            first = parts[0]
            for prefix in ["bikin", "buat", "create", "build", "make", "website", "web"]:
                if first.lower().startswith(prefix):
                    first = first[len(prefix):].strip()
            if first:
                result["name"] = first

        if len(parts) >= 2:
            result["what"] = parts[1]

        # Purpose conjunctions that introduce WHY and must be stripped so the
        # stored purpose is the clean actionable phrase (e.g. "orang booking
        # WA", not "biar orang booking WA").
        why_patterns = [
            r"biar\s+(.+)",
            r"supaya\s+(.+)",
            r"agar\s+(.+)",
            r"so that\s+(.+)",
            r"to\s+(.+)",
            r"for\s+(.+)",
        ]

        if len(parts) >= 3:
            why = parts[2]
            # Strip a leading purpose conjunction from the third segment.
            for pattern in why_patterns:
                match = re.fullmatch(pattern, why.strip(), re.IGNORECASE)
                if match:
                    why = match.group(1).strip()
                    break
            result["why"] = why
        else:
            # Extract WHY from a purpose clause anywhere in the text — but
            # NEVER fabricate a destination URL.
            for pattern in why_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    result["why"] = match.group(1).strip()
                    break

        # why_destination is only set when an explicit URL/phone is present
        url_match = re.search(r"https?://[^\s]+", text)
        phone_match = re.search(r"\+\d{10,15}", text)
        if url_match:
            result["why_destination"] = url_match.group(0)
        elif phone_match:
            result["why_destination"] = phone_match.group(0)

        return result

    def _fallback_readiness(self, scope: Scope, brief: Dict[str, Optional[str]]) -> Readiness:
        if scope in (Scope.OUT_OF_SCOPE, Scope.MIXED, Scope.UNCLEAR):
            return Readiness.NEEDS_CLARIFICATION
        if all([brief.get("name"), brief.get("what"), brief.get("why")]):
            return Readiness.DISCOVERY_READY
        return Readiness.NEEDS_CLARIFICATION

    def _fallback_clarification(self, brief: Dict[str, Optional[str]]) -> Optional[str]:
        if not brief.get("name"):
            return "What is the name of the website or business?"
        if not brief.get("what"):
            return f"What is {brief['name']}?"
        if not brief.get("why"):
            return f"What should visitors primarily understand or do on {brief['name']}?"
        return None

    def apply_to_project(self, project_id: str, result: IntakeResult,
                         principal_id=None, event_id=None) -> None:
        """Apply intake result to project state. Application owns transitions."""
        with self.store.acquire_writer(project_id) as state:
            require_mutating_role(state, principal_id)
            if event_id is not None and event_id in state.processed_events:
                return
            # Update brief if we have new information
            if result.brief.get("name"):
                state.brief["name"] = result.brief["name"]
            if result.brief.get("what"):
                state.brief["what"] = result.brief["what"]
            if result.brief.get("why"):
                state.brief["why"] = result.brief["why"]
            if result.brief.get("why_destination"):
                state.brief["why_destination"] = result.brief["why_destination"]

            # Handle pause/resume through lifecycle authority
            if result.pause_detected:
                state.pause_state["paused"] = True
                state.pause_state["paused_at"] = time.time()
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.PAUSED)
            elif result.resume_detected:
                state.pause_state["paused"] = False
                state.pause_state["resumed_at"] = time.time()
                # Resume to DISCOVERING — the canonical resume target
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.DISCOVERING)

            # Update lifecycle based on readiness through lifecycle authority.
            if result.readiness == Readiness.DISCOVERY_READY:
                # requirements_version counts every accepted completed brief,
                # including legitimate post-READY corrections — preserve that
                # semantics so corrections stay observable.
                state.revisions.requirements_version += 1
                # READY -> READY is intentionally invalid in lifecycle.py.
                # When the project is already READY, a fresh semantically
                # complete intake must SAFELY REMAIN READY rather than
                # attempting a no-op transition through the authority (which
                # would raise LifecycleError and strand the dispatch claim in
                # CLAIMED, surfacing as EVENT_RECONCILIATION_REQUIRED on the
                # next event). The lifecycle state machine itself is NOT
                # weakened — the intake layer simply recognizes that READY is
                # already correct.
                if state.lifecycle != ProjectLifecycle.READY.value:
                    if (
                        state.lifecycle == ProjectLifecycle.FAILED.value
                        and state.revisions.qa_revision == 0
                        and not state.deployment.get("latest_shown_preview")
                        and state.revisions.approved_revision == 0
                        and not state.deployment.get("live_url")
                    ):
                        # Initial build failure recovery:
                        # Reset source_revision to 0 and clear failure so auto-build
                        # can trigger cleanly under the canonical
                        # (lifecycle == READY and source_revision == 0) contract.
                        # Preserves brief, project_id, conversation_id, requirements_version.
                        # Never resets if the project has ever shown a preview, reached QA success,
                        # or been live in production.
                        state.revisions.source_revision = 0
                        state.failure = None
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.READY)
            elif result.readiness == Readiness.NEEDS_CLARIFICATION:
                if state.lifecycle == ProjectLifecycle.DISCOVERING.value:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.WAITING_INPUT)

            if event_id is not None:
                state.processed_events.add(event_id)
            self.store.save(state)
