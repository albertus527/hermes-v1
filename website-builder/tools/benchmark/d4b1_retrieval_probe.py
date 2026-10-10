#!/usr/bin/env python3
"""D4b.1 -- Indonesian/English retrieval investigation probe (read-only).

Answers the mission's §8 questions with REAL data, without changing the frozen
corpus, thresholds, or ground truth:

  * the raw relevance distribution returned by the live OpenViking server for the
    SAME planner queries the accepted D4b path issues, with the relevance floor
    DISABLED (``min_score=0.0``), so the rejected references and their scores are
    visible;
  * how many references the operational 0.62 floor admits vs rejects, per brief
    and per language;
  * whether the bottleneck is the *typed decision* layer (Laya) or the
    *embedding/relevance* layer.

It writes ``results/d4b1_retrieval_probe.json``. It performs retrieval only; it
never writes to the corpus and never calls a paid model.

    ./.venv/bin/python tools/benchmark/d4b1_retrieval_probe.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BENCH_DIR = Path(__file__).resolve().parent

from app.core import laya_context as planner  # noqa: E402
from app.core.openviking_retrieval import (  # noqa: E402
    OpenVikingConfig, RetrievalBudget, build_adapter,
)

FLOOR = 0.62


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:1933")
    ap.add_argument("--project-id", default="wb-design")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--out", default=str(BENCH_DIR / "results" / "d4b1_retrieval_probe.json"))
    args = ap.parse_args()

    data = json.loads((BENCH_DIR / "d4b1_dataset.json").read_text())
    cases = data["cases"]
    cfg = planner.LayaConfig(enabled=True, library_project_id=args.project_id, min_score=FLOOR)
    api_key = os.environ.get("OPENVIKING_API_KEY") or None
    adapter = build_adapter(OpenVikingConfig(
        enabled=True, base_url=args.base_url, api_key=api_key, timeout_seconds=args.timeout))

    per_case = []
    for c in cases:
        queries = planner.plan_queries(c["brief"], None, cfg)
        # no floor: see everything the embedding layer returns
        budget = RetrievalBudget(max_items=20, min_score=0.0)
        all_items = []
        for q in queries:
            r = adapter.retrieve_context(query=q, project_id=args.project_id,
                                         scope=cfg.categories, budget=budget)
            if r.status == planner.STATUS_OK:
                for it in r.items:
                    all_items.append({"uri": it.uri, "category": it.category,
                                      "score": round(float(it.score), 4), "query": q})
            else:
                all_items.append({"error": r.status, "query": q})
        scores = sorted((x["score"] for x in all_items if "score" in x), reverse=True)
        admitted = [s for s in scores if s >= FLOOR]
        rejected = [s for s in scores if s < FLOOR]
        per_case.append({
            "id": c["id"], "language": c["language"], "scenario": c["scenario"],
            "n_queries": len(queries), "queries": queries,
            "n_returned": len(scores), "top_score": scores[0] if scores else None,
            "admitted_ge_floor": len(admitted), "rejected_lt_floor": len(rejected),
            "empty_pack_with_floor": len(admitted) == 0,
            "rejected_scores": [round(s, 4) for s in rejected],
            "admitted_scores": [round(s, 4) for s in admitted],
            "items": all_items,
        })
        print(f"[probe] {c['id']:38s} {c['language']} top={per_case[-1]['top_score']} "
              f"adm={len(admitted)} rej={len(rejected)}", flush=True)

    def agg(lang):
        sub = [p for p in per_case if p["language"] == lang]
        n = len(sub)
        empty = sum(1 for p in sub if p["empty_pack_with_floor"])
        tops = [p["top_score"] for p in sub if p["top_score"] is not None]
        return {
            "n": n,
            "empty_pack_with_floor": empty,
            "empty_pack_rate": round(empty / n, 4) if n else None,
            "top_score_mean": round(sum(tops) / len(tops), 4) if tops else None,
            "top_score_min": min(tops) if tops else None,
            "top_score_max": max(tops) if tops else None,
            "top_scores": [round(t, 4) for t in tops],
            "cases_with_no_return": sum(1 for p in sub if p["n_returned"] == 0),
        }

    summary = {
        "floor": FLOOR,
        "base_url": args.base_url,
        "project_id": args.project_id,
        "overall": {
            "n": len(per_case),
            "empty_pack_with_floor": sum(1 for p in per_case if p["empty_pack_with_floor"]),
            "empty_pack_rate": round(sum(1 for p in per_case if p["empty_pack_with_floor"]) / len(per_case), 4),
        },
        "indonesian": agg("id"),
        "english": agg("en"),
        "per_case": per_case,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {args.out}")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_case"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
