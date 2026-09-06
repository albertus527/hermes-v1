"""R2.8.1 Phase-2 — hermetic tests for the offline classifier benchmark
harness (backtest/news/classifier_benchmark.py).

No network, no LLM, no DB. All fixtures are synthetic labeled worksheets
and offline candidate prediction files in temp dirs.
"""

import copy
import csv
import json
from pathlib import Path

import pytest

from backtest.news.classifier_benchmark import (
    BenchmarkInputError,
    load_candidate_files,
    load_candidate_file,
    load_labeled_worksheet,
    run_benchmark,
)


GOOD_LABEL = {"category": "EARNINGS", "direction": "BULLISH",
              "severity": "MEDIUM", "ma_role": "NEITHER"}

ROWS = [
    ("CAL-0001-" + "a" * 64, "a" * 64, "AAPL", "src1",
     "Apple beats earnings expectations", GOOD_LABEL),
    ("CAL-0002-" + "b" * 64, "b" * 64, "MSFT", "src2",
     "Microsoft guidance disappoints", {"category": "GUIDANCE",
                                        "direction": "BEARISH",
                                        "severity": "HIGH",
                                        "ma_role": "NEITHER"}),
    ("CAL-0003-" + "c" * 64, "c" * 64, "NVDA", "src1",
     "Analyst upgrades Nvidia to buy", {"category": "ANALYST",
                                        "direction": "BULLISH",
                                        "severity": "LOW",
                                        "ma_role": "NEITHER"}),
]


def _hash(text: str) -> str:
    from trading_core.news_effects import headline_hash
    return headline_hash(text)


def _write_worksheet(path: Path, rows=ROWS, labels=None) -> Path:
    cols = ("sample_id", "headline_hash", "ticker", "published_at",
            "source", "headline_text", "human_label", "human_notes")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        for i, (sid, hh, ticker, source, text, label) in enumerate(rows):
            lbl = labels[i] if labels else label
            w.writerow([sid, hh, ticker, "2024-01-05T14:30:00+00:00",
                        source, text,
                        json.dumps(lbl, sort_keys=True) if lbl else "", ""])
    return path


def _write_candidate(path: Path, candidate_id="cand-a", rows=ROWS,
                     predictions=None, prompt_version="p1") -> Path:
    labels_by_sid = {r[0]: r[5] for r in ROWS}
    with open(path, "w", encoding="utf-8") as fh:
        for i, (sid, hh, ticker) in enumerate(rows):
            pred = (predictions or {}).get(i)
            label = pred if pred is not None else labels_by_sid.get(
                sid, GOOD_LABEL)
            fh.write(json.dumps({
                "candidate_id": candidate_id,
                "prompt_version": prompt_version,
                "sample_id": sid,
                "headline_hash": hh,
                "ticker": ticker,
                "label": label,
            }) + "\n")
    return path


@pytest.fixture
def ws(tmp_path):
    rows = [(sid, _hash(text), ticker, source, text, label)
            for sid, _hh, ticker, source, text, label in ROWS]
    return _write_worksheet(tmp_path / "worksheet.csv", rows=rows)


@pytest.fixture
def cand(tmp_path, ws):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    return _write_candidate(tmp_path / "cand_a.jsonl", rows=rows)


def test_exact_prediction_exact_agreement(ws, cand):
    report = run_benchmark(load_labeled_worksheet(ws),
                           load_candidate_files([cand]))
    assert report.candidates[0]["exact_label_match"] == 3
    assert all(v == 1.0 for v in
               report.candidates[0]["per_field_accuracy"].values())


def test_mismatch_reflected_in_correct_dimension(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    # flip only direction on row 0
    wrong = copy.deepcopy(ROWS[0][5]); wrong["direction"] = "BEARISH"
    c = _write_candidate(tmp_path / "c.jsonl", rows=rows,
                         predictions={0: wrong})
    report = run_benchmark(load_labeled_worksheet(ws),
                           load_candidate_files([c]))
    res = report.candidates[0]
    assert res["per_field_correct"]["direction"] == 2
    for f in ("category", "severity", "ma_role"):
        assert res["per_field_correct"][f] == 3
    assert res["exact_label_match"] == 2


def test_row_ordering_does_not_affect_metrics_or_digests(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    c1 = _write_candidate(tmp_path / "c1.jsonl", rows=rows)
    c2 = _write_candidate(tmp_path / "c2.jsonl", rows=list(reversed(rows)))
    labeled = load_labeled_worksheet(ws)
    r1 = run_benchmark(labeled, load_candidate_files([c1]))
    # reversed worksheet too
    rev_rows = list(reversed([(sid, _hash(text), ticker, source, text, label)
                              for sid, _hh, ticker, source, text, label
                              in ROWS]))
    ws2 = _write_worksheet(tmp_path / "rev.csv", rows=rev_rows)
    r2 = run_benchmark(load_labeled_worksheet(ws2),
                       load_candidate_files([c2]))
    assert r1.to_json() == r2.to_json()


def test_blank_human_label_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker, source, text, label)
            for sid, _hh, ticker, source, text, label in ROWS]
    path = _write_worksheet(tmp_path / "w.csv", rows=rows,
                            labels=[GOOD_LABEL, GOOD_LABEL, None])
    with pytest.raises(BenchmarkInputError, match="blank"):
        load_labeled_worksheet(path)


