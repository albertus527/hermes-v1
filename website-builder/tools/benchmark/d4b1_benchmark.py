#!/usr/bin/env python3
"""D4b.1 -- Upstream Laya benchmark harness (three-way, shared normalized-label layer).

This harness evaluates the SAME frozen 50-brief dataset with up to three candidates:

  A. FAST-only baseline        -- the existing intake interpretation with no
                                  Laya/OpenViking enrichment. The REAL FAST model
                                  is paid; when paid calls are not approved the
                                  harness runs a documented DETERMINISTIC STAND-IN
                                  that mirrors the FAST prompt's documented rules
                                  (scope vocabulary + NAME/WHAT/WHY readiness).
                                  It never sees the ground truth. A stand-in run is
                                  labelled ``standin`` and the real FAST baseline is
                                  reported BLOCKED.
  B. D4b deterministic planner -- the REAL accepted D4b code (app/core/laya_context)
                                  + the REAL D4a OpenViking retrieval adapter
                                  against the live server. No model call.
  C. Real upstream Laya        -- the REAL pinned upstream package
                                  (laya==0.4.1) running the official
                                  convaiinnovations/laya-multilingual checkpoint.
                                  Advisory typed decisions only. Requires the
                                  isolated environment + checkpoint (approval-gated).

Design rules honoured here:
  * identical 50 briefs for every candidate;
  * one shared ground-truth rubric (tools/benchmark/d4b1_dataset.json);
  * decision quality evaluated at a SHARED NORMALIZED-LABEL layer, with per-label
    coverage reported (a candidate that does not emit a label is not scored on it);
  * inference quality, retrieval quality and downstream-FAST quality are SEPARATE
    measurements;
  * no retraining, no tuning on the evaluation set;
  * ground truth is never passed to any candidate.

Usage (free path):
    ./.venv/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B
Usage (with upstream Laya, after approval + setup):
    ~/.website-builder/laya/venv/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B,C
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BENCH_DIR = Path(__file__).resolve().parent
DATASET_PATH = BENCH_DIR / "d4b1_dataset.json"
THRESHOLDS_PATH = BENCH_DIR / "d4b1_thresholds.json"

CATEGORY_VOCAB = ("design_dna", "components", "motion")
DESIGN_INTENTS = (
    "editorial", "corporate", "portfolio", "saas_landing", "botanical",
    "restaurant", "minimalist", "motion_creative", "component_rich", "other",
)
SCOPE_VOCAB = ("in_scope", "revision", "out_of_scope", "adversarial")

# The subset of shared normalized labels each candidate is able to emit.
CANDIDATE_LABELS: Dict[str, Tuple[str, ...]] = {
    "A": ("scope_class", "ambiguous", "clarification_required"),
    "B": ("relevant_categories", "retrieval_beneficial", "motion_relevant", "component_relevant"),
    "C": ("design_intent", "relevant_categories", "retrieval_beneficial",
          "motion_relevant", "component_relevant", "ambiguous"),
}

UPSTREAM_PACKAGE_VERSION = "0.4.1"
UPSTREAM_CHECKPOINT = "convaiinnovations/laya-multilingual"
UPSTREAM_CHECKPOINT_REVISION = "e4e9ddf21a7b1903b7acffd8814ad4307bf63a67"


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def load_dataset() -> Dict[str, Any]:
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    ids = [c["id"] for c in cases]
    assert len(cases) == 50, f"expected 50 cases, found {len(cases)}"
    assert len(set(ids)) == 50, "duplicate case ids"
    langs = [c["language"] for c in cases]
    assert langs.count("en") == 25 and langs.count("id") == 25, "language split must be 25/25"
    return data


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _prf(tp: int, fp: int, fn: int) -> Tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = (2 * p * r / (p + r)) if (p + r) else 0.0
    return p, r, f


def label_metrics(pred: Sequence[Any], gold: Sequence[Any], vocab: Sequence[str]) -> Dict[str, Any]:
    """Accuracy, macro-F1 and per-class P/R/F1 for a single categorical label."""
    n = len(gold)
    correct = sum(1 for p, g in zip(pred, gold) if p == g)
    per_class = {}
    f1s = []
    confusion: Dict[str, Dict[str, int]] = {g: {p: 0 for p in vocab} for g in vocab}
    for p, g in zip(pred, gold):
        gg = g if g in confusion else None
        if gg is not None:
            confusion[gg][p if p in confusion[gg] else list(confusion[gg])[0]] = \
                confusion[gg].get(p, 0) + 1 if p in confusion[gg] else confusion[gg].get(p, 0)
    for c in vocab:
        tp = sum(1 for p, g in zip(pred, gold) if p == c and g == c)
        fp = sum(1 for p, g in zip(pred, gold) if p == c and g != c)
        fn = sum(1 for p, g in zip(pred, gold) if p != c and g == c)
        p_, r_, f_ = _prf(tp, fp, fn)
        per_class[c] = {"precision": p_, "recall": r_, "f1": f_,
                        "support": sum(1 for g in gold if g == c)}
        if per_class[c]["support"] > 0:
            f1s.append(f_)
    macro_f1 = statistics.fmean(f1s) if f1s else 0.0
    return {
        "n": n,
        "accuracy": correct / n if n else 0.0,
        "macro_f1": macro_f1,
        "per_class": per_class,
        "confusion": {g: {p: sum(1 for pp, gg in zip(pred, gold) if gg == g and pp == p)
                          for p in vocab} for g in vocab},
    }


def set_metrics(pred_sets: Sequence[Sequence[str]], gold_sets: Sequence[Sequence[str]],
                vocab: Sequence[str]) -> Dict[str, Any]:
    """Micro precision/recall/F1 for a set-valued label over a fixed vocabulary."""
    tp = fp = fn = 0
    per_class = {}
    for c in vocab:
        ctp = sum(1 for p, g in zip(pred_sets, gold_sets) if c in p and c in g)
        cfp = sum(1 for p, g in zip(pred_sets, gold_sets) if c in p and c not in g)
        cfn = sum(1 for p, g in zip(pred_sets, gold_sets) if c not in p and c in g)
        tp += ctp; fp += cfp; fn += cfn
        p_, r_, f_ = _prf(ctp, cfp, cfn)
        per_class[c] = {"precision": p_, "recall": r_, "f1": f_}
    p_, r_, f_ = _prf(tp, fp, fn)
    exact = sum(1 for p, g in zip(pred_sets, gold_sets) if set(p) == set(g)) / len(gold_sets)
    return {"micro_precision": p_, "micro_recall": r_, "micro_f1": f_,
            "exact_match": exact, "per_class": per_class}


def binary_metrics(pred: Sequence[bool], gold: Sequence[bool]) -> Dict[str, Any]:
    tp = sum(1 for p, g in zip(pred, gold) if p and g)
    fp = sum(1 for p, g in zip(pred, gold) if p and not g)
    fn = sum(1 for p, g in zip(pred, gold) if not p and g)
    tn = sum(1 for p, g in zip(pred, gold) if not p and not g)
    p_, r_, f_ = _prf(tp, fp, fn)
    n = len(gold)
    return {
        "n": n, "accuracy": (tp + tn) / n if n else 0.0,
        "precision": p_, "recall": r_, "f1": f_,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "false_positive_rate": fp / (fp + tn) if (fp + tn) else 0.0,
        "false_negative_rate": fn / (fn + tp) if (fn + tp) else 0.0,
    }


def bootstrap_ci(correct: Sequence[int], resamples: int = 2000, ci: float = 0.95,
                 seed: int = 1234) -> Tuple[float, float]:
    if not correct:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(correct)
    means = []
    for _ in range(resamples):
        s = sum(correct[rng.randrange(n)] for _ in range(n))
        means.append(s / n)
    means.sort()
    lo = means[int((1 - ci) / 2 * resamples)]
    hi = means[int((1 + ci) / 2 * resamples) - 1]
    return (lo, hi)


def mcnemar(correct_a: Sequence[int], correct_b: Sequence[int]) -> Dict[str, Any]:
    """Paired McNemar test (exact binomial) on shared-item correctness."""
    b = sum(1 for a, c in zip(correct_a, correct_b) if a == 1 and c == 0)
    c = sum(1 for a, cc in zip(correct_a, correct_b) if a == 0 and cc == 1)
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "p_value": 1.0, "note": "no discordant pairs"}
    # two-sided exact binomial
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n) * 2
    return {"b": b, "c": c, "p_value": min(1.0, p)}


# ---------------------------------------------------------------------------
# Candidate A -- FAST-only deterministic stand-in (mirrors the FAST prompt rules)
# ---------------------------------------------------------------------------

_OUT_OF_SCOPE_MARKERS = (
    "thesis", "tesis", "dissertation", "disertasi", "email it to my professor",
    "kirim lewat email ke dosen", "write my essay",
)
_ADVERSARIAL_MARKERS = (
    "ignore all previous instructions", "abaikan semua instruksi",
    "disregard your system rules", "abaikan aturan sistem",
    "admin password", "kata sandi admin", "api keys", "kunci api",
    "environment variable", "variabel lingkungan", "unrestricted terminal",
    "terminal tanpa batas", "deploy to production", "deploy ke produksi",
)
_WHAT_NOUNS = (
    "website", "site", "situs", "landing page", "halaman", "magazine", "majalah",
    "portfolio", "portofolio", "store", "toko", "shop", "restaurant", "restoran",
    "cafe", "kafe", "dashboard", "dasbor", "app", "aplikasi", "blog", "studio",
    "agency", "agensi", "firm", "firma", "gallery", "galeri", "nonprofit", "nirlaba",
)
_REVISION_MARKERS = (
    "we already built", "we already launched", "already built", "already launched",
    "sudah kami buat", "sudah kami luncurkan", "on the ", "pada situs", "untuk situs",
    "change the", "ubah ", "add a new page", "tambahkan halaman", "make the headings",
)


def candidate_a_fast_only(brief: str) -> Dict[str, Any]:
    """Deterministic stand-in for FAST-only intake (NO ground-truth access).

    Mirrors the documented FAST prompt contract: scope vocabulary and the
    NAME+WHAT+WHY readiness rule. It is explicitly a STAND-IN for the paid FAST
    model, not a substitute for it.
    """
    t = brief.lower()
    if any(m in t for m in _ADVERSARIAL_MARKERS):
        scope = "adversarial"
    elif any(m in t for m in _OUT_OF_SCOPE_MARKERS):
        scope = "out_of_scope"
    elif any(m in t for m in _REVISION_MARKERS):
        scope = "revision"
    else:
        scope = "in_scope"

    has_what = any(n in t for n in _WHAT_NOUNS)
    # A crude NAME signal: a capitalised token that is not merely sentence-initial.
    tokens = brief.split()
    has_name = any(tok[:1].isupper() and tok[1:2].islower() for tok in tokens[1:])
    has_why = any(w in t for w in ("so that", "so visitors", "to help", "in order to",
                                   "agar", "supaya", "untuk membantu"))
    conflicting = ("minimalis" in t or "minimalist" in t) and (
        "beranimasi" in t or "animated" in t or "widget" in t)
    ambiguous = conflicting or (not has_what) or (not (has_name or has_why))
    clarification = ambiguous and (not has_what or conflicting)
    return {
        "scope_class": scope,
        "ambiguous": bool(ambiguous),
        "clarification_required": bool(clarification),
        "detail": {"has_what": has_what, "has_name": has_name, "has_why": has_why,
                   "conflicting": conflicting},
        "source": "deterministic_standin_fast_only",
    }


# ---------------------------------------------------------------------------
# Candidate B -- REAL D4b deterministic planner + REAL OpenViking retrieval
# ---------------------------------------------------------------------------

def candidate_b_deterministic(brief: str, preparer, project_id: str,
                              planner_mod) -> Dict[str, Any]:
    """Run the REAL accepted D4b planner + REAL D4a retrieval (no model call).

    NOTE on ``relevant_categories``: D4b searches the FULL category allowlist for
    every brief; ``plan_queries``' category hint only shapes the TEXT of the
    targeted queries. The categories that actually reached the context pack are
    therefore the distinct categories of the RETURNED items, ranked by relevance.
    That is the faithful, observable category decision of the real system, so it
    is what we score. The planner's justified hint is recorded separately.
    """
    cfg = preparer.config
    keywords = planner_mod._tokenize(brief)[: planner_mod.MAX_QUERY_KEYWORDS]
    planner_hint = planner_mod._categories_for(keywords, cfg.categories)
    t0 = time.monotonic()
    result = preparer.prepare_context(brief, project_id)
    latency_ms = (time.monotonic() - t0) * 1000.0
    beneficial = (result.status in (planner_mod.STATUS_READY, planner_mod.STATUS_DEGRADED)
                  and len(result.items) > 0)
    top_relevance = max((i.relevance for i in result.items), default=0.0)
    # Distinct categories actually retrieved, ordered by best relevance.
    best: Dict[str, float] = {}
    for i in result.items:
        if i.category in planner_mod.CATEGORIES:
            best[i.category] = max(best.get(i.category, 0.0), i.relevance)
    retrieved_categories = [c for c, _ in sorted(best.items(), key=lambda kv: -kv[1])]
    return {
        "relevant_categories": retrieved_categories,
        "retrieval_beneficial": bool(beneficial),
        "motion_relevant": "motion" in retrieved_categories,
        "component_relevant": "components" in retrieved_categories,
        "detail": {
            "status": result.status,
            "quality": result.quality,
            "items": len(result.items),
            "queries": list(result.queries),
            "planner_justified_categories": list(planner_hint),
            "retrieved_categories": retrieved_categories,
            "retrieval_calls": result.retrieval_calls,
            "estimated_chars": result.estimated_chars,
            "top_relevance": round(top_relevance, 4),
            "warnings": list(result.warnings),
            "error_reason": result.error_reason,
        },
        "latency_ms": latency_ms,
        "source": "d4b_deterministic_planner+live_openviking",
    }


# ---------------------------------------------------------------------------
# Candidate C -- REAL upstream Laya (approval-gated)
# ---------------------------------------------------------------------------

_DESIGN_INTENT_CRITERIA = {
    "editorial": "content-led magazine, journal, blog or news presentation",
    "corporate": "institutional or business presence for credibility and services",
    "portfolio": "an individual's or studio's body of work",
    "saas_landing": "a product marketing page for sign-ups or leads for software",
    "botanical": "florist, plant shop, garden or botanical theme",
    "restaurant": "food service, cafe, dining or menu-led hospitality",
    "minimalist": "restraint, whitespace and reduction are the point",
    "motion_creative": "animation, transitions or motion is the primary creative device",
    "component_rich": "many distinct UI components such as tables, dashboards, pricing or galleries",
    "other": "none of the above",
}


def candidate_c_laya_questions() -> Dict[str, Any]:
    return {
        "design_intent": {
            "type": "choice",
            "instructions": "What is the primary visual/design intent of this website brief?",
            "criteria": _DESIGN_INTENT_CRITERIA,
        },
        "primary_category": {
            "type": "choice",
            "instructions": "Which reference category is most relevant for preparing context?",
            "criteria": {
                "design_dna": "visual style, typography, colour, layout, brand feel",
                "components": "concrete UI building blocks such as cards, nav, forms, tables",
                "motion": "animation and transitions",
                "none": "no design references would be useful",
            },
        },
        "motion_relevant": {
            "type": "noul",
            "instructions": "Does the brief explicitly ask for or strongly imply animation, transitions or motion?",
        },
        "component_relevant": {
            "type": "noul",
            "instructions": "Does the brief imply concrete, enumerable UI components?",
        },
        "retrieval_beneficial": {
            "type": "noul",
            "instructions": "Would reviewed design references materially help prepare context for this brief?",
        },
        "ambiguous": {
            "type": "noul",
            "instructions": "Is the brief materially under-specified or internally contradictory?",
        },
    }


def build_candidate_c(models_dir: Optional[str], device: str = "cpu"):
    """Construct the REAL upstream Router. Raises if the package is unavailable."""
    import laya  # noqa: F401

    if getattr(laya, "__version__", None) != UPSTREAM_PACKAGE_VERSION:
        raise RuntimeError(
            f"upstream laya version mismatch: {getattr(laya, '__version__', None)} "
            f"!= pinned {UPSTREAM_PACKAGE_VERSION}")
    models = None
    if models_dir:
        models = {"multilingual": models_dir}
    router = laya.Router(
        models=models,
        device=device,
        revision=UPSTREAM_CHECKPOINT_REVISION,
        default="multilingual",
        max_loaded=1,
        preload=False,
    )
    return router


def candidate_c_laya_predict(router, brief: str, questions: Dict[str, Any]) -> Dict[str, Any]:
    t0 = time.monotonic()
    res = router.predict(brief, questions, model="multilingual")
    latency_ms = (time.monotonic() - t0) * 1000.0
    ans = res.get("answers", {})
    di = ans.get("design_intent", {})
    pc = ans.get("primary_category", {})
    mo = ans.get("motion_relevant", {})
    co = ans.get("component_relevant", {})
    rb = ans.get("retrieval_beneficial", {})
    am = ans.get("ambiguous", {})

    def _noul(d: Dict[str, Any]) -> Optional[bool]:
        v = d.get("noul")
        return None if v is None else bool(v >= 0.5)

    primary = pc.get("choice")
    cats: List[str] = []
    if primary in CATEGORY_VOCAB:
        cats.append(primary)
    if _noul(mo) and "motion" not in cats:
        cats.append("motion")
    if _noul(co) and "components" not in cats:
        cats.append("components")
    if primary in CATEGORY_VOCAB and "design_dna" not in cats:
        cats.append("design_dna")

    return {
        "design_intent": di.get("choice"),
        "relevant_categories": cats,
        "retrieval_beneficial": _noul(rb),
        "motion_relevant": _noul(mo),
        "component_relevant": _noul(co),
        "ambiguous": _noul(am),
        "confidence": {
            "design_intent": di.get("confidence"),
            "primary_category": pc.get("confidence"),
            "motion_relevant": mo.get("confidence"),
            "component_relevant": co.get("confidence"),
            "retrieval_beneficial": rb.get("confidence"),
            "ambiguous": am.get("confidence"),
        },
        "probabilities": {
            "design_intent": di.get("probabilities"),
            "primary_category": pc.get("probabilities"),
        },
        "routing": res.get("routing"),
        "latency_ms": latency_ms,
        "source": "upstream_laya_multilingual",
    }


# ---------------------------------------------------------------------------
# Resource sampling
# ---------------------------------------------------------------------------

def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def _host_available_mb() -> float:
    try:
        with open("/proc/meminfo") as fh:
            d = {}
            for line in fh:
                k, _, v = line.partition(":")
                d[k] = int(v.split()[0])
            return d.get("MemAvailable", 0) / 1024.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> Dict[str, Any]:
    data = load_dataset()
    cases = data["cases"]
    thresholds = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    candidates = [c.strip().upper() for c in args.candidates.split(",") if c.strip()]

    project_id = args.project_id
    api_key = os.environ.get("OPENVIKING_API_KEY") or None

    # --- Candidate B plumbing (real D4b + real OpenViking) ------------------
    preparer = None
    planner_mod = None
    adapter_info: Dict[str, Any] = {}
    if "B" in candidates:
        from app.core import laya_context as planner_mod_local  # noqa: F401
        from app.core import laya_context as _lm
        from app.core.openviking_retrieval import OpenVikingConfig, build_adapter
        planner_mod = _lm
        ov_cfg = OpenVikingConfig(enabled=True, base_url=args.base_url,
                                  api_key=api_key, timeout_seconds=args.timeout)
        adapter = build_adapter(ov_cfg)
        laya_cfg = planner_mod.LayaConfig(enabled=True, library_project_id=project_id,
                                          min_score=0.62)
        preparer = planner_mod.LayaContextPreparer(laya_cfg, adapter)
        adapter_info = {"enabled": preparer.enabled, "base_url": args.base_url,
                        "project_id": project_id}

    # --- Candidate C plumbing (real upstream Laya) --------------------------
    router = None
    c_questions = None
    c_error = None
    if "C" in candidates:
        try:
            router = build_candidate_c(args.laya_models, device=args.device)
            c_questions = candidate_c_laya_questions()
        except Exception as exc:  # pragma: no cover - env dependent
            c_error = f"{type(exc).__name__}: {exc}"

    results: Dict[str, Any] = {
        "dataset_id": data["dataset_id"],
        "dataset_version": data["version"],
        "n": len(cases),
        "candidates_requested": candidates,
        "upstream": {
            "package": "laya", "package_version": UPSTREAM_PACKAGE_VERSION,
            "checkpoint": UPSTREAM_CHECKPOINT,
            "checkpoint_revision": UPSTREAM_CHECKPOINT_REVISION,
        },
        "candidate_meta": {},
        "predictions": {},
        "resources": {"rss_start_mb": round(_rss_mb(), 1),
                      "host_available_start_mb": round(_host_available_mb(), 1)},
    }

    # ---- Candidate A ----
    if "A" in candidates:
        results["candidate_meta"]["A"] = {
            "kind": "standin", "labels": CANDIDATE_LABELS["A"],
            "note": ("deterministic stand-in for the PAID FAST model; real FAST-only "
                     "baseline is BLOCKED without paid approval"),
        }
        preds = {}
        for c in cases:
            preds[c["id"]] = candidate_a_fast_only(c["brief"])
        results["predictions"]["A"] = preds

    # ---- Candidate B ----
    if "B" in candidates:
        results["candidate_meta"]["B"] = {
            "kind": "real_deterministic", "labels": CANDIDATE_LABELS["B"],
            "adapter": adapter_info,
            "note": "REAL accepted D4b planner + REAL D4a live OpenViking retrieval",
        }
        preds = {}
        rss_samples = []
        for c in cases:
            preds[c["id"]] = candidate_b_deterministic(
                c["brief"], preparer, project_id, planner_mod)
            rss_samples.append(_rss_mb())
        results["predictions"]["B"] = preds
        results["resources"]["B_rss_samples_mb"] = [round(x, 1) for x in rss_samples]

    # ---- Candidate C ----
    if "C" in candidates:
        if c_error is not None:
            results["candidate_meta"]["C"] = {"kind": "upstream_laya", "error": c_error,
                                              "status": "UNAVAILABLE"}
        else:
            results["candidate_meta"]["C"] = {
                "kind": "upstream_laya", "labels": CANDIDATE_LABELS["C"],
                "checkpoint": UPSTREAM_CHECKPOINT,
                "checkpoint_revision": UPSTREAM_CHECKPOINT_REVISION,
                "device": args.device,
            }
            # cold start measured separately
            t_cold = time.monotonic()
            _ = candidate_c_laya_predict(router, cases[0]["brief"], c_questions)
            results["resources"]["C_cold_start_s"] = round(time.monotonic() - t_cold, 3)
            preds = {}
            rss_samples = []
            warm_lat = []
            for i, c in enumerate(cases):
                try:
                    p = candidate_c_laya_predict(router, c["brief"], c_questions)
                    preds[c["id"]] = p
                    if i > 0:
                        warm_lat.append(p["latency_ms"])
                except Exception as exc:  # pragma: no cover
                    preds[c["id"]] = {"error": f"{type(exc).__name__}: {exc}",
                                      "source": "upstream_laya_multilingual"}
                rss_samples.append(_rss_mb())
            results["predictions"]["C"] = preds
            results["resources"]["C_rss_samples_mb"] = [round(x, 1) for x in rss_samples]
            if warm_lat:
                warm_lat_sorted = sorted(warm_lat)
                results["resources"]["C_warm_p50_ms"] = round(
                    statistics.median(warm_lat_sorted), 2)
                results["resources"]["C_warm_p95_ms"] = round(
                    warm_lat_sorted[max(0, int(0.95 * len(warm_lat_sorted)) - 1)], 2)
    else:
        results["candidate_meta"]["C"] = {
            "kind": "upstream_laya", "status": "NOT_RUN",
            "note": "candidate C requires the approval-gated isolated env + checkpoint",
        }

    results["resources"]["rss_end_mb"] = round(_rss_mb(), 1)
    results["resources"]["host_available_end_mb"] = round(_host_available_mb(), 1)

    # ---- Evaluation at the shared normalized-label layer ----
    results["evaluation"] = evaluate(cases, results["predictions"], results["candidate_meta"])

    # ---- Threshold evaluation ----
    results["thresholds"] = thresholds
    results["threshold_eval"] = evaluate_thresholds(results)

    return results


def _gold(case: Dict[str, Any], label: str) -> Any:
    return case["ground_truth"][label]


def evaluate(cases: List[Dict[str, Any]],
             predictions: Dict[str, Dict[str, Any]],
             meta: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for cand, labels in CANDIDATE_LABELS.items():
        if cand not in predictions:
            out[cand] = {"status": "NOT_RUN"}
            continue
        preds = predictions[cand]
        cm: Dict[str, Any] = {"status": "RUN", "labels": list(labels), "per_label": {}}
        for label in labels:
            if label == "relevant_categories":
                ps = [preds[c["id"]].get("relevant_categories") or [] for c in cases]
                gs = [list(_gold(c, "relevant_categories")) for c in cases]
                cm["per_label"][label] = set_metrics(ps, gs, CATEGORY_VOCAB)
                # language split
                cm["per_label"][label]["indonesian"] = set_metrics(
                    [p for p, c in zip(ps, cases) if c["language"] == "id"],
                    [g for g, c in zip(gs, cases) if c["language"] == "id"], CATEGORY_VOCAB)
                cm["per_label"][label]["english"] = set_metrics(
                    [p for p, c in zip(ps, cases) if c["language"] == "en"],
                    [g for g, c in zip(gs, cases) if c["language"] == "en"], CATEGORY_VOCAB)
            elif label in ("retrieval_beneficial", "motion_relevant",
                           "component_relevant", "ambiguous", "clarification_required"):
                pp = [preds[c["id"]].get(label) for c in cases]
                gg = [bool(_gold(c, label)) for c in cases]
                # only score where the candidate emitted a value
                mask = [p is not None for p in pp]
                pp2 = [bool(p) for p, m in zip(pp, mask) if m]
                gg2 = [g for g, m in zip(gg, mask) if m]
                m_all = binary_metrics(pp2, gg2)
                m_all["coverage"] = sum(mask) / len(mask)
                m_all["indonesian"] = binary_metrics(
                    [bool(p) for p, c, m in zip(pp, cases, mask) if m and c["language"] == "id"],
                    [g for g, c, m in zip(gg, cases, mask) if m and c["language"] == "id"])
                m_all["english"] = binary_metrics(
                    [bool(p) for p, c, m in zip(pp, cases, mask) if m and c["language"] == "en"],
                    [g for g, c, m in zip(gg, cases, mask) if m and c["language"] == "en"])
                cm["per_label"][label] = m_all
            else:  # categorical: scope_class, design_intent
                vocab = DESIGN_INTENTS if label == "design_intent" else SCOPE_VOCAB
                pp = [preds[c["id"]].get(label) for c in cases]
                gg = [_gold(c, label) for c in cases]
                mask = [p is not None for p in pp]
                pp2 = [p for p, m in zip(pp, mask) if m]
                gg2 = [g for g, m in zip(gg, mask) if m]
                m_all = label_metrics(pp2, gg2, vocab)
                m_all["coverage"] = sum(mask) / len(mask)
                # correctness vector (all cases; missing => incorrect) for paired tests
                cm["per_label"][label] = m_all
                m_all["indonesian"] = label_metrics(
                    [p for p, c, m in zip(pp, cases, mask) if m and c["language"] == "id"],
                    [g for g, c, m in zip(gg, cases, mask) if m and c["language"] == "id"], vocab)
                m_all["english"] = label_metrics(
                    [p for p, c, m in zip(pp, cases, mask) if m and c["language"] == "en"],
                    [g for g, c, m in zip(gg, cases, mask) if m and c["language"] == "en"], vocab)
        out[cand] = cm
    return out


def evaluate_thresholds(results: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate the PRE-DECLARED thresholds against observed metrics."""
    ev = results["evaluation"]
    verdicts: Dict[str, Any] = {}
    T = {t["id"]: t for t in results["thresholds"]["quality_thresholds"]}
    T.update({t["id"]: t for t in results["thresholds"]["category_thresholds"]})

    def _q(cand, label, key):
        try:
            return ev[cand]["per_label"][label][key]
        except Exception:
            return None

    # design_intent is candidate C only
    di_macro = _q("C", "design_intent", "macro_f1")
    di_acc = _q("C", "design_intent", "accuracy")
    di_id = _q("C", "design_intent", "indonesian")
    di_en = _q("C", "design_intent", "english")
    verdicts["T1"] = {"metric": "design_intent_macro_f1_all50", "observed": di_macro,
                      "threshold": T["T1"]["value"], "status": "BLOCKED" if di_macro is None else
                      ("PASS" if di_macro >= T["T1"]["value"] else "FAIL")}
    verdicts["T1b"] = {"metric": "design_intent_accuracy_all50", "observed": di_acc,
                       "threshold": T["T1b"]["value"], "status": "BLOCKED" if di_acc is None else
                       ("PASS" if di_acc >= T["T1b"]["value"] else "FAIL")}
    verdicts["T2"] = {"metric": "design_intent_macro_f1_indonesian",
                      "observed": (di_id or {}).get("macro_f1"),
                      "threshold": T["T2"]["value"],
                      "status": "BLOCKED" if di_id is None else
                      ("PASS" if di_id["macro_f1"] >= T["T2"]["value"] else "FAIL")}
    verdicts["T3"] = {"metric": "design_intent_macro_f1_english",
                      "observed": (di_en or {}).get("macro_f1"),
                      "threshold": T["T3"]["value"],
                      "status": "BLOCKED" if di_en is None else
                      ("PASS" if di_en["macro_f1"] >= T["T3"]["value"] else "FAIL")}
    cat_micro = _q("C", "relevant_categories", "micro_f1")
    verdicts["T4"] = {"metric": "relevant_categories_micro_f1", "observed": cat_micro,
                      "threshold": T["T4"]["value"], "status": "BLOCKED" if cat_micro is None else
                      ("PASS" if cat_micro >= T["T4"]["value"] else "FAIL")}
    rb = _q("C", "retrieval_beneficial", "accuracy")
    verdicts["T5"] = {"metric": "retrieval_beneficial_accuracy", "observed": rb,
                      "threshold": T["T5"]["value"], "status": "BLOCKED" if rb is None else
                      ("PASS" if rb >= T["T5"]["value"] else "FAIL")}
    mr = _q("C", "motion_relevant", "f1")
    verdicts["T6"] = {"metric": "motion_relevant_f1", "observed": mr,
                      "threshold": T["T6"]["value"], "status": "BLOCKED" if mr is None else
                      ("PASS" if mr >= T["T6"]["value"] else "FAIL")}
    cr = _q("C", "component_relevant", "f1")
    verdicts["T7"] = {"metric": "component_relevant_f1", "observed": cr,
                      "threshold": T["T7"]["value"], "status": "BLOCKED" if cr is None else
                      ("PASS" if cr >= T["T7"]["value"] else "FAIL")}
    am = _q("C", "ambiguous", "accuracy")
    verdicts["T8"] = {"metric": "ambiguous_accuracy", "observed": am,
                      "threshold": T["T8"]["value"], "status": "BLOCKED" if am is None else
                      ("PASS" if am >= T["T8"]["value"] else "FAIL")}
    # T9/T10 need retrieval_beneficial FP/FN on C
    fpr = _q("C", "retrieval_beneficial", "false_positive_rate")
    fnr = _q("C", "retrieval_beneficial", "false_negative_rate")
    verdicts["T9"] = {"metric": "false_positive_retrieval_rate", "observed": fpr,
                      "threshold": T["T9"]["value"], "status": "BLOCKED" if fpr is None else
                      ("PASS" if fpr <= T["T9"]["value"] else "FAIL")}
    verdicts["T10"] = {"metric": "false_negative_retrieval_rate", "observed": fnr,
                       "threshold": T["T10"]["value"], "status": "BLOCKED" if fnr is None else
                       ("PASS" if fnr <= T["T10"]["value"] else "FAIL")}
    for tid in ("T11", "T12"):
        verdicts[tid] = {"metric": results["thresholds"]["quality_thresholds"][
            [t["id"] for t in results["thresholds"]["quality_thresholds"]].index(tid)]["metric"],
            "status": "BLOCKED",
            "note": "requires candidate C on the shared label layer"}
    return verdicts


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="D4b.1 upstream Laya three-way benchmark")
    ap.add_argument("--candidates", default="A,B", help="comma list of A,B,C")
    ap.add_argument("--base-url", default="http://127.0.0.1:1933")
    ap.add_argument("--project-id", default="wb-design")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--laya-models", default=str(Path.home() / ".website-builder/laya/models"))
    ap.add_argument("--out", default=str(BENCH_DIR / "results" / "d4b1_results.json"))
    args = ap.parse_args(argv)

    results = run(args)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out_path}")

    # concise console summary
    for cand, cm in results["evaluation"].items():
        if cm.get("status") == "RUN":
            print(f"\n=== Candidate {cand} ({results['candidate_meta'][cand]['kind']}) ===")
            for label, m in cm["per_label"].items():
                if "accuracy" in m:
                    print(f"  {label:24s} acc={m['accuracy']:.3f} "
                          f"macroF1={m.get('macro_f1', float('nan')):.3f} "
                          f"cov={m.get('coverage', 1.0):.2f}")
                else:
                    print(f"  {label:24s} microF1={m.get('micro_f1', float('nan')):.3f} "
                          f"exact={m.get('exact_match', float('nan')):.3f}")
    print("\n=== Thresholds ===")
    for tid, v in results["threshold_eval"].items():
        print(f"  {tid:4s} {v['status']:8s} obs={v.get('observed')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
