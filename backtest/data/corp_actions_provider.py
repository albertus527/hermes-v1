"""Corporate-actions provider interface (R2.7 §3.6 data contract).

Designated provider: Alpaca Corporate Actions endpoint (coverage MUST
CONFIRM). Implementations must NOT derive ratios/dividends from adjusted ÷
unadjusted price quotients (§3.6 rule 3, §21 item 28).
"""

from __future__ import annotations

import datetime as _dt
from abc import ABC, abstractmethod

from trading_core.corporate_actions import CorporateAction, CoverageAttestation


class CorporateActionsProvider(ABC):
    """§3.6 contract: splits and cash dividends with ex/record/pay dates,
    versioned (corp_actions_version) and attested via coverage manifests."""

    @abstractmethod
    def get_events(
        self, ticker: str, *, start: _dt.date, end: _dt.date,
    ) -> list[CorporateAction]: ...

    @abstractmethod
    def coverage_attestations(
        self, ticker: str, *, start: _dt.date, end: _dt.date,
    ) -> list[CoverageAttestation]:
        """FP-5 attestations (source_kind=CORP_ACTIONS); a verified
        attestation may cover a span with no events (verified-zero)."""

    @abstractmethod
    def dataset_version(self) -> str:
        """The version logged on every simulated trade (§3.6 rule 1)."""
