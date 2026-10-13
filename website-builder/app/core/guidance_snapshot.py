"""Per-invocation guidance snapshots and QA/VISION parity (N6-E).

N6-E is about *evidence*: the exact document version each FRONTEND invocation was
given must be recorded at the moment of that invocation, and QA/VISION must
verify against **that record** — never against a digest merely recomputed from
whatever happens to be on disk later.

Three properties are load-bearing.

**Recorded at the invocation, not inferred.** ``record_invocation`` is called
from the single FRONTEND choke point, so it captures PRD.md, DesignGuidance.md
and (when present) Design.md exactly as they were when the model ran. A document
created *after* the invocation — Design.md is compiled after FRONTEND produces
the Design DNA — is recorded as absent, never retroactively represented as input
(requirement 4).

**Bounded, never overwriting current-release evidence.** The history keeps a
bounded number of recent invocations, and the newest record (the one bound to the
current ``source_revision``) is always retained, so evidence the current release
depends on can never be pruned away.

**A true mismatch fails closed.** ``verify_invocation`` compares the RECORDED
digest of every document that was used against the document now on disk. A
document that was used and has since changed — or has disappeared — is a hard
inconsistency (``GUIDANCE_VERSION_MISMATCH``). A document that was absent at
invocation and appeared later is *not* a mismatch; that is the expected
post-build Design.md. Parity is proven against the record, never inferred.

This module introduces no new state machine: the record lives in the existing
``state.deployment`` mapping, beside the document digests N2 already writes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from app.core.document_contracts import ERR_GUIDANCE_VERSION_MISMATCH

#: Key under which per-invocation guidance snapshots are recorded in
#: ``state.deployment``.
DEPLOYMENT_GUIDANCE_KEY = "frontend_guidance"

#: Maximum number of invocation snapshots retained. Small and bounded, because
#: this is diagnostic evidence, not a log.
MAX_SNAPSHOT_HISTORY = 20

#: The document kinds a snapshot tracks, mapped to their snapshot key.
_SNAPSHOT_FIELDS = (
    ("prd", "prd_digest"),
    ("design", "design_digest"),
    ("design_guidance", "design_guidance_digest"),
)


def _snapshot_digests(workspace) -> Dict[str, Optional[str]]:
    """Return the per-document digests present in ``workspace`` right now."""
    from app.core.document_store import read_document

    result: Dict[str, Optional[str]] = {}
    for kind, key in _SNAPSHOT_FIELDS:
        stored = read_document(workspace, kind)
        result[key] = stored.digest if stored else None
    return result


def record_invocation(
    store,
    project_id: str,
    workspace,
    *,
    invocation_id: str,
) -> Optional[Dict[str, Any]]:
    """Record the guidance snapshot for one FRONTEND invocation.

    Called from the FRONTEND choke point, immediately BEFORE the model runs, so
    the snapshot is exactly the documents that invocation was given. Returns the
    record written, or ``None`` when the workspace carries no documents (a
    legacy project — nothing to record, and the deployment mapping is untouched).
    """
    from app.core.document_guidance import guidance_digest

    digests = _snapshot_digests(workspace)
    if all(value is None for value in digests.values()):
        return None

    with store.acquire_writer(project_id) as state:
        revision = state.revisions.source_revision
        record: Dict[str, Any] = {
            "invocation_id": str(invocation_id),
            "source_revision": revision,
            **digests,
            "combined_digest": guidance_digest(
                _read_text(workspace, "prd"),
                _read_text(workspace, "design"),
                _read_text(workspace, "design_guidance"),
            ),
        }
        existing = state.deployment.get(DEPLOYMENT_GUIDANCE_KEY)
        history: List[Dict[str, Any]] = (
            list(existing.get("history") or []) if isinstance(existing, dict) else []
        )
        # Replace a same-invocation entry (idempotent re-record), else append.
        history = [h for h in history if h.get("invocation_id") != record["invocation_id"]]
        history.append(record)
        # Bound the history but NEVER drop the newest record, which is the one the
        # current release depends on.
        if len(history) > MAX_SNAPSHOT_HISTORY:
            history = history[-MAX_SNAPSHOT_HISTORY:]
        state.deployment[DEPLOYMENT_GUIDANCE_KEY] = {
            "latest_invocation_id": record["invocation_id"],
            "latest_source_revision": revision,
            "history": history,
        }
        store.save(state)
    return record


def _read_text(workspace, kind: str) -> Optional[str]:
    from app.core.document_store import read_document

    stored = read_document(workspace, kind)
    return stored.text if stored else None


def latest_invocation(state) -> Optional[Dict[str, Any]]:
    """Return the most recently recorded invocation snapshot, or ``None``."""
    block = state.deployment.get(DEPLOYMENT_GUIDANCE_KEY)
    if not isinstance(block, Mapping):
        return None
    history = block.get("history") or []
    if not history:
        return None
    return history[-1]


def find_invocation(state, invocation_id: str) -> Optional[Dict[str, Any]]:
    """Return the recorded snapshot for ``invocation_id``, or ``None``."""
    block = state.deployment.get(DEPLOYMENT_GUIDANCE_KEY)
    if not isinstance(block, Mapping):
        return None
    for record in block.get("history") or []:
        if record.get("invocation_id") == invocation_id:
            return record
    return None


def verify_invocation(workspace, record: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return ``None`` when the workspace still matches ``record``, else a reason.

    The rule is deliberately asymmetric, and that asymmetry is the whole point:

    * A document that WAS used (a non-``None`` recorded digest) must still be
      present and unchanged. Changed or missing is a true mismatch — the exact
      ``GUIDANCE_VERSION_MISMATCH`` this module exists to catch.
    * A document that was ABSENT at invocation (``None``) is not required to
      remain absent. The post-build ``Design.md`` is written *after* FRONTEND by
      design, so requiring it to stay absent would forbid the normal flow; and
      treating it as "now present, therefore parity" would be inferring parity
      from the current workspace — exactly what must not happen.

    A ``None`` record (no documents at invocation) verifies trivially.
    """
    if not record:
        return None
    from app.core.document_store import read_document

    for kind, key in _SNAPSHOT_FIELDS:
        recorded = record.get(key)
        if recorded is None:
            # Absent at invocation: its later appearance is expected, not a
            # mismatch. We do NOT infer parity from it either.
            continue
        stored = read_document(workspace, kind)
        if stored is None:
            return (
                f"{ERR_GUIDANCE_VERSION_MISMATCH}: {kind} document was used at "
                f"invocation {record.get('invocation_id')} but is now missing"
            )
        if stored.digest != recorded:
            return (
                f"{ERR_GUIDANCE_VERSION_MISMATCH}: {kind} document changed since "
                f"invocation {record.get('invocation_id')} "
                f"(used {recorded}, now {stored.digest})"
            )
    return None


__all__ = [
    "DEPLOYMENT_GUIDANCE_KEY",
    "MAX_SNAPSHOT_HISTORY",
    "record_invocation",
    "latest_invocation",
    "find_invocation",
    "verify_invocation",
]
