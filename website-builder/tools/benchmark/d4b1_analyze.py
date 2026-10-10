#!/usr/bin/env python3
"""D4b.1 -- analyze benchmark results: three-way comparison, CIs, paired tests.

Reads the benchmark result JSON and prints (and can write) a compact comparison
at the shared normalized-label layer, with bootstrap 95% CIs and paired McNemar
tests where two candidates emit the same label. With n=50, differences whose CI
straddles zero are reported as INCONCLUSIVE, never as improvements.

    ./.venv/bin/python tools/benchmark/d4b1_analyze.py --in tools/benchmark/results/d4b1_AB.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.benchmark.d4b1_benchmark import (  # noqa: E402
    bootstrap_ci, mcnemar, load_dataset, CANDIDATE_LABELS,
)


def _correct_vector(cand_preds, cases, label, kind):
    """Per-case correctness (1/0), treating a missing prediction as incorrect."""
    out = []
    for c in cases:
        p = cand_preds.get(c["id"], {})
        gold = c["ground_truth"][label]
        if kind == "set":
            pv = set(p.get(label) or [])
            out.append(1 if pv == set(gold) else 0)
        elif kind == "bool":
            pv = p.get(label)
            out.append(1 if (pv is not None and bool(pv) == bool(gold)) else 0)
        else:
            pv = p.get(label)
            out.append(1 if pv == gold else 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data = load_dataset()
    cases = data["cases"]
    r = json.loads(Path(args.inp).read_text())
    preds = r["predictions"]
    ev = r["evaluation"]

    print(f"# D4b.1 three-way comparison  (n={len(cases)})\n")
    # shared label -> which candidates emit it
    shared = {}
    for cand, labels in CANDIDATE_LABELS.items():
        if cand in preds:
            for lbl in labels:
                shared.setdefault(lbl, []).append(cand)

    report = {"n": len(cases), "labels": {}}
    for label, cands in shared.items():
        print(f"## {label}  (candidates: {', '.join(cands)})")
        vectors = {}
        for cand in cands:
            kind = "set" if label == "relevant_categories" else (
                "bool" if label in ("retrieval_beneficial", "motion_relevant",
                                    "component_relevant", "ambiguous",
                                    "clarification_required") else "cat")
            v = _correct_vector(preds[cand], cases, label, kind)
            lo, hi = bootstrap_ci(v, resamples=2000)
            acc = sum(v) / len(v)
            vectors[cand] = v
            print(f"  {cand}: acc={acc:.3f}  95% CI [{lo:.3f}, {hi:.3f}]")
        # paired tests between every pair
        cands_l = list(cands)
        for i in range(len(cands_l)):
            for j in range(i + 1, len(cands_l)):
                a, b = cands_l[i], cands_l[j]
                mc = mcnemar(vectors[a], vectors[b])
                verdict = ("candidate %s better" % a if sum(vectors[a]) > sum(vectors[b])
                           else "candidate %s better" % b if sum(vectors[b]) > sum(vectors[a])
                           else "tie")
                sig = "SIGNIFICANT" if mc["p_value"] < 0.05 else "INCONCLUSIVE"
                print(f"    McNemar {a} vs {b}: b={mc['b']} c={mc['c']} "
                      f"p={mc['p_value']:.4f} -> {sig} ({verdict})")
        report["labels"][label] = {"accuracy": {c: sum(vectors[c]) / len(vectors[c])
                                                for c in cands}}
        print()

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
