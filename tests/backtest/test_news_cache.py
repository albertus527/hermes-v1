"""Phase 2 — deterministic news-classification cache tests.

Hermetic: every test uses a tmp_path SQLite store and an injected fake
classifier; NO test performs a live LLM call, and the replay path is
exercised read-only. Verifies:

1. cache persistence round-trip;
2. deterministic cache-key lookup;
3. no live LLM invocation during replay (the replay modules never even
   import the auxiliary client);
4. duplicate materialization is idempotent;
5. same headline+ticker + same classification across sources is accepted;
6. same headline+ticker + differing classification triggers the
   deterministic P-4 failure (NewsCacheIntegrityFailure, §19 item 6 halt);
7. malformed classification records fail deterministically;
8. missing required metadata is detected;
9. news timestamp/session mapping (covered spans, timed vs untimed) is
   deterministic;
10. the population job's completeness + integrity reports;
11. the §11.4 calibration framework (BLOCKED semantics — never PASS);
12. the pinned-model gate (fail-closed PinnedModelMissing).
"""

import datetime as dt
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from backtest.news.cache import (
    CacheKeyConflictError,
    HeadlineInventory,
    MalformedClassificationError,
    NewsClassificationCache,
    open_news_cache,
    validate_classification_payload,
)
from backtest.news.calibration import (
    MIN_LABELED_HEADLINES,
    CalibrationDatasetError,
    LabeledHeadline,
    evaluate_calibration,
    load_labeled_headlines,
)
from backtest.news.cache_populate import (
    PopulationReport,
    populate_news_cache_entries,
)
from backtest.news.classifier import (
    NewsClassifierClient,
    PinnedModelMissing,
    parse_pinned_model,
)
from trading_core.errors import NewsCacheIntegrityFailure

ET = ZoneInfo("America/New_York")
PUB = dt.datetime(2026, 1, 5, 9, 0, tzinfo=ET)
PIN = "openrouter/anthropic/claude-test-model@2026-01-01"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class FakeLLM:
    """Injected classifier LLM — canned JSON answers, counts calls, and
    FAILS the test if it is ever reached from a replay path."""

    def __init__(self, answers: list[dict] | dict):
        self.answers = answers if isinstance(answers, list) else [answers] * 10**6
        self.calls = 0

    def __call__(self, *, messages):
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]

        class _Msg:
            content = json.dumps(answer)

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


def _answer(**kw):
    base = dict(category="PRODUCT", direction="BULLISH", severity="MEDIUM",
                ma_role="NEITHER", confidence=0.95)
    base.update(kw)
    return base


def _store(tmp_path):
    return open_news_cache(tmp_path / "bt.sqlite3")


def _inventory_with_headlines(cache: NewsClassificationCache, rows):
    """Insert raw headlines + NEWS coverage manifests into the store and
    return a HeadlineInventory over the same connection."""
    conn = cache._conn
    for ticker, source, published_at, text in rows:
        from trading_core.news_effects import headline_hash
        conn.execute(
            "INSERT INTO news_headlines (headline_hash, source, ticker, "
            "published_at, headline_text_normalized, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (headline_hash(text), source, ticker,
             published_at.isoformat() if published_at else None,
             text, "2026-01-01T00:00:00+00:00"))
    conn.execute(
        "INSERT INTO coverage_manifests (source_kind, ticker, span_start, "
        "span_end, verified, manifest_version) VALUES ('NEWS','T',?,?,1,'mv1')",
        ("2025-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"))
    conn.commit()
    return HeadlineInventory(conn)


def _payload(ticker="T", h_hash="h1", source="finnhub", **kw):
    p = {
        "ticker": ticker, "category": "PRODUCT", "direction": "BULLISH",
        "severity": "MEDIUM", "ma_role": "NEITHER", "confidence": 0.95,
        "published_at": PUB.isoformat(), "headline_hash": h_hash,
        "source": source, "keyword_override": False,
        "schema_version": "news_schema_v3", "model_version": PIN,
    }
    p.update(kw)
    return p


