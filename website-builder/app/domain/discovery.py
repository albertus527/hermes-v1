"""Domain discovery for Website Builder R1.

One read-only discovery source. No purchase, no DNS inference.
Provider-neutral boundary — concrete provider configured externally.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.core.contracts import DomainCheckResult
from app.core.state import DomainState, ProjectStateStore


class DomainStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE_UNRESERVED = "AVAILABLE_UNRESERVED"
    UNAVAILABLE = "UNAVAILABLE"
    OWNED_UNVERIFIED = "OWNED_UNVERIFIED"
    OWNED_VERIFIED = "OWNED_VERIFIED"


# Bounded candidate generation
_MAX_CANDIDATES = 5
_MAX_ALTERNATIVES = 3

# Common TLDs for R1
_DEFAULT_TLDS = [".com", ".id", ".co.id", ".net", ".io"]


@dataclass
class DomainCandidate:
    """A domain candidate to check."""

    domain: str
    source: str = "generated"


def generate_candidates(name: str, tlds: Optional[List[str]] = None) -> List[DomainCandidate]:
    """Generate a small bounded candidate set from NAME.

    Rules:
    - lowercase, strip non-alphanumeric except hyphens
    - no leading/trailing hyphens
    - max 63 chars per label
    - bounded to _MAX_CANDIDATES
    """
    if not name:
        return []

    tlds = tlds or _DEFAULT_TLDS
    # Normalize name: lowercase, replace spaces with hyphens, strip invalid chars
    normalized = re.sub(r"[^a-z0-9\-]", "", name.lower().replace(" ", "-"))
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        return []

    candidates: List[DomainCandidate] = []
    for tld in tlds:
        if len(candidates) >= _MAX_CANDIDATES:
            break
        domain = f"{normalized}{tld}"
        # Validate domain label length
        label = normalized[:63]
        if len(label) < len(normalized):
            domain = f"{label}{tld}"
        candidates.append(DomainCandidate(domain=domain))

    return candidates


def generate_alternatives(name: str, checked: List[str]) -> List[DomainCandidate]:
    """Generate a small bounded set of sensible alternatives when preferred is unavailable."""
    if not name:
        return []

    normalized = re.sub(r"[^a-z0-9\-]", "", name.lower().replace(" ", "-"))
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        return []

    alternatives: List[DomainCandidate] = []
    suffixes = ["hq", "app", "site", "web", "co"]

    for suffix in suffixes:
        if len(alternatives) >= _MAX_ALTERNATIVES:
            break
        candidate = f"{normalized}{suffix}.com"
        if candidate not in checked:
            alternatives.append(DomainCandidate(domain=candidate, source="alternative"))

    return alternatives


class DomainDiscovery:
    """Domain discovery with provider-neutral boundary.

    The lookup_fn is injected — no hardcoded provider.
    If no provider is configured, all results are UNKNOWN.
    """

    def __init__(
        self,
        store: ProjectStateStore,
        lookup_fn: Optional[Callable[[str], DomainCheckResult]] = None,
    ):
        self.store = store
        self.lookup_fn = lookup_fn

    def check_domain(self, domain: str) -> DomainCheckResult:
        """Check a single domain. Returns UNKNOWN on failure."""
        if self.lookup_fn is None:
            return DomainCheckResult(
                success=False,
                error="No domain discovery provider configured",
                error_code="NO_PROVIDER",
                retryable=False,
                domain=domain,
                status=DomainStatus.UNKNOWN.value,
                checked_at=time.time(),
            )

        try:
            result = self.lookup_fn(domain)
            if not isinstance(result, DomainCheckResult):
                return DomainCheckResult(
                    success=False,
                    error="Invalid provider response",
                    error_code="INVALID_RESPONSE",
                    retryable=True,
                    domain=domain,
                    status=DomainStatus.UNKNOWN.value,
                    checked_at=time.time(),
                )
            return result
        except Exception as exc:
            return DomainCheckResult(
                success=False,
                error=str(exc),
                error_code="PROVIDER_ERROR",
                retryable=True,
                domain=domain,
                status=DomainStatus.UNKNOWN.value,
                checked_at=time.time(),
            )

    def discover_for_project(
        self, project_id: str, name: str
    ) -> List[DomainCheckResult]:
        """Run bounded domain discovery for a project and persist results."""
        candidates = generate_candidates(name)
        results: List[DomainCheckResult] = []
        checked_domains: List[str] = []

        for candidate in candidates:
            result = self.check_domain(candidate.domain)
            results.append(result)
            checked_domains.append(candidate.domain)

            # Persist to project state
            with self.store.acquire_writer(project_id) as state:
                state.domain.domain = result.domain
                state.domain.status = result.status
                state.domain.price = result.price
                state.domain.currency = result.currency
                state.domain.term = result.term
                state.domain.renewal_price = result.renewal_price
                state.domain.source = result.source
                state.domain.checked_at = result.checked_at
                self.store.save(state)

            # Stop early if we found an available domain
            if result.status == DomainStatus.AVAILABLE_UNRESERVED.value:
                break

        # Generate alternatives if preferred is unavailable
        if results and results[0].status == DomainStatus.UNAVAILABLE.value:
            alternatives = generate_alternatives(name, checked_domains)
            for alt in alternatives:
                result = self.check_domain(alt.domain)
                results.append(result)
                with self.store.acquire_writer(project_id) as state:
                    state.domain.alternatives.append(
                        {
                            "domain": result.domain,
                            "status": result.status,
                            "price": result.price,
                            "currency": result.currency,
                        }
                    )
                    self.store.save(state)

        return results

    def defer_domain(self, project_id: str) -> None:
        """Mark domain decision as deferred — continue with .vercel.app."""
        with self.store.acquire_writer(project_id) as state:
            state.domain.deferred = True
            state.domain.domain = None
            state.domain.status = DomainStatus.UNKNOWN.value
            self.store.save(state)
