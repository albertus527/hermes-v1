#!/usr/bin/env python3
"""D4b.1 -- short-lived isolated Laya worker (Mode B lifecycle benchmark).

Loads the official upstream multilingual checkpoint, runs REAL typed-decision
inference for exactly one brief, prints a BOUNDED JSON result on stdout, and then
exits completely so the OS reclaims every byte of the process memory.

This is deliberately NOT a daemon and NOT a pool: the caller spawns exactly one
worker per brief (concurrency = 1) and waits for it to exit. It uses the real
upstream ``laya.Router`` API -- no custom inference engine.

    <laya-venv>/bin/python d4b1_worker.py --brief "..." [--models DIR]

The process must be started with a fresh interpreter so the checkpoint load is a
genuine cold start. Stdout carries ONLY the JSON result; diagnostics go to stderr.
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

UPSTREAM_CHECKPOINT_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", required=True)
    ap.add_argument("--models", default=str(Path.home() / ".website-builder/laya/models"))
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    # Import + construct inside the worker so import cost is part of cold start.
    t_import = time.monotonic()
    import laya  # noqa: F401
    from tools.benchmark.d4b1_benchmark import (  # noqa: E402
        build_candidate_c, candidate_c_laya_questions,
    )
    import_s = time.monotonic() - t_import

    t_load = time.monotonic()
    router = build_candidate_c(args.models, device=args.device)
    questions = candidate_c_laya_questions()
    build_s = time.monotonic() - t_load
    # ``build_candidate_c`` uses ``preload=False``, so the checkpoint load happens
    # lazily INSIDE the first ``predict`` call. The cold-start cost of a
    # short-lived worker is therefore import + (load + first forward pass), and
    # the second term is exactly the first ``predict``'s wall time.
    t_infer = time.monotonic()
    pred = router.predict(args.brief, questions, model="multilingual")
    infer_s = time.monotonic() - t_infer

    ans = pred.get("answers", {})
    bounded = {
        "worker_pid": os.getpid(),
        "import_s": round(import_s, 3),
        "router_build_s": round(build_s, 3),
        # lazy checkpoint load + first forward pass (dominant cold-start term)
        "cold_load_and_first_forward_s": round(infer_s, 3),
        "infer_s": round(infer_s, 3),
        "routing": pred.get("routing"),
        "answers": {
            k: {kk: vv for kk, vv in v.items() if kk != "probabilities"}
            for k, v in ans.items()
        },
        "design_intent_choice": ans.get("design_intent", {}).get("choice"),
        "primary_category_choice": ans.get("primary_category", {}).get("choice"),
    }
    # explicit unload before exit (belt-and-suspenders; process exit is the real release)
    try:
        router.unload()
    except Exception as exc:  # pragma: no cover
        print(f"unload warning: {exc}", file=sys.stderr)
    sys.stdout.write(json.dumps(bounded))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