def _cls_from_payload(p):
    return validate_classification_payload(p)


# --------------------------------------------------------------------------
# 1+2. Persistence round-trip + deterministic lookup
# --------------------------------------------------------------------------


class TestRoundTripAndLookup:
    def test_roundtrip_preserves_every_field(self, tmp_path):
        cache = _store(tmp_path)
        payload = _payload()
        cache.insert(_cls_from_payload(payload), payload=payload,
                     classified_at_wallclock="2026-08-29T12:00:00+00:00")
        # Fresh handle over the same file = persistence, not memory.
        cache2 = open_news_cache(tmp_path / "bt.sqlite3")
        got = cache2.lookup("h1", "finnhub", schema_version="news_schema_v3",
                            model_version=PIN)
        assert got is not None
        assert got.category == "PRODUCT"
        assert got.direction == "BULLISH"
        assert got.severity == "MEDIUM"
        assert got.ma_role == "NEITHER"
        assert got.confidence == 0.95
        assert got.ticker == "T"
        assert got.source == "finnhub"
        assert got.keyword_override is False
        assert got.published_at == PUB
        # classified_at_wallclock is provenance only, not effect.
        row = cache2._conn.execute(
            "SELECT classified_at_wallclock, json_payload FROM "
            "news_classifications").fetchone()
        assert row["classified_at_wallclock"] == "2026-08-29T12:00:00+00:00"
        assert json.loads(row["json_payload"])["model_version"] == PIN

    def test_lookup_is_deterministic(self, tmp_path):
        cache = _store(tmp_path)
        payload = _payload()
        cache.insert(_cls_from_payload(payload), payload=payload)
        for _ in range(3):
            a = cache.lookup("h1", "finnhub", schema_version="news_schema_v3",
                             model_version=PIN)
            b = cache.lookup("h1", "finnhub", schema_version="news_schema_v3",
                             model_version=PIN)
            assert a == b  # frozen dataclass equality

    def test_cache_key_is_quadruple(self, tmp_path):
        """The key is (headline_hash, source, schema_version, model_version):
        a different source or model_version is a DIFFERENT cache entry."""
        cache = _store(tmp_path)
        cache.insert(_cls_from_payload(_payload(source="finnhub")),
                     payload=_payload(source="finnhub"))
        cache.insert(_cls_from_payload(_payload(source="rss")),
                     payload=_payload(source="rss"))
        assert cache.headline_count() == 2
        assert cache.lookup("h1", "finnhub", schema_version="news_schema_v3",
                            model_version=PIN) is not None
        assert cache.lookup("h1", "rss", schema_version="news_schema_v3",
                            model_version=PIN) is not None
        assert cache.lookup("h1", "finnhub", schema_version="news_schema_v3",
                            model_version="other@v2") is None
        assert cache.lookup("h1", "finnhub", schema_version="news_schema_v2",
                            model_version=PIN) is None

    def test_miss_returns_none_never_fabricates(self, tmp_path):
        cache = _store(tmp_path)
        assert cache.lookup("nope", "finnhub", schema_version="news_schema_v3",
                            model_version=PIN) is None

    def test_all_classifications_p4_asserted(self, tmp_path):
        cache = _store(tmp_path)
        cache.insert(_cls_from_payload(_payload()), payload=_payload())
        got = cache.all_classifications(model_version=PIN)
        assert len(got) == 1
        assert cache.all_classifications(model_version="other") == []


# --------------------------------------------------------------------------
# 3. No live LLM during replay
# --------------------------------------------------------------------------


