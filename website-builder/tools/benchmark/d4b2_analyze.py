#!/usr/bin/env python3
"""D4b.2 -- analyze candidate experiment results (read-only).

Reads ``results/d4b2_experiment.json`` (and optionally the frozen
``results/d4b1_retrieval_probe.json`` for the baseline) and computes the
mission's required metrics per candidate and per language:

  * empty-pack rate (floor 0.62);
  * recall / precision / F1 of the case-level retrieval_beneficial decision
    (a pack is "non-empty" => the candidate asserts retrieval IS beneficial);
  * false-positive / false-negative retrieval rate;
  * relevant-category micro precision/recall/F1 (from admitted categories);
  * correct-abstention rate (rb=false AND empty) and true-failure count
    (rb=true AND empty) -- the honest separation the mission asks for;
  * p50 / p95 per-query latency (from the live run, recorded separately).

    ./.venv/bin/python tools/benchmark/d4b2_analyze.py --in results/d4b2_experiment.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List

BENCH_DIR = Path(__file__).resolve().parent
FLOOR = 0.62


def _prf(tp: int, fp: int, fn: int):
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = (2 * p * r / (p + r)) if (p + r) else 0.0
    return p, r, f


def case_decision_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    tp = sum(1 for r in rows if (not r["empty_pack_with_floor"]) and r["ground_truth"]["retrieval_beneficial"])
    fp = sum(1 for r in rows if (not r["empty_pack_with_floor"]) and not r["ground_truth"]["retrieval_beneficial"])
    fn = sum(1 for r in rows if r["empty_pack_with_floor"] and r["ground_truth"]["retrieval_beneficial"])
    tn = sum(1 for r in rows if r["empty_pack_with_floor"] and not r["ground_truth"]["retrieval_beneficial"])
    p, rec, f1 = _prf(tp, fp, fn)
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(p, 4), "recall": round(rec, 4), "f1": round(f1, 4),
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else 0.0,
        "false_negative_rate": round(fn / (fn + tp), 4) if (fn + tp) else 0.0,
        "correct_abstention_rate": round(tn / (tn + fn), 4) if (tn + fn) else 0.0,
    }


def category_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Micro P/R/F1 over relevant_categories using admitted item categories."""
    tp = fp = fn = 0
    for r in rows:
        gold = set(r["ground_truth"]["relevant_categories"])
        # Predicted categories = categories of references the candidate ADMITTED
        # (>= floor). We only know scores, not categories, in the experiment file
        # for admitted items; recover from the per-item list if present.
        pred = set()
        for it in r.get("admitted_items", []):
            pred.add(it.get("category"))
        tp += len(gold & pred)
        fp += len(pred - gold)
        fn += len(gold - pred)
    p, rec, f1 = _prf(tp, fp, fn)
    return {"micro_precision": round(p, 4), "micro_recall": round(rec, 4),
            "micro_f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn}


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    empty = sum(1 for r in rows if r["empty_pack_with_floor"])
    tops = [r["top_score"] for r in rows if r["top_score"] is not None]
    out = {
        "n": n,
        "empty_pack": empty,
        "empty_pack_rate": round(empty / n, 4) if n else None,
        "top_score_mean": round(statistics.mean(tops), 4) if tops else None,
        "top_score_min": min(tops) if tops else None,
        "top_score_max": max(tops) if tops else None,
        "case_decision": case_decision_metrics(rows),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(BENCH_DIR / "results" / "d4b2_experiment.json"))
    ap.add_argument("--out", default=str(BENCH_DIR / "results" / "d4b2_analysis.json"))
    args = ap.parse_args()

    data = json.loads(Path(args.inp).read_text())
    report: Dict[str, Any] = {"floor": data.get("floor", FLOOR), "candidates": {}}
    for cand, rows in data["candidates"].items():
        langs = {
            "all": rows,
            "id": [r for r in rows if r["language"] == "id"],
            "en": [r for r in rows if r["language"] == "en"],
        }
        report["candidates"][cand] = {
            "overall": summarize(rows),
            "indonesian": summarize(langs["id"]),
            "english": summarize(langs["en"]),
        }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2))

    print(f"{'cand':5s} {'lang':4s} {'empty':6s} {'rate':6s} {'topmean':7s} {'P':6s} {'R':6s} {'F1':6s} {'FPR':6s} {'FNR':6s}")
    for cand, r in report["candidates"].items():
        for lang in ("all", "id", "en"):
            s = r[lang]
            d = s["case_decision"]
            print(f"{cand:5s} {lang:4s} {s['empty_pack']:>3d}/{s['n']:<2d} {str(s['empty_pack_rate']):6s} "
                  f"{str(s['top_score_mean']):7s} {d['precision']:<6.3f} {d['recall']:<6.3f} {d['f1']:<6.3f} "
                  f"{d['false_positive_rate']:<6.3f} {d['false_negative_rate']:<6.3f}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