def test_missing_candidate_prediction_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS][:2]
    c = _write_candidate(tmp_path / "c.jsonl", rows=rows)
    with pytest.raises(BenchmarkInputError, match="partially evaluated"):
        run_benchmark(load_labeled_worksheet(ws), load_candidate_files([c]))


def test_unknown_candidate_identity_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    extra = list(rows) + [("CAL-9999-" + "z" * 64, "z" * 64, "TSLA")]
    c = _write_candidate(tmp_path / "c.jsonl", rows=extra)
    with pytest.raises(BenchmarkInputError, match="unknown sample_id"):
        run_benchmark(load_labeled_worksheet(ws), load_candidate_files([c]))


def test_duplicate_human_identity_fails_closed(tmp_path):
    rows = [(sid, _hash(text), ticker, source, text, label)
            for sid, _hh, ticker, source, text, label in ROWS]
    rows = rows + [rows[0]]
    path = _write_worksheet(tmp_path / "w.csv", rows=rows)
    with pytest.raises(BenchmarkInputError, match="duplicate labeled"):
        load_labeled_worksheet(path)


def test_duplicate_candidate_identity_fails_closed(tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    path = _write_candidate(tmp_path / "c.jsonl", rows=rows)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "candidate_id": "cand-a",
            "sample_id": rows[0][0], "headline_hash": rows[0][1],
            "ticker": rows[0][2], "label": GOOD_LABEL}) + "\n")
    with pytest.raises(BenchmarkInputError, match="duplicate candidate"):
        load_candidate_file(path)


def test_invalid_human_enum_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker, source, text, label)
            for sid, _hh, ticker, source, text, label in ROWS]
    bad = copy.deepcopy(ROWS[0][5]); bad["severity"] = "APOCALYPTIC"
    path = _write_worksheet(tmp_path / "w.csv", rows=rows,
                            labels=[bad, None, None])
    with pytest.raises(BenchmarkInputError, match="human label invalid"):
        load_labeled_worksheet(path)


def test_invalid_candidate_enum_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    bad = copy.deepcopy(ROWS[0][5]); bad["direction"] = "SIDEWAYS"
    c = _write_candidate(tmp_path / "c.jsonl", rows=rows,
                         predictions={0: bad})
    with pytest.raises(BenchmarkInputError, match="prediction invalid"):
        load_candidate_files([c])


def test_wrong_headline_identity_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    rows[1] = (rows[1][0], "d" * 64, rows[1][2])   # swapped hash
    c = _write_candidate(tmp_path / "c.jsonl", rows=rows)
    with pytest.raises(BenchmarkInputError, match="wrong headline"):
        run_benchmark(load_labeled_worksheet(ws), load_candidate_files([c]))


def test_partially_evaluated_candidate_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    c = _write_candidate(tmp_path / "c.jsonl", rows=rows[:-1])
    with pytest.raises(BenchmarkInputError, match="partially evaluated"):
        run_benchmark(load_labeled_worksheet(ws), load_candidate_files([c]))


