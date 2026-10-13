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
from typing import Any, Mapping, Sequence

from app.core.decision_provider import DesignCandidate
from app.core.openviking_retrieval import (
    OpenVikingConfig,
    OpenVikingRetrievalAdapter,
)

logger = logging.getLogger(__name__)

#: The provider name used by the registry.
OPENVIKING_PROVIDER_NAME = "openviking"

#: Hard cap on candidates this provider will ever return.
MAX_OPENVIKING_CANDIDATES = 6


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
        scope = str((context or {}).get("library_project_id") or "").strip()
        if not scope:
            return ()

        adapter = self.adapter
        if adapter is None:
            return ()
        try:
            result = adapter.retrieve_context(
                query,
                project_id=scope,
                scope=(scope,),
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
        """
        source_uri = str(getattr(item, "source_uri", "") or "").strip()
        source_revision = str(getattr(item, "source_revision", "") or "").strip()
        if not source_uri or not source_revision:
            return None
        source_id = str(getattr(item, "source_id", "") or "").strip()
        if not source_id:
            return None
        excerpt = str(getattr(item, "excerpt", "") or "").strip()
        summary = str(getattr(item, "summary", "") or "").strip()
        relevance = getattr(item, "relevance", 0.0)
        return DesignCandidate(
            candidate_id=source_id,
            label=summary[:120] or source_id,
            descriptor=excerpt[:600],
            palette=(),
            source_uri=source_uri,
            source_revision=source_revision,
            score=relevance if isinstance(relevance, (int, float)) else 0.0,
            uncertainty=str(getattr(item, "uncertainty", "") or ""),
        )


__all__ = [
    "OPENVIKING_PROVIDER_NAME",
    "MAX_OPENVIKING_CANDIDATES",
    "OpenVikingDecisionProvider",
]
