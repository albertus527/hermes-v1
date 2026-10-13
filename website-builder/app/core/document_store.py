"""Git-backed document storage for Website Builder R2 (N2).

The Requirement Creator produces ``PRD.md`` and ``Design.md`` text. This module
is the single place those documents are written into a project workspace, read
back, and bound to the release source.

Design constraints (from the approved roadmap and the N1 architecture contract):

**No second state machine.** A document's identity is *derived* from the
workspace's ``source_revision`` and the project id — both already owned by the
existing revision flow. This module persists no state of its own beyond the
document files and a small digest record the caller may store in the existing
``state.deployment`` mapping. It never introduces a lifecycle, a queue, or a
release pointer.

**Same release commit.** Documents live *inside* the workspace, so they ride the
existing source snapshot: whatever commit publishes ``src/`` publishes the
documents beside it. Writing them happens before the source snapshot is taken,
so they are part of the checked bytes.

**Deterministic and idempotent.** Writing the same documents twice produces
byte-identical files; a re-read returns the same text. Digests are SHA-256 of the
exact bytes.

**Legacy is a first-class state.** A workspace with no documents reads back as
``None`` for each — never an error, never a fabricated empty document. This is
what lets R1 projects continue untouched.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from app.core.document_contracts import (
    DESIGN_FILE_NAME,
    DESIGN_GUIDANCE_FILE_NAME,
    DOCUMENT_KIND_DESIGN,
    DOCUMENT_KIND_DESIGN_GUIDANCE,
    DOCUMENT_KIND_PRD,
    DOCUMENT_KINDS,
    PRD_FILE_NAME,
    DocumentIdentity,
    assess_presence,
    identity_from_front_matter,
    parse_front_matter,
    validate_design,
    validate_design_guidance,
    validate_prd,
)

#: Key under which document digests are recorded in the existing
#: ``state.deployment`` mapping. Chosen to sit beside the other publication
#: evidence without creating a new top-level state field.
DEPLOYMENT_DOCUMENTS_KEY = "documents"


@dataclass(frozen=True)
class StoredDocument:
    """One document as it exists on disk, with its derived identity and digest."""

    kind: str
    file_name: str
    text: str
    digest: str
    identity: DocumentIdentity

    def to_record(self) -> Dict[str, Any]:
        """A JSON-safe provenance record for ``state.deployment``."""
        return {
            "kind": self.kind,
            "file": self.file_name,
            "digest": self.digest,
            "document_id": self.identity.document_id,
            "revision_id": self.identity.revision_id,
            "source_revision": self.identity.source_revision,
        }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_path(workspace: Path, kind: str) -> Path:
    """Return the canonical path of a document inside ``workspace``."""
    if kind == DOCUMENT_KIND_PRD:
        return Path(workspace) / PRD_FILE_NAME
    if kind == DOCUMENT_KIND_DESIGN:
        return Path(workspace) / DESIGN_FILE_NAME
    if kind == DOCUMENT_KIND_DESIGN_GUIDANCE:
        return Path(workspace) / DESIGN_GUIDANCE_FILE_NAME
    raise ValueError(f"unknown document kind: {kind!r}")


def _validate(kind: str, text: str):
    """Validate ``text`` for ``kind``, returning ``(identity, violations)``."""
    if kind == DOCUMENT_KIND_PRD:
        return validate_prd(text)
    if kind == DOCUMENT_KIND_DESIGN:
        return validate_design(text)
    if kind == DOCUMENT_KIND_DESIGN_GUIDANCE:
        return validate_design_guidance(text)
    raise ValueError(f"unknown document kind: {kind!r}")


def write_document(workspace: Path, kind: str, text: str) -> StoredDocument:
    """Write a validated document into ``workspace`` and return its record.

    The text is validated **before** anything touches disk: an invalid document
    is refused, never written. This is what keeps a malformed compile from
    becoming a "document" a later reader would trust.

    The write is atomic (temp file + ``os.replace``) so a crash cannot leave a
    half-written document inside the release source.
    """
    identity, violations = _validate(kind, text)

    if violations:
        raise ValueError(
            f"refusing to write invalid {kind} document: "
            + ", ".join(v.code for v in violations)
        )
    if identity is None:  # defensive: validators return an identity when clean
        raise ValueError(f"refusing to write {kind} document with no identity")

    path = document_path(workspace, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return StoredDocument(
        kind=kind,
        file_name=path.name,
        text=text,
        digest=_digest(text),
        identity=identity,
    )


def read_document(workspace: Path, kind: str) -> Optional[StoredDocument]:
    """Read a document back, or ``None`` when it is absent/unreadable/invalid.

    ``None`` covers every reason the document is unusable, so a caller has one
    branch. This mirrors :func:`app.core.design_dna.load_persisted_design_dna`.

    ``UnicodeDecodeError`` is caught explicitly: a binary or corrupt file is not
    an ``OSError``, so without it a damaged document would raise out of a reader
    whose entire contract is to answer ``None`` for anything unusable.
    """
    path = document_path(workspace, kind)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    identity, violations = _validate(kind, text)
    if violations or identity is None:
        return None
    return StoredDocument(
        kind=kind,
        file_name=path.name,
        text=text,
        digest=_digest(text),
        identity=identity,
    )


def store_documents(
    workspace: Path,
    *,
    prd_text: Optional[str] = None,
    design_text: Optional[str] = None,
) -> Tuple[Optional[StoredDocument], Optional[StoredDocument]]:
    """Write whichever documents were supplied; return ``(prd, design)``.

    A ``None`` argument writes nothing for that kind, so a caller that has only
    a PRD does not fabricate a Design.md. Writing is all-or-nothing per kind and
    the function is idempotent for identical text.
    """
    prd = write_document(workspace, DOCUMENT_KIND_PRD, prd_text) if prd_text else None
    design = (
        write_document(workspace, DOCUMENT_KIND_DESIGN, design_text)
        if design_text
        else None
    )
    return prd, design


def store_design_guidance(workspace: Path, guidance_text: str) -> StoredDocument:
    """Write ``DesignGuidance.md`` (N6, pre-build) and return its record.

    A dedicated function rather than a third slot in :func:`store_documents`, so
    the existing ``(prd, design)`` contract that callers already depend on is
    untouched.
    """
    return write_document(workspace, DOCUMENT_KIND_DESIGN_GUIDANCE, guidance_text)


def read_design_guidance(workspace: Path) -> Optional[StoredDocument]:
    """Read ``DesignGuidance.md`` (N6), or ``None`` when absent/unusable."""
    return read_document(workspace, DOCUMENT_KIND_DESIGN_GUIDANCE)


def read_documents(workspace: Path) -> Tuple[Optional[StoredDocument], Optional[StoredDocument]]:
    """Read ``(prd, design)`` from ``workspace``; either may be ``None``."""
    return (
        read_document(workspace, DOCUMENT_KIND_PRD),
        read_document(workspace, DOCUMENT_KIND_DESIGN),
    )


def documents_present(workspace: Path) -> bool:
    """Return ``True`` iff the workspace carries both canonical documents."""
    prd, design = read_documents(workspace)
    return prd is not None and design is not None


def document_presence_record(workspace: Path) -> Dict[str, Any]:
    """Return a presence summary for a workspace (legacy-aware, non-failing)."""
    prd, design = read_documents(workspace)
    presence = assess_presence(
        prd_text=prd.text if prd else None,
        design_text=design.text if design else None,
    )
    return {
        "legacy": presence.legacy,
        "partial": presence.partial,
        "has_documents": presence.has_documents,
    }


def document_records(
    prd: Optional[StoredDocument],
    design: Optional[StoredDocument],
) -> Optional[Dict[str, Any]]:
    """Build the ``state.deployment['documents']`` record, or ``None``.

    Returns ``None`` when no document is present, so a legacy project's
    deployment mapping is left byte-identical (the key is simply absent).
    """
    entries = [d.to_record() for d in (prd, design) if d is not None]
    if not entries:
        return None
    return {
        "entries": entries,
        "digests": {e["kind"]: e["digest"] for e in entries},
    }


def verify_records_match_workspace(
    workspace: Path,
    records: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """Return ``None`` if the stored records match the workspace, else a reason.

    This is the hydration-consistency check: a revision must evaluate the
    documents of the exact source it is building. A digest mismatch means the
    recorded identity no longer describes the bytes on disk, which must be
    treated as a hard inconsistency rather than silently accepted.
    """
    if not records:
        return None
    entries = records.get("entries") or []
    for entry in entries:
        kind = entry.get("kind")
        if kind not in DOCUMENT_KINDS:
            return f"unknown document kind in records: {kind!r}"
        stored = read_document(workspace, kind)
        if stored is None:
            return f"recorded {kind} document is missing from the workspace"
        if stored.digest != entry.get("digest"):
            return (
                f"recorded {kind} digest {entry.get('digest')} does not match "
                f"workspace digest {stored.digest}"
            )
    return None


__all__ = [
    "DEPLOYMENT_DOCUMENTS_KEY",
    "StoredDocument",
    "document_path",
    "write_document",
    "read_document",
    "store_documents",
    "store_design_guidance",
    "read_design_guidance",
    "read_documents",
    "documents_present",
    "document_presence_record",
    "document_records",
    "verify_records_match_workspace",
]
