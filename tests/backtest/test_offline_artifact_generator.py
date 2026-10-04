"""R2.8.1 Phase-2 — offline artifact GENERATOR tests (bounded retry +
deterministic sharded resume).

Hermetic: NO network, NO credentials, NO LLM. A fake client stands in
for :class:`~backtest.news.classifier.NewsClassifierClient` and counts
every invocation, so the retry bound and the resume guarantee are
asserted on REAL call counts rather than on a report field.

The generator is the ONLY layer with a retry: the in-tree offline
publisher (``cache_populate_offline``) is asserted to stay
deterministic/non-retrying (no classifier reference, no retry loop).
"""

import datetime as dt
import json

import pytest

from backtest.news.cache import MalformedClassificationError
from backtest.news.offline_artifact_generator import (
    DEFAULT_MAX_ATTEMPTS,
    GENERATOR_SHARD_CHECKPOINT_VERSION,
    GenerationIdentity,
    TransientProviderError,
    classify_with_bounded_retry,
    compose_offline_artifact,
    deterministic_shards,
    generate_offline_artifact,
    is_transient_provider_error,
    load_completed_shards,
    shard_for_identity,
)

PIN = "openrouter/testorg/test-model@v1"
CFG = "cfg-1"
SCHEMA = "news_schema_v3"
T0 = dt.datetime(2024, 1, 5, 12, 0, tzinfo=dt.timezone.utc)


class _Response:
    def __init__(self, text):
        self._text = text
        self.choices = [type("M", (), {"message": type(
            "C", (), {"content": text})()})()]


class FakeClient:
    """Counts every classifier invocation. ``script`` is consumed one
    entry per attempt: an entry may be an exception instance (raised),
    a dict (a valid canonical payload), or a raw string (model output)."""

    def __init__(self, script=None, *, default=None, pin=PIN):
        self.model_version = pin
        self.schema_version = SCHEMA
        self.calls = 0
        self.calls_by_identity = {}
        self.script = list(script or [])
        self.default = default

    def classify_with_normalization(self, *, ticker, headline_text, source,
                                    published_at):
        self.calls += 1
        key = (ticker, source)
        self.calls_by_identity[key] = self.calls_by_identity.get(key, 0) + 1
        if self.script:
            nxt = self.script.pop(0)
        else:
            nxt = self.default
        if nxt is None:
            nxt = _valid_raw()
        if isinstance(nxt, BaseException):
            raise nxt
        raw = json.loads(nxt) if isinstance(nxt, str) else nxt
        # Exercise the REAL canonical client path so the payload shape,
        # the §11.1 normalization and the keyword fallback are genuine.
        from backtest.news.normalize import normalize_classification_payload
        payload = {
            "ticker": ticker,
            "category": raw.get("category"),
            "direction": raw.get("direction"),
            "severity": raw.get("severity"),
            "ma_role": raw.get("ma_role"),
            "confidence": raw.get("confidence"),
            "published_at": published_at.isoformat(),
            "headline_hash": _hash(headline_text),
            "source": source,
            "keyword_override": False,
            "schema_version": SCHEMA,
            "model_version": self.model_version,
        }
        norm = normalize_classification_payload(payload)
        return None, norm.canonical, norm


