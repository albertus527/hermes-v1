"""R2.7 Phase-0 — FRED VIXCLS ingestion (§3.1, §5.2 volatility state).

Grounded strictly in the published FRED API:

``GET https://api.stlouisfed.org/fred/series/observations?series_id=VIXCLS&api_key=…&file_type=json&observation_start=YYYY-MM-DD&observation_end=YYYY-MM-DD``

Response: ``{"observations": [{date, value, …}, …]}`` where ``value`` is
the daily VIX close or ``"."`` (FRED's missing-value encoding — those
rows are skipped, never coerced to a number).

Coverage honesty: the FEE-free FRED VIXCLS series is KNOWN available
(§3.1); this job records the observed observation window. The §5.2
five-calendar-day gap rule is applied at consumption
(trading_core.regime.resolve_vix), not here.
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable

from backtest.data.ingest_core import (
    FetchLog,
    FetchRecord,
    IngestionError,
    IngestStore,
    _now_utc_iso,
    fetch_json,
)

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
VIXCLS_SERIES_ID = "VIXCLS"


class CredentialsMissing(IngestionError):
    """FRED_API_KEY is not configured (§20 Phase 0 credential
    prerequisite). Fail-closed: the job refuses to run."""


def _fred_params() -> dict[str, str]:
    from hermes_cli.config import get_env_value
    key = get_env_value("FRED_API_KEY")
    if not key:
        raise CredentialsMissing(
            "FRED_API_KEY is not configured (~/.hermes/.env); the FRED "
            "VIXCLS ingestion job refuses to run")
    return {"api_key": key, "file_type": "json"}


def fetch_vixcls(
    *,
    start: _dt.date,
    end: _dt.date,
    http_get: Callable[..., tuple[int, str]] | None = None,
    fetch_log: FetchLog | None = None,
) -> list[dict]:
    """Fetch VIXCLS observations for [start, end].

    Returns rows for :meth:`IngestStore.upsert_vix`:
    ``{observation_date, value}`` (both strings). FRED ``"."`` missing
    values are skipped — never stored, never coerced.
    """
    log = fetch_log or FetchLog()
    doc = fetch_json(
        FRED_BASE,
        params={**_fred_params(), "series_id": VIXCLS_SERIES_ID,
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat()},
        http_get=http_get)
    observations = doc.get("observations") if isinstance(doc, dict) else None
    if observations is None:
        raise IngestionError("FRED response missing 'observations'")
    rows: list[dict] = []
    for obs in observations:
        date = obs.get("date")
        value = obs.get("value")
        if not date or value is None or str(value) == ".":
            continue  # FRED missing-value encoding — skip, never coerce
        rows.append({"observation_date": str(date), "value": str(value)})
    log.add(FetchRecord(
        provider="fred", endpoint="series/observations",
        params={"series_id": VIXCLS_SERIES_ID,
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat()},
        fetched_at=_now_utc_iso(), items=len(rows)))
    return rows