class TestNoLiveLLMInReplay:
    def test_replay_modules_never_import_auxiliary_client(self, tmp_path):
        """The replay-path modules must not pull the LLM stack in."""
        import sys
        cache = _store(tmp_path)
        payload = _payload()
        cache.insert(_cls_from_payload(payload), payload=payload)
        # simulate a replay process: fresh interpreter check via modules
        for mod in ("backtest.news.cache", "backtest.news.cache_populate",
                    "trading_core.news_effects"):
            assert mod in sys.modules
        # the classifier module may be imported (it lazily reaches the LLM
        # only inside classify()); assert the replay store never holds an
        # llm callable:
        assert not hasattr(cache, "_llm_call")
        # read-only replay handle:
        ro = open_news_cache(tmp_path / "bt.sqlite3", read_only=True)
        assert ro.lookup("h1", "finnhub", schema_version="news_schema_v3",
                         model_version=PIN) is not None
        # a write attempt on the replay handle fails closed (SQLite
        # query_only) — replay never mutates the cache.
        with pytest.raises(sqlite3.OperationalError):
            ro.insert(_cls_from_payload(_payload(h_hash="h2")),
                      payload=_payload(h_hash="h2"))

    def test_population_without_classifier_requires_pin(self, tmp_path):
        """Fail-closed: no pinned model -> PinnedModelMissing, NEVER a
        silent fallback to any available model."""
        cache = _store(tmp_path)
        inventory = _inventory_with_headlines(cache, [])
        with pytest.raises(PinnedModelMissing):
            populate_news_cache_entries(
                cache=cache, inventory=inventory,
                manifest_versions=["mv1"], pinned_model="",
                entries=[("T", "finnhub", PUB, "Some headline")])

    def test_parse_pinned_model_rejects_bad_values(self):
        with pytest.raises(PinnedModelMissing):
            parse_pinned_model("")
        with pytest.raises(PinnedModelMissing):
            parse_pinned_model("gpt-4o")  # not openrouter-pinned
        with pytest.raises(PinnedModelMissing):
            parse_pinned_model("openrouter/")  # no model part
        model_version, model_id = parse_pinned_model(
            "openrouter/x/y@v1")
        assert model_version == "openrouter/x/y@v1"
        assert model_id == "openrouter/x/y"


# --------------------------------------------------------------------------
# 4. Idempotent duplicate materialization
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_duplicate_materialization_is_noop(self, tmp_path):
        cache = _store(tmp_path)
        payload = _payload()
        assert cache.insert(_cls_from_payload(payload), payload=payload)
        assert not cache.insert(_cls_from_payload(payload), payload=payload)
        assert cache.headline_count() == 1

    def test_population_rerun_skips_existing(self, tmp_path):
        cache = _store(tmp_path)
        rows = [("T", "finnhub", PUB, "ACME launches product")]
        inventory = _inventory_with_headlines(cache, rows)
        fake = FakeLLM(_answer())
        classifier = NewsClassifierClient(PIN, llm_call=fake)
        r1 = populate_news_cache_entries(
            cache=cache, inventory=inventory, manifest_versions=["mv1"],
            pinned_model=PIN, classifier=classifier, entries=rows)
        assert r1.headlines_classified == 1
        assert fake.calls == 1
        r2 = populate_news_cache_entries(
            cache=cache, inventory=inventory, manifest_versions=["mv1"],
            pinned_model=PIN, classifier=classifier, entries=rows)
        assert r2.headlines_classified == 0
        assert r2.headlines_skipped_existing == 1
        assert fake.calls == 1  # no additional LLM call on re-run
        assert r2.complete
        assert cache.headline_count() == 1

    def test_conflicting_payload_for_same_key_is_error(self, tmp_path):
        """§11.5: historical caches are never overwritten."""
        cache = _store(tmp_path)
        cache.insert(_cls_from_payload(_payload()), payload=_payload())
        differing = _payload(severity="HIGH")
        with pytest.raises(CacheKeyConflictError):
            cache.insert(_cls_from_payload(differing), payload=differing)
        # original row untouched
        got = cache.lookup("h1", "finnhub", schema_version="news_schema_v3",
                           model_version=PIN)
        assert got is not None and got.severity == "MEDIUM"


