"""OpenViking-backed design candidate retrieval for the Decision Engine (N3).

This is the concrete backend the N3 Decision Engine can retrieve design
candidates from. It is built on the **existing** D4a OpenViking retrieval seam
(`app.core.openviking_retrieval`), so it inherits that adapter's provenance,
isolation, and fail-closed behaviour rather than re-implementing them.

Three properties are load-bearing:

**Bounded.** Every candidate list is capped, and the underlying D4a adapter
already bounds queries, per-query items, and pack size. This module adds no
unbounded path.

**Provenance required.** A candidate is only emitted with a real
``source_uri`` and ``source_revision``. A match without complete provenance is
dropped here, before it can reach the engine — provenance is checked at the
source, not only at the engine.

**Fail-closed.** If the underlying adapter is disabled or unavailable, this
provider returns an empty candidate list (honest-empty), never a fabricated or
unverified candidate. The Decision Engine then abstains, and FAST's requirements
are never overridden.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from app.core.decision_provider import DesignCandidate
from app.core.design_reference import (
    DIMENSION_COLOR,
    DIMENSION_CONSISTENCY,
    DIMENSION_HIERARCHY,
    DIMENSION_LAYOUT,
    DIMENSION_TYPOGRAPHY,
    DIMENSION_USABILITY,
    EVIDENCE_NONE,
    EVIDENCE_RENDERED,
    EVIDENCE_STRUCTURAL,
    SCREENSHOT_KINDS,
    StructuralEvidence,
)
from app.core.openviking_library import CATEGORIES as _LIBRARY_CATEGORIES
from app.core.openviking_retrieval import (
    OpenVikingConfig,
    OpenVikingRetrievalAdapter,
)

logger = logging.getLogger(__name__)

#: The provider name used by the registry.
OPENVIKING_PROVIDER_NAME = "openviking"

#: Hard cap on candidates this provider will ever return.
MAX_OPENVIKING_CANDIDATES = 6

#: The design-relevant retrieval CATEGORIES this provider asks for.
#:
#: Retrieval scope is a set of *category names* (see
#: :func:`app.core.openviking_retrieval.normalize_scope`), NOT the library
#: project id. Passing the project id as the scope was the live-integration
#: defect this provider corrects: ``normalize_scope`` refused it as an unknown
#: category, the provider swallowed the error, and it silently abstained.
#: The set is a subset of the library's closed category set and is validated
#: below, so it can never name a category the library does not define.
DESIGN_RETRIEVAL_CATEGORIES: Tuple[str, ...] = tuple(
    c for c in ("design_dna", "components", "motion") if c in _LIBRARY_CATEGORIES
) or _LIBRARY_CATEGORIES


def _assets_from_result(result: Any) -> Dict[str, Sequence[Any]]:
    """Pull the manifest sidecar's asset map off a retrieval result.

    The D4a adapter surfaces the record sidecar; a reference manifest stores its
    asset list there. Returns an empty map when nothing is present, so a
    text-only library behaves exactly as before.
    """
    raw = getattr(result, "assets_by_uri", None)
    if isinstance(raw, Mapping):
        return {str(k): tuple(v) for k, v in raw.items() if v}
    return {}


def _bind_assets(
    declared: Sequence[Any], source_revision: str
) -> Tuple[Any, ...]:
    """Return only the assets whose revision matches *source_revision*.

    This is the binding rule: an asset produced for a different revision of the
    source is STALE. It is dropped rather than attached, because attaching it
    would show the model an image that no longer corresponds to the reference.
    """
    bound = []
    for asset in declared:
        revision = str(getattr(asset, "revision", "") or "").strip()
        digest = str(getattr(asset, "digest", "") or "").strip()
        kind = str(getattr(asset, "kind", "") or "").strip()
        if revision != source_revision or not digest or kind not in SCREENSHOT_KINDS:
            continue
        bound.append(asset)
    return tuple(bound)


def _measurements_from_result(result: Any) -> Dict[str, Sequence[Any]]:
    """Pull the measured-evidence map off a retrieval result.

    Mirrors :func:`_assets_from_result`: the map is optional, and a result
    without one behaves exactly as before (screenshot-only evidence).

    The map is keyed by the reference's ``source_uri`` -- the same key the
    asset map uses -- so a measurement and the image it describes are looked up
    together and cannot be crossed over.
    """
    raw = getattr(result, "measurements_by_uri", None)
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, Sequence[Any]] = {}
    for key, value in raw.items():
        if not value:
            continue
        out[str(key)] = tuple(value)
    return out


def _asset_digest(asset: Any) -> str:
    """Read an asset's digest from either an object or a serialised mapping.

    Assets reach this module both as live objects and as the plain mappings
    ``to_dict()`` produces. Both are legitimate, so both must be readable.
    """
    if isinstance(asset, Mapping):
        return str(asset.get("digest", "") or "").strip()
    return str(getattr(asset, "digest", "") or "").strip()


def _bound_measurements(
    declared: Sequence[Any],
    *,
    candidate_id: str,
    source_revision: str,
    bound_assets: Sequence[Any],
) -> Tuple[Any, ...]:
    """Keep only measurements that bind to THIS candidate and ITS assets.

    Three things must agree before a measurement may travel with a candidate:

      1. the measurement's own candidate id is this candidate
      2. the measurement's source revision is this candidate's revision
      3. the measurement's screenshot digest is the digest of an asset this
         candidate actually owns

    Rule 3 is the one that matters most. A measurement taken against a
    DIFFERENT image describes a different page, even if the revision string
    happens to match. Binding to the digest is what makes "the picture the
    model sees" and "the page the numbers describe" the same object.

    A measurement that fails any check is DROPPED. It is never repaired and
    never re-bound: guessing which anchor was intended is how stale evidence
    gets attached to the wrong image.
    """
    if not declared or not bound_assets:
        return ()
    digests = {
        _asset_digest(asset) for asset in bound_assets
    }
    digests.discard("")

    kept: list = []
    for evidence in declared:
        if not isinstance(evidence, StructuralEvidence):
            continue
        if evidence.candidate_id != candidate_id:
            continue
        if evidence.source_revision != source_revision:
            continue
        if evidence.screenshot_digest not in digests:
            continue
        if not evidence.collected:
            continue
        kept.append(evidence)
    return tuple(kept)


def _evidence_for_assets(bound: Sequence[Any]) -> Tuple[Tuple[str, str], ...]:
    """Evidence class per rubric dimension, from BOUND assets only.

    A screenshot is rendered evidence. Nothing here can manufacture structural
    or interactive evidence: those require measurements and observations that
    an image alone does not provide.
    """
    if not bound:
        return tuple(
            (dim, EVIDENCE_NONE)
            for dim in (
                DIMENSION_HIERARCHY, DIMENSION_TYPOGRAPHY, DIMENSION_LAYOUT,
                DIMENSION_COLOR, DIMENSION_USABILITY, DIMENSION_CONSISTENCY,
            )
        )
    return (
        (DIMENSION_HIERARCHY, EVIDENCE_RENDERED),
        (DIMENSION_TYPOGRAPHY, EVIDENCE_RENDERED),
        (DIMENSION_LAYOUT, EVIDENCE_RENDERED),
        (DIMENSION_COLOR, EVIDENCE_RENDERED),
        (DIMENSION_USABILITY, EVIDENCE_RENDERED),
        (DIMENSION_CONSISTENCY, EVIDENCE_RENDERED),
    )


class OpenVikingDecisionProvider:
    """Retrieves design candidates from OpenViking via the D4a adapter.

    The adapter is injected so this provider is testable offline with a fake
    backend, exactly as the D4a/D4b tests do. When the adapter is ``None`` or
    disabled, ``propose_design_candidates`` returns ``()``.
    """

    def __init__(
        self,
        adapter: Any,
        *,
        config: OpenVikingConfig | None = None,
    ) -> None:
        self.adapter = adapter
        if config is not None:
            self.config = config
        else:
            # Prefer the adapter's own config when it exposes one (the real D4a
            # adapter does); otherwise fall back to "adapter present ⇒ usable".
            self.config = getattr(adapter, "config", None)

    @property
    def name(self) -> str:
        return OPENVIKING_PROVIDER_NAME

    @property
    def enabled(self) -> bool:
        if self.adapter is None:
            return False
        # No config means the caller injected an adapter directly (as the tests
        # do); the adapter's own enabled state governs. With a config, both must
        # agree, so an operator disabling OpenViking disables retrieval here too.
        if self.config is None:
            return True
        return bool(self.config.enabled)

    def classify_intake(
        self, request_text: str, context: Mapping[str, Any]
    ) -> Any:
        """Not this provider's job — defer to the deterministic classifier.

        A retrieval backend has no intake authority. Raising here (rather than
        guessing) means the engine falls back deterministically, which is the
        correct behaviour for a provider that does not implement intake.
        """
        from app.core.decision_provider import DecisionProviderError

        raise DecisionProviderError("NOT_IMPLEMENTED", "openviking provides retrieval only")

    def propose_design_candidates(
        self,
        context: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[DesignCandidate]:
        """Return up to ``limit`` provenanced candidates, or ``()`` when disabled.

        Never raises for an outage: an adapter failure becomes an empty list, so
        the engine abstains and the safe FAST fallback holds.
        """
        if not self.enabled or limit <= 0:
            return ()

        query = str((context or {}).get("query") or "").strip()
        if not query:
            return ()

        # ``library_project_id`` is the application-owned reviewed-library scope
        # (never user input). The D4a adapter requires a valid project id, so a
        # missing one means there is nothing safe to retrieve.
        project_id = str((context or {}).get("library_project_id") or "").strip()
        if not project_id:
            return ()

        # Scope is a set of CATEGORY names, NOT the project id. The caller may
        # narrow it via ``design_categories``; the value is validated by the
        # adapter's ``normalize_scope`` (an unknown category is refused there),
        # so a typo can neither widen nor silently narrow retrieval.
        raw_categories = (context or {}).get("design_categories")
        if isinstance(raw_categories, (list, tuple)) and raw_categories:
            scope: Any = tuple(str(c).strip() for c in raw_categories if str(c).strip())
        else:
            scope = DESIGN_RETRIEVAL_CATEGORIES

        adapter = self.adapter
        if adapter is None:
            return ()
        try:
            result = adapter.retrieve_context(
                query,
                project_id=project_id,
                scope=scope,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never raise
            logger.warning(
                "OpenViking decision retrieval failed (%s); abstaining",
                type(exc).__name__,
            )
            return ()

        items = getattr(result, "items", ()) or ()
        assets_by_uri = _assets_from_result(result)
        candidates: list = []
        for item in items:
            if len(candidates) >= min(limit, MAX_OPENVIKING_CANDIDATES):
                break
            candidate = self._to_candidate(
                item, assets_by_uri, _measurements_from_result(result)
            )
            if candidate is not None:
                candidates.append(candidate)
        return tuple(candidates)

    @staticmethod
    def _to_candidate(
        item: Any,
        assets_by_uri: Optional[Mapping[str, Sequence[Any]]] = None,
        measurements_by_uri: Optional[Mapping[str, Sequence[Any]]] = None,
    ) -> DesignCandidate | None:
        """Convert one retrieved item to a candidate, or drop it.

        A drop happens when provenance is incomplete. The retrieved item is
        DATA; it carries no authority, and this conversion deliberately copies
        only presentation fields plus provenance — never any instruction.

        Field mapping is against the REAL D4a ``ContextItem`` contract
        (``uri`` / ``source_id`` / ``source_revision`` / ``title`` / ``summary``
        / ``body`` / ``score`` / ``trust``). ``uri`` maps to
        ``DesignCandidate.source_uri``; the genuine ``source_revision`` and
        ``source_id`` are preserved verbatim. Nothing is synthesised: an item
        missing any provenance anchor is dropped rather than repaired.
        """
        source_uri = str(getattr(item, "uri", "") or "").strip()
        source_revision = str(getattr(item, "source_revision", "") or "").strip()
        if not source_uri or not source_revision:
            return None
        source_id = str(getattr(item, "source_id", "") or "").strip()
        if not source_id:
            return None
        title = str(getattr(item, "title", "") or "").strip()
        summary = str(getattr(item, "summary", "") or "").strip()
        body = str(getattr(item, "body", "") or "").strip()
        score = getattr(item, "score", 0.0)
        trust = str(getattr(item, "trust", "") or "").strip()
        # Presentation only: a bounded label + descriptor, never an instruction.
        label = title[:120] or summary[:120] or source_id
        descriptor = (summary or body)[:600]
        # Honest, non-fabricated annotation: reviewed content carries none;
        # anything lower-trust is labelled so the engine/caller can see it.
        uncertainty = "" if trust in ("", "reviewed") else f"trust={trust}"

        # N8 B4: bind the reference's assets, but ONLY those whose revision
        # matches this item's source_revision. A stale asset is not attached:
        # the candidate simply carries no visual evidence, and a scorer must
        # then refuse to judge visual quality rather than judge the wrong image.
        bound = _bind_assets(
            (assets_by_uri or {}).get(source_uri, ()), source_revision
        )
        evidence = _evidence_for_assets(bound)

        return DesignCandidate(
            candidate_id=source_id,
            label=label,
            descriptor=descriptor,
            # A reference's palette is DECLARED metadata, not visual evidence.
            # It is deliberately NOT promoted into the candidate: only assets
            # bound by digest may raise an evidence class.
            palette=(),
            source_uri=source_uri,
            source_revision=source_revision,
            score=score if isinstance(score, (int, float)) else 0.0,
            uncertainty=uncertainty,
            evidence=evidence,
            evidence_revision=source_revision if bound else "",
            assets=bound,
            # MEASURED evidence, carried only when it binds to THIS candidate
            # and to an asset THIS candidate owns. Anything else is dropped
            # here rather than reaching the scorer.
            measurements=_bound_measurements(
                measurements_by_uri.get(source_uri, ()),
                candidate_id=source_id,
                source_revision=source_revision,
                bound_assets=bound,
            ),
        )


__all__ = [
    "OPENVIKING_PROVIDER_NAME",
    "MAX_OPENVIKING_CANDIDATES",
    "OpenVikingDecisionProvider",
]
