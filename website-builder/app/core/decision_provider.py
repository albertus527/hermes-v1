"""Replaceable Decision Provider interface for Website Builder R2 (N3).

The approved roadmap introduces a **Decision Engine** between FAST and the rest of
the pipeline. It has two jobs, both *advisory*:

* **Intake** — classify an incoming request (new website vs revision, whether a
  reference was supplied).
* **Design** — retrieve candidate design directions and rank them.

Both jobs are behind a **replaceable** :class:`DecisionProvider`, so the concrete
backend (a hosted model, a deterministic local provider, a future OpenViking
candidate service) can be swapped without touching the engine. This is the N3
"replaceable DecisionProvider interface".

Three invariants are structural, not conventions:

**FAST remains authoritative.** A provider may only ever *propose*. It cannot set
scope, requirements, or a decision. The engine (see
:mod:`app.core.decision_engine`) enforces this by treating every provider output
as untrusted input to validation.

**A provider outage never breaks the safe fallback.** Every provider call is
wrapped; a raised error becomes a labelled unavailability, and the deterministic
provider is always available, so the pipeline always has a decision.

**Untrusted candidates never override requirements.** A candidate carries
provenance; the engine drops any candidate without complete provenance and
refuses to rank a score it cannot validate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from app.core.design_reference import StructuralEvidence

# ---------------------------------------------------------------------------
# Stable status / error codes
# ---------------------------------------------------------------------------

#: Provider call outcomes. A closed set.
PROVIDER_OK = "ok"
PROVIDER_UNAVAILABLE = "unavailable"
PROVIDER_INVALID = "invalid"

#: The deterministic provider's name. Always available; the fallback.
DETERMINISTIC_PROVIDER_NAME = "deterministic"

#: Intake classifications. A closed set.
INTAKE_NEW = "new"
INTAKE_REVISION = "revision"
INTAKE_UNKNOWN = "unknown"


class DecisionProviderError(RuntimeError):
    """A provider could not answer. Carries a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Data carried across the interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeClassification:
    """A provider's *proposal* for how to classify a request.

    Never authoritative: the engine validates it and, when it is missing or
    invalid, falls back to the deterministic classification.
    """

    kind: str
    has_reference: bool = False
    confidence: float = 0.0
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.kind not in (INTAKE_NEW, INTAKE_REVISION, INTAKE_UNKNOWN):
            raise DecisionProviderError("INVALID_CLASSIFICATION", f"kind={self.kind!r}")