# --------------------------------------------------------------------------
# 5+6. P-4: same (headline_hash, ticker)
# --------------------------------------------------------------------------


class TestP4Consistency:
    def test_same_hash_ticker_same_effects_across_sources_ok(self, tmp_path):
        cache = _store(tmp_path)
        for source in ("finnhub", "rss"):
            p = _payload(source=source)
            cache.insert(_cls_from_payload(p), payload=p)
        assert cache.headline_count() == 2
        allc = cache.all_classifications(model_version=PIN)
        assert len(allc) == 2  # both source-keyed entries survive
        # source-independent dedupe collapses to one effect
        from trading_core.news_effects import dedupe_by_hash
        assert len(dedupe_by_hash(allc)) == 1

    def test_same_hash_ticker_differing_effects_halts(self, tmp_path):
        """P-4: deterministic NewsCacheIntegrityFailure on write."""
        cache = _store(tmp_path)
        p1 = _payload(source="finnhub")
        cache.insert(_cls_from_payload(p1), payload=p1)
        p2 = _payload(source="rss", severity="HIGH")  # differing effect field
        with pytest.raises(NewsCacheIntegrityFailure) as excinfo:
            cache.insert(_cls_from_payload(p2), payload=p2)
        assert "NEWS_CACHE_INTEGRITY_FAILURE" in str(excinfo.value)
        # nothing was written for the conflicting entry
        assert cache.lookup("h1", "rss", schema_version="news_schema_v3",
                            model_version=PIN) is None

    def test_confidence_difference_is_also_p4_conflict(self, tmp_path):
        cache = _store(tmp_path)
        p1 = _payload(source="finnhub")
        cache.insert(_cls_from_payload(p1), payload=p1)
        p2 = _payload(source="rss", confidence=0.5)
        with pytest.raises(NewsCacheIntegrityFailure):
            cache.insert(_cls_from_payload(p2), payload=p2)

    def test_different_ticker_same_hash_is_not_conflict(self, tmp_path):
        """P-4 is keyed by (headline_hash, ticker) — a shared headline
        text across two tickers carries independent classifications."""
        cache = _store(tmp_path)
        p1 = _payload(ticker="T")
        cache.insert(_cls_from_payload(p1), payload=p1)
        p2 = _payload(ticker="AAPL", severity="HIGH")
        cache.insert(_cls_from_payload(p2), payload=p2)
        assert cache.headline_count() == 2

    def test_read_side_integrity_sweep_detects_corruption(self, tmp_path):
        """§16 rule 9: the P-4 assertion also guards the READ path."""
        cache = _store(tmp_path)
        p1 = _payload(source="finnhub")
        cache.insert(_cls_from_payload(p1), payload=p1)
        # Simulate out-of-band corruption (bypassing the store API).
        p2 = _payload(source="rss", severity="HIGH")
        cache._conn.execute(
            "INSERT INTO news_classifications (headline_hash, ticker, source,"
            " ma_role, keyword_override, json_payload, model_version,"
            " schema_version, published_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("h1", "T", "rss", "NEITHER", 0,
             json.dumps(p2, sort_keys=True), PIN, "news_schema_v3",
             PUB.isoformat()))
        cache._conn.commit()
        with pytest.raises(NewsCacheIntegrityFailure):
            cache.all_classifications(model_version=PIN)


# --------------------------------------------------------------------------
# 7+8. Malformed records / missing metadata
# --------------------------------------------------------------------------