class _RecordingClient(FakeClient):
    """Records the exact kwargs of every attempt so retry identity can be
    asserted."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen = []

    def classify_with_normalization(self, **kw):
        self.seen.append(tuple(sorted(kw.items())))
        return super().classify_with_normalization(**kw)


def _hash(text):
    from trading_core.news_effects import headline_hash
    return headline_hash(text)


def _valid_raw(**overrides):
    raw = {"category": "EARNINGS", "direction": "BULLISH",
           "severity": "MEDIUM", "ma_role": "NEITHER", "confidence": 0.9}
    raw.update(overrides)
    return raw


def _identity(ticker="AAPL", source="finnhub", text=None, when=None):
    text = text or f"Synthetic headline for {ticker} item"
    when = when or T0
    return GenerationIdentity(
        ticker=ticker, source=source, headline_hash=_hash(text),
        published_at=when.isoformat(), headline_text_normalized=text)


def _ids(n, tickers=("AAPL", "MSFT")):
    """n DISTINCT identities spread round-robin across the tickers —
    every (headline_hash, source, ticker) is unique."""
    out = []
    for i in range(n):
        ticker = tickers[i % len(tickers)]
        out.append(_identity(ticker, text=f"Headline number {i:03d} "
                                         f"for {ticker}"))
    return out


# ---------------------------------------------------------------------------
# transient vs deterministic classification
# ---------------------------------------------------------------------------

def test_transient_classification_distinguishes_deterministic_failures():
    assert is_transient_provider_error(TimeoutError("timed out"))
    assert is_transient_provider_error(RuntimeError("429 rate limit exceeded"))
    assert is_transient_provider_error(
        TransientProviderError("provider unavailable"))
    # deterministic validation failures are NEVER transient
    assert not is_transient_provider_error(
        MalformedClassificationError("ma_role must be NEITHER"))
    assert not is_transient_provider_error(ValueError("bad json"))
    assert not is_transient_provider_error(TypeError("boom"))


# ---------------------------------------------------------------------------
# bounded retry
# ---------------------------------------------------------------------------

def test_transient_failure_is_retried_up_to_the_bound():
    client = FakeClient(script=[RuntimeError("503 service unavailable")] * 2)
    identity = _identity()
    payload, attempts, _norm = classify_with_bounded_retry(client, identity)
    assert client.calls == 3          # two transient, third succeeds
    assert attempts == 3
    assert payload["ticker"] == "AAPL"


def test_transient_failure_exhausting_the_bound_fails_closed():
    client = FakeClient(script=[RuntimeError("rate limit")] * 10)
    identity = _identity()
    with pytest.raises(TransientProviderError) as exc:
        classify_with_bounded_retry(client, identity,
                                    max_attempts=DEFAULT_MAX_ATTEMPTS)
    assert client.calls == DEFAULT_MAX_ATTEMPTS  # exactly 5, never more
    assert DEFAULT_MAX_ATTEMPTS == 5
    assert "5 identical attempts" in str(exc.value)


def test_deterministic_validation_failure_is_not_retried():
    client = FakeClient(script=[MalformedClassificationError(
        "ma_role must be NEITHER for category 'EARNINGS'")] * 10)
    with pytest.raises(MalformedClassificationError):
        classify_with_bounded_retry(client, _identity())
    assert client.calls == 1  # short-circuits; no repeat paid call


def test_malformed_model_output_fails_closed_after_bounded_attempts():
    # A model that keeps emitting an unparseable body is a deterministic
    # failure: it fails closed on the FIRST attempt, never five times.
    client = FakeClient(script=["not json at all"] * 5)
    from backtest.news.classifier import NewsClassifierClient
    real = NewsClassifierClient(PIN, llm_call=lambda **kw: _Response(
        "not json at all"))
    with pytest.raises(Exception) as exc:
        classify_with_bounded_retry(real, _identity())
    assert isinstance(exc.value, MalformedClassificationError)
    assert client.calls == 0


def test_attempts_are_identical_across_retries():
    """Every retry must carry the SAME ticker, headline, source and
    timestamp — nothing mutated between attempts."""
    client = _RecordingClient(script=[TimeoutError("x"), TimeoutError("x")])
    when = dt.datetime(2024, 3, 4, 5, 6, tzinfo=dt.timezone.utc)
    identity = _identity(ticker="NVDA", source="wsj",
                         text="One fixed headline", when=when)
    classify_with_bounded_retry(client, identity)
    assert len(client.seen) == 3
    assert all(s == client.seen[0] for s in client.seen)
    assert dict(client.seen[0])["ticker"] == "NVDA"
    assert dict(client.seen[0])["headline_text"] == "One fixed headline"
    assert dict(client.seen[0])["source"] == "wsj"
    assert dict(client.seen[0])["published_at"] == when


def test_max_attempts_below_one_is_refused():
    with pytest.raises(ValueError):
        classify_with_bounded_retry(FakeClient(), _identity(),
                                    max_attempts=0)


# ---------------------------------------------------------------------------
# deterministic sharding
# ---------------------------------------------------------------------------

def test_shards_are_deterministic_and_cover_every_identity():
    ids = _ids(40)
    a = deterministic_shards(ids, 4)
    b = deterministic_shards(list(reversed(ids)), 4)
    assert [i.sort_key() for s in a for i in s] == \
           [i.sort_key() for s in b for i in s]
    assert sum(len(s) for s in a) == len(ids)
    assert {i.sort_key() for s in a for i in s} == \
           {i.sort_key() for i in ids}
    # shard_count > identities collapses to one row per identity, no crash
    assert sum(len(s) for s in deterministic_shards(ids, 1000)) == len(ids)
    assert deterministic_shards([], 4) == []


def test_shard_membership_is_reproducible_by_a_single_identity():
    ids = _ids(20)
    for identity in ids:
        idx = shard_for_identity(ids, identity, 4)
        shard = deterministic_shards(ids, 4)[idx]
        assert identity.sort_key() in {i.sort_key() for i in shard}
    with pytest.raises(KeyError):
        shard_for_identity(ids, _identity(ticker="ZZZZ"), 4)


def test_shard_count_must_be_positive():
    with pytest.raises(ValueError):
        deterministic_shards(_ids(4), 0)


# ---------------------------------------------------------------------------
# checkpoint + resume
# ---------------------------------------------------------------------------

def test_completed_shards_checkpoint_and_resume_without_recalls(tmp_path):
    ids = _ids(12)
    ckpt = tmp_path / "ckpt"
    client = FakeClient()
    report, rows = generate_offline_artifact(
        identities=ids, client=client, checkpoint_dir=ckpt,
        llm_config_version=CFG, shard_count=3)
    assert report.provider_calls == 12
    assert report.error_count == 0
    assert report.row_count == 12
    assert len(rows) == 12
    assert sorted(report.generated_shards) == [0, 1, 2]
    assert report.resumed_shards == []

    completed = load_completed_shards(ckpt)
    assert sorted(completed) == [0, 1, 2]
    assert completed[0]["checkpoint_version"] == \
        GENERATOR_SHARD_CHECKPOINT_VERSION

    # RESTART with a brand-new client: a resumed shard makes ZERO calls.
    client2 = FakeClient()
    report2, rows2 = generate_offline_artifact(
        identities=ids, client=client2, checkpoint_dir=ckpt,
        llm_config_version=CFG, shard_count=3)
    assert client2.calls == 0
    assert report2.provider_calls == 0
    assert report2.resumed_shards == [0, 1, 2]
    assert report2.resumed_identity_count == 12
    assert len(rows2) == 12
    # the composition is byte-identical across runs
    assert compose_offline_artifact(rows, llm_config_version=CFG) == \
           compose_offline_artifact(rows2, llm_config_version=CFG)


def test_partial_resume_only_recalls_the_incomplete_shard(tmp_path):
    ids = _ids(12)
    ckpt = tmp_path / "ckpt"
    first = FakeClient()
    generate_offline_artifact(identities=ids, client=first,
                              checkpoint_dir=ckpt, llm_config_version=CFG,
                              shard_count=3)
    # Simulate a crash after shard 0: remove shards 1 and 2.
    for idx in (1, 2):
        (ckpt / f"shard-{idx:05d}.json").unlink()
    second = FakeClient()
    report, rows = generate_offline_artifact(
        identities=ids, client=second, checkpoint_dir=ckpt,
        llm_config_version=CFG, shard_count=3)
    assert report.resumed_shards == [0]
    assert report.provider_calls == 8   # exactly the 2 missing shards
    assert len(rows) == 12
    assert sum(1 for _ in rows) == 12


def test_errored_shard_is_never_checkpointed(tmp_path):
    ids = _ids(6)
    ckpt = tmp_path / "ckpt"
    client = FakeClient(script=[_valid_raw(), MalformedClassificationError(
        "ma_role must be NEITHER for category 'EARNINGS'")] * 12)
    report, rows = generate_offline_artifact(
        identities=ids, client=client, checkpoint_dir=ckpt,
        llm_config_version=CFG, shard_count=2)
    assert report.error_count >= 1
    assert report.row_count < len(ids)
    done = load_completed_shards(ckpt)
    # at most the error-free shard was checkpointed; the failed one is absent
    assert len(done) < 2
    for doc in done.values():
        assert len(doc["rows"]) == len(doc["identities"])


def test_checkpoint_is_invalidated_by_a_changed_pin(tmp_path):
    ids = _ids(4)
    ckpt = tmp_path / "ckpt"
    generate_offline_artifact(identities=ids, client=FakeClient(),
                              checkpoint_dir=ckpt, llm_config_version=CFG,
                              shard_count=2)
    other = FakeClient(pin="openrouter/otherorg/other-model@v2")
    report, _rows = generate_offline_artifact(
        identities=ids, client=other, checkpoint_dir=ckpt,
        llm_config_version=CFG, shard_count=2)
    assert report.resumed_shards == []
    assert report.provider_calls == 4  # pin changed => regenerate


def test_truncated_checkpoint_is_regenerated_not_trusted(tmp_path):
    ids = _ids(4)
    ckpt = tmp_path / "ckpt"
    generate_offline_artifact(identities=ids, client=FakeClient(),
                              checkpoint_dir=ckpt, llm_config_version=CFG,
                              shard_count=2)
    path = ckpt / "shard-00000.json"
    doc = json.loads(path.read_text())
    doc["rows"] = doc["rows"][:1]           # truncated: 2 identities, 1 row
    path.write_text(json.dumps(doc))
    assert 0 not in load_completed_shards(ckpt)
    client = FakeClient()
    report, rows = generate_offline_artifact(
        identities=ids, client=client, checkpoint_dir=ckpt,
        llm_config_version=CFG, shard_count=2)
    assert 0 in report.generated_shards
    assert len(rows) == 4


def test_missing_checkpoint_dir_loads_nothing(tmp_path):
    assert load_completed_shards(tmp_path / "absent") == {}
    assert load_completed_shards(None) == {}


# ---------------------------------------------------------------------------
# artifact composition is deterministic and consumable
# ---------------------------------------------------------------------------

def test_compose_is_order_independent_and_clock_free():
    ids = _ids(6)
    client = FakeClient()
    _r, rows = generate_offline_artifact(
        identities=ids, client=client, checkpoint_dir=None,
        llm_config_version=CFG, shard_count=2)
    a = compose_offline_artifact(rows, llm_config_version=CFG)
    b = compose_offline_artifact(list(reversed(rows)), llm_config_version=CFG)
    assert a == b
    doc = json.loads(a)
    assert doc["format_version"] == "r281-offline-classification-1"
    assert doc["llm_config_version"] == CFG
    assert len(doc["results"]) == 6
    assert "generated_at" not in doc
    keys = [(r["classification"]["headline_hash"], r["source"], r["ticker"])
            for r in doc["results"]]
    assert keys == sorted(keys)


def test_generated_artifact_is_accepted_by_the_real_offline_publisher(tmp_path):
    """End-to-end through the UNMODIFIED publisher: the generated
    artifact validates and publishes on a TEMP DB."""
    from backtest.news.cache import HeadlineInventory, open_news_cache
    from backtest.news.cache_populate_offline import (
        populate_cache_from_offline_artifact,
    )
    db = tmp_path / "bt.sqlite3"
    cache = open_news_cache(db)
    ids = _ids(4)
    for identity in ids:
        cache._conn.execute(
            "INSERT INTO news_headlines (headline_hash, source, ticker, "
            "published_at, headline_text_normalized, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (identity.headline_hash, identity.source, identity.ticker,
             identity.published_at, identity.headline_text_normalized,
             "2024-01-01T00:00:00+00:00"))
    for ticker in ("AAPL", "MSFT"):
        cache._conn.execute(
            "INSERT INTO coverage_manifests (source_kind, ticker, "
            "span_start, span_end, verified, manifest_version) VALUES "
            "('NEWS',?,?,?,?,?)",
            (ticker, dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
             .isoformat(),
             dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc).isoformat(),
             1, "mv-1"))
    cache._conn.commit()

    _report, rows = generate_offline_artifact(
        identities=ids, client=FakeClient(), checkpoint_dir=None,
        llm_config_version=CFG, shard_count=2)
    artifact = tmp_path / "artifact.json"
    artifact.write_text(compose_offline_artifact(rows, llm_config_version=CFG))

    result = populate_cache_from_offline_artifact(
        str(artifact), cache=cache,
        inventory=HeadlineInventory(cache._conn),
        expected_model_version=PIN, manifest_versions=["mv-1"],
        requested_tickers=["AAPL", "MSFT"], requested_start="2024-01-01",
        requested_end="2024-01-31", llm_config_version=CFG, final=True)
    assert result.complete
    assert result.inserted_row_count == 4
    assert cache.headline_count() == 4


# ---------------------------------------------------------------------------
# the publisher stays deterministic (no retry, no classifier)
# ---------------------------------------------------------------------------

def test_publisher_has_no_retry_and_no_classifier_reference():
    import inspect
    from backtest.news import cache_populate_offline as pub
    src = inspect.getsource(pub)
    for forbidden in ("classify_with_normalization", "max_attempts",
                      "retry", "time.sleep", "NewsClassifierClient"):
        assert forbidden not in src, forbidden
    assert not hasattr(pub, "classify_with_bounded_retry")