def test_candidate_metadata_does_not_affect_metrics(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    c1 = _write_candidate(tmp_path / "a.jsonl", candidate_id="zzz-model",
                          rows=rows, prompt_version="v9")
    c2 = _write_candidate(tmp_path / "b.jsonl", candidate_id="aaa-model",
                          rows=rows, prompt_version="")
    labeled = load_labeled_worksheet(ws)
    r = run_benchmark(labeled, load_candidate_files([c1, c2]))
    by_id = {c["candidate_id"]: c for c in r.candidates}
    m1 = {k: v for k, v in by_id["zzz-model"].items()
          if k not in ("candidate_id", "prompt_version")}
    m2 = {k: v for k, v in by_id["aaa-model"].items()
          if k not in ("candidate_id", "prompt_version")}
    assert m1 == m2


def test_no_network_or_llm_import_in_harness():
    import backtest.news.classifier_benchmark as m
    src = Path(m.__file__).read_text()
    assert "auxiliary_client" not in src
    assert "urllib" not in src and "requests" not in src
    assert "httpx" not in src


def test_stable_digest_on_identical_inputs(ws, cand):
    labeled = load_labeled_worksheet(ws)
    r1 = run_benchmark(labeled, load_candidate_files([cand]))
    r2 = run_benchmark(labeled, load_candidate_files([cand]))
    assert r1.labels_digest == r2.labels_digest
    assert r1.candidates_digest == r2.candidates_digest
    assert r1.to_json() == r2.to_json()


def test_source_worksheet_not_mutated(ws, cand):
    before = ws.read_bytes()
    report = run_benchmark(load_labeled_worksheet(ws),
                           load_candidate_files([cand]))
    assert ws.read_bytes() == before
    assert report.labeled_count == 3


def test_multiple_candidates_evaluated_independently(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    wrong = copy.deepcopy(ROWS[0][5]); wrong["severity"] = "LOW"
    c_perfect = _write_candidate(tmp_path / "perfect.jsonl",
                                 candidate_id="perfect", rows=rows)
    c_wrong = _write_candidate(tmp_path / "wrong.jsonl",
                               candidate_id="wrong", rows=rows,
                               predictions={0: wrong})
    r = run_benchmark(load_labeled_worksheet(ws),
                      load_candidate_files([c_perfect, c_wrong]))
    by_id = {c["candidate_id"]: c for c in r.candidates}
    assert by_id["perfect"]["exact_label_match"] == 3
    assert by_id["wrong"]["exact_label_match"] == 2
    assert by_id["wrong"]["per_field_correct"]["severity"] == 2


def test_candidate_file_ordering_does_not_change_result(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    c1 = _write_candidate(tmp_path / "one.jsonl", candidate_id="one",
                          rows=rows)
    c2 = _write_candidate(tmp_path / "two.jsonl", candidate_id="two",
                          rows=rows)
    labeled = load_labeled_worksheet(ws)
    r1 = run_benchmark(labeled, load_candidate_files([c1, c2]))
    r2 = run_benchmark(labeled, load_candidate_files([c2, c1]))
    assert r1.candidates == r2.candidates
    assert r1.candidates_digest == r2.candidates_digest


def test_descriptive_metrics_not_normative_pass(ws, cand):
    r = run_benchmark(load_labeled_worksheet(ws),
                      load_candidate_files([cand]))
    assert r.normative_status == "HUMAN ADJUDICATION REQUIRED"
    text = r.to_json()
    assert "PASS" not in text
    # meets_minimum is False below 200 and is the ONLY normative gate
    assert r.meets_minimum is False


def test_no_production_db_mutation(tmp_path, monkeypatch):
    """The harness never opens the backtest DB or any canonical store —
    run it under a HERMES_HOME pointing at an empty temp dir and assert
    nothing under backtest/ appears and the module never opens sqlite."""
    import sqlite3
    opened = []
    real_connect = sqlite3.connect

    def spy(*a, **kw):
        opened.append(a)
        return real_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    import backtest.news.classifier_benchmark as m
    ws_path = tmp_path / "w.csv"
    rows = [(sid, _hash(text), ticker, source, text, label)
            for sid, _hh, ticker, source, text, label in ROWS]
    _write_worksheet(ws_path, rows=rows)
    cpath = tmp_path / "c.jsonl"
    rows_c = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    _write_candidate(cpath, rows=rows_c)
    run_benchmark(load_labeled_worksheet(ws_path),
                  load_candidate_files([cpath]))
    assert opened == []


def test_fp4_hash_mismatch_in_worksheet_fails_closed(tmp_path):
    rows = [(sid, _hash(text), ticker, source, text, label)
            for sid, _hh, ticker, source, text, label in ROWS]
    rows[2] = (rows[2][0], "f" * 64, rows[2][2], rows[2][3],
               rows[2][4], rows[2][5])
    path = _write_worksheet(tmp_path / "w.csv", rows=rows)
    with pytest.raises(BenchmarkInputError, match="headline_hash mismatch"):
        load_labeled_worksheet(path)


def test_ma_role_conditional_validity_enforced(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    bad = {"category": "EARNINGS", "direction": "BULLISH",
           "severity": "MEDIUM", "ma_role": "TARGET"}
    c = _write_candidate(tmp_path / "c.jsonl", rows=rows,
                         predictions={0: bad})
    with pytest.raises(BenchmarkInputError, match="ma_role must be NEITHER"):
        load_candidate_files([c])


def test_duplicate_candidate_id_across_files_fails_closed(ws, tmp_path):
    rows = [(sid, _hash(text), ticker) for sid, _hh, ticker, _src, text, _lbl in ROWS]
    c1 = _write_candidate(tmp_path / "x.jsonl", candidate_id="same",
                          rows=rows)
    c2 = _write_candidate(tmp_path / "y.jsonl", candidate_id="same",
                          rows=rows)
    with pytest.raises(BenchmarkInputError, match="duplicate candidate_id"):
        load_candidate_files([c1, c2])


def test_worksheet_minimum_normative_gate_only():
    """The only normative quantity is the §11.4 ≥200 minimum; the report
    structure must carry it separately from descriptive metrics."""
    from backtest.news.classifier_benchmark import BenchmarkReport
    r = BenchmarkReport()
    assert r.meets_minimum is False
    assert r.normative_status == "HUMAN ADJUDICATION REQUIRED"
    assert "DESCRIPTIVE METRICS" in r.descriptive_metrics_note


def test_generated_at_does_not_affect_digest(ws, cand):
    labeled = load_labeled_worksheet(ws)
    r1 = run_benchmark(labeled, load_candidate_files([cand]))
    r2 = run_benchmark(labeled, load_candidate_files([cand]))
    r1.generated_at = "2026-01-01T00:00:00+00:00"
    r2.generated_at = "2027-12-31T23:59:59+00:00"
    assert r1.labels_digest == r2.labels_digest
    assert r1.candidates_digest == r2.candidates_digest