@dataclass(frozen=True)
class DesignCandidate:
    """One candidate design direction, with the provenance it must carry.

    ``source_uri`` and ``source_revision`` are the provenance anchors. A
    candidate without both is *untrusted* and is dropped by the engine — it can
    never reach ranking. There is deliberately **no** field that could carry a
    requirement, an override, or an instruction.

    ``score`` is the RETRIEVAL relevance score. It is never overwritten: the
    evidence pipeline's own ranking signal must survive ranking, so a model's
    preference is expressed through ``suitability`` instead.

    The N8 fields below are all optional and defaulted, so every existing
    construction site keeps working unchanged.
    """

    candidate_id: str
    label: str
    descriptor: str
    palette: Tuple[str, ...] = ()
    source_uri: str = ""
    source_revision: str = ""
    score: float = 0.0
    uncertainty: str = ""

    # -- N8 B2: design evidence -------------------------------------------
    #: Validated suitability in ``[0, 1]``, or ``None`` when this candidate was
    #: not scored under a suitability contract. Deliberately separate from
    #: ``score`` above.
    suitability: Optional[float] = None
    #: Per-dimension breakdown as ``((name, value), ...)``. Audit only; ranking
    #: reads ``suitability``.
    suitability_detail: Tuple[Tuple[str, float], ...] = ()
    #: Identifier of the scoring contract the suitability was produced under.
    #: Two suitability values are comparable only when these match, so a stale
    #: or foreign contract can never silently win a ranking.
    suitability_contract: str = ""
    #: Evidence class per rubric dimension: ``(("hierarchy", "E2"), ...)``.
    evidence: Tuple[Tuple[str, str], ...] = ()
    #: The ``source_revision`` the attached assets were bound to. A mismatch
    #: means the evidence is stale and the candidate must not be scored.
    evidence_revision: str = ""
    #: Stored artefacts whose ``revision`` matched ``source_revision``.
    assets: Tuple[Any, ...] = ()
    #: The REQUIREMENT revision the requirement-fit score was computed against.
    #: This is the brief's own revision, not the reference's.
    requirement_version: str = ""
    #: TYPED, BOUND measurements for this candidate, when they exist.
    #:
    #: ``evidence`` above records the evidence CLASS per dimension ("E2", "E3").
    #: This field carries the measurements themselves, each bound to the exact
    #: candidate, source revision, screenshot digest, viewport and probe
    #: version. It is a tuple of ``StructuralEvidence`` -- a concrete type, not
    #: an unbounded ``Any`` -- so a caller cannot smuggle an untyped blob into a
    #: scoring request.
    #:
    #: Empty means "no measurement", which is NOT the same as "measured clean".
    #: The scorer treats an absent measurement as the screenshot-only tier.
    measurements: Tuple[StructuralEvidence, ...] = ()

    @property
    def has_provenance(self) -> bool:
        return bool(self.source_uri.strip()) and bool(self.source_revision.strip())

    @property
    def evidence_map(self) -> dict:
        return dict(self.evidence)

    def evidence_for(self, dimension: str) -> str:
        """The evidence class recorded for *dimension*, or ``"E0"``."""
        return dict(self.evidence).get(dimension, "E0")

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "label": self.label,
            "descriptor": self.descriptor,
            "palette": list(self.palette),
            "source_uri": self.source_uri,
            "source_revision": self.source_revision,
            "score": round(float(self.score), 4),
            "uncertainty": self.uncertainty,
            "suitability": (
                round(float(self.suitability), 4)
                if self.suitability is not None
                else None
            ),
            "suitability_detail": {
                name: round(float(value), 4)
                for name, value in self.suitability_detail
            },
            "suitability_contract": self.suitability_contract,
            "evidence": dict(self.evidence),
            "evidence_revision": self.evidence_revision,
            "measurements": [m.to_dict() for m in self.measurements],
            "assets": [
                a.to_dict() if hasattr(a, "to_dict") else dict(a)
                for a in self.assets
            ],
            "requirement_version": self.requirement_version,
        }


@dataclass(frozen=True)
class ProviderResponse:
    """The result of one provider call, normalised by the engine.

    ``status`` is one of the ``PROVIDER_*`` codes. ``error_code`` is populated
    only when ``status != PROVIDER_OK``.
    """

    status: str
    value: Any = None
    error_code: str = ""
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == PROVIDER_OK


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class DecisionProvider(Protocol):
    """A replaceable decision backend.

    Implementations must be safe to call with untrusted input and must never
    mutate project state. They are *advisors*: their output is validated by the
    engine before it is used.
    """

    @property
    def name(self) -> str:  # pragma: no cover - protocol
        ...

    def classify_intake(
        self, request_text: str, context: Mapping[str, Any]
    ) -> IntakeClassification:  # pragma: no cover - protocol
        ...

    def propose_design_candidates(
        self,
        context: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[DesignCandidate]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# The deterministic provider (always available; the safe fallback)
# ---------------------------------------------------------------------------


#: Reference markers the deterministic classifier recognises. Deterministic and
#: bounded — never a model judgement.
_REFERENCE_MARKERS: Tuple[str, ...] = (
    "http://",
    "https://",
    "www.",
    "reference",
    "referensi",
    "screenshot",
    "logo",
    "brand asset",
    "contoh",
)

#: Revision markers (English + Indonesian), matched on word boundaries.
_REVISION_MARKERS: Tuple[str, ...] = (
    "change",
    "update",
    "revise",
    "revision",
    "edit",
    "modify",
    "tweak",
    "ganti",
    "ubah",
    "revisi",
    "tambah",
    "hapus",
)

#: New-website markers.
_NEW_MARKERS: Tuple[str, ...] = (
    "new website",
    "new site",
    "build a",
    "buat website",
    "buat situs",
    "landing page",
    "company profile",
)


def _has_marker(text: str, markers: Sequence[str]) -> bool:
    import re

    for marker in markers:
        if re.search(r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])", text):
            return True
    return False


