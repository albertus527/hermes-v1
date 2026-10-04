"""R2.8.1 §11.1 — deterministic ``ma_role`` normalization contract tests.

THE STRUCTURAL FAILURE MODE
---------------------------
``NEWS_SCHEMA_V3_JSON_SCHEMA`` enumerates ``ma_role`` independently of
``category``, so a strict ``json_schema`` cannot express "``ma_role`` is
NEITHER unless ``category`` is M&A". That cross-field invariant lived only
in ``validate_classification_payload``, which FAILS CLOSED. Measured on the
99-row safety-challenge benchmark (2026-10-03), every fail-closed candidate
failure across GPT-6 Luna (3), GLM 5.3 Flash (9) and Qwen 3.7 Flash (5) was
exactly that violation, and every one repeated identically across all 5
bounded retries.

These tests pin the contract of the patch:
- the canonical rule (non-M&A + non-NEITHER role -> NEITHER, recorded);
- the safety boundary (nothing else is ever rewritten; M&A is never
  touched; every OTHER contract violation still fails closed);
- raw-output preservation and audit metadata;
- idempotence.

No test performs a live LLM call; the fake classifier is injected.
"""

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from backtest.news.cache import (
    MalformedClassificationError,
    open_news_cache,
)
from backtest.news.cache_populate import populate_news_cache_entries
from backtest.news.classifier import (
    NEWS_SCHEMA_V3_JSON_SCHEMA,
    NewsClassifierClient,
    build_messages,
)
from backtest.news.classifier_benchmark import (
    BenchmarkInputError,
    load_candidate_file,
    load_candidate_files,
    run_benchmark,
)
from backtest.news.normalize import (
    MA_CATEGORY,
    NEITHER_MA_ROLE,
    NORMALIZATION_REASON_NON_MA_MA_ROLE,
    normalize_classification_payload,
)
from trading_core.news_effects import CATEGORIES, MA_ROLES

ET = ZoneInfo("America/New_York")
PUB = dt.datetime(2026, 1, 5, 9, 0, tzinfo=ET)
PIN = "openrouter/anthropic/claude-test-model@2026-01-01"