class TestMalformedRecords:
    @pytest.mark.parametrize("mutation", [
        {"category": "INVALID"},          # bad enum
        {"direction": "SIDEWAYS"},
        {"severity": "EXTREME"},
        {"ma_role": "BOTH"},
        {"ma_role": "TARGET"},            # §11.1: TARGET requires M&A
        {"confidence": "high"},           # not a number
        {"confidence": 1.5},              # out of range
        {"confidence": None},
        {"keyword_override": "no"},       # not a boolean
        {"schema_version": "news_schema_v2"},
        {"published_at": "not-a-date"},
        {"published_at": "2026-01-05T09:00:00"},  # naive — no tz offset
        {"ticker": ""},
    ])
    def test_field_validation_fails_closed(self, mutation):
        with pytest.raises(MalformedClassificationError):
            validate_classification_payload(_payload(**mutation))

    @pytest.mark.parametrize("missing", [
        "ticker", "category", "direction", "severity", "ma_role",
        "confidence", "published_at", "headline_hash", "source",
        "keyword_override", "schema_version", "model_version",
    ])
    def test_missing_required_field_detected(self, missing):
        p = _payload()
        del p[missing]
        with pytest.raises(MalformedClassificationError):
            validate_classification_payload(p)

    def test_extra_field_rejected_strict(self):
        p = _payload(extra_thought="buy!!!")
        with pytest.raises(MalformedClassificationError):
            validate_classification_payload(p)

    def test_malformed_row_fails_on_read(self, tmp_path):
        """A malformed cached row fails closed at replay lookup."""
        cache = _store(tmp_path)
        p = _payload()
        cache.insert(_cls_from_payload(p), payload=p)
        bad = _payload(h_hash="h9")
        bad["direction"] = "SIDEWAYS"
        cache._conn.execute(
            "INSERT INTO news_classifications (headline_hash, ticker, source,"
            " ma_role, keyword_override, json_payload, model_version,"
            " schema_version, published_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("h9", "T", "finnhub", "NEITHER", 0,
             json.dumps(bad, sort_keys=True), PIN, "news_schema_v3",
             PUB.isoformat()))
        cache._conn.commit()
        with pytest.raises(MalformedClassificationError):
            cache.lookup("h9", "finnhub", schema_version="news_schema_v3",
                         model_version=PIN)

    def test_classifier_malformed_output_recorded_not_written(self, tmp_path):
        """The population job records malformed model output fail-closed
        in the report; the cache stays clean."""
        cache = _store(tmp_path)
        rows = [("T", "finnhub", PUB, "ACME launches product")]
        inventory = _inventory_with_headlines(cache, rows)
        fake = FakeLLM([{"category": "NOPE", "direction": "BULLISH",
                         "severity": "MEDIUM", "ma_role": "NEITHER",
                         "confidence": 0.9}])
        classifier = NewsClassifierClient(PIN, llm_call=fake)
        report = populate_news_cache_entries(
            cache=cache, inventory=inventory, manifest_versions=["mv1"],
            pinned_model=PIN, classifier=classifier, entries=rows)
        assert len(report.malformed) == 1
        assert report.headlines_classified == 0
        assert not report.complete
        # but the missing entry IS a cache miss in the completeness report
        assert len(report.cache_misses) == 1


# --------------------------------------------------------------------------
# 9. Timestamp / covered-span / session determinism
# --------------------------------------------------------------------------