class DeterministicDecisionProvider:
    """A pure, offline decision provider. Always available; the safe fallback.

    It performs no network call, no model call, and no I/O. Intake classification
    is keyword-based and reference detection is marker-based. Design candidates
    are derived from the *context's own* approved Design DNA — it never invents a
    candidate that the application did not already supply, so it can never
    override requirements.
    """

    @property
    def name(self) -> str:
        return DETERMINISTIC_PROVIDER_NAME

    def classify_intake(
        self, request_text: str, context: Mapping[str, Any]
    ) -> IntakeClassification:
        text = (request_text or "").lower()
        has_reference = _has_marker(text, _REFERENCE_MARKERS)
        # A revision wins only when the project already has a source revision;
        # otherwise "change" on a brand-new project is not a revision.
        has_source = bool((context or {}).get("has_source"))
        if has_source and _has_marker(text, _REVISION_MARKERS):
            return IntakeClassification(
                kind=INTAKE_REVISION,
                has_reference=has_reference,
                confidence=0.6,
                rationale="revision marker with an existing source revision",
            )
        if _has_marker(text, _NEW_MARKERS):
            return IntakeClassification(
                kind=INTAKE_NEW,
                has_reference=has_reference,
                confidence=0.6,
                rationale="new-website marker",
            )
        # Default to new for a project with no source, revision otherwise.
        return IntakeClassification(
            kind=INTAKE_REVISION if has_source else INTAKE_NEW,
            has_reference=has_reference,
            confidence=0.3,
            rationale="no marker; defaulted by existing source",
        )

    def propose_design_candidates(
        self,
        context: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[DesignCandidate]:
        """Propose candidates derived ONLY from the approved context.

        The context supplies ``design_dna`` (the accepted machine DNA) and an
        optional ``direction_label``. Because the candidate is a re-expression of
        an already-approved decision, this provider can never introduce a design
        the application did not already authorise.
        """
        if limit <= 0:
            return ()
        dna = (context or {}).get("design_dna") or {}
        if not isinstance(dna, Mapping) or not dna:
            return ()
        brand = dna.get("brand_personality")
        if isinstance(brand, Mapping):
            brand = "; ".join(f"{k}: {v}" for k, v in sorted(brand.items()))
        brand = str(brand).strip() if brand else ""
        palette = dna.get("palette") or {}
        palette_values: Tuple[str, ...] = ()
        if isinstance(palette, Mapping):
            palette_values = tuple(
                str(v).strip()
                for _, v in sorted(palette.items())
                if str(v).strip()
            )
        elif isinstance(palette, (list, tuple)):
            palette_values = tuple(str(v).strip() for v in palette if str(v).strip())
        if not brand and not palette_values:
            return ()
        source_revision = str((context or {}).get("source_revision") or "0")
        return (
            DesignCandidate(
                candidate_id="dna",
                label=str((context or {}).get("direction_label") or "Approved DNA"),
                descriptor=brand,
                palette=palette_values,
                source_uri="design-dna.json",
                source_revision=source_revision,
                score=1.0,
                uncertainty="",
            ),
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class DecisionProviderRegistry:
    """A name → provider map with a guaranteed fallback.

    The deterministic provider is always present, so :meth:`resolve` can never
    return ``None``. Selecting an unknown or unavailable provider falls back to
    it rather than failing — a decision backend being missing must never stop the
    pipeline.
    """

    providers: dict = field(default_factory=dict)
    fallback_name: str = DETERMINISTIC_PROVIDER_NAME

    def __post_init__(self) -> None:
        if DETERMINISTIC_PROVIDER_NAME not in self.providers:
            self.providers[DETERMINISTIC_PROVIDER_NAME] = DeterministicDecisionProvider()

    def register(self, provider: DecisionProvider) -> None:
        self.providers[provider.name] = provider

    def resolve(self, name: Optional[str]) -> DecisionProvider:
        if name and name in self.providers:
            return self.providers[name]
        return self.providers[self.fallback_name]

    @property
    def fallback(self) -> DecisionProvider:
        return self.providers[self.fallback_name]


__all__ = [
    "PROVIDER_OK",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_INVALID",
    "DETERMINISTIC_PROVIDER_NAME",
    "INTAKE_NEW",
    "INTAKE_REVISION",
    "INTAKE_UNKNOWN",
    "DecisionProviderError",
    "IntakeClassification",
    "DesignCandidate",
    "ProviderResponse",
    "DecisionProvider",
    "DeterministicDecisionProvider",
    "DecisionProviderRegistry",
]