AUDIT_KEYS = {
    "raw_category", "raw_direction", "raw_severity", "raw_ma_role",
    "canonical_category", "canonical_direction", "canonical_severity",
    "canonical_ma_role", "normalization_applied", "normalization_reason",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class FakeLLM:
    """Injected classifier LLM — canned JSON answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, *, messages):
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]

        class _Msg:
            content = (answer if isinstance(answer, str)
                       else json.dumps(answer))

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


def _payload(**kw):
    base = dict(ticker="T", category="PRODUCT", direction="BULLISH",
                severity="MEDIUM", ma_role="NEITHER", confidence=0.95,
                published_at=PUB.isoformat(), headline_hash="x" * 64,
                source="s", keyword_override=False,
                schema_version="news_schema_v3", model_version=PIN)
    base.update(kw)
    return base


def _classify(answer):
    """Run the CANONICAL client over one canned model answer."""
    return NewsClassifierClient(PIN, llm_call=FakeLLM([answer]))


def _canonical(answer=None, *, ticker="T", headline_text="Some headline",
               source="s", published_at=PUB):
    client = _classify(answer if answer is not None
                       else _answer())
    return client.classify_with_normalization(
        ticker=ticker, headline_text=headline_text, source=source,
        published_at=published_at)


def _inventory(cache, rows):
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
    from backtest.news.cache import HeadlineInventory
    return HeadlineInventory(conn)


# --------------------------------------------------------------------------
# 1-6: the canonical rule itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ACQUIRER", "TARGET"])
def test_non_ma_role_is_forced_to_neither(role):
    """1+2: non-M&A + ACQUIRER/TARGET -> NEITHER."""
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", ma_role=role))
    assert norm.canonical["ma_role"] == NEITHER_MA_ROLE
    assert norm.normalization_applied is True


def test_non_ma_neither_is_unchanged():
    """3: non-M&A + NEITHER -> unchanged, normalization_applied=false."""
    norm = normalize_classification_payload(
        _payload(category="OTHER", ma_role="NEITHER"))
    assert norm.canonical["ma_role"] == "NEITHER"
    assert norm.normalization_applied is False
    assert norm.normalization_reason is None


@pytest.mark.parametrize("role", ["ACQUIRER", "TARGET", "NEITHER"])
def test_ma_rows_are_never_normalized(role):
    """4+5+6+21: M&A + any role -> untouched, no normalization recorded."""
    norm = normalize_classification_payload(
        _payload(category=MA_CATEGORY, ma_role=role))
    assert norm.canonical["ma_role"] == role
    assert norm.normalization_applied is False
    assert norm.normalization_reason is None


def test_normalization_never_infers_ma():
    """A non-NEITHER role does NOT promote the category to M&A."""
    norm = normalize_classification_payload(
        _payload(category="OTHER", ma_role="TARGET"))
    assert norm.canonical["category"] == "OTHER"
    assert norm.canonical["ma_role"] == "NEITHER"


def test_target_acquirer_are_never_swapped():
    """Normalization maps TO NEITHER, never between TARGET and ACQUIRER."""
    for role in ("TARGET", "ACQUIRER"):
        norm = normalize_classification_payload(
            _payload(category="LEGAL", ma_role=role))
        assert norm.canonical["ma_role"] == "NEITHER"
        assert role not in norm.canonical.values()


# --------------------------------------------------------------------------
# 7-10: the safety boundary — only ma_role may change
# --------------------------------------------------------------------------


@pytest.mark.parametrize("category", ["REGULATORY", "EARNINGS", "LEGAL",
                                      "OTHER", "MACRO", "INSIDER"])
def test_category_is_never_modified(category):
    """7: category passes through byte-identically."""
    norm = normalize_classification_payload(
        _payload(category=category, ma_role="ACQUIRER"))
    assert norm.canonical["category"] == category
    assert norm.raw_category == category


@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH", "NEUTRAL"])
def test_direction_is_never_modified(direction):
    """8: direction passes through byte-identically."""
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", direction=direction, ma_role="TARGET"))
    assert norm.canonical["direction"] == direction


@pytest.mark.parametrize("severity", ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
def test_severity_is_never_modified_or_softened(severity):
    """9: severity passes through byte-identically (no softening)."""
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", severity=severity,
                 ma_role="ACQUIRER"))
    assert norm.canonical["severity"] == severity


@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.98, 1.0])
def test_confidence_is_never_modified(confidence):
    """10: confidence passes through byte-identically."""
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", confidence=confidence,
                 ma_role="ACQUIRER"))
    assert norm.canonical["confidence"] == confidence


def test_only_ma_role_differs_between_raw_and_canonical():
    """The canonical payload differs from the raw in exactly ONE key."""
    raw = _payload(category="REGULATORY", direction="BEARISH", severity="HIGH",
                   confidence=0.71, ma_role="ACQUIRER")
    norm = normalize_classification_payload(raw)
    differing = {k for k in raw
                 if raw[k] != norm.canonical.get(k)}
    assert differing == {"ma_role"}


def test_input_mapping_is_not_mutated():
    """Raw model output is preserved by the caller, not by this function."""
    raw = _payload(category="REGULATORY", ma_role="ACQUIRER")
    normalize_classification_payload(raw)
    assert raw["ma_role"] == "ACQUIRER"


# --------------------------------------------------------------------------
# 11-16: every OTHER contract violation still fails closed
# --------------------------------------------------------------------------


def test_malformed_category_still_fails_closed():
    """11: an unknown category is NOT converted."""
    from backtest.news.cache import validate_classification_payload
    norm = normalize_classification_payload(
        _payload(category="NOT_A_CATEGORY", ma_role="ACQUIRER"))
    assert norm.normalization_applied is False
    assert norm.canonical["category"] == "NOT_A_CATEGORY"
    with pytest.raises(MalformedClassificationError):
        validate_classification_payload(norm.canonical)


def test_malformed_direction_still_fails_closed():
    """12: direction is never repaired."""
    from backtest.news.cache import validate_classification_payload
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", direction="VERY_BULLISH",
                 ma_role="ACQUIRER"))
    assert norm.canonical["direction"] == "VERY_BULLISH"
    with pytest.raises(MalformedClassificationError):
        validate_classification_payload(norm.canonical)


def test_malformed_severity_still_fails_closed():
    """13: severity is never softened to a valid enum."""
    from backtest.news.cache import validate_classification_payload
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", severity="CATASTROPHIC",
                 ma_role="ACQUIRER"))
    assert norm.canonical["severity"] == "CATASTROPHIC"
    with pytest.raises(MalformedClassificationError):
        validate_classification_payload(norm.canonical)


def test_malformed_ma_role_enum_still_fails_closed():
    """14: an unknown ma_role is never coerced to NEITHER."""
    from backtest.news.cache import validate_classification_payload
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", ma_role="SELLER"))
    assert norm.normalization_applied is False
    assert norm.canonical["ma_role"] == "SELLER"
    with pytest.raises(MalformedClassificationError):
        validate_classification_payload(norm.canonical)


def test_missing_field_still_fails_closed():
    """15: a missing field is not filled in by normalization."""
    from backtest.news.cache import validate_classification_payload
    broken = _payload(category="REGULATORY", ma_role="ACQUIRER")
    del broken["direction"]
    norm = normalize_classification_payload(broken)
    assert "direction" not in norm.canonical
    with pytest.raises(MalformedClassificationError):
        validate_classification_payload(norm.canonical)


def test_malformed_json_still_fails_closed():
    """16: unparseable model output fails closed in the client."""
    with pytest.raises(MalformedClassificationError):
        _canonical(answer="{not json")


def test_non_object_model_output_still_fails_closed():
    """16b: a JSON array/scalar from the model is rejected."""
    with pytest.raises(MalformedClassificationError):
        _canonical(answer="[1, 2, 3]")


def test_invalid_confidence_still_fails_closed():
    """Invalid confidence is not touched by normalization."""
    from backtest.news.cache import validate_classification_payload
    for bad in (1.5, -0.1, "high"):
        norm = normalize_classification_payload(
            _payload(category="REGULATORY", confidence=bad,
                     ma_role="ACQUIRER"))
        assert norm.canonical["confidence"] == bad
        with pytest.raises(MalformedClassificationError):
            validate_classification_payload(norm.canonical)


# --------------------------------------------------------------------------
# 17: idempotence
# --------------------------------------------------------------------------


def test_normalization_is_idempotent():
    """Applying normalization twice yields the same canonical payload and
    the SECOND application records nothing (no duplicate audit event)."""
    once = normalize_classification_payload(
        _payload(category="REGULATORY", ma_role="ACQUIRER"))
    twice = normalize_classification_payload(once.canonical)
    assert once.canonical == twice.canonical
    assert once.normalization_applied is True
    assert twice.normalization_applied is False
    assert twice.normalization_reason is None


def test_idempotent_over_a_canonical_row():
    """The documented idempotent example: OTHER/NEUTRAL/LOW/NEITHER."""
    norm = normalize_classification_payload(
        _payload(category="OTHER", direction="NEUTRAL", severity="LOW",
                 ma_role="NEITHER"))
    again = normalize_classification_payload(norm.canonical)
    assert norm.canonical == again.canonical
    assert again.normalization_applied is False


def test_client_is_idempotent_end_to_end():
    """The canonical client's own payload re-normalizes to itself."""
    _classification, payload, _norm = _canonical(
        answer=_answer(category="REGULATORY", ma_role="ACQUIRER"))
    again = normalize_classification_payload(payload)
    assert again.canonical == payload
    assert again.normalization_applied is False


def test_normalization_applied_once_per_client_call():
    """A normalized row produces exactly ONE audit event from one call."""
    _classification, _payload_out, norm = _canonical(
        answer=_answer(category="REGULATORY", ma_role="ACQUIRER"))
    assert norm.normalization_applied is True
    assert norm.canonical["ma_role"] == "NEITHER"


# --------------------------------------------------------------------------
# 18-20: raw preservation + audit metadata
# --------------------------------------------------------------------------


def test_raw_ma_role_is_preserved_in_audit_metadata():
    """18: the model's ma_role is preserved verbatim in the audit record."""
    norm = normalize_classification_payload(
        _payload(category="REGULATORY", ma_role="ACQUIRER"))
    audit = norm.audit_record()
    assert audit["raw_ma_role"] == "ACQUIRER"


def test_canonical_ma_role_is_recorded_separately():
    """19: canonical_ma_role is distinct from raw_ma_role."""
    audit = normalize_classification_payload(
        _payload(category="REGULATORY", ma_role="TARGET")).audit_record()
    assert audit["raw_ma_role"] == "TARGET"
    assert audit["canonical_ma_role"] == "NEITHER"


def test_normalization_reason_is_exact_and_stable():
    """20: the reason string is part of the audit contract."""
    audit = normalize_classification_payload(
        _payload(category="REGULATORY", ma_role="ACQUIRER")).audit_record()
    assert audit["normalization_reason"] == "NON_MA_ROLE_FORCED_NEITHER"
    assert NORMALIZATION_REASON_NON_MA_MA_ROLE == "NON_MA_ROLE_FORCED_NEITHER"


def test_audit_record_exposes_both_raw_and_canonical_fields():
    """All ten required provenance fields are present in every audit row."""
    audit = normalize_classification_payload(
        _payload(category="REGULATORY", direction="BEARISH",
                 severity="HIGH", ma_role="ACQUIRER")).audit_record()
    assert set(audit) == AUDIT_KEYS
    assert audit["raw_category"] == "REGULATORY"
    assert audit["raw_direction"] == "BEARISH"
    assert audit["raw_severity"] == "HIGH"
    assert audit["canonical_category"] == "REGULATORY"
    assert audit["canonical_direction"] == "BEARISH"
    assert audit["canonical_severity"] == "HIGH"
    assert json.loads(json.dumps(audit)) == audit   # JSON-serializable


def test_audit_record_present_even_when_nothing_is_normalized():
    """A no-op pass still yields a full audit record (normalization_applied
    false) so every candidate evaluation reports its reliability evidence."""
    audit = normalize_classification_payload(
        _payload(category="OTHER", ma_role="NEITHER")).audit_record()
    assert set(audit) == AUDIT_KEYS
    assert audit["normalization_applied"] is False
    assert audit["normalization_reason"] is None


# --------------------------------------------------------------------------
# Client integration: normalized output is what gets validated + persisted
# --------------------------------------------------------------------------


def test_client_normalizes_non_ma_acquirer_and_continues():
    """A non-M&A row with ACQUIRER now classifies instead of failing."""
    classification, payload, norm = _canonical(
        answer=_answer(category="REGULATORY", direction="BEARISH",
                       severity="HIGH", ma_role="ACQUIRER"))
    assert classification.ma_role == NEITHER_MA_ROLE
    assert classification.category == "REGULATORY"
    assert payload["ma_role"] == NEITHER_MA_ROLE
    assert payload["category"] == "REGULATORY"
    assert norm.raw_ma_role == "ACQUIRER"
    assert norm.normalization_applied is True


def test_client_leaves_ma_rows_untouched():
    """M&A + TARGET survives the client unchanged."""
    classification, payload, norm = _canonical(
        answer=_answer(category="M&A", ma_role="TARGET"))
    assert classification.ma_role == "TARGET"
    assert payload["ma_role"] == "TARGET"
    assert norm.normalization_applied is False


def test_client_still_fails_closed_on_other_violations():
    """Normalization does not weaken any other fail-closed surface."""
    for answer in (_answer(category="NOPE"), _answer(direction="UP"),
                   _answer(severity="EXTREME"), _answer(ma_role="BUYER"),
                   _answer(confidence=7.0)):
        with pytest.raises(MalformedClassificationError):
            _canonical(answer=answer)


def test_classify_delegates_to_the_single_normalization_point():
    """``classify`` and ``classify_with_normalization`` agree — the rule is
    applied in exactly one place, never duplicated per caller."""
    client = _classify(_answer(category="REGULATORY", ma_role="ACQUIRER"))
    cls_two, payload_two = client.classify(
        ticker="T", headline_text="H", source="s", published_at=PUB)
    cls_three, payload_three, norm = client.classify_with_normalization(
        ticker="T", headline_text="H", source="s", published_at=PUB)
    assert (cls_two.ma_role, payload_two["ma_role"]) == (
        cls_three.ma_role, payload_three["ma_role"]) == ("NEITHER", "NEITHER")
    assert norm.normalization_applied is True


# --------------------------------------------------------------------------
# 22: keyword fallback behavior unchanged
# --------------------------------------------------------------------------


def test_keyword_fallback_behavior_unchanged():
    """§11.3 keyword fallback still applies AFTER normalization, exactly as
    before, and its persisted flag is unaffected by the patch."""
    from trading_core.news_effects import classify_with_keyword_fallback
    headline = "Apple beats earnings estimates and raises guidance"
    client = _classify(_answer(category="REGULATORY", ma_role="ACQUIRER"))
    classification, payload, _norm = client.classify_with_normalization(
        ticker="AAPL", headline_text=headline, source="s", published_at=PUB)
    expected = classify_with_keyword_fallback(
        ticker="AAPL", headline_text=headline,
        category=classification.category, direction=classification.direction,
        severity=classification.severity, ma_role=classification.ma_role,
        confidence=classification.confidence,
        published_at=classification.published_at, source="s")
    assert (payload["keyword_override"], payload["direction"],
            payload["severity"]) == (expected.keyword_override,
                                     expected.direction, expected.severity)
    assert classification.ma_role == "NEITHER"


# --------------------------------------------------------------------------
# 23-24: the prompt and the schema are untouched
# --------------------------------------------------------------------------


def test_classifier_prompt_unchanged():
    """23: build_messages still yields the canonical prompt (no extra
    instruction was added, so a candidate's prompt_version stays valid)."""
    messages = build_messages("AAPL", "Apple to acquire Foo")
    assert messages[0]["role"] == "system"
    assert "deterministic financial-news classifier" in messages[0]["content"]
    assert "Output strict JSON only." in messages[0]["content"]
    assert messages[1]["content"] == (
        "Ticker: AAPL\nHeadline: Apple to acquire Foo\n"
        "Classify this headline. Respond with JSON matching the schema.")


def test_news_schema_v3_unchanged():
    """24: the strict JSON schema is byte-for-byte the frozen contract —
    the cross-field rule is NOT smuggled into it."""
    assert NEWS_SCHEMA_V3_JSON_SCHEMA["required"] == [
        "category", "direction", "severity", "ma_role", "confidence"]
    assert NEWS_SCHEMA_V3_JSON_SCHEMA["additionalProperties"] is False
    assert tuple(NEWS_SCHEMA_V3_JSON_SCHEMA["properties"]["category"]["enum"]) \
        == CATEGORIES
    assert tuple(NEWS_SCHEMA_V3_JSON_SCHEMA["properties"]["ma_role"]["enum"]) \
        == MA_ROLES
    assert set(NEWS_SCHEMA_V3_JSON_SCHEMA["properties"]) == {
        "category", "direction", "severity", "ma_role", "confidence"}


def test_normalization_metadata_is_not_in_the_canonical_payload():
    """The §16 cache payload stays the strict news_schema_v3 field set — the
    audit record is provenance, never new payload fields."""
    _classification, payload, norm = _canonical(
        answer=_answer(category="REGULATORY", ma_role="ACQUIRER"))
    assert norm.audit_record().keys() >= AUDIT_KEYS
    for key in AUDIT_KEYS:
        assert key not in payload
    from backtest.news.cache import validate_classification_payload
    validate_classification_payload(payload)   # still strictly valid


# --------------------------------------------------------------------------
# Benchmark harness: 25 — operates on canonical normalized output + audit
# --------------------------------------------------------------------------


def _worksheet(tmp_path, labels):
    import csv
    from trading_core.news_effects import headline_hash
    path = tmp_path / "ws.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sample_id", "headline_hash", "ticker", "source",
                    "headline_text", "human_label", "human_notes"])
        for i, (text, label) in enumerate(labels, start=1):
            sid = f"S-{i:04d}"
            w.writerow([sid, headline_hash(text), "T", "s", text,
                        json.dumps(label, sort_keys=True), ""])
    return path


