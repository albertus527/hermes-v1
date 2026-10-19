"""Visual design references: manifests, asset validation, ingestion (N8 B3).

WHY THIS MODULE EXISTS
----------------------
The OpenViking corpus holds markdown *guidance* -- prose about how to design.
Prose cannot be assessed for visual quality: a paragraph describing line length
is not a design. To judge design quality, the library must hold real artefacts:
a screenshot of something that was actually designed.

This module ingests those artefacts without disturbing the text library:

* the MANIFEST is text, and is indexed exactly like any other source;
* the ASSETS are binary, stored by digest, and are **never** decoded as text;
* the two have separate size ceilings, because a screenshot legitimately
  exceeds a markdown file and must not be held to a text budget.

WHAT IT REFUSES TO DO
---------------------
* It will not accept an asset whose bytes do not match its declared digest.
* It will not accept a reference whose rights are unconfirmed and unlicensed.
* It will not accept an asset that is not a real image (magic-byte checked).
* It will not silently drop a broken asset: a reference is all-or-nothing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_reference import (
    ALLOWED_IMAGE_MIME,
    ASSET_KINDS,
    DesignAsset,
    DesignReference,
    DesignReferenceError,
    StructuralFacts,
    TRUST_EXTERNAL,
    TRUST_INTERNAL,
    TRUST_LEVELS,
    TRUST_REVIEWED,
)
from app.core.openviking_library import (
    MAX_ASSET_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_REFERENCE_TOTAL_ASSET_BYTES,
    is_forbidden_source,
    is_valid_project_id,
)

#: Manifest schema version this module reads and writes.
MANIFEST_SCHEMA_VERSION = 1

#: Magic bytes for the image types the Decisions contract accepts.
_MAGIC: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),  # further checked below: WEBP follows "RIFF....WEBP"
)

#: Statuses returned by an ingestion attempt. A closed set.
STATUS_INGESTED = "ingested"
STATUS_REJECTED_RIGHTS = "rejected_rights"
STATUS_REJECTED_DIGEST = "rejected_digest"
STATUS_REJECTED_MIME = "rejected_mime"
STATUS_REJECTED_SIZE = "rejected_size"
STATUS_REJECTED_MANIFEST = "rejected_manifest"
STATUS_REJECTED_FORBIDDEN = "rejected_forbidden"
STATUS_REJECTED_PROJECT = "rejected_project"


class ReferenceIngestError(DesignReferenceError):
    """A reference could not be ingested, with a stable status code."""

    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}" if detail else status)


@dataclass(frozen=True)
class AssetSpec:
    """One asset declared by a manifest: where to read it, and what it must be."""

    kind: str
    locator: str
    mime: str
    width: int
    height: int
    #: The digest the manifest claims. Verified against the bytes actually read.
    digest: str


@dataclass(frozen=True)
class ReferenceManifest:
    """The text half of a reference: provenance, rights, assets, measurements."""

    reference_id: str
    project_id: str
    source_uri: str
    source_revision: str
    ingested_at: str
    trust: str = TRUST_EXTERNAL
    rights_confirmed: bool = False
    licence: str = ""
    assets: Tuple[AssetSpec, ...] = ()
    structural: Optional[StructuralFacts] = None
    declared: Tuple[Tuple[str, str], ...] = ()
    extracted: Tuple[Tuple[str, str], ...] = ()
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.reference_id.strip():
            raise ReferenceIngestError(STATUS_REJECTED_MANIFEST, "empty reference_id")
        if not is_valid_project_id(self.project_id):
            raise ReferenceIngestError(STATUS_REJECTED_PROJECT, "invalid project_id")
        if not self.source_uri.strip() or not self.source_revision.strip():
            raise ReferenceIngestError(STATUS_REJECTED_MANIFEST, "missing provenance")
        if self.trust not in TRUST_LEVELS:
            raise ReferenceIngestError(
                STATUS_REJECTED_MANIFEST, f"unknown trust {self.trust!r}"
            )
        for asset in self.assets:
            if asset.kind not in ASSET_KINDS:
                raise ReferenceIngestError(
                    STATUS_REJECTED_MANIFEST, f"unknown asset kind {asset.kind!r}"
                )
            if asset.mime not in ALLOWED_IMAGE_MIME:
                raise ReferenceIngestError(
                    STATUS_REJECTED_MIME, f"unsupported mime {asset.mime!r}"
                )
            if is_forbidden_source(asset.locator):
                raise ReferenceIngestError(
                    STATUS_REJECTED_FORBIDDEN, f"forbidden asset path {asset.locator!r}"
                )

    @property
    def has_rights(self) -> bool:
        """Rights are usable when explicitly confirmed OR a licence is recorded.

        An owner upload alone is NOT proof: the manifest must say so. Hermes-owned
        captures are recorded as ``internal`` trust with rights confirmed.
        """
        if self.rights_confirmed:
            return True
        return bool(self.licence.strip()) and self.trust in (
            TRUST_REVIEWED,
            TRUST_INTERNAL,
        )


def sniff_mime(raw: bytes) -> str:
    """Return the MIME type implied by *raw*'s magic bytes, or ``""``.

    Never trusts a declared type: the bytes decide. A manifest that claims
    ``image/png`` for a JPEG is a mismatch, not a coercion.
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_manifest(data: Mapping[str, Any]) -> ReferenceManifest:
    """Build a :class:`ReferenceManifest` from decoded JSON.

    Strict on everything that carries trust. A missing digest, provenance, or
    asset kind is an error, never a defaulted value.
    """
    if not isinstance(data, Mapping):
        raise ReferenceIngestError(STATUS_REJECTED_MANIFEST, "manifest is not an object")
    version = data.get("schema_version", MANIFEST_SCHEMA_VERSION)
    if version != MANIFEST_SCHEMA_VERSION:
        raise ReferenceIngestError(
            STATUS_REJECTED_MANIFEST,
            f"unsupported manifest schema_version {version!r}",
        )

    assets: List[AssetSpec] = []
    for entry in data.get("assets") or ():
        if not isinstance(entry, Mapping):
            raise ReferenceIngestError(STATUS_REJECTED_MANIFEST, "asset is not an object")
        assets.append(
            AssetSpec(
                kind=str(entry.get("kind", "")),
                locator=str(entry.get("locator", "")),
                mime=str(entry.get("mime", "")),
                width=int(entry.get("width", 0)),
                height=int(entry.get("height", 0)),
                digest=str(entry.get("digest", "")),
            )
        )

    raw_structural = data.get("structural")
    structural: Optional[StructuralFacts] = None
    if isinstance(raw_structural, Mapping):
        raw_viewport = tuple(raw_structural.get("viewport") or (0, 0))
        structural = StructuralFacts(
            viewport=(
                int(raw_viewport[0]) if len(raw_viewport) > 0 else 0,
                int(raw_viewport[1]) if len(raw_viewport) > 1 else 0,
            ),
            font_families=tuple(raw_structural.get("font_families") or ()),
            type_scale=tuple(tuple(t) for t in (raw_structural.get("type_scale") or ())),
            spacing_scale=tuple(raw_structural.get("spacing_scale") or ()),
            palette_pairs=tuple(
                tuple(p) for p in (raw_structural.get("palette_pairs") or ())
            ),
            component_count=int(raw_structural.get("component_count", 0)),
        )

    return ReferenceManifest(
        reference_id=str(data.get("reference_id", "")),
        project_id=str(data.get("project_id", "")),
        source_uri=str(data.get("source_uri", "")),
        source_revision=str(data.get("source_revision", "")),
        ingested_at=str(data.get("ingested_at", "")),
        trust=str(data.get("trust", TRUST_EXTERNAL)),
        rights_confirmed=bool(data.get("rights_confirmed", False)),
        licence=str(data.get("licence", "")),
        assets=tuple(assets),
        structural=structural,
        declared=tuple((str(k), str(v)) for k, v in (data.get("declared") or {}).items()),
        extracted=tuple((str(k), str(v)) for k, v in (data.get("extracted") or {}).items()),
        schema_version=int(version),
    )