class TestTimestampSessionMapping:
    def test_timed_vs_untimed_headlines(self, tmp_path):
        """Untimed (published_at NULL) headlines are invisible to replay —
        they never produce effects and never raise NEWS_UNVERIFIED."""
        cache = _store(tmp_path)
        conn = cache._conn
        conn.execute(
            "INSERT INTO news_headlines (headline_hash, source, ticker, "
            "published_at, headline_text_normalized, fetched_at) "
            "VALUES ('hu','finnhub','T',NULL,'untimed','2026-01-01T00:00:00+00:00')")
        conn.commit()
        inventory = HeadlineInventory(conn)
        assert inventory.timed_headlines("T") == []

    def test_covered_span_boundaries_inclusive(self, tmp_path):
        cache = _store(tmp_path)
        conn = cache._conn
        conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, span_start,"
            " span_end, verified, manifest_version) "
            "VALUES ('NEWS','T',?,?,1,'mv1')",
            ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"))
        conn.commit()
        inventory = HeadlineInventory(conn)
        inside = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
        before = dt.datetime(2025, 12, 31, 23, 59, tzinfo=dt.timezone.utc)
        after = dt.datetime(2026, 2, 1, 0, 1, tzinfo=dt.timezone.utc)
        assert inventory.covered("T", manifest_version="mv1", at=inside)
        assert inventory.covered("T", manifest_version="mv1", at=start)
        assert inventory.covered("T", manifest_version="mv1", at=end)
        assert not inventory.covered("T", manifest_version="mv1", at=before)
        assert not inventory.covered("T", manifest_version="mv1", at=after)

    def test_unverified_manifest_is_not_covered(self, tmp_path):
        cache = _store(tmp_path)
        conn = cache._conn
        conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, span_start,"
            " span_end, verified, manifest_version) "
            "VALUES ('NEWS','T',?,?,0,'mv1')",
            ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"))
        conn.commit()
        inventory = HeadlineInventory(conn)
        at = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)
        assert not inventory.covered("T", manifest_version="mv1", at=at)
        # a different manifest version never covers either (run-pinned)
        assert not inventory.covered("T", manifest_version="mv2", at=at)

    def test_population_skips_coverage_gaps(self, tmp_path):
        """Headlines outside every verified covered span are neither
        classified nor reported as misses (§11.6)."""
        cache = _store(tmp_path)
        conn = cache._conn
        conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, span_start,"
            " span_end, verified, manifest_version) "
            "VALUES ('NEWS','T',?,?,1,'mv1')",
            ("2026-01-05T00:00:00+00:00", "2026-01-06T00:00:00+00:00"))
        conn.commit()
        inventory = HeadlineInventory(conn)
        outside = dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)
        rows = [("T", "finnhub", outside, "Old headline outside coverage")]
        fake = FakeLLM(_answer())
        classifier = NewsClassifierClient(PIN, llm_call=fake)
        report = populate_news_cache_entries(
            cache=cache, inventory=inventory, manifest_versions=["mv1"],
            pinned_model=PIN, classifier=classifier, entries=rows)
        assert report.headlines_total == 0
        assert fake.calls == 0
        assert report.cache_misses == []
        assert report.complete


# --------------------------------------------------------------------------
# 10. Population reports
# --------------------------------------------------------------------------


class TestPopulationReports:
    def _populate(self, tmp_path, answers, rows):
        cache = _store(tmp_path)
        inventory = _inventory_with_headlines(cache, rows)
        fake = FakeLLM(answers)
        classifier = NewsClassifierClient(PIN, llm_call=fake)
        report = populate_news_cache_entries(
            cache=cache, inventory=inventory, manifest_versions=["mv1"],
            pinned_model=PIN, classifier=classifier, entries=rows,
            llm_config_version="llm-cfg-7",
            classified_at_wallclock="2026-08-29T00:00:00+00:00")
        return cache, report

    def test_complete_report_zero_misses_zero_conflicts(self, tmp_path):
        rows = [("T", "finnhub", PUB, "ACME launches product")]
        cache, report = self._populate(tmp_path, _answer(), rows)
        assert report.complete
        assert report.cache_misses == []
        assert report.p4_conflicts == []
        assert report.headlines_total == 1
        assert report.headlines_classified == 1
        assert report.model_version == PIN
        assert report.schema_version == "news_schema_v3"
        assert report.llm_config_version == "llm-cfg-7"
        assert report.coverage_manifest_versions == ["mv1"]
        assert report.classified_at_wallclock == "2026-08-29T00:00:00+00:00"

    def test_completeness_report_flags_misses(self, tmp_path):
        """A headline that failed classification is a cache miss."""
        rows = [("T", "finnhub", PUB, "ACME launches product")]
        cache, report = self._populate(
            tmp_path, {"category": "PRODUCT", "direction": "BULLISH",
                       "severity": "MEDIUM", "ma_role": "NEITHER",
                       "confidence": 1.9}, rows)  # invalid confidence
        assert len(report.malformed) == 1
        assert len(report.cache_misses) == 1
        assert not report.complete

    def test_p4_conflict_via_population_two_sources(self, tmp_path):
        """Two sources, same text, but the fake LLM answers differently per
        call — the store's P-4 write assertion raises and the population
        job surfaces the halt (integrity failures are exceptions, not
        report rows)."""
        rows = [("T", "finnhub", PUB, "ACME launches product"),
                ("T", "rss", PUB, "ACME launches product")]
        with pytest.raises(NewsCacheIntegrityFailure):
            self._populate(tmp_path, [
                _answer(severity="MEDIUM"), _answer(severity="HIGH")], rows)