def _candidate_file(tmp_path, name, labels_by_sid):
    import csv
    from trading_core.news_effects import headline_hash
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as fh:
        for sid, label in labels_by_sid.items():
            fh.write(json.dumps({
                "candidate_id": "C1", "prompt_version": "pv",
                "sample_id": sid,
                "headline_hash": label.pop("_hh"),
                "ticker": "T", "label": label}) + "\n")
    return path


def test_benchmark_operates_on_canonical_normalized_output(tmp_path):
    """25: a candidate whose raw prediction violates §11.1 loads, is scored
    on the CANONICAL label, and its normalization is reported."""
    from trading_core.news_effects import headline_hash
    rows = [("Regulator opens probe", {
        "category": "REGULATORY", "direction": "BEARISH",
        "severity": "HIGH", "ma_role": "NEITHER"}),
            ("Company announces buyback", {
                "category": "CAPITAL_RETURN", "direction": "BULLISH",
        "severity": "LOW", "ma_role": "NEITHER"})]
    ws = _worksheet(tmp_path, rows)
    preds = {}
    for i, (text, _label) in enumerate(rows, start=1):
        sid = f"S-{i:04d}"
        preds[sid] = {"_hh": headline_hash(text),
                      "category": "REGULATORY", "direction": "BEARISH",
                      "severity": "HIGH", "ma_role": "ACQUIRER"}  # RAW invalid
    cand = _candidate_file(tmp_path, "c1.jsonl", preds)
    cf = load_candidate_file(cand)
    # canonical label: ma_role forced to NEITHER, everything else intact
    assert cf.predictions["S-0001"].label == {
        "category": "REGULATORY", "direction": "BEARISH",
        "severity": "HIGH", "ma_role": "NEITHER"}
    audit = cf.predictions["S-0001"].normalization
    assert audit["raw_ma_role"] == "ACQUIRER"
    assert audit["canonical_ma_role"] == "NEITHER"
    assert audit["normalization_applied"] is True
    report = run_benchmark(load_labeled_worksheet_safe(ws), [cf])
    # run_benchmark stores each candidate result as a JSON-ready dict.
    result = report.candidates[0]
    assert result["labeled_count"] == 2
    assert result["normalized_ma_role_count"] == 2
    assert result["raw_invalid_ma_role_count"] == 2
    assert result["normalization_rate"] == pytest.approx(1.0)
    assert result["normalized_sample_ids"] == ["S-0001", "S-0002"]
    assert result["normalization_reason"] == NORMALIZATION_REASON_NON_MA_MA_ROLE
    assert result["per_field_correct"]["ma_role"] == 2