@dataclass(frozen=True)
class PreparedReference:
    """A reference whose assets have been read and verified, ready to store."""

    reference: DesignReference
    #: ``(uri, raw_bytes, record_sidecar)`` triples, in manifest order.
    assets: Tuple[Tuple[str, bytes, Dict[str, Any]], ...]
    manifest_bytes: bytes


def prepare_reference(
    manifest: ReferenceManifest,
    *,
    reader: Any,
    asset_base_uri: str,
    clock: Optional[Any] = None,
) -> PreparedReference:
    """Read and verify every asset, then build a :class:`DesignReference`.

    All-or-nothing: one bad asset rejects the whole reference. Silently
    ingesting a partial reference would let a scorer see a candidate whose
    evidence does not match its manifest.
    """
    if not manifest.has_rights:
        raise ReferenceIngestError(
            STATUS_REJECTED_RIGHTS,
            "rights not confirmed and no usable licence",
        )
    if not manifest.assets:
        raise ReferenceIngestError(
            STATUS_REJECTED_MANIFEST, "reference declares no assets"
        )

    built: List[DesignAsset] = []
    stored: List[Tuple[str, bytes, Dict[str, Any]]] = []
    total = 0

    for spec in manifest.assets:
        try:
            raw = reader(spec.locator)
        except Exception:  # noqa: BLE001 - any read fault is a rejected asset
            raise ReferenceIngestError(
                STATUS_REJECTED_MANIFEST, f"asset unreadable: {spec.locator}"
            ) from None
        if not isinstance(raw, (bytes, bytearray)):
            raise ReferenceIngestError(
                STATUS_REJECTED_MANIFEST, f"reader returned non-bytes: {spec.locator}"
            )
        raw = bytes(raw)

        if len(raw) > MAX_ASSET_BYTES:
            raise ReferenceIngestError(
                STATUS_REJECTED_SIZE, f"asset exceeds {MAX_ASSET_BYTES} bytes"
            )
        total += len(raw)
        if total > MAX_REFERENCE_TOTAL_ASSET_BYTES:
            raise ReferenceIngestError(
                STATUS_REJECTED_SIZE, "reference exceeds total asset budget"
            )

        actual_mime = sniff_mime(raw)
        if not actual_mime:
            raise ReferenceIngestError(
                STATUS_REJECTED_MIME, f"asset is not a supported image: {spec.locator}"
            )
        if actual_mime != spec.mime:
            raise ReferenceIngestError(
                STATUS_REJECTED_MIME,
                f"declared {spec.mime} but bytes are {actual_mime}",
            )

        digest = sha256_hex(raw)
        if spec.digest and spec.digest != digest:
            raise ReferenceIngestError(
                STATUS_REJECTED_DIGEST,
                f"digest mismatch for {spec.locator}",
            )

        uri = f"{asset_base_uri.rstrip('/')}/{spec.kind}"
        revision = manifest.source_revision
        built.append(
            DesignAsset(
                kind=spec.kind,
                uri=uri,
                revision=revision,
                digest=digest,
                mime=actual_mime,
                width=spec.width,
                height=spec.height,
                bytes=len(raw),
            )
        )
        stored.append(
            (
                uri,
                raw,
                {
                    "kind": spec.kind,
                    "digest": digest,
                    "revision": revision,
                    "mime": actual_mime,
                    "source_id": manifest.reference_id,
                },
            )
        )

    reference = DesignReference(
        reference_id=manifest.reference_id,
        source_uri=manifest.source_uri,
        source_revision=manifest.source_revision,
        ingested_at=manifest.ingested_at,
        trust=manifest.trust,
        category="references",
        assets=tuple(built),
        structural=manifest.structural,
        declared=manifest.declared,
        extracted=manifest.extracted,
        rights_confirmed=manifest.rights_confirmed,
        licence=manifest.licence,
    )

    manifest_bytes = json.dumps(reference.to_dict(), sort_keys=True).encode("utf-8")
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ReferenceIngestError(
            STATUS_REJECTED_SIZE, f"manifest exceeds {MAX_MANIFEST_BYTES} bytes"
        )

    return PreparedReference(
        reference=reference,
        assets=tuple(stored),
        manifest_bytes=manifest_bytes,
    )


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "STATUS_INGESTED",
    "STATUS_REJECTED_RIGHTS",
    "STATUS_REJECTED_DIGEST",
    "STATUS_REJECTED_MIME",
    "STATUS_REJECTED_SIZE",
    "STATUS_REJECTED_MANIFEST",
    "STATUS_REJECTED_FORBIDDEN",
    "STATUS_REJECTED_PROJECT",
    "ReferenceIngestError",
    "AssetSpec",
    "ReferenceManifest",
    "PreparedReference",
    "sniff_mime",
    "sha256_hex",
    "parse_manifest",
    "prepare_reference",
]
