"""Effective-dated SEC/TAF/CAT regulatory fee schedule (spec §9.7).

Loads the versioned ``fee_schedule.yaml`` artifact and resolves the
three historical entry states per component per fee-computation date:

- verified dated rate        -> VERIFIED_RATE
- verified ``applicable: false`` -> VERIFIED_ZERO (a verified zero, not a gap)
- no entry for the date      -> unverified; eligible for
  TRAIN_SUBSTITUTED_ZERO **only** in TRAIN runs (§9.7 item 3)

There is no backward projection and no backfill: absence is never
interpreted as a historical fact (§9.7 item 3).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from trading_core.fees import FeeInputStatus, RegulatoryRates

REGULATORY_COMPONENTS = ("SEC", "TAF", "CAT")


@dataclass(frozen=True)
class ScheduleEntry:
    component: str
    effective_from: _dt.date
    effective_to: _dt.date              # inclusive; None = open-ended
    rate: Decimal | None                # None iff applicable is False
    applicable: bool
    verified: bool

    def covers(self, d: _dt.date) -> bool:
        if d < self.effective_from:
            return False
        if self.effective_to is not None and d > self.effective_to:
            return False
        return True


@dataclass(frozen=True)
class FeeSchedule:
    """A loaded, versioned fee schedule artifact."""

    version: str
    entries: tuple[ScheduleEntry, ...]

    def resolve(self, component: str, fee_computation_date: _dt.date) -> FeeInputStatus | tuple[FeeInputStatus, Decimal]:
        """Return the (status[, rate]) for a component on a date.

        Unverified dates return the bare TRAIN_SUBSTITUTED_ZERO status;
        legality is enforced by trading_core.fees at pricing time.
        """
        matches = [e for e in self.entries
                   if e.component == component and e.covers(fee_computation_date)]
        # Most recently effective entry wins on overlap.
        matches.sort(key=lambda e: e.effective_from)
        for entry in reversed(matches):
            if not entry.verified:
                continue
            if not entry.applicable:
                return FeeInputStatus.VERIFIED_ZERO
            return FeeInputStatus.VERIFIED_RATE, entry.rate
        return FeeInputStatus.TRAIN_SUBSTITUTED_ZERO

    def regulatory_rates_at(self, fee_computation_date: _dt.date) -> RegulatoryRates:
        statuses: dict[str, FeeInputStatus] = {}
        rates: dict[str, Decimal] = {}
        for comp in REGULATORY_COMPONENTS:
            resolved = self.resolve(comp, fee_computation_date)
            if isinstance(resolved, tuple):
                statuses[comp] = resolved[0]
                rates[comp] = resolved[1]
            else:
                statuses[comp] = resolved
        return RegulatoryRates(statuses=statuses, rates=rates,
                               schedule_version=self.version)


def load_fee_schedule(path: str | Path) -> FeeSchedule:
    """Load and validate a fee_schedule.yaml artifact (fail-closed)."""
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError(f"{path}: missing top-level 'version'")
    entries: list[ScheduleEntry] = []
    for i, raw in enumerate(data.get("entries") or []):
        try:
            component = str(raw["component"]).upper()
            if component not in REGULATORY_COMPONENTS:
                raise ValueError(f"unknown component {component!r}")
            eff = raw.get("effective") or []
            if len(eff) != 2:
                raise ValueError("'effective' must be [from, to]")
            eff_from = _dt.date.fromisoformat(str(eff[0]))
            eff_to = None if eff[1] in (None, "", "null") else _dt.date.fromisoformat(str(eff[1]))
            applicable = bool(raw.get("applicable", True))
            rate = None if not applicable else Decimal(str(raw["rate"]))
            entries.append(ScheduleEntry(
                component=component,
                effective_from=eff_from, effective_to=eff_to,
                rate=rate, applicable=applicable,
                verified=bool(raw.get("verified", False)),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: entry {i} invalid: {exc}") from exc
    return FeeSchedule(version=str(data["version"]), entries=tuple(entries))
