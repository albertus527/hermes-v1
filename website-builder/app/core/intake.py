"""Intake processing for Website Builder R1.

Handles pause/resume, scope enforcement, and NAME/WHAT/WHY extraction.
FAST is a logical role using existing Hermes/9Router — no new agent.
Application code remains authoritative over resulting state transitions.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from app.channels.telegram import NormalizedMessage
from app.core.authz import require_mutating_role
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore

logger = logging.getLogger(__name__)


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


# Why a clarification is being asked. MISSING_FIELD is the per-field NAME /
# WHAT / WHY ladder; SCOPE is the scope gate (MIXED / OUT_OF_SCOPE / UNCLEAR)
# asking for the smallest question that resolves the scope; GENERIC is the
# bounded last-resort question that exists only so the
# "NEEDS_CLARIFICATION implies an actionable question" invariant can never be
# violated.
CLARIFICATION_MISSING_FIELD = "MISSING_FIELD"
CLARIFICATION_SCOPE = "SCOPE"
CLARIFICATION_GENERIC = "GENERIC"


@dataclass
class IntakeResult:
    """Result of processing an intake message."""

    readiness: Readiness
    scope: Scope
    brief: Dict[str, Any]
    clarification_question: Optional[str] = None
    pause_detected: bool = False
    resume_detected: bool = False
    # Why a clarification is being asked (see the CLARIFICATION_* constants).
    # None whenever no clarification is outstanding.
    clarification_reason: Optional[str] = None
    # The brief field a MISSING_FIELD clarification is about, when known.
    clarification_field: Optional[str] = None
    # 1 for the first ask of this question, 2+ when the same question has to be
    # re-asked. Bounded by the templates in ``_scope_clarification``; never
    # used to auto-clear the scope gate.
    clarification_attempt: int = 1


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
        # Word-boundary match so a pause/resume keyword does not fire on a
        # larger word that merely contains it — e.g. "wait" inside "waiting",
        # "gas" inside "gasifikasi", or "pause" inside "paused". Multi-word
        # phrases ("hold on", "stop dulu") still match as a bounded unit.
        if re.search(r"\b" + re.escape(phrase) + r"\b", lower):
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

    def _persisted_pause_state(self, project_id: Optional[str]) -> Dict[str, Any]:
        """Return the persisted pause_state dict for this project ({} if none).

        Same load-failure contract as _persisted_brief: a genuinely missing
        project yields {}, but a real load failure propagates rather than
        being silently treated as "not paused".
        """
        if not project_id:
            return {}
        state = self.store.load(project_id)
        return dict(state.pause_state) if state is not None else {}

    def _persisted_pending_clarification(self, project_id: Optional[str]) -> Dict[str, Any]:
        """Return the outstanding clarification for this project ({} if none).

        Same load-failure contract as _persisted_brief: only a genuinely
        missing project yields {}, a real load failure propagates.
        """
        if not project_id:
            return {}
        state = self.store.load(project_id)
        return dict(getattr(state, "pending_clarification", None) or {}) if state is not None else {}

    def _context_messages(self, persisted: Dict[str, Any],
                         pending: Optional[Dict[str, Any]] = None) -> Optional[List[Dict[str, str]]]:
        """Prior accumulated brief + the outstanding clarification, handed to FAST
        as conversation context.

        The prompt gets the already-collected values so a follow-up turn is
        interpreted as the answer to the outstanding clarification rather
        than as a brand-new brief. The outstanding question itself is included
        because it is the strongest signal that a follow-up sentence is an
        ANSWER rather than a new brief: without it, a turn that plainly
        answers "cuma katalog, tidak ada checkout" can be re-read as a
        brand-new, commerce-flavoured request and re-classified MIXED forever.
        """
        parts = [f"{f}={persisted[f]}" for f in self._BRIEF_FIELDS if persisted.get(f)]
        if pending and pending.get("question"):
            parts.append(f"outstanding clarification={pending['question']}")
        if not parts:
            return None
        return [{"role": "assistant", "content": "Known brief so far: " + ", ".join(parts)}]

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

    @staticmethod
    def _missing_brief_field(brief: Dict[str, Any]) -> Optional[str]:
        """The first genuinely missing NAME / WHAT / WHY, or None when the
        accumulated brief is complete. Same order the questions are asked in.
        """
        for field in ("name", "what", "why"):
            if not brief.get(field):
                return field
        return None

    def _scope_clarification(self, scope: Scope, brief: Dict[str, Any],
                             attempt: int = 1) -> Optional[str]:
        """The smallest scope-specific question for a blocked scope.

        Only used when NAME + WHAT + WHY are all present yet scope is still
        MIXED / OUT_OF_SCOPE / UNCLEAR: in that state the per-field ladder has
        nothing left to ask, but the user still has to be told what the blocker
        actually is, otherwise the project sits in WAITING_INPUT and Telegram
        sends nothing at all.

        Deterministic templates keyed by the blocking scope, built only from
        the collected NAME. No business fact is ever invented or assumed: the
        MIXED question asks the user to CHOOSE between a display-only site and
        transactional features, it never asserts which one they wanted.

        ``attempt`` above 1 escalates to an explicit numbered choice so a
        repeated scope verdict is answered with a 1/2 instead of another open
        question. The scope gate is never cleared by this: MIXED / OUT_OF_SCOPE
        keeps failing closed no matter how many times the user is asked.
        """
        name = str(brief.get("name") or "").strip()
        subject = name or "website ini"
        repeat = attempt > 1
        if scope is Scope.MIXED:
            if repeat:
                return (
                    f"{subject} ini butuh konfirmasi satu hal. Jawab 1 atau 2 ya:\n"
                    "1. Cuma katalog/tampilan produk dengan tombol beli/pesan, "
                    "tanpa checkout, payment, login, database, atau backend transaksi.\n"
                    "2. Perlu fitur transaksi (checkout/pembayaran/login/database) — "
                    "itu belum bisa aku buat."
                )
            return (
                f"Sebelum lanjut, aku mau pastikan dulu: {subject} ini hanya "
                "katalog/tampilan produk dengan tombol beli atau pesan (tanpa "
                "checkout, payment, login, database, atau backend transaksi), "
                "atau memang perlu fitur transaksi juga? Jawab 1 untuk katalog "
                "saja, 2 untuk butuh transaksi."
            )
        if scope is Scope.OUT_OF_SCOPE:
            if repeat:
                return (
                    "Aku baru bisa bikin website statis. Dari yang kamu sebut, "
                    "bagian mana yang mau dijadikan website? Jawab dengan 1 atau 2 "
                    "supaya aku lanjut."
                )
            return (
                f"Permintaan itu kayaknya bukan website biasa. Dari yang kamu "
                f"sebut, bagian mana yang mau dijadiin website {subject}? "
                "Balikin aja dengan bahasa yang kamu pakai."
            )
        if scope is Scope.UNCLEAR:
            if repeat:
                return (
                    "Aku masih belum nangkep mauannya. Jawab 1 atau 2:\n"
                    f"1. Website {subject} untuk introduce/ibtaro, tanpa fitur transaksi.\n"
                    "2. Website yang butuh transaksi (checkout/pembayaran/login/database)."
                )
            return (
                f"Bisa jelasin singkat ga, {subject} ini buat siapa dan tujuannya "
                "apa? Cukup 1-2 kalimat aja."
            )
        return None

    def _generic_clarification(self) -> str:
        """Last-resort bounded question.

        Exists so the invariant
        ``readiness == NEEDS_CLARIFICATION => an actionable question exists``
        holds for EVERY input, including a scope the template table does not
        cover. It asks for the WHAT/WHY in the user's own words and therefore
        invents nothing.
        """
        return (
            "Boleh jelasin singkat website ini isinya apa dan tujuannya apa? "
            "Cukup 1-2 kalimat aja."
        )

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
                clarification_reason=CLARIFICATION_MISSING_FIELD,
                clarification_field="what",
            )

        # Pause/resume detection (deterministic, application-owned)
        pause_detected = _contains_phrase(text, _PAUSE_PHRASES)
        resume_detected = _contains_phrase(text, _RESUME_PHRASES)

        persisted = self._persisted_brief(project_id)
        pending_clarification = self._persisted_pending_clarification(project_id)

        # Resume is only meaningful when the project is actually paused. A
        # resume phrase on a non-paused project (e.g. "gas" / "lanjut" typed
        # while DISCOVERING) must NOT force readiness=RESUMED — that would
        # suppress the clarification question the user still needs to answer.
        # Read the persisted pause flag up-front so the readiness override
        # below can gate on it.
        paused_now = bool(self._persisted_pause_state(project_id).get("paused"))
        effective_resume = resume_detected and paused_now

        # Use Hermes FAST for semantic interpretation when available, handing
        # it the accumulated brief as context. If FAST fails (raises), the
        # application executes the deterministic fallback — FAST owns
        # interpretation, never state-transition authority.
        used_fallback = False
        fast_ambiguity_question: Optional[str] = None
        if self.hermes_adapter is not None:
            try:
                fast_result = self.hermes_adapter.fast_interpret(
                    text, project_id,
                    self._context_messages(persisted, pending_clarification),
                )
            except Exception:
                fast_result = None

            if fast_result is not None:
                # fast_interpret() does NOT raise on Hermes failure — it returns
                # the deterministic fallback dict tagged source="fallback_heuristic".
                # Treat that as a fallback turn too, so the merge below applies the
                # shift-into-next-missing-field protection and a lone fallback token
                # doesn't clobber an already-established NAME.
                if fast_result.get("source") == "fallback_heuristic":
                    used_fallback = True
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
        clarification_reason: Optional[str] = None
        clarification_field: Optional[str] = None
        scope_clarification_attempt = 0
        if readiness == Readiness.DISCOVERY_READY:
            clarification_question = None
        elif fast_ambiguity_question:
            clarification_question = fast_ambiguity_question
            clarification_reason = CLARIFICATION_MISSING_FIELD
        else:
            clarification_field = self._missing_brief_field(brief)
            clarification_question = self._fallback_clarification(brief)
            if clarification_question:
                clarification_reason = CLARIFICATION_MISSING_FIELD
            else:
                # NAME + WHAT + WHY are all present, so the per-field ladder has
                # nothing to ask, yet the SCOPE gate is still blocking. Without
                # this branch the result was readiness=NEEDS_CLARIFICATION with
                # clarification_question=None: the project moved to
                # WAITING_INPUT and Telegram sent nothing, which is exactly
                # what "the system looks hung" means. Ask the smallest
                # scope-specific question instead.
                previous_attempt = int(
                    (pending_clarification.get("attempt") or 0)
                    if (pending_clarification.get("reason") == CLARIFICATION_SCOPE
                        and pending_clarification.get("question"))
                    else 0
                )
                attempt = previous_attempt + 1
                scope_clarification_attempt = attempt
                clarification_question = self._scope_clarification(scope, brief, attempt)
                if clarification_question:
                    clarification_reason = CLARIFICATION_SCOPE
                else:
                    # Defensive: no template for this scope. Ask rather than
                    # leave the turn silently unanswerable.
                    clarification_question = self._generic_clarification()
                    clarification_reason = CLARIFICATION_GENERIC
        same_question_as_pending = (
            clarification_reason == CLARIFICATION_MISSING_FIELD
            and bool(pending_clarification.get("question"))
            and pending_clarification.get("question") == clarification_question
        )
        clarification_attempt = (
            int(pending_clarification.get("attempt") or 0) + 1
            if same_question_as_pending else 1
        )
        if scope_clarification_attempt:
            clarification_attempt = scope_clarification_attempt

        # HARD INVARIANT: an outstanding clarification always carries an
        # actionable, non-empty question. Asserted here (not merely intended)
        # so a future template gap can never resurrect the silent WAITING_INPUT
        # state.
        if readiness == Readiness.NEEDS_CLARIFICATION and not (
            isinstance(clarification_question, str) and clarification_question.strip()
        ):
            clarification_question = self._generic_clarification()
            clarification_reason = CLARIFICATION_GENERIC
            clarification_field = None
            clarification_attempt = 1

        # Application enforces pause/resume regardless of FAST result.
        # Resume only overrides readiness when the project is actually paused
        # (effective_resume); a bare resume phrase on a non-paused project
        # leaves readiness/clarification intact.
        if pause_detected:
            readiness = Readiness.PAUSED
            clarification_question = None
        elif effective_resume:
            readiness = Readiness.RESUMED
            clarification_question = None

        # No clarification is outstanding unless one is actually being asked.
        if clarification_question is None:
            clarification_reason = None
            clarification_field = None

        return IntakeResult(
            readiness=readiness,
            scope=scope,
            brief=brief,
            clarification_question=clarification_question,
            pause_detected=pause_detected,
            resume_detected=effective_resume,
            clarification_reason=clarification_reason,
            clarification_field=clarification_field,
            clarification_attempt=clarification_attempt,
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

        # Word-boundary matching: a bare substring test makes "app" match inside
        # "WhatsApp" (and "web" inside "website" is fine, but "app" inside
        # "WhatsApp" flips a legitimate website-with-WhatsApp brief to MIXED /
        # OUT_OF_SCOPE). Match keywords as whole words / phrases instead.
        def _kw_present(kw: str) -> bool:
            return re.search(r"\b" + re.escape(kw) + r"\b", lower) is not None

        has_website = any(_kw_present(kw) for kw in website_keywords)
        has_out = any(_kw_present(kw) for kw in out_keywords)

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
                # Record where we paused from so resume can return there.
                state.pause_state["pre_pause_lifecycle"] = state.lifecycle
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.PAUSED)
            elif result.resume_detected:
                state.pause_state["paused"] = False
                state.pause_state["resumed_at"] = time.time()
                # Resume to the lifecycle the project was in when paused
                # (READY, PREVIEW_READY, etc.), falling back to DISCOVERING.
                # Resuming a paused READY project to DISCOVERING would strand
                # it: the auto-build gate requires lifecycle == READY, so the
                # project would never build until re-driven through intake.
                target_name = state.pause_state.get("pre_pause_lifecycle")
                target = ProjectLifecycle.DISCOVERING
                if target_name:
                    try:
                        candidate = ProjectLifecycle(target_name)
                    except ValueError:
                        candidate = None
                    if candidate is not None and candidate in (
                        ProjectLifecycle.DISCOVERING,
                        ProjectLifecycle.WAITING_INPUT,
                        ProjectLifecycle.READY,
                        ProjectLifecycle.QUEUED,
                        ProjectLifecycle.RUNNING,
                        ProjectLifecycle.PREVIEW_READY,
                        ProjectLifecycle.REVISION_REQUESTED,
                        ProjectLifecycle.PUBLISHING,
                        ProjectLifecycle.LIVE,
                    ):
                        target = candidate
                self.store.transition_lifecycle_locked(state, target)

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
                        and not state.production_url
                        and not state.revisions.live_revision
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
                # A semantically complete brief (DISCOVERY_READY) means the user
                # has re-engaged with full requirements. If the pause flag is
                # still set while the lifecycle is being driven to READY, the
                # stale flag would silently suppress the auto-build gate (which
                # requires not pause_state.paused) even though the lifecycle is
                # READY — a brief/pause desync. Clear it so a READY project with
                # a complete brief can actually build.
                if state.pause_state.get("paused"):
                    state.pause_state["paused"] = False
                    state.pause_state["resumed_at"] = time.time()
                # The brief is complete, so nothing is outstanding. Clearing
                # the record here is what makes a stale scope question unable
                # to re-appear as FAST context on a later turn.
                state.pending_clarification = {}
            elif result.readiness == Readiness.NEEDS_CLARIFICATION:
                # HARD INVARIANT, enforced at the persistence boundary: a
                # project may only enter WAITING_INPUT while an actionable
                # clarification is actually recorded. An empty question would
                # leave the project parked in WAITING_INPUT with nothing sent
                # to Telegram and nothing durable explaining why — the "hung"
                # failure mode. Fail closed instead: stay DISCOVERING and make
                # the defect visible to the operator.
                question = result.clarification_question
                if isinstance(question, str) and question.strip():
                    state.pending_clarification = {
                        "question": question,
                        "reason": result.clarification_reason or CLARIFICATION_GENERIC,
                        "field": result.clarification_field,
                        "scope": result.scope.value,
                        "attempt": int(result.clarification_attempt or 1),
                        "asked_at": time.time(),
                    }
                    if state.lifecycle == ProjectLifecycle.DISCOVERING.value:
                        self.store.transition_lifecycle_locked(
                            state, ProjectLifecycle.WAITING_INPUT
                        )
                else:
                    logger.error(
                        "Refusing to persist WAITING_INPUT for project %s: "
                        "NEEDS_CLARIFICATION with no actionable clarification "
                        "question (scope=%s).",
                        project_id, result.scope.value,
                    )

            if event_id is not None:
                state.processed_events.add(event_id)
            self.store.save(state)
