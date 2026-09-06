"""P-NEWS-EMPTY (DROP_INVALID_FP4_EMPTY) + P-NEWS-ATOMIC focused tests.

Covers the two approved policies added to the Alpha Vantage NEWS adapter:

1. FP4-EMPTY DROP: a structurally valid, non-empty/non-whitespace
   provider title whose ``normalize_headline_text(title) == ""`` is
   DROPPED (no canonical row, no headline_hash/classification, no error)
   with a deterministic observable drop count. Missing / null /
   non-string / "" / whitespace-only titles still FAIL CLOSED exactly as
   before.

2. ATOMIC PUBLICATION: canonical ``news_headlines`` +
   corresponding ``coverage_manifests`` rows for one Alpha Vantage
   publication unit are BOTH durable or NEITHER (temporary SQLite only;
   no production DB, no network).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

import backtest.data.fetch_alphavantage as fetch_alphavantage
from backtest.data.ingest_core import (
    DROP_INVALID_FP4_EMPTY,
    FetchLog,
    FetchRecord,
    IngestStore,
    IngestionError,
)
from trading_core.news_effects import normalize_headline_text

from tests.backtest.test_fetch_alphavantage import (
    _feed_item,
    _payload,
    av_creds,  # noqa: F401  (fixture import, shared)
    fake_transport,
)


# ---------------------------------------------------------------------------
# FP4-EMPTY drop policy
# ---------------------------------------------------------------------------


def _one_window(body, *, ticker="AAPL",
                start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30)):
    """Run one unsaturated window fetch; return (rows, drops)."""
    calls: list = []
    http = fake_transport({"alphavantage.co": [(200, body)]}, calls)
    return fetch_alphavantage.fetch_news_inventory(
        ticker=ticker, start=start, end=end, http_get=http,
        sleep_fn=lambda _s: None, fetch_log=FetchLog()), calls


def _normalize_only(titles, *, ticker="AAPL"):
    """normalize_news_payload over synthetic items; returns (rows, drops)."""
    items = [_feed_item(title=t) for t in titles]
    return fetch_alphavantage.normalize_news_payload(
        _payload(items), ticker=ticker, fetched_at="2019-02-01T00:00:00+00:00")


class TestFp4EmptyFailClosed:
    """missing / null / non-string / '' / whitespace-only FAIL CLOSED."""

    @pytest.mark.parametrize("bad_title", [None, "", "   ", "\t\n ", 12345,
                                           [], {}])
    def test_invalid_title_fails_closed(self, av_creds, bad_title):
        with pytest.raises(fetch_alphavantage.IngestionError, match="title"):
            _normalize_only([bad_title])

    def test_invalid_title_via_fetch_fails_closed(self, av_creds):
        with pytest.raises(fetch_alphavantage.IngestionError, match="title"):
            _one_window(_payload([_feed_item(title=None)]))


class TestFp4EmptyDrop:
    """Structurally valid but FP4-empty titles DROP deterministically."""

    def test_underscore_drops(self):
        rows, drops = _normalize_only(["_"])
        assert rows == []
        assert drops == 1

    def test_ascii_punctuation_only_drops(self):
        rows, drops = _normalize_only(["?!..."])
        assert rows == []
        assert drops == 1

    def test_unicode_punctuation_only_drops(self):
        # Unicode punctuation (General Category P*) normalizes to empty.
        rows, drops = _normalize_only(["«»—–‒―…"])
        assert rows == []
        assert drops == 1

    def test_zero_drops_reported_for_all_valid(self):
        rows, drops = _normalize_only(
            ["Apple beats synthetic earnings estimates",
             "Fed raises rates again"])
        assert len(rows) == 2
        assert drops == 0

    def test_mixed_valid_and_drops_preserves_valid_rows(self):
        rows, drops = _normalize_only([
            "_",                                  # drop
            "Apple beats synthetic earnings estimates",  # keep
            "?!",                                 # drop
            "Fed raises rates again",             # keep
        ])
        assert len(rows) == 2
        assert {r["headline_text_normalized"] for r in rows} == {
            normalize_headline_text("Apple beats synthetic earnings estimates"),
            normalize_headline_text("Fed raises rates again"),
        }
        assert drops == 2

    def test_input_order_deterministic(self):
        seq_a = ["_", "Apple beats synthetic earnings estimates", "?!", "X"]
        seq_b = ["?!", "X", "_", "Apple beats synthetic earnings estimates"]
        rows_a, drops_a = _normalize_only(seq_a)
        rows_b, drops_b = _normalize_only(seq_b)
        assert drops_a == drops_b == 2
        assert (len(rows_a), len(rows_b)) == (2, 2)
        # Same set of identities regardless of input order.
        assert {r["headline_hash"] for r in rows_a} == \
            {r["headline_hash"] for r in rows_b}

    def test_drop_count_in_fetch_record(self, av_creds):
        body = _payload([
            _feed_item(title="Apple beats synthetic earnings estimates"),
            _feed_item(title="_",
                       time_published="20190102T160000"),
        ])
        (rows, drops), _calls = _one_window(body)
        assert len(rows) == 1
        assert drops == 1

    def test_drop_reason_identifier_is_stable(self):
        assert DROP_INVALID_FP4_EMPTY == "DROP_INVALID_FP4_EMPTY"


class TestSaturationInvariant:
    """Saturation uses the RAW feed count BEFORE dropping."""

    def _saturating_with_droppable_titles(self, n=1000):
        items = []
        for i in range(n):
            # Every 3rd item is FP4-empty (would be dropped later).
            title = ("_" if i % 3 == 0 else
                     f"Apple synthetic headline number {i}")
            items.append(_feed_item(
                title=title, time_published=f"2019010{(i % 8) + 1}T000000"))
        return _payload(items)

    def test_raw_1000_remains_saturated_despite_drops(self, av_creds,
                                                      tmp_path):
        # The response holds exactly 1000 RAW items; even though ~333
        # titles would be dropped, saturation is decided on len(feed).
        http = fake_transport({"alphavantage.co": [
            (200, self._saturating_with_droppable_titles(1000))]})
        with pytest.raises(fetch_alphavantage.WindowSaturatedError):
            fetch_alphavantage.fetch_news_inventory(
                ticker="AAPL", start=dt.date(2019, 1, 1),
                end=dt.date(2019, 12, 31), http_get=http,
                sleep_fn=lambda _s: None, checkpoint_dir=str(tmp_path))

    def test_unsaturated_with_drops_stays_unsaturated(self, av_creds):
        # 999 raw items (< limit) with droppable titles → NOT saturated;
        # the sweep completes and returns the valid rows + drop count.
        http = fake_transport({"alphavantage.co": [
            (200, self._saturating_with_droppable_titles(999))]})
        rows, drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None)
        assert drops == 333
        assert len(rows) == 666


# ---------------------------------------------------------------------------
# Checkpoint / resume semantics with dropped items
# ---------------------------------------------------------------------------


class TestCheckpointDropSemantics:
    def test_completed_leaf_with_dropped_item_checkpoints_valid_rows(
            self, av_creds, tmp_path):
        # The checkpoint stores only VALID canonical rows; the dropped
        # item never enters the artifact.
        body = _payload([
            _feed_item(title="Apple beats synthetic earnings estimates"),
            _feed_item(title="_", time_published="20190102T160000"),
        ])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        rows, drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        assert len(rows) == 1 and drops == 1
        docs = [json.loads(p.read_text())
                for p in tmp_path.glob("*.json")]
        assert len(docs) == 1
        assert docs[0]["complete"] is True
        assert len(docs[0]["rows"]) == 1  # dropped item not persisted
        # Drop COUNT persisted (never the raw dropped item's content).
        assert docs[0]["drops"] == {"DROP_INVALID_FP4_EMPTY": 1}
        assert all(r["headline_text_normalized"]
                   for r in docs[0]["rows"])  # no empty/placeholder rows

    def test_replay_requires_zero_http_and_restores_drop_count(
            self, av_creds, tmp_path):
        body = _payload([
            _feed_item(title="Apple beats synthetic earnings estimates"),
            _feed_item(title="_", time_published="20190102T160000"),
        ])
        calls: list = []
        http = fake_transport({"alphavantage.co": [(200, body)]}, calls)
        first_rows, first_drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        assert len(calls) == 1
        assert first_drops == 1

        # Replay: NO new HTTP, the STORED drop count is restored (the
        # exclusion is represented by the replayed inventory), the
        # dropped item is NOT resurrected, rows identical to live fetch.
        replay_http = fake_transport({"alphavantage.co": [
            (500, "{}")]})  # any HTTP would fail loudly
        replay_rows, replay_drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=replay_http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        assert replay_rows == first_rows
        assert replay_drops == 1
        assert all("apple" in r["headline_text_normalized"]
                   for r in replay_rows)

    def test_replay_does_not_mutate_checkpoint(self, av_creds, tmp_path):
        body = _payload([
            _feed_item(title="Apple beats synthetic earnings estimates"),
            _feed_item(title="_", time_published="20190102T160000"),
        ])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        path = next(tmp_path.glob("*.json"))
        before = path.read_bytes()
        for _ in range(3):
            replay_http = fake_transport({"alphavantage.co": [(500, "{}")]})
            rows, drops = fetch_alphavantage.fetch_news_inventory(
                ticker="AAPL", start=dt.date(2019, 1, 1),
                end=dt.date(2019, 1, 30), http_get=replay_http,
                sleep_fn=lambda _s: None, checkpoint_dir=str(tmp_path))
            assert drops == 1
            assert len(rows) == 1
        assert path.read_bytes() == before

    def test_saturation_marker_no_drop_count_invented(self, av_creds,
                                                      tmp_path):
        # A saturated response is DISCARDED before row normalization —
        # its potential FP4-empty items are not canonical exclusions, so
        # neither the saturation marker nor the run may invent a drop
        # count for it. The marker carries no drop metadata at all.
        from backtest.data.fetch_alphavantage import _load_saturation_marker

        def http(url, headers=None, params=None, timeout=30.0):
            items = [_feed_item(
                title="_" if i % 2 == 0 else f"Apple synthetic {i}",
                time_published=f"2019010{(i % 8) + 1}T000000")
                for i in range(1000)]
            return 200, json.dumps(_payload(items))

        with pytest.raises(fetch_alphavantage.WindowSaturatedError):
            fetch_alphavantage.fetch_news_inventory(
                ticker="AAPL", start=dt.date(2019, 1, 1),
                end=dt.date(2019, 1, 30), http_get=http,
                sleep_fn=lambda _s: None, checkpoint_dir=str(tmp_path))
        a = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
        b = dt.datetime(2019, 1, 30, 23, 59, tzinfo=dt.timezone.utc)
        root_marker = (fetch_alphavantage._checkpoint_path(
            "AAPL", a, b, tmp_path))
        doc = json.loads(root_marker.read_text())
        assert doc["saturated"] is True
        assert "drops" not in doc          # no drop metadata on markers
        assert "rows" not in doc           # raw feed never persisted
        assert _load_saturation_marker(root_marker, ticker="AAPL", a=a, b=b)

    def test_no_raw_dropped_headline_content_persisted(
            self, av_creds, tmp_path):
        # Observability persists ONLY the count/reason metadata; the
        # dropped raw provider item (title/source/summary/timestamp)
        # must not appear anywhere in the checkpoint artifact.
        dropped_title = "??!!.."
        body = _payload([
            _feed_item(title="Apple beats synthetic earnings estimates"),
            _feed_item(title=dropped_title,
                       time_published="20190102T160000"),
        ])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        text = next(tmp_path.glob("*.json")).read_text()
        assert dropped_title not in text
        doc = json.loads(text)
        assert doc["drops"] == {"DROP_INVALID_FP4_EMPTY": 1}
        assert len(doc["rows"]) == 1

# ---------------------------------------------------------------------------
# Observability: FetchRecord.drops / FetchLog.total_drops
# ---------------------------------------------------------------------------


class TestObservability:
    def test_zero_drops_reported_as_zero(self, av_creds):
        log = FetchLog()
        body = _payload([_feed_item()])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None, fetch_log=log)
        assert log.total_items == 1
        assert log.total_drops == 0
        assert log.records[0].drops == 0

    def test_one_drop_reported_as_one(self, av_creds):
        log = FetchLog()
        body = _payload([
            _feed_item(),
            _feed_item(title="_", time_published="20190102T160000"),
        ])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None, fetch_log=log)
        assert log.records[0].items == 2   # RAW provider count
        assert log.records[0].drops == 1
        assert log.total_drops == 1

    def test_multiple_drops_exact(self, av_creds):
        log = FetchLog()
        body = _payload([
            _feed_item(title="_"),
            _feed_item(title="?!", time_published="20190102T160000"),
            _feed_item(title="«»", time_published="20190103T160000"),
            _feed_item(title="Apple valid synthetic headline"),
        ])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None, fetch_log=log)
        assert log.records[0].drops == 3
        assert log.total_drops == 3

    def test_resume_accounting_semantics(self, av_creds, tmp_path):
        # RESUME ACCOUNTING: drop counts describe deterministic canonical
        # exclusions represented by the fetched/replayed inventory used
        # by THIS run — not merely exclusions observed during HTTP calls
        # made by this process. A live leaf fetch reports its drops and
        # persists them in the checkpoint; a replayed leaf re-reports the
        # stored count verbatim (zero HTTP, never recounted).
        log = FetchLog()
        body = _payload([_feed_item(title="_")])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        rows, drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None, fetch_log=log,
            checkpoint_dir=str(tmp_path))
        assert rows == [] and drops == 1
        assert log.total_drops == 1
        # The FetchLog serializes drops into the report artifact.
        report = json.loads(log.to_json())
        assert report[0]["drops"] == 1

        # A resumed run that replays the same leaf (zero HTTP) re-reports
        # the SAME exclusion count via the restored checkpoint metadata.
        replay_http = fake_transport({"alphavantage.co": [(500, "{}")]})
        replay_log = FetchLog()
        replay_rows, replay_drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=replay_http, sleep_fn=lambda _s: None,
            fetch_log=replay_log, checkpoint_dir=str(tmp_path))
        assert replay_drops == 1
        assert replay_log.total_drops == 1

    def test_interrupted_acquisition_exclusions_survive_resume(
            self, av_creds, tmp_path):
        # Scenario: leaf A succeeds live (1 FP4-empty item) and its
        # checkpoint persists; the run then FAILS on a later interval
        # (HTTP 500) BEFORE any fetch report is persisted. A subsequent
        # invocation replays A (zero HTTP) and its final accumulated drop
        # count STILL includes A's exclusion.
        def leaf_a_body():
            return _payload([
                _feed_item(title="Apple beats synthetic earnings estimates"),
                _feed_item(title="_", time_published="20190102T160000"),
            ])

        # Run 1: leaf A (December 2018) succeeds; the 2019 window fails.
        leaf_a = (dt.date(2018, 12, 15), dt.date(2018, 12, 31))
        feb = (dt.date(2019, 1, 1), dt.date(2019, 1, 31))
        pages = {"alphavantage.co": [(200, leaf_a_body()), (500, "{}")]}
        http = fake_transport(pages)
        with pytest.raises(fetch_alphavantage.IngestionError):
            fetch_alphavantage.fetch_news_inventory(
                ticker="AAPL", start=leaf_a[0], end=feb[1], http_get=http,
                sleep_fn=lambda _s: None, checkpoint_dir=str(tmp_path))
        # Leaf A's checkpoint exists; no report survived (nothing was
        # returned from the failed run at all).
        assert len(list(tmp_path.glob("*.json"))) == 1

        # Run 2: resume leaf A's window only — zero HTTP, and its final
        # accumulated drop count STILL includes A's 1 exclusion.
        replay_http = fake_transport({"alphavantage.co": [(500, "{}")]})
        replay_log = FetchLog()
        replay_rows, replay_drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=leaf_a[0], end=leaf_a[1],
            http_get=replay_http, sleep_fn=lambda _s: None,
            fetch_log=replay_log, checkpoint_dir=str(tmp_path))
        assert replay_drops == 1
        assert replay_log.total_drops == 1
        assert len(replay_rows) == 1

    def test_multiple_dropped_items_count_survives_replay(
            self, av_creds, tmp_path):
        body = _payload([
            _feed_item(title="_"),
            _feed_item(title="?!", time_published="20190102T160000"),
            _feed_item(title="«»—", time_published="20190103T160000"),
            _feed_item(title="Apple valid synthetic headline"),
        ])
        http = fake_transport({"alphavantage.co": [(200, body)]})
        rows, drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        assert drops == 3 and len(rows) == 1
        doc = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert doc["drops"] == {"DROP_INVALID_FP4_EMPTY": 3}
        replay_http = fake_transport({"alphavantage.co": [(500, "{}")]})
        replay_rows, replay_drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=replay_http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        assert replay_drops == 3
        assert replay_rows == rows

    def test_legacy_checkpoint_without_drop_metadata_replays_zero(
            self, av_creds, tmp_path):
        # A legacy checkpoint carries no "drops" field; it must read
        # successfully and replay with DROP_INVALID_FP4_EMPTY = 0.
        from backtest.data.fetch_alphavantage import (
            _checkpoint_path, _save_checkpoint)
        valid_rows = [{
            "headline_hash": "a" * 64,
            "source": "Synthetic Wire",
            "ticker": "AAPL",
            "published_at": "2019-01-02T15:30:00+00:00",
            "headline_text_normalized": "apple beats synthetic estimates",
            "fetched_at": "2019-02-01T00:00:00+00:00",
        }]
        a = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
        b = dt.datetime(2019, 1, 30, 23, 59, tzinfo=dt.timezone.utc)
        _save_checkpoint(
            _checkpoint_path("AAPL", a, b, tmp_path),
            ticker="AAPL", a=a, b=b, rows=valid_rows)
        # Strip the drop metadata to emulate a legacy artifact.
        path = next(tmp_path.glob("*.json"))
        doc = json.loads(path.read_text())
        assert "drops" in doc
        del doc["drops"]
        path.write_text(json.dumps(doc, sort_keys=True))
        # Replay: zero HTTP, rows intact, drops=0.
        replay_http = fake_transport({"alphavantage.co": [(500, "{}")]})
        rows, drops = fetch_alphavantage.fetch_news_inventory(
            ticker="AAPL", start=dt.date(2019, 1, 1), end=dt.date(2019, 1, 30),
            http_get=replay_http, sleep_fn=lambda _s: None,
            checkpoint_dir=str(tmp_path))
        assert rows == valid_rows
        assert drops == 0


# ---------------------------------------------------------------------------
# Atomic publication (temporary SQLite only)
# ---------------------------------------------------------------------------


def _headlines_row(h: str, *, published="2019-01-02T15:30:00+00:00"):
    return {
        "headline_hash": h,
        "source": "Synthetic Wire",
        "ticker": "AAPL",
        "published_at": published,
        "headline_text_normalized": f"synthetic headline {h[:8]}",
        "fetched_at": "2019-02-01T00:00:00+00:00",
    }


def _manifest_row(*, verified=True):
    return {
        "source_kind": "NEWS",
        "ticker": "AAPL",
        "span_start": "2019-01-01T00:00:00+00:00",
        "span_end": "2019-01-31T23:59:59+00:00",
        "verified": verified,
        "manifest_version": "alphavantage-news-1",
    }


@pytest.fixture
def store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "atomic-test.sqlite3"))
    from backtest.db.schema import init_db
    init_db(conn)
    yield IngestStore(conn, run_id="test-run")
    conn.close()


class TestAtomicPublication:
    def test_success_persists_both(self, store):
        h, m = store.publish_alphavantage_news(
            headlines=[_headlines_row("a" * 64)],
            manifest=[_manifest_row()])
        assert (h, m) == (1, 1)
        assert store.headline_count("AAPL") == 1
        assert store._conn.execute(
            "SELECT COUNT(*) FROM coverage_manifests").fetchone()[0] == 1

    def test_repeated_success_idempotent(self, store):
        rows = [_headlines_row("a" * 64)]
        man = [_manifest_row()]
        assert store.publish_alphavantage_news(
            headlines=rows, manifest=man) == (1, 1)
        assert store.publish_alphavantage_news(
            headlines=rows, manifest=man) == (0, 0)
        assert store.headline_count("AAPL") == 1

    def test_headline_conflict_persists_neither(self, store):
        # Pre-existing headline row with DIFFERENT content.
        store._conn.execute(
            "INSERT INTO news_headlines (headline_hash, source, ticker, "
            "published_at, headline_text_normalized, fetched_at) "
            "VALUES (?, 'Synthetic Wire', 'AAPL', "
            "'2019-01-03T15:30:00+00:00', 'a different text', 'y')",
            ("a" * 64,))
        store._conn.commit()
        with pytest.raises(IngestionError):
            store.publish_alphavantage_news(
                headlines=[_headlines_row("a" * 64)],
                manifest=[_manifest_row()])
        # Manifest NOT written; the conflicting pre-existing row survives.
        assert store._conn.execute(
            "SELECT COUNT(*) FROM coverage_manifests").fetchone()[0] == 0
        assert store._conn.execute(
            "SELECT headline_text_normalized FROM news_headlines"
        ).fetchone()[0] == "a different text"

    def test_manifest_failure_rolls_back_headlines(self, store, monkeypatch):
        # Pre-existing manifest row with a CONFLICTING verified value
        # forces the manifest phase to fail AFTER headlines inserted.
        store._conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, "
            "span_start, span_end, verified, manifest_version) "
            "VALUES ('NEWS', 'AAPL', '2019-01-01T00:00:00+00:00', "
            "'2019-01-31T23:59:59+00:00', 0, 'alphavantage-news-1')")
        store._conn.commit()
        with pytest.raises(IngestionError, match="refusing rewrite"):
            store.publish_alphavantage_news(
                headlines=[_headlines_row("b" * 64)],
                manifest=[_manifest_row(verified=True)])
        # Headlines were ROLLED BACK — no new unattested row remains.
        assert store.headline_count("AAPL") == 0

    def test_preexisting_rows_survive_rollback(self, store):
        # A committed headline from ANOTHER publication unit is untouched
        # by a later failed unit.
        store.publish_alphavantage_news(
            headlines=[_headlines_row("c" * 64)], manifest=[_manifest_row()])
        # Pre-register an MSFT manifest with verified=0; a later unit
        # writing verified=True for MSFT conflicts and fails.
        store._conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, "
            "span_start, span_end, verified, manifest_version) "
            "VALUES ('NEWS', 'MSFT', '2019-01-01T00:00:00+00:00', "
            "'2019-01-31T23:59:59+00:00', 0, 'alphavantage-news-1')")
        store._conn.commit()
        conflicting = [dict(_manifest_row(), ticker="MSFT")]
        with pytest.raises(IngestionError, match="refusing rewrite"):
            store.publish_alphavantage_news(
                headlines=[_headlines_row("e" * 64)],
                manifest=conflicting)
        # Pre-existing AAPL unit fully intact; failed unit's headline gone.
        assert store.headline_count("AAPL") == 1
        assert store.headline_count() == 1

    def test_failed_publication_leaves_no_unattested_rows(self, store):
        # Simulate a crash-style failure inside the manifest phase via a
        # monkeypatched internal error: neither table gains rows.
        def boom(rows):
            raise RuntimeError("simulated storage failure")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(store, "_write_manifest_atomic", boom)
        with pytest.raises(RuntimeError):
            store.publish_alphavantage_news(
                headlines=[_headlines_row("f" * 64)],
                manifest=[_manifest_row()])
        monkeypatch.undo()
        assert store.headline_count() == 0
        assert store._conn.execute(
            "SELECT COUNT(*) FROM coverage_manifests").fetchone()[0] == 0
        # Connection is clean: a subsequent publication succeeds.
        assert store.publish_alphavantage_news(
            headlines=[_headlines_row("f" * 64)],
            manifest=[_manifest_row()]) == (1, 1)

    def test_empty_publication_unit_is_valid_zero(self, store):
        # A verified-zero sweep (no headlines) still attests the span.
        h, m = store.publish_alphavantage_news(
            headlines=[], manifest=[_manifest_row()])
        assert (h, m) == (0, 1)
        assert store._conn.execute(
            "SELECT verified FROM coverage_manifests").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# FetchRecord / FetchLog plumbing sanity (dataclass contract)
# ---------------------------------------------------------------------------


class TestFetchRecordDropsField:
    def test_drops_defaults_to_zero(self):
        r = FetchRecord(provider="p", endpoint="e", params={}, fetched_at="t")
        assert r.drops == 0
        assert FetchLog().total_drops == 0

    def test_total_drops_sums_records(self):
        log = FetchLog()
        log.add(FetchRecord(provider="p", endpoint="e", params={},
                            fetched_at="t", items=5, drops=2))
        log.add(FetchRecord(provider="p", endpoint="e", params={},
                            fetched_at="t", items=3, drops=0))
        log.add(FetchRecord(provider="p", endpoint="e", params={},
                            fetched_at="t", items=4, drops=1))
        assert log.total_items == 12
        assert log.total_drops == 3
