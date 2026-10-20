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

import hashlib
from dataclasses import dataclass, field
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


#: Provenance of a set of structural facts.
#:
#: ``measured``   produced by the browser probe against a rendered page. These
#:                facts carry binding provenance and may raise a dimension to
#:                E3.
#: ``declared``   copied from an ingest manifest. These are CLAIMS. They carry
#:                no provenance and never raise a dimension to E3, because
#:                nothing verified them.
#:
#: The distinction exists because a manifest that merely states
#: ``contrast_ratio: 21.0`` for an untrusted reference would otherwise reach
#: E3 having measured nothing.
PROVENANCE_MEASURED = "measured"
PROVENANCE_DECLARED = "declared"
PROVENANCES: Tuple[str, ...] = (PROVENANCE_MEASURED, PROVENANCE_DECLARED)


def _read_provenance(raw: Mapping[str, Any]) -> str:
    """Read a structural provenance value, defaulting to ``declared``.

    The default matters: an older stored manifest has no provenance field, and
    it must not be assumed measured.
    """
    value = str(raw.get("provenance") or PROVENANCE_DECLARED).strip()
    return value if value in PROVENANCES else PROVENANCE_DECLARED


@dataclass(frozen=True)
class StructuralFacts:
    """Measurements taken from the artefact itself.

    Every field is optional because not every reference can be measured. What
    matters is that a *populated* field means a measurement happened -- these
    are not declarations copied from metadata.

    ``palette_pairs`` carries ``(foreground, background, contrast_ratio)``. The
    ratio is the measured value, which is why colour/contrast can reach E3 only
    when this is populated: a screenshot cannot yield a ratio.

    ``provenance`` distinguishes a fact the probe took from one a manifest
    declared. Only ``measured`` facts can promote a dimension to E3; a
    ``declared`` fact is a claim and stays at the screenshot-only tier.
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
    #: How these facts were obtained. Defaults to ``declared`` so that a
    #: manifest-supplied block is never accidentally treated as measured.
    provenance: str = PROVENANCE_DECLARED

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCES:
            raise DesignReferenceError(
                f"unknown structural provenance {self.provenance!r}"
            )

    @property
    def is_measured(self) -> bool:
        """True only when a probe actually produced these facts."""
        return self.provenance == PROVENANCE_MEASURED

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
            "provenance": self.provenance,
        }


# ---------------------------------------------------------------------------
# Measured structural evidence
# ---------------------------------------------------------------------------

#: Bumped whenever the probe payload shape or semantics change. Stored with every
#: measurement so a later change cannot be mistaken for a change in the page.
PROBE_VERSION = "structural-probe-v1"

#: Contrast states. ``measured`` is the only one that may carry a ratio.
CONTRAST_MEASURED = "measured"
CONTRAST_UNCERTAIN = "uncertain"
CONTRAST_UNSUPPORTED = "unsupported"

#: WCAG 2.2 SC 1.4.3 thresholds, recorded as REFERENCE ONLY. This is not a gate
#: and not a score; the caller decides what a ratio means.
WCAG_AA_NORMAL = 4.5
WCAG_AA_LARGE = 3.0


@dataclass(frozen=True)
class ContrastMeasurement:
    """One foreground/background pair, with its honesty state attached.

    ``ratio`` is populated ONLY for ``state == "measured"``. Every other state
    carries ``reason`` instead, because a number would be an invention.
    """

    state: str
    ratio: Optional[float]
    foreground: str
    background: str
    font_size_px: float
    reason: str = ""
    sample_tag: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "ratio": self.ratio,
            "foreground": self.foreground,
            "background": self.background,
            "font_size_px": self.font_size_px,
            "reason": self.reason,
            "sample_tag": self.sample_tag,
            # Reference only. Deliberately not a gate and not a score.
            "wcag_aa_normal": WCAG_AA_NORMAL,
            "meets_aa_normal": (
                self.ratio >= WCAG_AA_NORMAL if self.ratio is not None else None
            ),
        }


@dataclass(frozen=True)
class StructuralEvidence:
    """Measurements bound to exactly one candidate, revision, viewport, image.

    The binding is not decoration. Without ``screenshot_digest`` a stale
    measurement could be paired with a fresh image and no one would notice, which
    is precisely the failure mode this exists to prevent.
    """

    candidate_id: str
    source_revision: str
    viewport: Tuple[int, int]
    screenshot_digest: str
    measured_at_url: str
    probe_version: str = PROBE_VERSION
    font_families: Tuple[str, ...] = ()
    type_scale: Tuple[Tuple[str, float, float], ...] = ()
    spacing_scale: Tuple[float, ...] = ()
    contrast: Tuple[ContrastMeasurement, ...] = ()
    component_count: int = 0
    counts: Mapping[str, Any] = field(default_factory=dict)
    document_height: int = 0
    collected: bool = True
    error: str = ""

    def __post_init__(self) -> None:
        # A measurement with no binding anchors cannot be attached to anything.
        # Allowing it to be constructed would mean an unbound record could
        # reach a scoring request, which is exactly what this type exists to
        # prevent. An uncollected record (the probe failed) is still allowed to
        # exist -- it is honest about carrying nothing -- but it must still
        # name what it was an attempt at.
        if self.collected:
            for name in ("candidate_id", "source_revision", "screenshot_digest"):
                if not str(getattr(self, name)).strip():
                    raise DesignReferenceError(
                        f"structural evidence {name} is empty"
                    )
        if not str(self.probe_version).strip():
            raise DesignReferenceError("structural evidence probe_version is empty")

    @property
    def has_typography(self) -> bool:
        return bool(self.font_families) and bool(self.type_scale)

    @property
    def has_spacing(self) -> bool:
        return bool(self.spacing_scale)

    @property
    def has_contrast(self) -> bool:
        """True only when at least one pair is genuinely ``measured``.

        Uncertain and unsupported pairs do NOT count. This is what keeps an
        external reference at E2 instead of letting a doubt masquerade as
        measurement.
        """
        return any(m.state == CONTRAST_MEASURED for m in self.contrast)

    @property
    def measured_pairs(self) -> Tuple[ContrastMeasurement, ...]:
        return tuple(m for m in self.contrast if m.state == CONTRAST_MEASURED)

    @property
    def doubtful_pairs(self) -> Tuple[ContrastMeasurement, ...]:
        return tuple(m for m in self.contrast if m.state != CONTRAST_MEASURED)

    def binding(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_revision": self.source_revision,
            "viewport": list(self.viewport),
            "screenshot_digest": self.screenshot_digest,
            "measured_at_url": self.measured_at_url,
            "probe_version": self.probe_version,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.binding(),
            "font_families": list(self.font_families),
            "type_scale": [list(t) for t in self.type_scale],
            "spacing_scale": list(self.spacing_scale),
            "contrast": [m.to_dict() for m in self.contrast],
            "component_count": self.component_count,
            "counts": dict(self.counts),
            "document_height": self.document_height,
            "collected": self.collected,
            "error": self.error,
        }

    def digest(self) -> str:
        """Stable digest of the measurements, excluding the binding."""
        payload = {
            "font_families": list(self.font_families),
            "type_scale": [list(t) for t in self.type_scale],
            "spacing_scale": list(self.spacing_scale),
            "contrast": [
                [m.state, m.ratio, m.foreground, m.background, m.font_size_px]
                for m in self.contrast
            ],
            "component_count": self.component_count,
            "probe_version": self.probe_version,
        }
        return hashlib.sha256(
            repr(sorted(payload.items())).encode("utf-8")
        ).hexdigest()[:16]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StructuralEvidence":
        """Rebuild evidence from its serialised form.

        A malformed entry is an error, not a default. An evidence record that
        lost its candidate, revision or screenshot digest on the way through
        storage is no longer evidence: it cannot be bound to anything, and
        silently defaulting those fields would attach a measurement to the
        wrong image.
        """
        if not isinstance(data, Mapping):
            raise DesignReferenceError("structural evidence is not a mapping")
        viewport = data.get("viewport") or (0, 0)
        contrast: list = []
        for raw in data.get("contrast") or ():
            if not isinstance(raw, Mapping):
                continue
            ratio = raw.get("ratio")
            if ratio is not None:
                try:
                    ratio = float(ratio)
                except (TypeError, ValueError):
                    ratio = None
            contrast.append(
                ContrastMeasurement(
                    state=str(raw.get("state") or CONTRAST_UNCERTAIN),
                    ratio=ratio,
                    foreground=str(raw.get("foreground") or ""),
                    background=str(raw.get("background") or ""),
                    font_size_px=float(raw.get("font_size_px") or 0.0),
                    reason=str(raw.get("reason") or ""),
                    sample_tag=str(raw.get("sample_tag") or ""),
                )
            )
        counts = data.get("counts")
        return cls(
            candidate_id=str(data.get("candidate_id") or ""),
            source_revision=str(data.get("source_revision") or ""),
            viewport=(int(viewport[0]), int(viewport[1])),
            screenshot_digest=str(data.get("screenshot_digest") or ""),
            measured_at_url=str(data.get("measured_at_url") or ""),
            probe_version=str(data.get("probe_version") or ""),
            font_families=tuple(str(f) for f in (data.get("font_families") or ())),
            type_scale=tuple(
                (str(t[0]), float(t[1]), float(t[2]))
                for t in (data.get("type_scale") or ())
            ),
            spacing_scale=tuple(float(v) for v in (data.get("spacing_scale") or ())),
            contrast=tuple(contrast),
            component_count=int(data.get("component_count") or 0),
            counts=dict(counts) if isinstance(counts, Mapping) else {},
            document_height=int(data.get("document_height") or 0),
            collected=bool(data.get("collected", True)),
            error=str(data.get("error") or ""),
        )






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
    #: MEASURED evidence, bound to this reference's own revision and to the
    #: exact screenshot digest each measurement was taken against.
    #:
    #: Kept separate from ``structural`` on purpose. ``structural`` is the
    #: measurement SUMMARY and may be declared by a manifest; ``measurements``
    #: carries the full records WITH their provenance, and only a probe can
    #: produce them. A reference with declared structural facts but no
    #: measurements stays at the screenshot-only evidence tier.
    measurements: Tuple[StructuralEvidence, ...] = ()
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
        if facts is not None and facts.is_measured:
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
            "measurements": [m.to_dict() for m in self.measurements],
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
                # Provenance is PRESERVED across a round trip. A stored
                # measurement keeps its "measured" marking; a manifest that
                # merely declared facts stays "declared" and can never raise a
                # dimension to E3 on its own.
                provenance=_read_provenance(raw_structural),
            )

        measurements: list = []
        for raw in data.get("measurements") or ():
            if not isinstance(raw, Mapping):
                continue
            try:
                measurements.append(StructuralEvidence.from_dict(raw))
            except (DesignReferenceError, TypeError, ValueError):
                # A malformed measurement is dropped, not repaired: it cannot
                # be bound to anything, and half-bound evidence is worse than
                # no evidence.
                continue
        return cls(
            reference_id=str(data.get("reference_id", "")),
            source_uri=str(data.get("source_uri", "")),
            source_revision=str(data.get("source_revision", "")),
            ingested_at=str(data.get("ingested_at", "")),
            trust=str(data.get("trust", TRUST_EXTERNAL)),
            category=str(data.get("category", "references")),
            assets=assets,
            structural=structural,
            measurements=tuple(measurements),
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