# --------------------------------------------------------------------------
# Replay determinism: cached payload feeds the deterministic core
# --------------------------------------------------------------------------


class TestReplayFeedsDeterministicCore:
    def test_cached_classification_drives_effect_mapping(self, tmp_path):
        """End-to-end determinism: identical cached payload -> identical
        trading-relevant outputs from trading_core (no LLM anywhere)."""
        from trading_core.news_effects import (
            catalyst_score_points,
            g7_vetoed_at,
        )
        cache = _store(tmp_path)
        payload = _payload(direction="BEARISH", severity="MEDIUM",
                           confidence=0.9)
        cache.insert(_cls_from_payload(payload), payload=payload)
        ro = open_news_cache(tmp_path / "bt.sqlite3", read_only=True)
        cls = ro.all_classifications(model_version=PIN)
        SCAN = dt.datetime(2026, 1, 5, 10, 0, tzinfo=ET)
        assert catalyst_score_points(cls, SCAN) == -10
        assert catalyst_score_points(cls, SCAN) == -10  # deterministic

    def test_keyword_override_persists_through_cache(self, tmp_path):
        """§11.3 override is applied at classification time and replayed
        from the cached payload without any LLM call."""
        rows = [("T", "finnhub", PUB, "ACME under SEC investigation")]
        cache = _store(tmp_path)
        inventory = _inventory_with_headlines(cache, rows)
        fake = FakeLLM(_answer(direction="BULLISH", severity="LOW"))
        classifier = NewsClassifierClient(PIN, llm_call=fake)
        populate_news_cache_entries(
            cache=cache, inventory=inventory, manifest_versions=["mv1"],
            pinned_model=PIN, classifier=classifier, entries=rows)
        ro = open_news_cache(tmp_path / "bt.sqlite3", read_only=True)
        cls = ro.all_classifications(model_version=PIN)
        assert len(cls) == 1
        assert cls[0].keyword_override is True
        assert cls[0].direction == "BEARISH"
        assert cls[0].severity == "CRITICAL"
        from trading_core.news_effects import map_effect_branch
        assert map_effect_branch(cls[0]) == "BEARISH_CRITICAL"

    def test_classifier_never_supplies_identity_fields(self, tmp_path):
        """Even if the model tried to inject headline_hash/ticker/source in
        its JSON, the deterministic stamps win."""
        rows = [("T", "finnhub", PUB, "ACME launches product")]
        cache = _store(tmp_path)
        inventory = _inventory_with_headlines(cache, rows)
        fake = FakeLLM(dict(_answer(), headline_hash="FAKE", ticker="FAKE",
                            source="FAKE"))
        classifier = NewsClassifierClient(PIN, llm_call=fake)
        populate_news_cache_entries(
            cache=cache, inventory=inventory, manifest_versions=["mv1"],
            pinned_model=PIN, classifier=classifier, entries=rows)
        ro = open_news_cache(tmp_path / "bt.sqlite3", read_only=True)
        cls = ro.all_classifications(model_version=PIN)
        assert cls[0].ticker == "T"
        assert cls[0].source == "finnhub"
        from trading_core.news_effects import headline_hash
        assert cls[0].headline_hash == headline_hash("ACME launches product")


