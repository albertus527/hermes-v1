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

    def process(
        self, message: NormalizedMessage, project_id: Optional[str] = None
    ) -> IntakeResult:
        """Process a normalized message and return intake result.

        The application owns state transitions. FAST interprets; code enforces.
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

        # Use Hermes FAST for semantic interpretation when available.
        # If FAST fails (raises), the application executes the deterministic
        # fallback — FAST owns interpretation, never state-transition authority.
        if self.hermes_adapter is not None:
            try:
                fast_result = self.hermes_adapter.fast_interpret(text, project_id)
            except Exception:
                fast_result = None

            if fast_result is not None:
                scope = Scope(fast_result.get("scope", "UNCLEAR"))
                brief = {
                    "name": fast_result.get("name"),
                    "what": fast_result.get("what"),
                    "why": fast_result.get("why"),
                    "why_destination": fast_result.get("why_destination"),
                }
                readiness_str = fast_result.get("readiness", "NEEDS_CLARIFICATION")
                readiness = Readiness(readiness_str) if readiness_str in Readiness.__members__ else Readiness.NEEDS_CLARIFICATION
                clarification_question = fast_result.get("clarification_question")
            else:
                # Deterministic fallback after FAST failure (bounded, safe)
                scope = self._fallback_scope(text)
                brief = self._fallback_extract(text)
                readiness = self._fallback_readiness(scope, brief)
                clarification_question = self._fallback_clarification(brief)
        else:
            # Deterministic fallback (bounded, safe)
            scope = self._fallback_scope(text)
            brief = self._fallback_extract(text)
            readiness = self._fallback_readiness(scope, brief)
            clarification_question = self._fallback_clarification(brief)

        # Application enforces pause/resume regardless of FAST result
        if pause_detected:
            readiness = Readiness.PAUSED
        elif resume_detected:
            readiness = Readiness.RESUMED

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

            # Update lifecycle based on readiness through lifecycle authority
            if result.readiness == Readiness.DISCOVERY_READY:
                state.revisions.requirements_version += 1
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.READY)
            elif result.readiness == Readiness.NEEDS_CLARIFICATION:
                if state.lifecycle == ProjectLifecycle.DISCOVERING.value:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.WAITING_INPUT)

            if event_id is not None:
                state.processed_events.add(event_id)
            self.store.save(state)
