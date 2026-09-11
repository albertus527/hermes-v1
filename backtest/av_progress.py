"""Verified Alpha Vantage NEWS coverage attestation primitives.

Shared by two consumers:

- ``backtest.cli.cmd_fetch_alphavantage_news`` — the verified-span skip
  (skip re-fetching a ticker whose requested span is ALREADY attested by
  a verified ``coverage_manifests`` row under the requested manifest
  version; §3.8 publication unit is the only coverage evidence).
- the AV acquisition wrapper (``~/.hermes/scripts/run_av_news_batches.sh``
  via ``verified_news_digest``) — morning/afternoon progress detection
  must depend on verified NEWS coverage attestation, NOT resume-directory
  mutations (checkpoints, saturation markers, partial leaf progress, and
  replay are implementation state and never coverage evidence).

Pure reads; no provider requests; no schema changes; stdlib only.
"""

from __future__ import annotations

import hashlib


def _verified_news_rows(conn) -> list[tuple]:
    """All verified Alpha Vantage NEWS attestation rows, canonically
    ordered: (ticker, span_start, span_end, manifest_version)."""
    return sorted(conn.execute(
        "SELECT ticker, span_start, span_end, manifest_version "
        "FROM coverage_manifests "
        "WHERE source_kind='NEWS' AND verified=1 "
        "AND manifest_version LIKE 'alphavantage-news-%'").fetchall())


def verified_news_digest(db_path) -> str:
    """Digest of verified Alpha Vantage NEWS coverage attestation.

    Changes ONLY when a new verified ``alphavantage-news-*`` span is
    published to ``coverage_manifests``. Idempotent republication,
    headline upserts, checkpoint churn, saturation markers, and
    fetch-log/report writes do NOT move it. Missing store → sentinel.
    """
    import sqlite3
    from pathlib import Path

    db_path = Path(db_path)
    if not db_path.exists():
        return "NO_BACKTEST_DB"
    conn = sqlite3.connect(str(db_path))
    try:
        rows = _verified_news_rows(conn)
    finally:
        conn.close()
    payload = "\n".join("|".join(r) for r in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def has_verified_news_cover(conn, *, ticker: str, span_start: str,
                            span_end: str, manifest_version: str) -> bool:
    """True when an EXISTING verified NEWS row attests AT LEAST the
    requested span for the ticker under the requested manifest version.

    Coverage evidence is the manifest row ONLY. Never checkpoints, never
    ``news_headlines`` presence, never partial/narrower coverage (the
    existing row must cover the full requested bounds).
    """
    row = conn.execute(
        "SELECT 1 FROM coverage_manifests "
        "WHERE source_kind='NEWS' AND ticker=? AND verified=1 "
        "AND manifest_version=? AND span_start<=? AND span_end>=? "
        "LIMIT 1",
        (ticker, manifest_version, span_start, span_end)).fetchone()
    return row is not None
