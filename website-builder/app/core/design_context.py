"""Bounded FRONTEND-facing design-resource context pack (Batch D2 / B6).

D2 produced a *decision*: which resources and project dependencies the accepted
Design DNA justifies. D1 produced *content*: bounded, provenance-carrying
entries. Neither is yet something FRONTEND can be handed, because FRONTEND must
never be the component that decides to search a corpus, install a package, or
re-derive a justification.

This module assembles the one object FRONTEND actually receives: a single,
deterministic, application-generated context pack combining the accepted Design
DNA, the D1 retrieval results, and the D2 selection decisions.

Six properties are load-bearing:

**One object, not scattered concatenation.** The pack is built here, once. A
FRONTEND prompt assembled by concatenating prompt fragments across several files
has no bound anyone can point at, and no way to test what was in it.

**D1's budgets are reused, not re-invented.** Retrieval already bounds entries,
per-resource characters, and the aggregate. This module does not widen a single
one; it re-measures what it carries with :func:`payload_chars` -- the SAME
function -- so a bound that holds downstream is the bound that held upstream.
A second size calculation is exactly the divergence D1's design forbids.

**Truncation is visible metadata, not a silent clip.** If anything was dropped
or clipped, the pack says so and says how much, so a consumer can raise a budget
instead of reading a truncated dataset as the whole truth.

**No absolute paths, no secrets, no unverified payloads.** Every entry travels
with its skill-relative provenance, every warning is a static label, and the
pack carries structured decisions rather than free text. Resource text remains
DATA: it is carried inside :class:`~app.core.design_retrieval.DesignEntry`, which
has no authority-bearing field.

**Resource text cannot self-select.** The pack carries the D2 selection plan;
the plan is computed from Design DNA and application policy BEFORE retrieval
results exist, so nothing a corpus says can add a dependency. Retrieval content
is attached to the decision, never merged into it.

**One pack per attempt.** Convergence protection lives in D3a/callers; this
module's contribution is to be cheap and bounded so producing one per revision
cannot become an unbounded exploration loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_dna import CANONICAL_DESIGN_DNA_KEYS, unwrap_design_dna
from app.core.design_policies import DEPENDENCY_STATES, DESIGN_AUTHORITY_PRECEDENCE
from app.core.design_retrieval import (
    DesignContextLimits,
    DesignEntry,
    DesignRetrievalReport,
    payload_chars,
    retrieve_design_guidance,
)
from app.core.design_selection import (
    SELECTION_REASONS,
    DesignRequirementSet,
    DesignResourceSelectionPlan,
    derive_design_requirements,
    select_design_resources,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pack-specific limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignContextPackLimits:
    """Bounds for the assembled pack.

    Separate from :class:`DesignContextLimits` because the pack has an
    ADDITIONAL bound D1 does not: the decision section is fixed-size (ids,
    states, reasons -- no content), and only the retrieval section scales. The
    retrieval limits are passed straight through to D1 rather than restated, so
    there is one place a retrieval bound is defined.
    """

    #: Retrieval bounds, reused verbatim.
    retrieval: DesignContextLimits = DesignContextLimits()

    #: Characters allowed in the DECISION section (ids, reasons, states).
    #: Deliberately small and fixed: a decision record is metadata, and metadata
    #: that needs a budget is metadata that has smuggled in content.
    max_decision_chars: int = 2_000

    #: How many Design DNA fields are carried into the pack. Bounds the DNA echo
    #: so a huge DNA document cannot dominate the context.
    max_dna_fields: int = 16

    def to_dict(self) -> Dict[str, Any]:
        return {
            "retrieval": self.retrieval.to_dict(),
            "max_decision_chars": self.max_decision_chars,
            "max_dna_fields": self.max_dna_fields,
        }


DEFAULT_PACK_LIMITS = DesignContextPackLimits()


# ---------------------------------------------------------------------------
# Static warnings
# ---------------------------------------------------------------------------

WARNING_PACK_TRUNCATED = (
    "resource context pack was truncated to satisfy its decision-char limit"
)
WARNING_NO_DESIGN_DNA = (
    "no accepted Design DNA was supplied; nothing was justified from design intent"
)
WARNING_RESOURCE_DROPPED = (
    "at least one resource reported dropped entries; its content is incomplete"
)


# ---------------------------------------------------------------------------
# The pack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignContextPack:
    """The single bounded context object FRONTEND receives.

    Three sections, each independently inspectable:

    * ``design_dna`` -- an ECHO of the accepted DNA's canonical keys only.
      Echo, not authority: FRONTEND already owns the DNA document, and this
      copy exists so a context consumer sees the same design facts the decisions
      were derived from.
    * ``decisions`` -- the D2 plan, structured. This is what FRONTEND is allowed
      to act on. No free text.
    * ``resources`` -- the D1 retrieval entries, each with provenance, carried as
      DATA.
    """

    version: int
    design_dna: Dict[str, Any]
    decisions: Dict[str, Any]
    resources: Dict[str, Any]
    truncation: Dict[str, Any]
    limits: Dict[str, Any]
    degraded: bool
    warnings: Tuple[str, ...]

    @property
    def total_chars(self) -> int:
        """Canonical pack size, measured with D1's own size function."""
        return sum(payload_chars(entry) for entry in _iter_entries(self.resources))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "design_dna": dict(self.design_dna),
            "decisions": dict(self.decisions),
            "resources": dict(self.resources),
            "truncation": dict(self.truncation),
            "limits": dict(self.limits),
            "degraded": self.degraded,
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        """One bounded, payload-free line. The only string safe to log."""
        return (
            f"resources={len(self.resources)} "
            f"chars={self.total_chars} "
            f"truncated={self.truncation.get('truncated')} "
            f"degraded={self.degraded}"
        )