def load_labeled_worksheet_safe(path):
    from backtest.news.classifier_benchmark import load_labeled_worksheet
    return load_labeled_worksheet(path)


def test_benchmark_rejects_other_label_violations(tmp_path):
    """25b: an unknown enum still fails the whole candidate file."""
    from trading_core.news_effects import headline_hash
    text = "Regulator opens probe"
    ws = _worksheet(tmp_path, [(text, {
        "category": "REGULATORY", "direction": "BEARISH",
        "severity": "HIGH", "ma_role": "NEITHER"})])
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({
        "candidate_id": "C1", "sample_id": "S-0001",
        "headline_hash": headline_hash(text), "ticker": "T",
        "label": {"category": "REGULATORY", "direction": "SIDEWAYS",
                  "severity": "HIGH", "ma_role": "ACQUIRER"}}) + "\n",
        encoding="utf-8")
    with pytest.raises(BenchmarkInputError):
        load_candidate_file(path)


def test_benchmark_normalization_counters_are_zero_when_clean(tmp_path):
    """A fully canonical candidate reports no normalization (rate is None,
    never a fabricated 0.0-with-confidence)."""
    from trading_core.news_effects import headline_hash
    text = "Regulator opens probe"
    ws = _worksheet(tmp_path, [(text, {
        "category": "REGULATORY", "direction": "BEARISH",
        "severity": "HIGH", "ma_role": "NEITHER"})])
    path = tmp_path / "clean.jsonl"
    path.write_text(json.dumps({
        "candidate_id": "C1", "sample_id": "S-0001",
        "headline_hash": headline_hash(text), "ticker": "T",
        "label": {"category": "REGULATORY", "direction": "BEARISH",
                  "severity": "HIGH", "ma_role": "NEITHER"}}) + "\n",
        encoding="utf-8")
    report = run_benchmark(load_labeled_worksheet_safe(ws),
                           load_candidate_files([path]))
    result = report.candidates[0]
    assert result["normalized_ma_role_count"] == 0
    assert result["raw_invalid_ma_role_count"] == 0
    assert result["normalization_rate"] is None
    assert result["normalized_sample_ids"] == []


