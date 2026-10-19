"""Durable, versioned design-evidence contracts (N8 B2).

WHAT THIS MODULE IS FOR
-----------------------
A Design candidate must be judged on evidence that actually supports the
judgment. This module defines the *only* vocabulary in which that evidence may
be described, so that a later scorer cannot quietly upgrade a claim into a
measurement.

FOUR KINDS OF KNOWLEDGE, KEPT APART
-----------------------------------
The owner requires observed evidence, declared metadata, inferred judgments and
computed measurements to remain distinguishable. They are separated here at the
type level:

``EVIDENCE_DECLARED``
    Metadata the SOURCE says about itself -- a palette, a style name, a prose
    description. Cheap, useful for retrieval, and **never** proof of quality.
``EVIDENCE_RENDERED``
    A real screenshot of the artefact. Shows what it looks like; cannot show a
    contrast ratio, a breakpoint, or a behaviour.
``EVIDENCE_STRUCTURAL``
    A measurement taken from the artefact -- computed font sizes, spacing scale,
    foreground/background pairs with a computed ratio.
``EVIDENCE_INTERACTIVE``
    An observation of behaviour. Only this class may support a claim about
    interaction.

``DesignReference.declared`` and ``DesignReference.extracted`` are deliberately
two separate fields rather than one mapping: merging them would destroy the
distinction the moment anything read it.

WHAT THIS MODULE DOES NOT DO
----------------------------
No filesystem access, no network, no retrieval, no scoring. It is a set of
immutable value objects plus pure predicates. Storage, indexing and scoring live
in later batches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Evidence classes
# ---------------------------------------------------------------------------

#: Nothing is known.
EVIDENCE_NONE = "E0"
#: The source DECLARES it. Metadata, labels, prose. Never proof of quality.
EVIDENCE_DECLARED = "E1"
#: A real screenshot exists. Proof of appearance, nothing more.
EVIDENCE_RENDERED = "E2"
#: A measurement was taken from the artefact (computed styles, contrast ratio).
EVIDENCE_STRUCTURAL = "E3"
#: Behaviour was observed. The only class that may support an interaction claim.
EVIDENCE_INTERACTIVE = "E4"

#: Ordered weakest to strongest.
EVIDENCE_LEVELS: Tuple[str, ...] = (
    EVIDENCE_NONE,
    EVIDENCE_DECLARED,
    EVIDENCE_RENDERED,
    EVIDENCE_STRUCTURAL,
    EVIDENCE_INTERACTIVE,
)

#: Rank lookup for comparisons.
EVIDENCE_RANK: Mapping[str, int] = {
    level: index for index, level in enumerate(EVIDENCE_LEVELS)
}

#: The weakest class that may support a *quality* judgment at all. Anything
#: below this is a claim, not evidence.
EVIDENCE_SCORABLE_FROM = EVIDENCE_RENDERED

# ---------------------------------------------------------------------------
# Asset vocabulary
# ---------------------------------------------------------------------------

ASSET_SCREENSHOT_DESKTOP = "screenshot_desktop"
ASSET_SCREENSHOT_MOBILE = "screenshot_mobile"
ASSET_HTML = "html"
ASSET_THUMBNAIL = "thumbnail"

#: A closed set: an asset that is not one of these is not something a consumer
#: knows how to read.
ASSET_KINDS: Tuple[str, ...] = (
    ASSET_SCREENSHOT_DESKTOP,
    ASSET_SCREENSHOT_MOBILE,
    ASSET_HTML,
    ASSET_THUMBNAIL,
)

#: Kinds that constitute *rendered* visual evidence.
SCREENSHOT_KINDS: Tuple[str, ...] = (
    ASSET_SCREENSHOT_DESKTOP,
    ASSET_SCREENSHOT_MOBILE,
)

#: MIME types the Decisions contract accepts for an image part.
ALLOWED_IMAGE_MIME: Tuple[str, ...] = ("image/png", "image/jpeg", "image/webp")

#: Trust levels, mirroring the OpenViking library's closed set.
TRUST_REVIEWED = "reviewed"
TRUST_INTERNAL = "internal"
TRUST_EXTERNAL = "external"
TRUST_LEVELS: Tuple[str, ...] = (TRUST_REVIEWED, TRUST_INTERNAL, TRUST_EXTERNAL)

#: The rubric dimensions this schema describes evidence for. A closed set, so a
#: typo cannot invent a dimension that no scorer knows how to handle.
DIMENSION_HIERARCHY = "hierarchy"
DIMENSION_TYPOGRAPHY = "typography"
DIMENSION_LAYOUT = "layout"
DIMENSION_COLOR = "color"
DIMENSION_USABILITY = "usability"
DIMENSION_CONSISTENCY = "consistency"

DIMENSIONS: Tuple[str, ...] = (
    DIMENSION_HIERARCHY,
    DIMENSION_TYPOGRAPHY,
    DIMENSION_LAYOUT,
    DIMENSION_COLOR,
    DIMENSION_USABILITY,
    DIMENSION_CONSISTENCY,
)

#: Dimensions whose *measured* form requires ``StructuralFacts``. Without it
#: they are capped at rendered evidence.
STRUCTURAL_DIMENSIONS: Tuple[str, ...] = (
    DIMENSION_TYPOGRAPHY,
    DIMENSION_LAYOUT,
    DIMENSION_COLOR,
)

#: Current schema version. Bumped whenever the shape below changes in a way a
#: consumer must notice.
SCHEMA_VERSION = 1

_SHA256_HEX = frozenset("0123456789abcdef")


class DesignReferenceError(ValueError):
    """A reference or asset is malformed, stale, or unbound."""


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignAsset:
    """One stored artefact belonging to a reference.

    ``digest`` is the SHA-256 of the EXACT bytes that will be attached to a
    scoring request. It is the binding that makes "the screenshot shown to the
    model" and "the screenshot recorded at ingestion" the same object: any
    mismatch is a hard reject, never a repair.
    """

    kind: str
    uri: str
    revision: str
    digest: str
    mime: str
    width: int
    height: int
    bytes: int

    def __post_init__(self) -> None:
        if self.kind not in ASSET_KINDS:
            raise DesignReferenceError(f"unknown asset kind {self.kind!r}")
        if self.mime not in ALLOWED_IMAGE_MIME:
            raise DesignReferenceError(f"unsupported mime {self.mime!r}")
        if not self.uri.strip():
            raise DesignReferenceError("asset uri is empty")
        if not self.revision.strip():
            raise DesignReferenceError("asset revision is empty")
        if len(self.digest) != 64 or not set(self.digest) <= _SHA256_HEX:
            raise DesignReferenceError("asset digest is not a sha256 hex digest")
        if self.width <= 0 or self.height <= 0:
            raise DesignReferenceError("asset dimensions must be positive")
        if self.bytes <= 0:
            raise DesignReferenceError("asset size must be positive")

    @property
    def is_screenshot(self) -> bool:
        return self.kind in SCREENSHOT_KINDS

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "uri": self.uri,
            "revision": self.revision,
            "digest": self.digest,
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
        }


# ---------------------------------------------------------------------------
# Structural measurements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructuralFacts:
    """Measurements taken from the artefact itself.

    Every field is optional because not every reference can be measured. What
    matters is that a *populated* field means a measurement happened -- these
    are not declarations copied from metadata.

    ``palette_pairs`` carries ``(foreground, background, contrast_ratio)``. The
    ratio is the measured value, which is why colour/contrast can reach E3 only
    when this is populated: a screenshot cannot yield a ratio.
    """

    viewport: Tuple[int, int] = (0, 0)
    font_families: Tuple[str, ...] = ()
    #: ``(selector, font_size_px, line_height_px)`` triples.
    type_scale: Tuple[Tuple[str, float, float], ...] = ()
    #: Measured spacing steps, in px.
    spacing_scale: Tuple[float, ...] = ()
    #: ``(foreground, background, contrast_ratio)`` triples.
    palette_pairs: Tuple[Tuple[str, str, float], ...] = ()
    component_count: int = 0

    @property
    def has_typography(self) -> bool:
        """True only when a family AND a scale were actually measured."""
        return bool(self.font_families) and bool(self.type_scale)

    @property
    def has_spacing(self) -> bool:
        return bool(self.spacing_scale)

    @property
    def has_contrast(self) -> bool:
        """True only when at least one pair carries a measured ratio."""
        return any(pair[2] > 0.0 for pair in self.palette_pairs)

    def to_dict(self) -> dict:
        return {
            "viewport": list(self.viewport),
            "font_families": list(self.font_families),
            "type_scale": [list(t) for t in self.type_scale],
            "spacing_scale": list(self.spacing_scale),
            "palette_pairs": [list(p) for p in self.palette_pairs],
            "component_count": self.component_count,
        }


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignReference:
    """One real design reference, with the provenance and evidence it carries.

    A reference is *not* a guidance document. Guidance describes how to design;
    a reference is an artefact that was designed, and therefore something whose
    quality can be assessed.
    """

    reference_id: str
    source_uri: str
    source_revision: str
    ingested_at: str
    trust: str = TRUST_EXTERNAL
    category: str = "references"
    assets: Tuple[DesignAsset, ...] = ()
    structural: Optional[StructuralFacts] = None
    #: What the SOURCE claims about itself. Never proof of quality.
    declared: Tuple[Tuple[str, str], ...] = ()
    #: What WE measured. Kept apart from ``declared`` on purpose.
    extracted: Tuple[Tuple[str, str], ...] = ()
    #: Whether the owner confirmed usage rights. Owner upload alone is not proof.
    rights_confirmed: bool = False
    #: Licence or permission reference. Required for externally-sourced material.
    licence: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.reference_id.strip():
            raise DesignReferenceError("reference_id is empty")
        if not self.source_uri.strip() or not self.source_revision.strip():
            raise DesignReferenceError("reference lacks provenance")
        if self.trust not in TRUST_LEVELS:
            raise DesignReferenceError(f"unknown trust level {self.trust!r}")
        seen: set = set()
        for asset in self.assets:
            if asset.uri in seen:
                raise DesignReferenceError(f"duplicate asset uri {asset.uri!r}")
            seen.add(asset.uri)

    # -- visual evidence ----------------------------------------------------

    @property
    def screenshots(self) -> Tuple[DesignAsset, ...]:
        return tuple(a for a in self.assets if a.is_screenshot)

    @property
    def has_visual_evidence(self) -> bool:
        """True only when a RENDERED artefact exists. A palette is not enough."""
        return bool(self.screenshots)

    @property
    def viewports(self) -> Tuple[str, ...]:
        return tuple(a.kind for a in self.screenshots)

    @property
    def is_measurable(self) -> bool:
        return self.structural is not None

    def asset(self, kind: str) -> Optional[DesignAsset]:
        for candidate in self.assets:
            if candidate.kind == kind:
                return candidate
        return None

    # -- evidence classification -------------------------------------------

    def evidence_for(self, dimension: str) -> str:
        """The strongest evidence class available for *dimension*.

        The rules encode what each kind of evidence can and cannot prove:

        * no screenshot  -> ``E0``. Declared metadata is never promoted.
        * screenshot only -> ``E2``. Appearance, not measurement.
        * typography/layout/colour reach ``E3`` only when the matching
          measurement is actually present in ``StructuralFacts``.
        * usability can never exceed ``E2`` here: a screenshot shows
          affordances, never behaviour. Interaction needs ``E4``, which this
          schema deliberately cannot manufacture from a picture.
        * consistency needs at least two captured surfaces to mean anything;
          with one it stays ``E2`` and the scorer must not use it alone.
        """
        if dimension not in DIMENSIONS:
            return EVIDENCE_NONE
        if not self.has_visual_evidence:
            return EVIDENCE_NONE

        if dimension == DIMENSION_USABILITY:
            # A picture shows affordances, not behaviour.
            return EVIDENCE_RENDERED

        if dimension == DIMENSION_CONSISTENCY:
            return EVIDENCE_RENDERED

        facts = self.structural
        if facts is not None:
            if dimension == DIMENSION_TYPOGRAPHY and facts.has_typography:
                return EVIDENCE_STRUCTURAL
            if dimension == DIMENSION_LAYOUT and facts.has_spacing:
                return EVIDENCE_STRUCTURAL
            if dimension == DIMENSION_COLOR and facts.has_contrast:
                return EVIDENCE_STRUCTURAL

        return EVIDENCE_RENDERED

    def evidence_map(self) -> Tuple[Tuple[str, str], ...]:
        """Every rubric dimension mapped to its evidence class, in fixed order."""
        return tuple((d, self.evidence_for(d)) for d in DIMENSIONS)

    def is_scorable(self, dimension: str) -> bool:
        """True only when the evidence class may support a quality judgment."""
        level = self.evidence_for(dimension)
        return EVIDENCE_RANK[level] >= EVIDENCE_RANK[EVIDENCE_SCORABLE_FROM]

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "reference_id": self.reference_id,
            "source_uri": self.source_uri,
            "source_revision": self.source_revision,
            "ingested_at": self.ingested_at,
            "trust": self.trust,
            "category": self.category,
            "assets": [a.to_dict() for a in self.assets],
            "structural": self.structural.to_dict() if self.structural else None,
            "declared": dict(self.declared),
            "extracted": dict(self.extracted),
            "rights_confirmed": self.rights_confirmed,
            "licence": self.licence,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DesignReference":
        """Rebuild a reference from ``to_dict()`` output.

        Strict on the parts that carry trust: a missing digest or provenance is
        an error, never a defaulted value.
        """
        if not isinstance(data, Mapping):
            raise DesignReferenceError("reference payload is not a mapping")
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise DesignReferenceError(
                f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})"
            )
        assets = tuple(
            DesignAsset(
                kind=str(a.get("kind", "")),
                uri=str(a.get("uri", "")),
                revision=str(a.get("revision", "")),
                digest=str(a.get("digest", "")),
                mime=str(a.get("mime", "")),
                width=int(a.get("width", 0)),
                height=int(a.get("height", 0)),
                bytes=int(a.get("bytes", 0)),
            )
            for a in (data.get("assets") or ())
        )
        raw_structural = data.get("structural")
        structural: Optional[StructuralFacts] = None
        if isinstance(raw_structural, Mapping):
            raw_viewport = tuple(raw_structural.get("viewport") or (0, 0))
            viewport: Tuple[int, int] = (
                int(raw_viewport[0]) if len(raw_viewport) > 0 else 0,
                int(raw_viewport[1]) if len(raw_viewport) > 1 else 0,
            )
            structural = StructuralFacts(
                viewport=viewport,
                font_families=tuple(raw_structural.get("font_families") or ()),
                type_scale=tuple(
                    tuple(t) for t in (raw_structural.get("type_scale") or ())
                ),
                spacing_scale=tuple(raw_structural.get("spacing_scale") or ()),
                palette_pairs=tuple(
                    tuple(p) for p in (raw_structural.get("palette_pairs") or ())
                ),
                component_count=int(raw_structural.get("component_count", 0)),
            )
        return cls(
            reference_id=str(data.get("reference_id", "")),
            source_uri=str(data.get("source_uri", "")),
            source_revision=str(data.get("source_revision", "")),
            ingested_at=str(data.get("ingested_at", "")),
            trust=str(data.get("trust", TRUST_EXTERNAL)),
            category=str(data.get("category", "references")),
            assets=assets,
            structural=structural,
            declared=tuple((str(k), str(v)) for k, v in (data.get("declared") or {}).items()),
            extracted=tuple((str(k), str(v)) for k, v in (data.get("extracted") or {}).items()),
            rights_confirmed=bool(data.get("rights_confirmed", False)),
            licence=str(data.get("licence", "")),
            schema_version=int(version),
        )


__all__ = [
    "EVIDENCE_NONE",
    "EVIDENCE_DECLARED",
    "EVIDENCE_RENDERED",
    "EVIDENCE_STRUCTURAL",
    "EVIDENCE_INTERACTIVE",
    "EVIDENCE_LEVELS",
    "EVIDENCE_RANK",
    "EVIDENCE_SCORABLE_FROM",
    "ASSET_SCREENSHOT_DESKTOP",
    "ASSET_SCREENSHOT_MOBILE",
    "ASSET_HTML",
    "ASSET_THUMBNAIL",
    "ASSET_KINDS",
    "SCREENSHOT_KINDS",
    "ALLOWED_IMAGE_MIME",
    "TRUST_REVIEWED",
    "TRUST_INTERNAL",
    "TRUST_EXTERNAL",
    "TRUST_LEVELS",
    "DIMENSION_HIERARCHY",
    "DIMENSION_TYPOGRAPHY",
    "DIMENSION_LAYOUT",
    "DIMENSION_COLOR",
    "DIMENSION_USABILITY",
    "DIMENSION_CONSISTENCY",
    "DIMENSIONS",
    "STRUCTURAL_DIMENSIONS",
    "SCHEMA_VERSION",
    "DesignReferenceError",
    "DesignAsset",
    "StructuralFacts",
    "DesignReference",
]
