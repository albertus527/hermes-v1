"""Conversation-level project registry for Website Builder R1.

Separates the immutable internal project identity (``project_id``) from the
human project/display name the user talks about (``webbandung`` ...). Users
never see or type internal IDs.

Persistence reuses the existing file-based style (one JSON document per
conversation under ``<state_root>/conversations/``) with the same atomic
temp-write + rename discipline as :class:`ProjectStateStore`. The registry
is deliberately small: an active-project pointer, a monotonic sequence for
internal ID allocation, and the known projects for the conversation.

No DB, no Redis, no queue, no framework.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.state import ProjectState


# ---------------------------------------------------------------------------
# Name normalization + internal ID derivation
# ---------------------------------------------------------------------------

_NAME_ALLOWED = re.compile(r"[^a-z0-9]+")
_CONVERSATION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def normalize_project_name(name: Optional[str]) -> Optional[str]:
    """Deterministically normalize a human project name.

    Rules (simple, deterministic, tested):
      * strip surrounding whitespace
      * lowercase
      * any run of characters outside ``[a-z0-9]`` collapses to a single ``-``
      * leading/trailing ``-`` trimmed

    Returns ``None`` when nothing usable remains. No fuzzy matching — this is
    intentionally conservative so distinct projects never silently merge.
    """
    if not name or not isinstance(name, str):
        return None
    normalized = _NAME_ALLOWED.sub("-", name.strip().lower()).strip("-")
    return normalized or None


# Public alias: the exact same conservative normalization doubles as the
# friendly Vercel project slug derivation. No truncation, no word-count
# limits, no invented business names — "Dapur Kedaton" -> "dapur-kedaton",
# "The Daily Bake" -> "the-daily-bake", "Kopi & Roti" -> "kopi-roti".
slugify_display_name = normalize_project_name


def project_id_for(conversation_id: str, seq: int) -> str:
    """Derive the immutable internal project ID.

    ``tg-<conversation_id>-p<seq>`` — internal only, never shown to users.
    """
    return f"tg-{conversation_id}-p{seq}"


# ---------------------------------------------------------------------------
# Registry data
# ---------------------------------------------------------------------------


@dataclass
class ProjectEntry:
    """One known project inside a conversation."""

    project_id: str
    display_name: str  # human name as the user typed/confirmed it
    aliases: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Stable, user-facing Vercel project slug. Set ONCE at first successful
    # Vercel project creation/reconciliation and never changed afterward —
    # a later display_name rename must never rename/recreate the bound
    # remote Vercel project (R1: no migration complexity). None until a
    # Vercel binding has actually been established.
    vercel_slug: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "created_at": self.created_at,
            "vercel_slug": self.vercel_slug,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectEntry":
        vercel_slug = data.get("vercel_slug")
        return cls(
            project_id=str(data.get("project_id", "")),
            display_name=str(data.get("display_name", "")),
            aliases=[str(a) for a in (data.get("aliases") or [])],
            created_at=float(data.get("created_at", time.time())),
            vercel_slug=str(vercel_slug) if vercel_slug else None,
        )


@dataclass
class ConversationRegistry:
    """Persistent per-conversation registry.

    Conversation metadata lives here — NOT inside any single project's state —
    so the registry survives active-project switches and project creation.
    """

    conversation_id: str
    active_project_id: Optional[str] = None
    next_project_seq: int = 1
    projects: List[ProjectEntry] = field(default_factory=list)
    # event_id -> project_id. Persisted BEFORE project state creation so a
    # replayed Telegram event can never allocate a second project.
    event_projects: Dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Minimal pending conversation state for multi-turn clarifications.
    # Example: {"action": "CREATE_PROJECT", "awaiting": "NAME"}
    # Set when the router asks a clarifying question that requires the next
    # turn to be consumed deterministically (before FAST is invoked).
    # Cleared immediately after the awaited value is consumed.
    # None when no clarification is pending.
    pending_action: Optional[Dict[str, str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "active_project_id": self.active_project_id,
            "next_project_seq": self.next_project_seq,
            "projects": [p.to_dict() for p in self.projects],
            "event_projects": dict(self.event_projects),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pending_action": self.pending_action,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConversationRegistry":
        raw_events = data.get("event_projects") or {}
        raw_pending = data.get("pending_action")
        pending_action: Optional[Dict[str, str]] = None
        if isinstance(raw_pending, dict):
            pending_action = {str(k): str(v) for k, v in raw_pending.items()}
        return cls(
            conversation_id=str(data.get("conversation_id", "")),
            active_project_id=data.get("active_project_id"),
            next_project_seq=int(data.get("next_project_seq", 1) or 1),
            projects=[ProjectEntry.from_dict(p) for p in (data.get("projects") or [])],
            event_projects={str(k): str(v) for k, v in raw_events.items()},
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            pending_action=pending_action,
        )

    # ------------------------------------------------------------------
    # Lookups (pure — the store owns mutation)
    # ------------------------------------------------------------------

    def find_by_id(self, project_id: str) -> Optional[ProjectEntry]:
        for entry in self.projects:
            if entry.project_id == project_id:
                return entry
        return None

    def resolve(self, name: Optional[str]) -> List[ProjectEntry]:
        """Resolve a human name to candidate entries.

        Returns ALL matching entries (exact normalized display-name match OR
        exact normalized alias match). The caller classifies:

          * exactly 1 -> select
          * 0         -> no match (ask clarification)
          * >1        -> ambiguous (ask clarification, never guess)

        A normalized name that matches BOTH a display name and an alias of
        two different projects is ambiguous — never silently resolved.
        """
        normalized = normalize_project_name(name)
        if normalized is None:
            return []
        matches = []
        for entry in self.projects:
            candidates = [normalize_project_name(entry.display_name)] + [
                normalize_project_name(a) for a in entry.aliases
            ]
            if normalized in {c for c in candidates if c}:
                matches.append(entry)
        return matches

    def active_entry(self) -> Optional[ProjectEntry]:
        if self.active_project_id is None:
            return None
        return self.find_by_id(self.active_project_id)

    def display_name_for(self, project_id: str) -> Optional[str]:
        entry = self.find_by_id(project_id)
        return entry.display_name if entry else None


# ---------------------------------------------------------------------------
# Resolution outcome
# ---------------------------------------------------------------------------


@dataclass
class ResolutionResult:
    """Outcome of resolving a human project name against the registry."""

    status: str  # "ok" | "none" | "ambiguous"
    entry: Optional[ProjectEntry] = None
    candidates: List[ProjectEntry] = field(default_factory=list)


class DuplicateProjectName(RuntimeError):
    """Raised when registering a display name that already exists (normalized)."""


class ConversationRegistryStore:
    """File-based per-conversation registry persistence.

    Layout: ``<root>/<conversation_id>.json`` alongside the per-project state
    files but under a dedicated ``conversations/`` subdirectory so project
    state globbing/locking semantics stay untouched.

    Writes are atomic (temp + fsync + rename) and guarded by a process-local
    lock per conversation — the Website Builder runtime is a single writer
    process for a conversation (same discipline as the project writer lock).
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _validate_conversation_id(self, conversation_id: str) -> None:
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or len(conversation_id) > 128
            or not _CONVERSATION_ID_RE.fullmatch(conversation_id)
        ):
            raise ValueError("Invalid conversation ID")

    def _path(self, conversation_id: str) -> Path:
        self._validate_conversation_id(conversation_id)
        return self.root / f"{conversation_id}.json"

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(conversation_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[conversation_id] = lock
            return lock

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self, conversation_id: str) -> Optional[ConversationRegistry]:
        path = self._path(conversation_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            return ConversationRegistry.from_dict(json.load(f))

    def save(self, registry: ConversationRegistry) -> None:
        registry.updated_at = time.time()
        path = self._path(registry.conversation_id)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(registry.to_dict(), f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def load_or_create(self, conversation_id: str) -> ConversationRegistry:
        """Return the persisted registry or a fresh in-memory one.

        A fresh registry is NOT saved here — the caller decides when a
        mutation is worth persisting. This keeps mere lookups side-effect
        free.
        """
        existing = self.load(conversation_id)
        if existing is not None:
            return existing
        return ConversationRegistry(conversation_id=conversation_id)

    # ------------------------------------------------------------------
    # Mutations (each is internally locked + atomically persisted)
    # ------------------------------------------------------------------

    def allocate_project(
        self,
        conversation_id: str,
        display_name: str,
        aliases: Optional[List[str]] = None,
        clear_pending: bool = False,
        event_id: Optional[str] = None,
    ) -> ProjectEntry:
        """Allocate exactly one immutable internal project ID and register it.

        Refuses a duplicate normalized display name/alias so a NEW_PROJECT
        with an already-taken name can never silently fork a second project.

        When ``clear_pending`` is True, ``pending_action`` is cleared in the
        SAME locked atomic write as the allocation.  This is used by the
        pending-NAME gate so the clear happens crash-safely after the identity
        is recorded, without requiring a second separate locked save.

        When ``event_id`` is provided, the ``event_id -> project_id`` replay
        mapping is recorded in the SAME locked atomic write. This ensures
        there is zero crash window between project allocation, event replay
        recording, and clearing pending_action.
        """
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            normalized = normalize_project_name(display_name)
            if normalized is None:
                raise ValueError("Invalid project display name")
            for entry in registry.projects:
                candidates = {normalize_project_name(entry.display_name)} | {
                    normalize_project_name(a) for a in entry.aliases
                }
                if normalized in candidates:
                    raise DuplicateProjectName(normalized)
            normalized_aliases = []
            for alias in aliases or []:
                a = normalize_project_name(alias)
                if a and a not in normalized_aliases:
                    normalized_aliases.append(a)
            entry = ProjectEntry(
                project_id=project_id_for(conversation_id, registry.next_project_seq),
                display_name=display_name,
                aliases=normalized_aliases,
            )
            registry.next_project_seq += 1
            registry.projects.append(entry)
            registry.active_project_id = entry.project_id
            if event_id:
                registry.event_projects[str(event_id)] = entry.project_id
            if clear_pending:
                registry.pending_action = None
            self.save(registry)
            return entry

    def adopt_project(
        self, conversation_id: str, project_id: str, display_name: str
    ) -> ProjectEntry:
        """Register a pre-existing project under a human name (first-project
        bootstrapping / legacy conversation adoption).

        Idempotent on ``project_id``: re-adopting the same project returns the
        existing entry unchanged. ``next_project_seq`` is advanced past the
        adopted sequence so later allocations never collide.
        """
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            existing = registry.find_by_id(project_id)
            if existing is not None:
                return existing
            normalized = normalize_project_name(display_name)
            if normalized is None:
                raise ValueError("Invalid project display name")
            for entry in registry.projects:
                candidates = {normalize_project_name(entry.display_name)} | {
                    normalize_project_name(a) for a in entry.aliases
                }
                if normalized in candidates:
                    raise DuplicateProjectName(normalized)
            entry = ProjectEntry(project_id=project_id, display_name=display_name)
            registry.projects.append(entry)
            seq_match = re.search(r"-p(\d+)$", project_id)
            if seq_match:
                registry.next_project_seq = max(
                    registry.next_project_seq, int(seq_match.group(1)) + 1
                )
            registry.active_project_id = project_id
            self.save(registry)
            return entry

    def set_active(self, conversation_id: str, project_id: str) -> None:
        """Deterministically switch the conversation's active project."""
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            if registry.find_by_id(project_id) is None:
                raise ValueError(f"Unknown project for conversation: {project_id}")
            registry.active_project_id = project_id
            self.save(registry)

    def set_vercel_slug_once(
        self, conversation_id: str, project_id: str, slug: str
    ) -> str:
        """Bind the project's stable Vercel slug on FIRST successful Vercel
        project creation/reconciliation only.
        Idempotent: if a slug is already bound, that existing slug is
        returned unchanged — a later call (e.g. after a display_name
        rename) NEVER overwrites it. This is the persistence half of the
        "no remote rename/recreation on local rename" R1 invariant.
        """
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            entry = registry.find_by_id(project_id)
            if entry is None:
                raise ValueError(f"Unknown project for conversation: {project_id}")
            if entry.vercel_slug:
                return entry.vercel_slug
            entry.vercel_slug = slug
            self.save(registry)
            return slug

    def add_alias(self, conversation_id: str, project_id: str, alias: str) -> None:
        """Attach a rename alias so the OLD name still resolves to the project."""
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            entry = registry.find_by_id(project_id)
            if entry is None:
                raise ValueError(f"Unknown project for conversation: {project_id}")
            normalized = normalize_project_name(alias)
            if normalized and normalized not in entry.aliases:
                entry.aliases.append(normalized)
                self.save(registry)

    # ------------------------------------------------------------------
    # PHASE C — canonical identity convergence
    # ------------------------------------------------------------------

    def converge_display_name(
        self, conversation_id: str, project_id: str, confirmed_name: str
    ) -> "ResolutionResult":
        """Converge a provisional registry display name onto the user-confirmed
        canonical name WITHOUT allocating a second identity.

        A first-contact/bootstrap fallback path may allocate a project under a
        PROVISIONAL or derived name, while later intake discovers the true
        human name (``brief['name']``). Vercel's slug derives from
        ``registry.display_name`` -- so if the registry stayed stale the remote
        project would be created under the provisional name.

        Rules (fail closed):
          * the project's ``vercel_slug`` is ALREADY bound -> NO implicit
            rename. The remote identity is frozen; return the existing entry
            unchanged (status ``"noop"``). Renaming here would fork remote
            identity, which R1 forbids.
          * the confirmed name already normalizes to the current display name
            -> no-op (status ``"noop"``).
          * the confirmed name collides with ANOTHER project in the
            conversation -> NO overwrite, NO duplicate: status ``"ambiguous"``
            with the colliding candidates. The caller decides how to surface
            this (clarification/failure).
          * otherwise -> the SAME entry is updated in place (display_name),
            the previous display name is retained as an alias so old
            references still resolve, and the entry is returned (status
            ``"ok"``). ``project_id`` never changes.
        """
        confirmed_normalized = normalize_project_name(confirmed_name)
        if confirmed_normalized is None:
            return ResolutionResult(status="none")
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            entry = registry.find_by_id(project_id)
            if entry is None:
                return ResolutionResult(status="none")
            # Remote identity already bound -> never rename/fork it.
            if entry.vercel_slug:
                return ResolutionResult(status="noop", entry=entry)
            current_normalized = normalize_project_name(entry.display_name)
            if current_normalized == confirmed_normalized:
                return ResolutionResult(status="noop", entry=entry)
            # Collision with a DIFFERENT project -> fail closed.
            collisions = [
                e for e in registry.projects
                if e.project_id != project_id
                and confirmed_normalized in (
                    {normalize_project_name(e.display_name)}
                    | {normalize_project_name(a) for a in e.aliases}
                )
            ]
            if collisions:
                return ResolutionResult(status="ambiguous", candidates=collisions)
            previous = entry.display_name
            entry.display_name = confirmed_name
            prev_normalized = normalize_project_name(previous)
            if (prev_normalized and prev_normalized != confirmed_normalized
                    and prev_normalized not in entry.aliases):
                entry.aliases.append(prev_normalized)
            self.save(registry)
            return ResolutionResult(status="ok", entry=entry)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def resolve_name(self, conversation_id: str, name: Optional[str]) -> ResolutionResult:
        registry = self.load_or_create(conversation_id)
        matches = registry.resolve(name)
        if len(matches) == 1:
            return ResolutionResult(status="ok", entry=matches[0], candidates=matches)
        if not matches:
            return ResolutionResult(status="none")
        return ResolutionResult(status="ambiguous", candidates=matches)

    def active_project_id(self, conversation_id: str) -> Optional[str]:
        return self.load_or_create(conversation_id).active_project_id

    def display_name_for(self, conversation_id: str, project_id: str) -> Optional[str]:
        return self.load_or_create(conversation_id).display_name_for(project_id)

    # ------------------------------------------------------------------
    # Replay/idempotency mapping (event_id -> project_id)
    # ------------------------------------------------------------------

    def recorded_event(self, conversation_id: str, event_id: str) -> Optional[str]:
        """Return the internal project a Telegram event already allocated."""
        if not event_id:
            return None
        return self.load_or_create(conversation_id).event_projects.get(str(event_id))

    def record_event(self, conversation_id: str, event_id: str, project_id: str) -> None:
        """Persist the event->project mapping. Called BEFORE project creation
        so a crash cannot produce an unregistered project, and so a replay of
        this event resolves to the same project instead of allocating anew.
        """
        if not event_id:
            return
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            if registry.find_by_id(project_id) is None:
                raise ValueError(f"Unknown project for conversation: {project_id}")
            registry.event_projects[str(event_id)] = project_id
            self.save(registry)

    # ------------------------------------------------------------------
    # Pending-action helpers (crash-safe multi-turn clarification state)
    # ------------------------------------------------------------------

    def set_pending_action(
        self, conversation_id: str, action: Dict[str, str]
    ) -> None:
        """Persist a pending conversation-level clarification action.

        Called atomically with the CLARIFICATION RouteResult so the next
        turn can consume the expected answer deterministically, before FAST
        is invoked. Only one pending action is tracked at a time (the most
        recent one wins).
        """
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            registry.pending_action = {str(k): str(v) for k, v in action.items()}
            self.save(registry)

    def clear_pending_action(self, conversation_id: str) -> None:
        """Clear a consumed pending action. Idempotent when already None.

        NOTE: prefer passing ``clear_pending=True`` to ``allocate_project``
        when the clear happens in the same logical operation as an allocation,
        so both changes land in a single atomic write (especially important on
        Windows where a second consecutive save to the same file can hit OS
        file-handle contention under test).
        """
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            if registry.pending_action is None:
                return
            registry.pending_action = None
            self.save(registry)

    def remove_project(self, conversation_id: str, project_id: str) -> None:
        """Rollback a registry entry whose project state failed to create.

        Also drops the active pointer when it referenced the removed project
        and any event->project mappings to it, so the registry never routes
        at a project that does not exist.
        """
        with self._lock_for(conversation_id):
            registry = self.load_or_create(conversation_id)
            registry.projects = [p for p in registry.projects if p.project_id != project_id]
            registry.event_projects = {
                k: v for k, v in registry.event_projects.items() if v != project_id
            }
            if registry.active_project_id == project_id:
                registry.active_project_id = None
            self.save(registry)


# ---------------------------------------------------------------------------
# Status derivation for LIST_PROJECTS (no LLM, derived from existing state)
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "PREVIEW_READY": "Preview ready",
    "PUBLISHING": "Publishing",
    "LIVE": "Live",
    "RUNNING": "Building",
    "QUEUED": "Queued",
    "REVISION_REQUESTED": "Revising",
    "PAUSED": "Paused",
    "FAILED": "Failed",
    "CANCELED": "Canceled",
    "READY": "Draft",
    "DISCOVERING": "Draft",
    "WAITING_INPUT": "Draft",
}


def project_status_label(state: Optional[ProjectState]) -> str:
    """Human-friendly status derived from the existing project state.

    Never exposes internal concepts (no lifecycle enum names, no error codes).
    """
    if state is None:
        return "Unknown"
    return _STATUS_LABELS.get(state.lifecycle, "Draft")