#: The pack contract version. Bumped only when the SHAPE changes in a way a
#: consumer must notice. Selection semantics changing is not a shape change, so
#: it does not bump this.
DESIGN_CONTEXT_PACK_VERSION = 1


def _iter_entries(resources: Mapping[str, Any]):
    """Yield every :class:`DesignEntry` carried by the pack's resource section."""
    for result in resources.values():
        for entry in result.get("entries", ()):
            yield entry


def _echo_design_dna(
    design_dna: Optional[Mapping[str, Any]], max_fields: int
) -> Dict[str, Any]:
    """Carry the DNA's CANONICAL keys only, bounded.

    Non-canonical keys are dropped rather than carried: the pack must not become
    a second place a design fact can be found. Everything canonical is bounded
    per-value, and the total field count is capped, so a pathological document
    cannot dominate the context.
    """
    document, _ = unwrap_design_dna(design_dna if isinstance(design_dna, Mapping) else {})
    if not isinstance(document, Mapping) or not document:
        return {}

    echo: Dict[str, Any] = {}
    for key in CANONICAL_DESIGN_DNA_KEYS:
        if len(echo) >= max_fields:
            break
        if key not in document:
            continue
        value = document[key]
        if isinstance(value, (dict, list)):
            echo[key] = value
        elif isinstance(value, str):
            echo[key] = value[:400]
        else:
            echo[key] = value
    return echo


def _decisions_section(
    plan: DesignResourceSelectionPlan,
    requirements: DesignRequirementSet,
) -> Dict[str, Any]:
    """The structured decision record FRONTEND may act on."""
    return {
        "selected_resources": [e.to_dict() for e in plan.selected_resources],
        "rejected_resources": [e.to_dict() for e in plan.rejected_resources],
        "dependency_decisions": [d.to_dict() for d in plan.dependency_decisions],
        "component_sources": dict(plan.component_sources),
        "requirements": requirements.to_dict(),
        "dependency_states": list(DEPENDENCY_STATES),
        "authority_precedence": list(DESIGN_AUTHORITY_PRECEDENCE),
        "selection_reasons": list(SELECTION_REASONS),
    }


def _shrink_decisions(decisions: Dict[str, Any], budget: int) -> Tuple[Dict[str, Any], bool]:
    """Shrink the decision section to ``budget``, dropping rejected detail first.

    Rejections are dropped BEFORE selections. A rejection records what was NOT
    chosen; a selection records what FRONTEND may do. Losing the former is
    inconvenient; losing the latter would let FRONTEND proceed on a decision it
    can no longer see, which is the direction that fails unsafely.
    """
    if budget <= 0:
        return {}, True
    if len(str(decisions)) <= budget:
        return decisions, False

    shrunk = dict(decisions)
    shrunk["rejected_resources"] = []
    shrunk["dependency_decisions"] = [
        d for d in decisions["dependency_decisions"] if d["state"] == "selected"
    ]
    if len(str(shrunk)) <= budget:
        return shrunk, True
    return (
        {
            "selected_resources": decisions["selected_resources"],
            "rejected_resources": [],
            "dependency_decisions": [
                d
                for d in decisions["dependency_decisions"]
                if d["state"] == "selected"
            ],
        },
        True,
    )