def test_benchmark_human_labels_are_never_normalized(tmp_path):
    """A §11.1-invalid HUMAN label still fails the worksheet — ground truth
    is never repaired by the model-output normalization."""
    from trading_core.news_effects import headline_hash
    import csv
    path = tmp_path / "bad_ws.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sample_id", "headline_hash", "ticker", "source",
                    "headline_text", "human_label", "human_notes"])
        text = "Regulator opens probe"
        w.writerow(["S-0001", headline_hash(text), "T", "s", text,
                    json.dumps({"category": "REGULATORY",
                                "direction": "BEARISH", "severity": "HIGH",
                                "ma_role": "ACQUIRER"}), ""])
    with pytest.raises(BenchmarkInputError):
        load_labeled_worksheet_safe(path)


# --------------------------------------------------------------------------
# Population report: the audit metrics are surfaced, not erased
# --------------------------------------------------------------------------


def test_population_report_records_normalization(tmp_path):
    """The population audit layer reports the normalization count + rate and
    keeps the raw model ma_role — reliability evidence is not erased."""
    cache = open_news_cache(tmp_path / "bt.sqlite3")
    rows = [("T", "s", PUB, "Regulator opens probe into pricing"),
            ("T", "s", PUB, "Firm buys a rival in cash deal")]
    inv = _inventory(cache, rows)
    answers = [
        _answer(category="REGULATORY", ma_role="ACQUIRER"),   # normalized
        _answer(category="M&A", ma_role="ACQUIRER"),           # untouched
    ]
    client = NewsClassifierClient(PIN, llm_call=FakeLLM(answers))
    report = populate_news_cache_entries(
        cache=cache, inventory=inv, manifest_versions=["mv1"],
        pinned_model=PIN, classifier=client, entries=rows)
    assert report.headlines_classified == 2
    assert report.normalized_ma_role_count == 1
    assert report.normalization_rate == pytest.approx(0.5)
    record = report.normalized_ma_role[0]
    assert record["raw_ma_role"] == "ACQUIRER"
    assert record["canonical_ma_role"] == "NEITHER"
    assert record["normalization_reason"] == NORMALIZATION_REASON_NON_MA_MA_ROLE


