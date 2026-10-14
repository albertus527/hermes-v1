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
from typing import Any, Mapping, Sequence, Tuple

from app.core.decision_provider import DesignCandidate
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
        candidates: list = []
        for item in items:
            if len(candidates) >= min(limit, MAX_OPENVIKING_CANDIDATES):
                break
            candidate = self._to_candidate(item)
            if candidate is not None:
                candidates.append(candidate)
        return tuple(candidates)

    @staticmethod
    def _to_candidate(item: Any) -> DesignCandidate | None:
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
        return DesignCandidate(
            candidate_id=source_id,
            label=label,
            descriptor=descriptor,
            palette=(),
            source_uri=source_uri,
            source_revision=source_revision,
            score=score if isinstance(score, (int, float)) else 0.0,
            uncertainty=uncertainty,
        )


__all__ = [
    "OPENVIKING_PROVIDER_NAME",
    "MAX_OPENVIKING_CANDIDATES",
    "OpenVikingDecisionProvider",
]
