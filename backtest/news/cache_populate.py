"""R2.7 §20 Phase 2 — the news-cache population job.

The ONLY authorized bulk live-LLM context (§11.5, §20 Phase 2): a one-time
batch pass that classifies EVERY timed headline in run-pinned verified NEWS
covered spans, irrespective of wall-clock age (N-21), and writes the
immutable cache keyed (headline_hash, source, schema_version,
model_version). This job is NOT a backtest — no trading logic runs here,
no scores are computed, no bars are read.

Outputs (per §20 Phase 2):
- the cache itself (append-only; idempotent re-runs skip already-classified
  headlines);
- a **cache-completeness report** verifying zero cache misses over covered
  spans (§11.2 trigger (b) prevention);
- a **P-4 cache-integrity report** proving identical effect fields for
  every shared (headline_hash, ticker) across source-keyed entries;
- a reproducibility record (model id, llm_config_version, headline count,
  coverage manifest_version).

Classification-time metadata (``classified_at_wallclock``) is recorded for
provenance only and has NO decision effect (N-21).

BLOCKED: the job refuses to run until ``backtest.pinned_model`` is set
(§11.5 [MISSING SOURCE CONTENT]) — it fails closed with
:class:`PinnedModelMissing` rather than guessing a model. The offline
injection path (an explicit ``classifier`` argument) exists for tests and
for future offline population from pre-recorded model outputs.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field

from trading_core.news_effects import headline_hash as compute_headline_hash

from backtest.news.cache import (
    HeadlineInventory,
    NewsClassificationCache,
)
from backtest.news.classifier import NewsClassifierClient, PinnedModelMissing

__all__ = [
    "PinnedModelMissing",
    "PopulationReport",
    "populate_news_cache_entries",
]


@dataclass
class PopulationReport:
    """§20 Phase 2 reproducibility record + the two mandatory reports."""
    model_version: str
    schema_version: str
    llm_config_version: str
    coverage_manifest_versions: list[str]
    headlines_total: int = 0            # timed headlines in covered spans
    headlines_classified: int = 0       # newly classified this run
    headlines_skipped_existing: int = 0 # already cached (idempotent)
    cache_misses: list[dict] = field(default_factory=list)
    p4_conflicts: list[dict] = field(default_factory=list)
    malformed: list[dict] = field(default_factory=list)
    classified_at_wallclock: str = ""

    @property
    def complete(self) -> bool:
        """Zero cache misses over covered spans AND zero P-4 conflicts
        AND zero malformed records."""
        return not (self.cache_misses or self.p4_conflicts or self.malformed)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, indent=2)


def populate_news_cache_entries(
    *,
    cache: NewsClassificationCache,
    inventory: HeadlineInventory,
    manifest_versions: list[str],
    pinned_model: str,
    llm_config_version: str = "",
    classifier: NewsClassifierClient | None = None,
    classified_at_wallclock: str | None = None,
    run_id: str = "",
    config_version: int = 0,
    code_commit: str = "",
    entries: list[tuple[str, str, _dt.datetime, str]],
) -> PopulationReport:
    """Population over explicit ``(ticker, source, published_at,
    headline_text)`` records (the shape the Phase-0 Finnhub ingest
    produces). For each timed record inside a verified NEWS covered span:

    1. skip if the cache key is already populated (idempotent);
    2. classify via the pinned client (or the injected one);
    3. insert with P-4 pre-write assertion;
    4. record malformed outputs fail-closed into the report (and re-raise
       for hard integrity failures — P-4 conflicts and cache-key
       conflicts are halts, not report rows).
    """
    if classifier is None:
        if not (pinned_model or "").strip():
            raise PinnedModelMissing(
                "backtest.pinned_model is not set; the Phase-2 population "
                "job refuses to run without the exact pinned identifier")
        classifier = NewsClassifierClient(pinned_model)
    report = PopulationReport(
        model_version=classifier.model_version,
        schema_version=classifier.schema_version,
        llm_config_version=llm_config_version,
        coverage_manifest_versions=list(manifest_versions),
        classified_at_wallclock=classified_at_wallclock or "",
    )
    cached = cache.cached_keys()
    for ticker, source, published_at, headline_text in entries:
        if not any(inventory.covered(ticker, manifest_version=mv,
                                     at=published_at)
                   for mv in manifest_versions):
            continue  # coverage gap — never classified, never a miss
        report.headlines_total += 1
        h_hash = compute_headline_hash(headline_text)
        if (h_hash, source, classifier.schema_version,
                classifier.model_version, ticker) in cached:
            report.headlines_skipped_existing += 1
            continue
        try:
            classification, payload = classifier.classify(
                ticker=ticker, headline_text=headline_text,
                source=source, published_at=published_at)
        except Exception as exc:  # malformed model output — fail-closed row
            report.malformed.append({
                "ticker": ticker, "source": source,
                "headline_hash": h_hash, "error": repr(exc)})
            continue
        cache.insert(
            classification, payload=payload,
            classified_at_wallclock=classified_at_wallclock,
            run_id=run_id, config_version=config_version,
            code_commit=code_commit)
        cached.add((h_hash, source, classifier.schema_version,
                    classifier.model_version, ticker))
        report.headlines_classified += 1
    # Completeness: after population, every covered-span timed headline
    # must have a cache entry (§11.2 trigger (b) prevention).
    report.cache_misses = compute_cache_misses(
        cache=cache, inventory=inventory, manifest_versions=manifest_versions,
        model_version=classifier.model_version,
        schema_version=classifier.schema_version,
        tickers=sorted({e[0] for e in entries}))
    # P-4 integrity sweep across the full (model_version, schema_version)
    # pin — identical effect fields per (headline_hash, ticker).
    report.p4_conflicts = compute_p4_conflicts(
        cache, model_version=classifier.model_version,
        schema_version=classifier.schema_version)
    return report


def compute_cache_misses(
    *,
    cache: NewsClassificationCache,
    inventory: HeadlineInventory,
    manifest_versions: list[str],
    model_version: str,
    schema_version: str,
    tickers: list[str] | None = None,
) -> list[dict]:
    """Every timed headline inside a verified NEWS covered span with no
    cache entry under the pin — the §20 Phase 2 completeness report body.
    Empty list == zero misses (canonical). Reads the news_headlines
    inventory directly (no model output involved)."""
    cached = cache.cached_keys()
    misses: list[dict] = []
    for ticker in tickers if tickers is not None else _inventory_tickers(inventory):
        for h_hash, source, published_at in inventory.timed_headlines(ticker):
            if not any(inventory.covered(ticker, manifest_version=mv,
                                         at=published_at)
                       for mv in manifest_versions):
                continue  # coverage gap — not a miss
            if (h_hash, source, schema_version, model_version,
                    ticker) not in cached:
                misses.append({"ticker": ticker, "source": source,
                               "headline_hash": h_hash})
    return misses


def _inventory_tickers(inventory: HeadlineInventory) -> list[str]:
    """Distinct tickers present in the news_headlines inventory."""
    conn = inventory._conn  # noqa: SLF001 — same-package read helper
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM news_headlines ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


def compute_p4_conflicts(
    cache: NewsClassificationCache, *, model_version: str,
    schema_version: str,
) -> list[dict]:
    """Scan the pinned cache slice for (headline_hash, ticker) groups whose
    effect fields differ across source-keyed entries. Non-empty means the
    cache is non-canonical (§16 rule 9); the deterministic core would halt
    under §19 item 6 — the report surfaces it at population time."""
    try:
        cache.all_classifications(model_version=model_version,
                                  schema_version=schema_version)
    except Exception as exc:
        return [{"error": repr(exc)}]
    return []
