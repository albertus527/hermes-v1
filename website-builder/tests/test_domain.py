"""Phase 4 tests: domain discovery, status handling, UNKNOWN failure, defer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.contracts import DomainCheckResult
from app.core.state import ProjectStateStore
from app.domain.discovery import (
    DomainDiscovery,
    DomainStatus,
    generate_candidates,
    generate_alternatives,
)


class TestCandidateGeneration(unittest.TestCase):
    def test_generate_candidates_basic(self):
        candidates = generate_candidates("Northcut")
        self.assertGreater(len(candidates), 0)
        self.assertLessEqual(len(candidates), 5)
        self.assertEqual(candidates[0].domain, "northcut.com")

    def test_generate_candidates_normalization(self):
        candidates = generate_candidates("My Cool Shop")
        domains = [c.domain for c in candidates]
        self.assertIn("my-cool-shop.com", domains)

    def test_generate_candidates_empty(self):
        self.assertEqual(generate_candidates(""), [])
        self.assertEqual(generate_candidates("   "), [])

    def test_generate_candidates_bounded(self):
        candidates = generate_candidates("A" * 100)
        self.assertLessEqual(len(candidates), 5)
        for c in candidates:
            label = c.domain.split(".")[0]
            self.assertLessEqual(len(label), 63)

    def test_generate_alternatives(self):
        checked = ["northcut.com"]
        alternatives = generate_alternatives("Northcut", checked)
        self.assertLessEqual(len(alternatives), 3)
        for alt in alternatives:
            self.assertNotIn(alt.domain, checked)


class TestDomainDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_provider_returns_unknown(self):
        discovery = DomainDiscovery(self.store, lookup_fn=None)
        result = discovery.check_domain("example.com")
        self.assertFalse(result.success)
        self.assertEqual(result.status, DomainStatus.UNKNOWN.value)
        self.assertEqual(result.error_code, "NO_PROVIDER")

    def test_provider_failure_returns_unknown(self):
        def failing_lookup(domain: str) -> DomainCheckResult:
            raise ConnectionError("Network unreachable")

        discovery = DomainDiscovery(self.store, lookup_fn=failing_lookup)
        result = discovery.check_domain("example.com")
        self.assertFalse(result.success)
        self.assertEqual(result.status, DomainStatus.UNKNOWN.value)
        self.assertEqual(result.error_code, "PROVIDER_ERROR")
        self.assertTrue(result.retryable)

    def test_available_domain_persisted(self):
        def available_lookup(domain: str) -> DomainCheckResult:
            return DomainCheckResult(
                success=True,
                domain=domain,
                status=DomainStatus.AVAILABLE_UNRESERVED.value,
                price="12.99",
                currency="USD",
                term="1y",
                renewal_price="14.99",
                source="test-provider",
                checked_at=1694500000.0,
            )

        discovery = DomainDiscovery(self.store, lookup_fn=available_lookup)
        results = discovery.discover_for_project("proj-dom-1", "Northcut")

        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].status, DomainStatus.AVAILABLE_UNRESERVED.value)

        state = self.store.load("proj-dom-1")
        self.assertEqual(state.domain.domain, "northcut.com")
        self.assertEqual(state.domain.status, DomainStatus.AVAILABLE_UNRESERVED.value)
        self.assertEqual(state.domain.price, "12.99")
        self.assertEqual(state.domain.currency, "USD")
        self.assertEqual(state.domain.source, "test-provider")

    def test_unavailable_domain_generates_alternatives(self):
        """When every preferred candidate is unavailable, alternatives are generated.

        discover_for_project checks each generated candidate in turn and
        persists the latest result, so to keep the preferred domain UNAVAILABLE
        the stub must report all base `northcut.*` candidates unavailable while
        the generated `*hq/*app/*site` alternatives are available.
        """

        def unavailable_lookup(domain: str) -> DomainCheckResult:
            # Base candidates are "northcut<tld>"; alternatives carry a suffix
            # (northcuthq.com, northcutapp.com, ...). Only the base candidates
            # are unavailable.
            is_alternative = domain not in {
                "northcut.com",
                "northcut.id",
                "northcut.co.id",
                "northcut.net",
                "northcut.io",
            }
            if is_alternative:
                return DomainCheckResult(
                    success=True,
                    domain=domain,
                    status=DomainStatus.AVAILABLE_UNRESERVED.value,
                    price="10.99",
                    currency="USD",
                    source="test-provider",
                    checked_at=1694500000.0,
                )
            return DomainCheckResult(
                success=True,
                domain=domain,
                status=DomainStatus.UNAVAILABLE.value,
                source="test-provider",
                checked_at=1694500000.0,
            )

        discovery = DomainDiscovery(self.store, lookup_fn=unavailable_lookup)
        results = discovery.discover_for_project("proj-dom-2", "Northcut")

        self.assertGreater(len(results), 1)
        state = self.store.load("proj-dom-2")
        self.assertEqual(state.domain.status, DomainStatus.UNAVAILABLE.value)
        self.assertGreater(len(state.domain.alternatives), 0)

    def test_defer_domain(self):
        discovery = DomainDiscovery(self.store, lookup_fn=None)
        discovery.defer_domain("proj-dom-3")

        state = self.store.load("proj-dom-3")
        self.assertTrue(state.domain.deferred)
        self.assertIsNone(state.domain.domain)
        self.assertEqual(state.domain.status, DomainStatus.UNKNOWN.value)

    def test_never_infer_from_dns(self):
        """DNS absence must never be treated as availability."""
        def dns_like_lookup(domain: str) -> DomainCheckResult:
            # Simulating a provider that returns NXDOMAIN-like response
            return DomainCheckResult(
                success=True,
                domain=domain,
                status=DomainStatus.UNKNOWN.value,  # Must be UNKNOWN, not AVAILABLE
                source="dns-lookup",
                checked_at=1694500000.0,
            )

        discovery = DomainDiscovery(self.store, lookup_fn=dns_like_lookup)
        result = discovery.check_domain("nonexistent-domain-12345.com")
        self.assertEqual(result.status, DomainStatus.UNKNOWN.value)


if __name__ == "__main__":
    unittest.main()