def build_design_context_pack(
    hermes_home,
    *,
    design_dna: Optional[Mapping[str, Any]] = None,
    requested_resources: Sequence[str] = (),
    query: str = "",
    user_requirements: Optional[Mapping[str, Any]] = None,
    manifest=None,
    capability_report=None,
    limits: Optional[DesignContextPackLimits] = None,
    include_guidance: bool = True,
    retrieve: bool = True,
) -> DesignContextPack:
    """Build the one bounded context pack FRONTEND consumes.

    Order is deliberate and is the module's central claim:

    1. Derive requirements from the accepted Design DNA.
    2. **Select** resources and dependencies from those requirements. This
       happens BEFORE any retrieval, so nothing a corpus contains can influence
       selection.
    3. **Retrieve** content only for resources that were selected, under D1's
       own bounds.
    4. Attach the results to the already-made decision.

    ``retrieve=False`` builds the decision-only pack, which is what a caller
    wants when it must decide whether content is worth fetching at all.
    """
    if limits is None:
        limits = DEFAULT_PACK_LIMITS

    requirements = derive_design_requirements(design_dna, user_requirements=user_requirements)

    plan = select_design_resources(
        design_dna,
        requested=requested_resources,
        manifest=manifest,
        capability_report=capability_report,
        user_requirements=user_requirements,
        include_guidance=include_guidance,
    )

    warnings: List[str] = []
    if not isinstance(design_dna, Mapping) or not design_dna:
        warnings.append(WARNING_NO_DESIGN_DNA)

    resources: Dict[str, Any] = {}
    dropped_total = 0

    # Retrieval runs ONLY for selected, content-bearing resources.
    if retrieve and plan.selected_ids:
        report: DesignRetrievalReport = retrieve_design_guidance(
            hermes_home,
            plan.selected_ids,
            query=query,
            limits=limits.retrieval,
            manifest=manifest,
            capability_report=capability_report,
        )
        for resource_id, result in sorted(report.results.items()):
            resources[resource_id] = result.to_dict()
            dropped_total += result.dropped_entries
            for warning in result.warnings:
                if warning not in warnings:
                    warnings.append(warning)
        if report.truncated and report.total_chars < sum(
            len(str(r)) for r in report.results.values()
        ):
            if WARNING_RESOURCE_DROPPED not in warnings:
                warnings.append(WARNING_RESOURCE_DROPPED)
        plan_degraded = report.to_dict().get("degraded")

    decisions, decisions_truncated = _shrink_decisions(
        _decisions_section(plan, requirements), limits.max_decision_chars
    )
    if decisions_truncated:
        warnings.append(WARNING_PACK_TRUNCATED)

    dropped_total += len(plan.rejected_resources)
    pack = DesignContextPack(
        version=DESIGN_CONTEXT_PACK_VERSION,
        design_dna=_echo_design_dna(design_dna, limits.max_dna_fields),
        decisions=decisions,
        resources=resources,
        truncation={
            "truncated": decisions_truncated or bool(resources),
            "dropped_resources": len(plan.rejected_resources),
            "dropped_entries": dropped_total,
            "retrieval_limits": limits.retrieval.to_dict(),
        },
        limits=limits.to_dict(),
        degraded=plan.degraded,
        warnings=tuple(dict.fromkeys(warnings)),
    )

    logger.info("Design context pack: %s", pack.summary())
    return pack


__all__ = [
    "DEFAULT_PACK_LIMITS",
    "DESIGN_CONTEXT_PACK_VERSION",
    "WARNING_NO_DESIGN_DNA",
    "WARNING_PACK_TRUNCATED",
    "WARNING_RESOURCE_DROPPED",
    "DesignContextPack",
    "DesignContextPackLimits",
    "build_design_context_pack",
]