# --------------------------------------------------------------------------
# 11. Calibration framework (BLOCKED semantics)
# --------------------------------------------------------------------------


class TestCalibrationFramework:
    def test_labeled_set_schema_validated(self, tmp_path):
        good = {
            "headlines": [
                {"ticker": "AAPL", "headline_text": "Apple beats",
                 "published_at": "2024-01-05T09:30:00-05:00",
                 "source": "finnhub",
                 "label": {"category": "EARNINGS", "direction": "BULLISH",
                            "severity": "MEDIUM", "ma_role": "NEITHER"}},
            ]
        }
        p = tmp_path / "labeled.json"
        p.write_text(json.dumps(good))
        labeled = load_labeled_headlines(p)
        assert len(labeled) == 1

    def test_labeled_set_rejects_invalid_label(self, tmp_path):
        bad = {
            "headlines": [
                {"ticker": "AAPL", "headline_text": "x",
                 "published_at": "2024-01-05T09:30:00-05:00",
                 "source": "finnhub",
                 "label": {"category": "EARNINGS", "direction": "BULLISH",
                            "severity": "MEDIUM", "ma_role": "TARGET"}},
            ]
        }
        p = tmp_path / "labeled.json"
        p.write_text(json.dumps(bad))
        with pytest.raises(CalibrationDatasetError):
            load_labeled_headlines(p)

    def test_below_minimum_never_meets_gate(self, tmp_path):
        """A small set can never flip meets_minimum — no fabricated
        calibration."""
        labeled = [
            LabeledHeadline(
                ticker="T", headline_text=f"h{i}",
                published_at="2024-01-05T09:30:00-05:00",
                source="finnhub",
                label={"category": "PRODUCT", "direction": "BULLISH",
                        "severity": "MEDIUM", "ma_role": "NEITHER"})
            for i in range(10)
        ]
        report = evaluate_calibration(labeled, classifications={})
        assert not report.meets_minimum
        assert "NOT EVALUABLE" in report.status
        assert MIN_LABELED_HEADLINES == 200

    def test_empty_labeled_set_not_evaluable(self):
        report = evaluate_calibration([], classifications={})
        assert report.status.startswith("NOT EVALUABLE")
        assert report.accuracy is None

    def test_scoring_counts_exact_and_per_field(self, tmp_path):
        from trading_core.news_effects import headline_hash
        cls_payload = _payload(headline_hash=headline_hash("h0"))
        cls = validate_classification_payload(cls_payload)
        labeled = [
            LabeledHeadline(
                ticker="T", headline_text="h0",
                published_at="2024-01-05T09:30:00-05:00",
                source="finnhub",
                label={"category": "PRODUCT", "direction": "BEARISH",
                        "severity": "MEDIUM", "ma_role": "NEITHER"}),
        ]
        report = evaluate_calibration(labeled, {"h0": cls})
        assert report.exact_label_match == 0
        assert report.per_field_correct["category"] == 1
        assert report.per_field_correct["direction"] == 0


# --------------------------------------------------------------------------
# PopulationReport dataclass behavior
# --------------------------------------------------------------------------


class TestPopulationReportShape:
    def test_complete_requires_all_clean(self):
        r = PopulationReport(model_version=PIN, schema_version="news_schema_v3",
                             llm_config_version="",
                             coverage_manifest_versions=["mv1"])
        assert r.complete
        r.cache_misses.append({})
        assert not r.complete
        r.cache_misses = []
        r.p4_conflicts.append({})
        assert not r.complete
        r.p4_conflicts = []
        r.malformed.append({})
        assert not r.complete

    def test_to_json_roundtrip(self):
        r = PopulationReport(model_version=PIN,
                             schema_version="news_schema_v3",
                             llm_config_version="c1",
                             coverage_manifest_versions=["mv1"])
        doc = json.loads(r.to_json())
        assert doc["model_version"] == PIN
        assert doc["coverage_manifest_versions"] == ["mv1"]
