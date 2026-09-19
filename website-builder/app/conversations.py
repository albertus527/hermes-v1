"""Conversation-level project routing for Website Builder R1.

This is the layer ABOVE a single project. It owns:

  * which project a conversation is currently talking about (active project),
  * resolving a human project name ("webbandung") to the immutable internal
    project, using the persisted conversation registry,
  * bounded conversation intents: NEW_PROJECT, SELECT_PROJECT, LIST_PROJECTS,
  * deterministic, LLM-free listing.

Division of authority (unchanged from the rest of the codebase):

  * AI (FAST) may interpret natural language and propose an intent plus a
    ``target_project_name`` STRING. It never chooses an internal project ID.
  * Application code (this module) resolves that name against the persisted
    registry and is the only writer of the active-project pointer.

Resolution is deliberately conservative: exact normalized match or exact
alias match only. No fuzzy/prefix matching, so a name never silently resolves
to the wrong project.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from app.core.registry import (
    ConversationRegistryStore,
    DuplicateProjectName,
    ProjectEntry,
    normalize_project_name,
    project_status_label,
)

logger = logging.getLogger(__name__)


class ConversationRoute(str, Enum):
    """Bounded routing outcomes for a conversation-level turn."""

    # Ordinary turn against the active project (intake/revise/approve/publish).
    PROJECT = "PROJECT"
    # Create a brand-new project (fresh state) and make it active.
    NEW_PROJECT = "NEW_PROJECT"
    # Resolve a named project and make it active.
    SELECT_PROJECT = "SELECT_PROJECT"
    # List the human project names + derived status (no LLM).
    LIST_PROJECTS = "LIST_PROJECTS"
    # The router cannot proceed without asking the user something.
    CLARIFICATION = "CLARIFICATION"


# ---------------------------------------------------------------------------
# Phrase detection (deterministic, bilingual ID/EN)
# ---------------------------------------------------------------------------

_NEW_PROJECT_PHRASES = (
    "website baru",
    "web baru",
    "situs baru",
    "project baru",
    "proyek baru",
    "project lain",
    "proyek lain",
    "website lain",
    "web lain",
    "bikin website baru",
    "buat website baru",
    "bikin web baru",
    "buat web baru",
    "new website",
    "new project",
    "another website",
    "another project",
    "start a new project",
)

_LIST_PROJECTS_PHRASES = (
    "project aku ada apa aja",
    "proyek aku ada apa aja",
    "project saya ada apa aja",
    "website yang pernah aku buat",
    "website apa aja",
    "web apa aja",
    "daftar project",
    "daftar proyek",
    "list project",
    "list projects",
    "my projects",
    "apa aja project",
    "apa saja project",
    "project apa aja",
    "project gue apa aja",
    "project ku apa aja",
)

_REVISE_VERBS = (
    "revisi",
    "ubah",
    "rubah",
    "ganti",
    "tambah",
    "hapus",
    "perbaiki",
    "revisi",
    "change",
    "revise",
    "update",
    "edit",
    "modify",
    "remove",
    "add ",
)

# Prefixes stripped when deriving a display name from a NEW_PROJECT message.
_NEW_PROJECT_PREFIX_RE = re.compile(
    r"^(?:oke\s+|ok\s+|sekarang\s+|mau\s+|aku\s+|saya\s+|tolong\s+|please\s+|i\s+want\s+to\s+|i'?d\s+like\s+to\s+)*"
    r"(?:bikin|buat|create|build|make|start|add|tambah)\s+"
    r"(?:a\s+|an\s+)?"
    r"(?:website|web|situs|site|project|proyek|halaman|page)?\s*"
    r"(?:baru|new|lain|another|other)?\s*",
    re.IGNORECASE,
)

# Natural creation phrasing (no literal "baru"/"new" required), e.g.
# "aku mau bikin website tentang cafe" / "buat web untuk portfolio" /
# "create a website called Daily Bake". Both a create verb AND a
# website-ish noun must be present, and no revision verb may be present
# (so "ubah hero webjogja" never matches).
_CREATE_VERB_RE = re.compile(
    r"\b(?:bikin|bikinin|buat|buatin|create|creates|creating|build|builds|"
    r"building|make|makes|making)\b",
    re.IGNORECASE,
)
_WEBSITE_NOUN_RE = re.compile(
    r"\b(?:website|web|situs|site|project|proyek|landing\s*page|homepage)\b",
    re.IGNORECASE,
)

# "namanya X" / "nama web nya X" / "nama webnya X" / "named X" / "called X"
_NAMED_RE = re.compile(
    r"(?:namanya|dengan\s+nama|named|called|"
    r"nama(?:\s+(?:web|website|situs|project|proyek)(?:nya)?)?(?:\s+nya)?)"
    r"\s+([^\n,;!?]+)",
    re.IGNORECASE,
)

# Bare business-topic words left over after the creation prefix is stripped
# are NOT project names ("aku mau bikin website cafe" asks for a name
# instead of silently naming the project "cafe"). Only single-word generic
# topics belong here; anything specific enough to be an identity resolves
# through the normal path.
_GENERIC_TOPIC_STOPWORDS = frozenset({
    "cafe", "kafe", "coffee", "kopi", "coffeeshop", "shop", "toko",
    "store", "restoran", "resto", "restaurant", "bakery", "barbershop",
    "barber", "salon", "laundry", "bengkel", "warung", "kuliner",
    "sekolah", "portfolio", "portofolio", "bisnis", "usaha", "online",
})

# Words that mark the boundary between a human project NAME and trailing
# instructions in a "namanya X …" clause. Hitting one of these ends the
# name capture deterministically.
_NAMED_TRAILING_STOPWORDS = frozenset({
    "desainnya", "desain", "design", "disain", "di", "dan", "yang",
    "dengan", "untuk", "buat", "bikin", "isinya", "tampilannya", "warna",
    "warnanya", "fitur", "fiturnya", "pakai", "pake", "aja", "saja",
    "dong", "ya", "yah", "please", "pls", "the", "a", "an", "itu",
    "terserah",
})


def _contains_any(text_lower: str, phrases) -> bool:
    return any(p in text_lower for p in phrases)


def _looks_like_natural_creation(text_lower: str) -> bool:
    """True when the user clearly wants to CREATE a website without needing
    the literal word "baru"/"new".

    Requires a create verb + a website-ish noun, and is suppressed by any
    revision verb (so "ubah hero webjogja" / "ganti web" stay REVISE).
    Known-project mentions are handled by the caller (that branch runs
    earlier), so a creation phrase that names an existing project still
    resolves/clarifies through the duplicate-name path instead of
    allocating a second project.
    """
    if not _CREATE_VERB_RE.search(text_lower or ""):
        return False
    if not _WEBSITE_NOUN_RE.search(text_lower or ""):
        return False
    if _contains_any(text_lower, _REVISE_VERBS):
        return False
    return True


# ---------------------------------------------------------------------------
# Route result
# ---------------------------------------------------------------------------


@dataclass
class RouteResult:
    """The router's decision for one normalized turn."""

    route: ConversationRoute
    project_id: Optional[str] = None
    entry: Optional[ProjectEntry] = None
    # Set when a project name was mentioned and successfully resolved.
    switched: bool = False
    # Set when the router already produced the exact user-facing text.
    reply: Optional[str] = None
    # Set for CLARIFICATION.
    clarification: Optional[str] = None
    # Project-scoped intent the runtime should honor ("REVISE" or None).
    forced_intent: Optional[str] = None
    # Name-scanned known projects that appear in the text (audit/debug).
    mentioned: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class ConversationRouter:
    """Deterministic conversation -> project routing.

    Collaborators are injected; this class owns no I/O beyond the registry
    store and (optionally) the FAST interpretation boundary.
    """

    def __init__(
        self,
        state_store,
        registry_store: ConversationRegistryStore,
        telegram_out=None,
        hermes=None,
    ):
        self.store = state_store
        self.registry = registry_store
        self.telegram_out = telegram_out
        self.hermes = hermes

    # ------------------------------------------------------------------
    # Name extraction (deterministic)
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_named_candidate(candidate: str) -> str:
        """Truncate a ``namanya/called`` capture to the human name itself.

        The capture may carry trailing free-form instructions:
        ``"thedailybake desainnya di sesuaiin aja"`` — only the leading name
        span is the identity. Deterministic word-token rules:

        * stop at the first word that is obvious instruction filler
          (Indonesian/English), and
        * when MORE than two words survive, keep at most two — a name
        longer than two words is almost certainly a name plus instructions.
        """
        words = candidate.split()
        kept: List[str] = []
        for word in words:
            if word.lower() in _NAMED_TRAILING_STOPWORDS:
                break
            kept.append(word)
            if len(kept) == 2:
                break
        return " ".join(kept) if kept else candidate

    @staticmethod
    def extract_new_project_name(text: str) -> Optional[str]:
        """Derive the human name of a newly requested project, if present.

        Deterministic: an explicit "namanya X" clause wins, otherwise the
        NEW_PROJECT trigger phrase and leading filler are stripped and any
        remaining content is used. Returns None when nothing usable remains
        (the caller then asks for a name instead of inventing one).
        """
        named = _NAMED_RE.search(text or "")
        if named:
            candidate = named.group(1).strip().strip("?.!\"'") or None
            if candidate:
                return ConversationRouter._truncate_named_candidate(candidate)
            return candidate

        stripped = _NEW_PROJECT_PREFIX_RE.sub("", (text or "").strip(), count=1).strip()
        # Drop a leading conjunction/possessive left over from the phrasing.
        stripped = re.sub(r"^(?:yang|untuk|with|for)\s+", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+(?:dong|ya|yah|please|pls)$", "", stripped, flags=re.IGNORECASE)
        stripped = stripped.strip().strip("?.!\"'")
        if not stripped:
            return None
        # A bare phrase like "bikin website baru" leaves nothing meaningful.
        if normalize_project_name(stripped) in _NEW_PROJECT_PHRASES:
            return None
        # A bare topic word ("aku mau bikin website cafe") is not a name —
        # ask for one instead of silently allocating an identity called
        # "cafe".
        if normalize_project_name(stripped) in _GENERIC_TOPIC_STOPWORDS:
            return None
        return stripped or None

    def _mentioned_projects(self, text: str, registry) -> List[ProjectEntry]:
        """Return the known projects whose name appears as a token in text.

        Token-boundary matching only — never substring matching, so "web" does
        not match "webbandung" and "bandung" does not match "webbandung".
        """
        tokens = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
        joined = normalize_project_name(text) or ""
        mentioned: List[ProjectEntry] = []
        for entry in registry.projects:
            names = {normalize_project_name(entry.display_name)} | {
                normalize_project_name(a) for a in entry.aliases
            }
            for name in names:
                if not name:
                    continue
                if name in tokens or name.replace("-", "") in {
                    t.replace("-", "") for t in tokens
                }:
                    mentioned.append(entry)
                    break
                # Multi-word names may survive normalization as a joined run.
                if name in joined.replace("-", ""):
                    mentioned.append(entry)
                    break
        return mentioned

    # ------------------------------------------------------------------
    # FAST — the primary conversation-level semantic authority.
    #
    # FAST interprets natural language into a BOUNDED intent
    # (CREATE_PROJECT | PROJECT_TURN | LIST_PROJECTS | AMBIGUOUS) plus two
    # STRINGS (target_project_name, proposed_new_project_name) and a
    # confidence level. It never sees or chooses an internal project ID —
    # target_project_name, when present, must exactly match a known
    # registry display name or the whole result is discarded (never
    # trusted partially). proposed_new_project_name is a free-form
    # candidate the application still validates before allocating an
    # identity. Deterministic application code (this class) remains the
    # only writer of the active-project pointer and the only authority
    # over whether a new identity is ever allocated.
    #
    # This call runs BEFORE any phrase/keyword heuristic — those heuristics
    # are demoted to a fallback used ONLY when FAST is unavailable, raises,
    # or returns something unparseable (see ``_route_deterministic_fallback``).
    # ------------------------------------------------------------------

    _TURN_INTENTS = frozenset(
        {"CREATE_PROJECT", "PROJECT_TURN", "LIST_PROJECTS", "AMBIGUOUS"}
    )
    _CONFIDENCE_LEVELS = frozenset({"high", "medium", "low"})

    def _fast_classify_turn(
        self,
        text: str,
        known_names: List[str],
        *,
        active_project_name: Optional[str] = None,
        active_project_lifecycle: Optional[str] = None,
        next_missing_intake_field: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Ask FAST for the conversation-level routing decision.

        Returns ``{"intent", "target_project_name", "proposed_new_project_name",
        "confidence"}`` or ``None`` on ANY failure: exception, malformed
        JSON, non-dict payload, or an unknown intent. An unresolvable
        ``target_project_name`` discards the WHOLE result for
        PROJECT_TURN/LIST_PROJECTS/AMBIGUOUS (the decision cannot be
        trusted at all); CREATE_PROJECT is allowed to name an existing
        project because the caller resolves that safely through the
        duplicate-name path (clarify, never duplicate/mutate).

        When ``active_project_name`` is provided, a bounded context block is
        injected into the prompt so FAST can distinguish an intake answer
        for the active project from a genuine new-project request.
        """
        if self.hermes is None:
            return None

        # Build the optional active-project context block.
        active_context_block = ""
        if active_project_name:
            field_hint = (
                f"\nIntake currently needs: {next_missing_intake_field}"
                if next_missing_intake_field
                else ""
            )
            active_context_block = f"""\nActive project: {active_project_name}
Lifecycle: {active_project_lifecycle or 'unknown'}{field_hint}

Important: The current message may simply be answering the active project's
ongoing intake question. If it could plausibly be an answer to the missing
field above, classify PROJECT_TURN (high confidence) rather than CREATE_PROJECT
or AMBIGUOUS. Only classify CREATE_PROJECT when the user clearly wants a
separate, independent website — not when the message could be an intake answer.
"""

        prompt = f"""You are FAST, routing one Website Builder conversation turn.

The user already owns these website projects (human names only):
{", ".join(known_names) if known_names else "(none yet)"}
{active_context_block}
Classify the user's message into exactly one bounded intent:
- CREATE_PROJECT: the user wants to start a brand-new, independent website
  (a new business/topic/name), even if they never say "new" or "baru".
- PROJECT_TURN: the user is continuing, switching to, revising, or asking
  about a website they already own (an existing project).
- LIST_PROJECTS: the user is asking what projects/websites they have.
- AMBIGUOUS: you genuinely cannot tell whether this is a new website or a
  continuation/change of an existing one.

Also extract:
- target_project_name: the EXISTING project (copied exactly from the list
  above) the user is referring to, or null if none is referenced.
- proposed_new_project_name: the human name for a NEW website the user is
  proposing (only meaningful for CREATE_PROJECT), or null if no name was
  given.
- confidence: "high", "medium", or "low" — how sure you are of `intent`.

Critical safety rule: if you are not sure whether the user wants to CREATE a
new, separate website versus MODIFY/continue an existing one, you MUST set
confidence to "low" (or intent to AMBIGUOUS) rather than guessing. Silently
mutating the wrong project is the single worst possible outcome.

Respond with ONLY a JSON object:
{{"intent": "...", "target_project_name": "..." | null,
  "proposed_new_project_name": "..." | null, "confidence": "..."}}

- Never invent a project name that is not in the list above for
  target_project_name.
- Never fabricate business facts.

User message:
{text}
"""
        try:
            result = self.hermes._run_fast_programmatic(
                prompt=prompt,
                role="FAST",
                skills=[
                    "website-builder-environment",
                    "website-builder-product-scope",
                ],
            )
        except Exception:
            return None
        if not getattr(result, "success", False):
            return None
        raw = getattr(result, "response", "") or ""
        raw = raw.strip().strip("`").strip()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None

        intent = payload.get("intent")
        if not isinstance(intent, str) or intent.strip().upper() not in self._TURN_INTENTS:
            return None
        intent = intent.strip().upper()

        confidence = payload.get("confidence")
        if not isinstance(confidence, str) or confidence.strip().lower() not in self._CONFIDENCE_LEVELS:
            # Missing/malformed confidence is treated as the least trusted
            # level rather than discarding the whole decision — the
            # per-intent confidence gate in ``_route_via_fast`` then decides
            # whether "low" is still safe to act on for this intent.
            confidence = "low"
        else:
            confidence = confidence.strip().lower()

        target_candidate = payload.get("target_project_name")
        target_name: Optional[str] = None
        if isinstance(target_candidate, str) and target_candidate.strip():
            normalized = normalize_project_name(target_candidate)
            for known in known_names:
                if normalize_project_name(known) == normalized:
                    target_name = known
                    break
            if target_name is None and intent not in ("CREATE_PROJECT", "AMBIGUOUS"):
                # PROJECT_TURN/LIST_PROJECTS naming an unknown project means
                # the decision cannot be trusted at all.
                return None

        proposed = payload.get("proposed_new_project_name")
        proposed_name = (
            proposed.strip() if isinstance(proposed, str) and proposed.strip() else None
        )

        return {
            "intent": intent,
            "target_project_name": target_name,
            "proposed_new_project_name": proposed_name,
            "confidence": confidence,
        }

    def _route_via_fast(
        self,
        conversation_id: str,
        text: str,
        event_id: Optional[str],
        fast: Dict[str, Any],
        registry,
        active_project_lifecycle: Optional[str] = None,
    ) -> RouteResult:
        """Resolve a successfully-classified FAST turn into a RouteResult.

        Confidence gating is risk-based, not a blanket threshold: read-only
        LIST_PROJECTS tolerates medium confidence, while any turn that could
        mutate/select a project requires the model to be sure. The single
        most important invariant is enforced here — uncertainty between
        CREATE and MODIFY always clarifies, never silently picks a side.

        WAITING_INPUT bias: when the active project is in WAITING_INPUT (i.e.
        an intake question was just asked and the bot is waiting for an
        answer), a non-high-confidence CREATE_PROJECT or AMBIGUOUS is treated
        as PROJECT_TURN against the active project. The user is almost
        certainly answering the intake question. A high-confidence CREATE is
        never overridden — an explicit new-project request still proceeds.
        """
        intent = fast["intent"]
        confidence = fast["confidence"]
        target_name = fast["target_project_name"]
        proposed_name = fast["proposed_new_project_name"]

        # WAITING_INPUT safety bias: when an intake question is outstanding
        # and FAST is uncertain, route to the active project rather than
        # firing another CREATE-vs-MODIFY clarification.
        #
        # * AMBIGUOUS is always uncertain by definition — bias fires regardless
        #   of the reported confidence value.
        # * CREATE_PROJECT: bias fires only at non-high confidence. A
        #   high-confidence CREATE is a deliberate new-project request and must
        #   NOT be overridden even when another project is WAITING_INPUT.
        if active_project_lifecycle == "WAITING_INPUT" and (
            intent == "AMBIGUOUS"
            or (intent == "CREATE_PROJECT" and confidence != "high")
        ):
            active_id = self.registry.active_project_id(conversation_id)
            if active_id and self._materialized(active_id):
                logger.debug(
                    "WAITING_INPUT bias: overriding %s(%s) -> PROJECT_TURN "
                    "for active project %s (conversation %s)",
                    intent, confidence, active_id, conversation_id,
                )
                return self._resolve_project_turn(conversation_id, None, registry)

        if intent == "LIST_PROJECTS":
            if confidence == "low":
                return RouteResult(
                    route=ConversationRoute.CLARIFICATION,
                    clarification=(
                        "Project mana yang mau dibahas, atau mau lihat daftar "
                        "semua project kamu?"
                    ),
                )
            return RouteResult(
                route=ConversationRoute.LIST_PROJECTS,
                reply=self.render_projects(conversation_id),
            )

        if intent == "CREATE_PROJECT":
            if confidence != "high":
                # The core safety invariant: CREATE vs MODIFY ambiguity
                # never silently proceeds in either direction.
                return RouteResult(
                    route=ConversationRoute.CLARIFICATION,
                    clarification=(
                        "Maksudnya bikin website baru, atau lanjut/ubah project "
                        "yang sudah ada? Bisa dijelasin lagi?"
                    ),
                )
            return self._resolve_create_project(
                conversation_id, text, event_id, target_name, proposed_name
            )

        if intent == "PROJECT_TURN":
            if confidence == "low":
                return RouteResult(
                    route=ConversationRoute.CLARIFICATION,
                    clarification=(
                        "Maksudnya lanjut/ubah project yang mana ya? Bisa "
                        "disebutin nama projectnya?"
                    ),
                )
            return self._resolve_project_turn(conversation_id, target_name, registry)

        # AMBIGUOUS — never guess between CREATE and MODIFY.
        return RouteResult(
            route=ConversationRoute.CLARIFICATION,
            clarification=(
                "Aku belum yakin maksudnya bikin website baru atau lanjut/ubah "
                "project yang sudah ada. Bisa dijelasin lagi?"
            ),
        )

    def _resolve_create_project(
        self,
        conversation_id: str,
        text: str,
        event_id: Optional[str],
        target_name: Optional[str],
        proposed_name: Optional[str],
    ) -> RouteResult:
        """Resolve a high-confidence CREATE_PROJECT decision.

        A ``target_name`` naming an EXISTING project is a duplicate-name
        situation: never silently duplicate, navigate, or mutate — ask the
        user whether to continue that project or pick a different name for
        the new one (same contract as the deterministic duplicate-name path).
        """
        if target_name:
            resolution = self.registry.resolve_name(conversation_id, target_name)
            if resolution.status == "ok" and resolution.entry:
                entry = resolution.entry
                return RouteResult(
                    route=ConversationRoute.CLARIFICATION,
                    entry=entry,
                    clarification=(
                        f"Kamu sudah punya project {entry.display_name}. "
                        "Lanjut ke project itu, atau kasih nama lain untuk "
                        "website barunya?"
                    ),
                )
            if resolution.status == "ambiguous":
                names = ", ".join(e.display_name for e in resolution.candidates)
                return RouteResult(
                    route=ConversationRoute.CLARIFICATION,
                    clarification=(
                        f"Nama itu cocok dengan beberapa project: {names}. "
                        "Mau yang mana, atau kasih nama lain?"
                    ),
                )

        # FAST's proposed name is the semantic authority for the human name;
        # deterministic extraction is only a derivation fallback when FAST
        # did not propose one at all.
        requested = proposed_name or self.extract_new_project_name(text)
        return self._route_new_project(
            conversation_id, text, event_id, requested_name=requested
        )

    def _resolve_project_turn(
        self, conversation_id: str, target_name: Optional[str], registry
    ) -> RouteResult:
        """Resolve a PROJECT_TURN decision against the registry.

        Never sets a project-level intent (REVISE/APPROVE/PUBLISH/INTAKE) —
        that decision is deferred entirely to
        ``TelegramReceiveLoop._classify_intent`` in ``app/runtime.py``, the
        existing bounded-and-lifecycle-gated FAST classifier, so the router
        and the runtime never make conflicting semantic decisions about the
        same turn.
        """
        if target_name:
            resolution = self.registry.resolve_name(conversation_id, target_name)
            if resolution.status == "ok" and resolution.entry:
                entry = resolution.entry
                if not self._materialized(entry.project_id):
                    return RouteResult(
                        route=ConversationRoute.CLARIFICATION,
                        entry=entry,
                        clarification=(
                            f"Project {entry.display_name} belum bisa dibuka. "
                            "Mau bikin project baru atau pilih project lain?"
                        ),
                    )
                switched = (
                    self.registry.active_project_id(conversation_id)
                    != entry.project_id
                )
                if switched:
                    self.registry.set_active(conversation_id, entry.project_id)
                return RouteResult(
                    route=ConversationRoute.PROJECT,
                    project_id=entry.project_id,
                    entry=entry,
                    switched=switched,
                    mentioned=[entry.project_id],
                )
            if resolution.status == "ambiguous":
                names = ", ".join(e.display_name for e in resolution.candidates)
                return RouteResult(
                    route=ConversationRoute.CLARIFICATION,
                    clarification=(
                        f"Ada beberapa project yang cocok: {names}. Mau yang mana?"
                    ),
                )
            # status == "none" should not occur here: an unresolvable
            # target_name already discards the whole FAST result in
            # ``_fast_classify_turn``. Fail safe to the active project.

        active_id = self.registry.active_project_id(conversation_id)
        if not active_id or not self._materialized(active_id):
            names = ", ".join(e.display_name for e in registry.projects)
            return RouteResult(
                route=ConversationRoute.CLARIFICATION,
                clarification=f"Project mana yang mau dibahas? Pilihan: {names}",
            )
        return RouteResult(
            route=ConversationRoute.PROJECT,
            project_id=active_id,
            entry=registry.find_by_id(active_id),
        )

    # ------------------------------------------------------------------
    # Bootstrapping / repair
    # ------------------------------------------------------------------

    # Prefix of a pre-router (R1) single-project-per-conversation ID.
    _LEGACY_PREFIX = "tg-"

    def _legacy_display_name(self, state, conversation_id: str) -> str:
        """Pick a friendly display name for an adopted legacy project.

        Prefers the user-visible site name already collected into ``brief``;
        falls back to ``website-1`` when intake has not captured one yet.
        """
        if state is not None:
            name = state.brief.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "website-1"

    def _ensure_registry(self, conversation_id: str):
        """Load the conversation registry, adopting legacy projects and
        repairing dangling pointers.

        A pre-router R1 conversation may already own a project stored as
        ``tg-<conversation_id>`` with no registry metadata at all. Such a
        project must be adopted into the registry (never orphaned, hidden,
        reset, or replaced by a fresh ``p1`` project) the first time the
        router sees the conversation. Adoption is idempotent.

        Additionally, a persisted ``active_project_id`` that is not present
        in ``projects`` (hand-edited or partially-written registry) is
        cleared rather than allowed to route the conversation at a project
        that does not exist.
        """
        registry = self.registry.load_or_create(conversation_id)

        # ---- Legacy (pre-router) adoption: tg-<conversation_id> exists on
        # disk but the conversation registry does not know about it yet.
        legacy_id = f"{self._LEGACY_PREFIX}{conversation_id}"
        if registry.find_by_id(legacy_id) is None:
            try:
                legacy_state = self.store.load(legacy_id)
            except Exception:
                legacy_state = None
            if legacy_state is not None:
                display_name = self._legacy_display_name(legacy_state, conversation_id)
                # Avoid a name clash with an already-registered entry (the
                # user may have created a "website-1" project via the router
                # before this legacy adoption path ran).
                if self.registry.resolve_name(conversation_id, display_name).status != "none":
                    display_name = "website-1"
                    seq = 2
                    while (
                        self.registry.resolve_name(conversation_id, display_name).status
                        != "none"
                    ):
                        display_name = f"website-{seq}"
                        seq += 1
                try:
                    self.registry.adopt_project(conversation_id, legacy_id, display_name)
                    logger.info(
                        "Adopted legacy project %s for conversation %s as %r",
                        legacy_id, conversation_id, display_name,
                    )
                except Exception:
                    logger.exception(
                        "Failed to adopt legacy project %s for conversation %s",
                        legacy_id, conversation_id,
                    )
                registry = self.registry.load_or_create(conversation_id)

        # ---- Repair a dangling active pointer.
        if registry.active_project_id is not None and (
            registry.find_by_id(registry.active_project_id) is None
        ):
            logger.warning(
                "Clearing dangling active_project_id for conversation %s",
                conversation_id,
            )
            registry.active_project_id = None
            try:
                self.registry.save(registry)
            except Exception:
                logger.exception("Failed to persist registry repair")
        return registry

    def _materialized(self, project_id: Optional[str]) -> bool:
        """True when the internal project's state actually exists on disk."""
        if not project_id:
            return False
        try:
            return self.store.load(project_id) is not None
        except Exception:
            logger.exception("Failed to load project state %s", project_id)
            return False

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route(
        self,
        conversation_id: str,
        text: str,
        event_id: Optional[str] = None,
        display_name_hint: Optional[str] = None,
        active_project_context: Optional[Dict[str, Any]] = None,
    ) -> RouteResult:
        """Route one conversation turn.

        FAST-first: natural-language semantics (CREATE vs continuing an
        existing project vs listing vs ambiguous) are interpreted by FAST
        on EVERY turn — there is no keyword pre-filter gating whether FAST
        gets a chance to interpret. Application code remains authoritative:
        it validates target_project_name against the registry, is the only
        writer of the active-project pointer, and is the only allocator of
        a new project identity.

        ``active_project_context`` is a bounded dict supplied by the caller:
          {"active_project_name": str,
           "active_project_lifecycle": str,
           "next_missing_intake_field": str | None}
        It enriches the FAST prompt so intake answers are not mistaken for
        new-project requests. The caller (TelegramReceiveLoop) loads it
        from the active project's persisted state; the router never loads
        per-project state itself.

        The deterministic phrase/regex heuristics below are a FALLBACK used
        ONLY when FAST is unavailable (no ``self.hermes``) or fails/returns
        something unparseable. The fallback is deliberately conservative:
        any genuine uncertainty between CREATE_PROJECT and continuing an
        existing project asks for clarification rather than guessing.
        """
        registry = self._ensure_registry(conversation_id)
        known_names = [e.display_name for e in registry.projects]

        # ---- 0. Event replay guard (crash-window recovery) -----------
        # If this Telegram event was already allocated to a project (e.g. crash
        # between allocation and ProjectState materialization, or duplicate
        # event delivery), deterministically recover that exact project
        # identity without re-running FAST, re-opening pending actions, or
        # allocating a second project.
        if event_id:
            replay = registry.event_projects.get(str(event_id))
            if replay:
                entry = registry.find_by_id(replay)
                if entry is not None:
                    if self._materialized(replay):
                        return RouteResult(
                            route=ConversationRoute.PROJECT,
                            project_id=replay,
                            entry=entry,
                            switched=False,
                        )
                    logger.warning(
                        "Recovering unmaterialized project %s for event %s "
                        "(conversation %s)",
                        replay, event_id, conversation_id,
                    )
                    return RouteResult(
                        route=ConversationRoute.NEW_PROJECT,
                        project_id=replay,
                        entry=entry,
                        clarification=(
                            "Lagi beresin project kamu yang tadi sempet "
                            "kepotong. Sebentar ya…"
                        ),
                    )

        # ---- 1. Pending-action gate (deterministic, pre-FAST) ----------
        # When the router previously asked a multi-turn clarifying question
        # (e.g. "what name for the new project?"), the expected answer is
        # consumed here before FAST is invoked.
        #
        # Crash-safe ordering:
        #   1. Extract / validate the name candidate.
        #   2. Call _route_new_project — allocates/recovers the identity and
        #      persists the registry (allocation + event mapping) BEFORE
        #      returning.
        #   3. Clear pending_action ONLY after the project identity is safely
        #      recorded (route is NEW_PROJECT or PROJECT).
        #
        # A crash between step 2 and step 3 leaves pending_action set but the
        # project already allocated. The next retry fires the gate again,
        # _route_new_project finds the project via resolve_name (duplicate
        # guard), returns PROJECT, and clears pending. No orphaned state.
        #
        # A crash before step 2 leaves both pending and allocation untouched —
        # the next retry re-asks for the name.
        pending = registry.pending_action
        if pending and pending.get("action") == "CREATE_PROJECT" and pending.get("awaiting") == "NAME":
            # Use the full text as the name candidate; deterministic
            # extraction/truncation applies as usual.
            requested_name = self.extract_new_project_name(text) or text.strip() or None
            logger.debug(
                "Consuming pending CREATE_PROJECT/NAME for conversation %s: %r",
                conversation_id, requested_name,
            )
            result = self._route_new_project(
                conversation_id, text, event_id,
                requested_name=requested_name,
                clear_pending_on_success=True,
            )
            return result

        # ---- FAST-first (active-project context enriched) ---------------
        active_lifecycle = (
            (active_project_context or {}).get("active_project_lifecycle")
        )
        fast = self._fast_classify_turn(
            text,
            known_names,
            active_project_name=(
                (active_project_context or {}).get("active_project_name")
            ),
            active_project_lifecycle=active_lifecycle,
            next_missing_intake_field=(
                (active_project_context or {}).get("next_missing_intake_field")
            ),
        )
        if fast is not None:
            return self._route_via_fast(
                conversation_id, text, event_id, fast, registry,
                active_project_lifecycle=active_lifecycle,
            )

        return self._route_deterministic_fallback(
            conversation_id, text, event_id, display_name_hint, registry
        )

    def _route_deterministic_fallback(
        self,
        conversation_id: str,
        text: str,
        event_id: Optional[str],
        display_name_hint: Optional[str],
        registry,
    ) -> RouteResult:
        """Degraded-mode routing used ONLY when FAST is unavailable/failed.

        This path is intentionally LESS capable than FAST: it recognizes
        only conservative, obvious, safe cases (explicit "website baru"
        phrasing, an explicit create-verb + website-noun combination with no
        revision verb present, and exact known-project-name mentions). Any
        remaining uncertainty about whether the user wants to CREATE a new
        website falls through to the active-project turn rather than
        guessing — correctness over capability while FAST is degraded.
        """
        lowered = (text or "").lower()

        # ---- 1. LIST_PROJECTS (deterministic phrase fallback) ----
        if _contains_any(lowered, _LIST_PROJECTS_PHRASES):
            return RouteResult(
                route=ConversationRoute.LIST_PROJECTS,
                reply=self.render_projects(conversation_id),
            )

        # ---- 2. Known project mentioned by name ----
        mentioned = self._mentioned_projects(text, registry)
        if len(mentioned) == 1:
            entry = mentioned[0]
            # A NEW_PROJECT phrase that names an EXISTING project is a
            # duplicate-name situation: never silently navigate; ask the user
            # whether to continue that project or pick another name.
            if _contains_any(lowered, _NEW_PROJECT_PHRASES):
                if event_id:
                    replay = self.registry.recorded_event(conversation_id, event_id)
                    if replay:
                        replay_entry = (
                            self.registry.load_or_create(conversation_id).find_by_id(replay)
                        )
                        if replay_entry is not None and not self._materialized(replay):
                            # Crash-window recovery for the SAME allocation.
                            return RouteResult(
                                route=ConversationRoute.NEW_PROJECT,
                                project_id=replay,
                                entry=replay_entry,
                                clarification=(
                                    "Lagi beresin project kamu yang tadi sempet "
                                    "kepotong. Sebentar ya…"
                                ),
                            )
                        return RouteResult(
                            route=ConversationRoute.PROJECT,
                            project_id=replay,
                            entry=replay_entry,
                        )
                if not self._materialized(entry.project_id):
                    return RouteResult(
                        route=ConversationRoute.CLARIFICATION,
                        entry=entry,
                        clarification=(
                            f"Kamu sudah punya project {entry.display_name}. "
                            "Lanjut ke project itu, atau kasih nama lain untuk "
                            "website barunya?"
                        ),
                    )
                if event_id:
                    self.registry.record_event(conversation_id, event_id, entry.project_id)
                return RouteResult(
                    route=ConversationRoute.PROJECT,
                    project_id=entry.project_id,
                    entry=entry,
                    switched=False,
                    clarification=(
                        f"Kamu sudah punya project {entry.display_name}. "
                        "Lanjut ke project itu, atau kasih nama lain untuk website barunya?"
                    ),
                )
            if not self._materialized(entry.project_id):
                return RouteResult(
                    route=ConversationRoute.CLARIFICATION,
                    entry=entry,
                    clarification=(
                        f"Project {entry.display_name} belum bisa dibuka. "
                        "Mau bikin project baru atau pilih project lain?"
                    ),
                )
            switched = self.registry.active_project_id(conversation_id) != entry.project_id
            if switched:
                self.registry.set_active(conversation_id, entry.project_id)
            # REVISE-vs-INTAKE for an already-resolved project is NOT decided
            # here — that decision is deferred entirely to
            # ``TelegramReceiveLoop._classify_intent`` in app/runtime.py, the
            # existing bounded FAST classifier gated by project lifecycle.
            # ``_REVISE_VERBS`` remains ONLY as that classifier's own
            # fallback (see runtime.py) — it is not consulted twice.
            return RouteResult(
                route=ConversationRoute.PROJECT,
                project_id=entry.project_id,
                entry=entry,
                switched=switched,
                mentioned=[entry.project_id],
            )
        if len(mentioned) > 1:
            names = ", ".join(e.display_name for e in mentioned)
            return RouteResult(
                route=ConversationRoute.CLARIFICATION,
                clarification=(
                    f"Ada beberapa project yang disebut: {names}. Mau yang mana?"
                ),
                mentioned=[e.project_id for e in mentioned],
            )

        # ---- 3. NEW_PROJECT (deterministic, conservative fallback) ----
        # Explicit trigger phrases ("website baru", "project lain", ...).
        if _contains_any(lowered, _NEW_PROJECT_PHRASES):
            return self._route_new_project(conversation_id, text, event_id)
        # Natural creation phrasing ("aku mau bikin website tentang cafe…")
        # — only recognized when unambiguous (create verb + website noun,
        # no revision verb). This is the fallback's ceiling of capability;
        # anything less obvious falls through to the active-project turn
        # below rather than guessing CREATE.
        if _looks_like_natural_creation(lowered):
            return self._route_new_project(conversation_id, text, event_id)

        # ---- 4. First contact in the conversation: bootstrap p1 ----
        if not registry.projects:
            return self._bootstrap_first_project(
                conversation_id, text, event_id, display_name_hint
            )

        # ---- 5. Active project (or ask when there is none) ----
        active_id = self.registry.active_project_id(conversation_id)
        if not active_id or not self._materialized(active_id):
            names = ", ".join(e.display_name for e in registry.projects)
            return RouteResult(
                route=ConversationRoute.CLARIFICATION,
                clarification=(
                    f"Project mana yang mau dibahas? Pilihan: {names}"
                ),
            )
        return RouteResult(
            route=ConversationRoute.PROJECT,
            project_id=active_id,
            entry=registry.find_by_id(active_id),
        )

    # ------------------------------------------------------------------
    # NEW_PROJECT
    # ------------------------------------------------------------------

    def _route_new_project(
        self,
        conversation_id: str,
        text: str,
        event_id: Optional[str],
        requested_name: Optional[str] = None,
        clear_pending_on_success: bool = False,
    ) -> RouteResult:
        """Route a CREATE_PROJECT decision to a new or recovered project.

        When ``clear_pending_on_success`` is True, the pending_action is
        cleared in the SAME atomic write as the allocation (via
        ``allocate_project(clear_pending=True)``), eliminating a separate
        second save and its associated Windows file-handle window.
        """
        if event_id:
            replay = self.registry.recorded_event(conversation_id, event_id)
            if replay:
                entry = self.registry.load_or_create(conversation_id).find_by_id(replay)
                if entry is None:
                    # Registry rolled back or corrupted — never route at an
                    # identity with no entry. Fall through to deterministic
                    # routing instead.
                    pass
                elif self._materialized(replay):
                    return RouteResult(
                        route=ConversationRoute.PROJECT,
                        project_id=replay,
                        entry=entry,
                        switched=False,
                    )
                else:
                    # Crash window: registry persisted the allocation (and the
                    # event mapping) but ProjectState creation never happened.
                    # Deterministically recover the SAME allocated identity by
                    # sending NEW_PROJECT with the originally-registered id so
                    # the caller can re-run creation for exactly this project.
                    logger.warning(
                        "Recovering unmaterialized project %s for event %s "
                        "(conversation %s)",
                        replay, event_id, conversation_id,
                    )
                    return RouteResult(
                        route=ConversationRoute.NEW_PROJECT,
                        project_id=replay,
                        entry=entry,
                        clarification=(
                            "Lagi beresin project kamu yang tadi sempet "
                            "kepotong. Sebentar ya…"
                        ),
                    )

        requested = requested_name or self.extract_new_project_name(text)
        if not requested:
            # Persist a pending action so the NEXT turn is consumed
            # deterministically (before FAST runs) as the project name.
            # This prevents the multi-turn loop where FAST re-classifies
            # the name answer from scratch and goes AMBIGUOUS again.
            self.registry.set_pending_action(
                conversation_id,
                {"action": "CREATE_PROJECT", "awaiting": "NAME"},
            )
            return RouteResult(
                route=ConversationRoute.CLARIFICATION,
                clarification=(
                    "Oke, bikin website baru. Mau kasih nama apa untuk "
                    "website barunya?"
                ),
            )

        # Duplicate name: never silently create a second project.
        existing = self.registry.resolve_name(conversation_id, requested)
        if existing.status == "ok" and existing.entry:
            self.registry.set_active(conversation_id, existing.entry.project_id)
            if event_id:
                self.registry.record_event(conversation_id, event_id, existing.entry.project_id)
            return RouteResult(
                route=ConversationRoute.PROJECT,
                project_id=existing.entry.project_id,
                entry=existing.entry,
                switched=True,
                clarification=(
                    f"Kamu sudah punya project {existing.entry.display_name}. "
                    "Lanjut ke project itu, atau kasih nama lain untuk website "
                    "barunya?"
                ),
            )
        if existing.status == "ambiguous":
            names = ", ".join(e.display_name for e in existing.candidates)
            return RouteResult(
                route=ConversationRoute.CLARIFICATION,
                clarification=(
                    f"Nama itu cocok dengan beberapa project: {names}. "
                    "Mau yang mana, atau kasih nama lain?"
                ),
            )

        entry = self.registry.allocate_project(
            conversation_id,
            requested,
            clear_pending=clear_pending_on_success,
            event_id=event_id,
        )
        return RouteResult(
            route=ConversationRoute.NEW_PROJECT,
            project_id=entry.project_id,
            entry=entry,
            reply=None,
        )

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _bootstrap_first_project(
        self, conversation_id: str, text: str, event_id: Optional[str],
        display_name_hint: Optional[str],
    ) -> RouteResult:
        """Register the conversation's first project (internal id p1).

        The internal project is materialized by the caller (dispatcher
        "create"), then adopted into the registry under a human name. When no
        human name is available yet the project is registered under a
        deterministic placeholder derived from the message, so the human
        name can be refined by a later rename.
        """
        # Replay guard: re-running a first-contact event recovers the SAME
        # allocation instead of allocating a second identity.
        if event_id:
            replay = self.registry.recorded_event(conversation_id, event_id)
            if replay:
                entry = self.registry.load_or_create(conversation_id).find_by_id(replay)
                if entry is not None and not self._materialized(replay):
                    return RouteResult(
                        route=ConversationRoute.NEW_PROJECT,
                        project_id=replay,
                        entry=entry,
                        clarification=(
                            "Lagi beresin project kamu yang tadi sempet "
                            "kepotong. Sebentar ya…"
                        ),
                    )

        name = (
            self.extract_new_project_name(text)
            or display_name_hint
            or self._derived_display_name(text)
        )
        entry = self.registry.allocate_project(
            conversation_id, name, event_id=event_id
        )
        return RouteResult(
            route=ConversationRoute.NEW_PROJECT,
            project_id=entry.project_id,
            entry=entry,
        )

    @staticmethod
    def _derived_display_name(text: str) -> str:
        """Deterministic fallback display name for an un-named first project."""
        normalized = normalize_project_name(text)
        if not normalized:
            return "website-1"
        words = [w for w in normalized.split("-") if w][:3]
        return "-".join(words) or "website-1"

    # ------------------------------------------------------------------
    # Materialization (registry stays consistent with project state)
    # ------------------------------------------------------------------

    def materialize_new_project(
        self,
        conversation_id: str,
        display_name: str,
        event_id: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ):
        """Allocate the internal ID, register it, then create the project state.

        Ordering matters: the registry entry (including the event->project
        replay mapping) is persisted BEFORE the project state so a crash
        between the two can never produce an unregistered project. If project
        creation fails, the registry entry is rolled back so the registry
        never points at a project that does not exist.

        When an entry with the same normalized display name already exists —
        the crash-window recovery case (registry persisted the allocation,
        ProjectState creation never happened) plus normal duplicate-name
        requests — the SAME id is reused; a second identity is never
        allocated for one logical project.
        """
        resolution = self.registry.resolve_name(conversation_id, display_name)
        if resolution.status == "ok" and resolution.entry is not None:
            entry = resolution.entry
        else:
            entry = self.registry.allocate_project(
                conversation_id, display_name, aliases, event_id=event_id
            )
        try:
            self.registry.set_active(conversation_id, entry.project_id)
        except Exception:
            logger.exception("Failed to persist active project after allocation")
        if event_id:
            self.registry.record_event(conversation_id, event_id, entry.project_id)
        return entry

    def rollback_project(self, conversation_id: str, project_id: str) -> None:
        """Remove a registry entry whose internal project failed to materialize."""
        self.registry.remove_project(conversation_id, project_id)

    # ------------------------------------------------------------------
    # LIST_PROJECTS rendering (deterministic, no internal IDs)
    # ------------------------------------------------------------------

    def render_projects(self, conversation_id: str) -> str:
        registry = self._ensure_registry(conversation_id)
        if not registry.projects:
            return (
                "Kamu belum punya project website. "
                "Mau bikin website baru?"
            )
        lines = ["Project kamu:"]
        for entry in registry.projects:
            try:
                state = self.store.load(entry.project_id)
            except Exception:
                state = None
            status = project_status_label(state)
            lines.append(f"• {entry.display_name} — {status}")
        lines.append("")
        lines.append("Mau lanjut yang mana?")
        return "\n".join(lines)

    def display_name_for(self, conversation_id: str, project_id: str) -> Optional[str]:
        return self.registry.display_name_for(conversation_id, project_id)