def test_population_report_clean_run_has_no_normalization(tmp_path):
    cache = open_news_cache(tmp_path / "bt.sqlite3")
    rows = [("T", "s", PUB, "Firm buys a rival in cash deal")]
    inv = _inventory(cache, rows)
    client = NewsClassifierClient(
        PIN, llm_call=FakeLLM([_answer(category="M&A", ma_role="TARGET")]))
    report = populate_news_cache_entries(
        cache=cache, inventory=inv, manifest_versions=["mv1"],
        pinned_model=PIN, classifier=client, entries=rows)
    assert report.normalized_ma_role_count == 0
    assert report.normalization_rate == 0.0


def test_population_report_survives_a_narrow_classifier_stub(tmp_path):
    """An injected stub exposing only ``classify`` keeps working verbatim."""
    class Narrow:
        model_version = PIN
        schema_version = "news_schema_v3"

        def classify(self, *, ticker, headline_text, source, published_at):
            from backtest.news.cache import validate_classification_payload
            from trading_core.news_effects import (
                classify_with_keyword_fallback, headline_hash)
            payload = _payload(ticker=ticker,
                               published_at=published_at.isoformat(),
                               headline_hash=headline_hash(headline_text),
                               source=source, category="M&A",
                               ma_role="TARGET")
            validated = validate_classification_payload(payload)
            final = classify_with_keyword_fallback(
                ticker=validated.ticker, headline_text=headline_text,
                category=validated.category, direction=validated.direction,
                severity=validated.severity, ma_role=validated.ma_role,
                confidence=validated.confidence,
                published_at=validated.published_at, source=validated.source)
            return final, payload

    cache = open_news_cache(tmp_path / "bt.sqlite3")
    rows = [("T", "s", PUB, "Firm buys a rival in cash deal")]
    inv = _inventory(cache, rows)
    report = populate_news_cache_entries(
        cache=cache, inventory=inv, manifest_versions=["mv1"],
        pinned_model=PIN, classifier=Narrow(), entries=rows)
    assert report.headlines_classified == 1
    assert report.normalized_ma_role_count == 0