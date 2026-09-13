"""Structured external-operation contracts for Website Builder R1.

Small, concrete success/error results for external integrations.
No generic provider framework — one boundary per integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class OperationResult:
    """Structured result for an external operation."""

    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_code: Optional[str] = None
    retryable: bool = False

    @classmethod
    def ok(cls, data: Optional[Dict[str, Any]] = None) -> "OperationResult":
        return cls(success=True, data=data or {})

    @classmethod
    def fail(
        cls,
        error: str,
        error_code: Optional[str] = None,
        retryable: bool = False,
    ) -> "OperationResult":
        return cls(
            success=False,
            error=error,
            error_code=error_code,
            retryable=retryable,
        )


@dataclass
class DomainCheckResult(OperationResult):
    """Result of a domain availability check."""

    domain: str = ""
    status: str = "UNKNOWN"
    price: Optional[str] = None
    currency: Optional[str] = None
    term: Optional[str] = None
    renewal_price: Optional[str] = None
    source: Optional[str] = None
    checked_at: Optional[float] = None


@dataclass
class PreviewResult(OperationResult):
    """Result of creating an external preview."""

    preview_url: Optional[str] = None
    deployment_id: Optional[str] = None
    source_revision: Optional[int] = None


@dataclass
class PublishResult(OperationResult):
    """Result of publishing to production."""

    production_url: Optional[str] = None
    deployment_id: Optional[str] = None
    source_revision: Optional[int] = None
