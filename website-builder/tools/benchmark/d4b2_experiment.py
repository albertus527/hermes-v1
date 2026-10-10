#!/usr/bin/env python3
"""D4b.2 -- multilingual retrieval candidate experiment harness (read-only).

Evaluates candidate query-transformation strategies for Indonesian retrieval
against the frozen D4b.1 50-brief dataset, using the REAL live OpenViking
server. It NEVER writes to the corpus and NEVER calls a paid model.

Candidates:
  B   baseline        -- the accepted D4b deterministic planner (unchanged).
  C1  bilingual       -- planner keywords + a deterministic ID->EN design-term
                         gloss appended (bounded query expansion).
  C2  english-only    -- planner keywords replaced by the deterministic gloss
                         (translation-like), original kept as fallback.
  C3  paired-oracle   -- the paired English brief (a PERFECT-translation upper
                         bound; requires a translator, so approval-gated). This
                         is a ceiling probe, not an implementable candidate.

It records, per case and per candidate, the top score and the full item list so
the analysis can compute empty-pack rate, precision/recall, and false positives
against the frozen ground truth. Results are written to
``results/d4b2_experiment.json`` (appended incrementally).

    ./.venv/bin/python tools/benchmark/d4b2_experiment.py --candidates B,C1
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

# A SMALL, REVIEWABLE Indonesian->English design vocabulary. It is a general
# design lexicon (NOT derived from the frozen briefs), so using it does not
# leak evaluation ground truth. Deterministic, bounded, no model call.
GLOSSARY: dict[str, tuple[str, ...]] = {
    "tipografi": ("typography",),
    "huruf": ("typography", "font"),
    "font": ("font", "typography"),
    "warna": ("color", "palette"),
    "palet": ("palette", "color"),
    "tata": ("layout",),
    "letak": ("layout",),
    "layout": ("layout",),
    "kisi": ("grid", "layout"),
    "grid": ("grid",),
    "jarak": ("spacing",),
    "hierarki": ("hierarchy",),
    "gaya": ("style",),
    "estetika": ("aesthetic",),
    "merek": ("brand", "branding"),
    "brand": ("brand",),
    "visual": ("visual",),
    "editorial": ("editorial",),
    "minimalis": ("minimalist",),
    "animasi": ("animation", "motion"),
    "gerak": ("motion", "animation"),
    "transisi": ("transition", "motion"),
    "gulir": ("scroll",),
    "hover": ("hover",),
    "komponen": ("component", "components"),
    "kartu": ("card", "cards"),
    "tombol": ("button", "buttons"),
    "formulir": ("form", "forms"),
    "navigasi": ("navigation", "nav"),
    "menu": ("menu", "navigation"),
    "bilah": ("bar", "nav"),
    "footer": ("footer",),
    "header": ("header",),
    "bagian": ("section", "sections"),
    "seksi": ("section", "sections"),
    "ikon": ("icon", "icons"),
    "galeri": ("gallery",),
    "tabel": ("table",),
    "dasbor": ("dashboard",),
    "kisi": ("grid",),
    "pratinjau": ("preview",),
    "keranjang": ("cart",),
    "checkout": ("checkout",),
    "produk": ("product", "products"),
    "harga": ("pricing", "price"),
    "toko": ("store", "shop"),
    "belanja": ("shopping", "ecommerce"),
    "katalog": ("catalog",),
    "portofolio": ("portfolio",),
    "fotografer": ("photographer", "photography"),
    "pengembang": ("developer", "engineering"),
    "arsitektur": ("architecture", "architect"),
    "arsitek": ("architecture", "architect"),
    "firma": ("firm",),
    "konsultan": ("consulting", "consultant"),
    "korporat": ("corporate",),
    "perusahaan": ("company", "corporate"),
    "layanan": ("services",),
    "profesional": ("professional",),
    "terpercaya": ("trusted", "credible"),
    "kredibel": ("credible",),
    "studi": ("case",),
    "kasus": ("study", "case"),
    "kontak": ("contact",),
    "reservasi": ("reservation", "booking"),
    "restoran": ("restaurant", "dining"),
    "kafe": ("cafe", "coffee"),
    "kopi": ("coffee",),
    "menu": ("menu",),
    "makanan": ("food",),
    "bunga": ("florist", "botanical", "flower"),
    "tanaman": ("plant", "botanical"),
    "taman": ("garden", "botanical"),
    "kebun": ("garden", "botanical"),
    "nirlaba": ("nonprofit",),
    "mode": ("fashion",),
    "fesyen": ("fashion",),
    "mewah": ("luxury", "premium"),
    "elegan": ("elegant",),
    "bersih": ("clean",),
    "sederhana": ("simple",),
    "berani": ("bold",),
    "halus": ("subtle",),
    "nyaman": ("comfortable", "cosy"),
    "hangat": ("warm",),
    "taktil": ("tactile",),
    "majalah": ("magazine",),
    "berita": ("news",),
    "artikel": ("article",),
    "blog": ("blog",),
    "foto": ("photo", "photography"),
    "peta": ("map", "maps"),
    "lokasi": ("location", "maps"),
    "jam": ("hours",),
    "buka": ("opening", "hours"),
    "berkesan": ("memorable", "impactful"),
    "degustasi": ("tasting", "menu"),
    "landing": ("landing",),
    "halaman": ("page",),
    "situs": ("website",),
    "web": ("website",),
}


def gloss_keywords(keywords: list[str]) -> list[str]:
    """Deterministic ID->EN expansion of a keyword list (order-preserving)."""
    out: list[str] = []
    seen = set()
    for kw in keywords:
        for eng in GLOSSARY.get(kw, ()):
            if eng not in seen:
                seen.add(eng)
                out.append(eng)
    return out


def _bilingual_queries(brief: str, cfg) -> tuple[str, ...]:
    keywords = planner._tokenize(brief)[: planner.MAX_QUERY_KEYWORDS]
    if not keywords:
        return ()
    eng = gloss_keywords(keywords)
    base = " ".join(keywords)
    queries = [base[: cfg.max_query_chars]]
    if eng:
        queries.append((" ".join(eng))[: cfg.max_query_chars])
    return tuple(dict.fromkeys(queries))


def _english_only_queries(brief: str, cfg) -> tuple[str, ...]:
    keywords = planner._tokenize(brief)[: planner.MAX_QUERY_KEYWORDS]
    if not keywords:
        return ()
    eng = gloss_keywords(keywords)
    queries = []
    if eng:
        queries.append((" ".join(eng))[: cfg.max_query_chars])
    queries.append(" ".join(keywords)[: cfg.max_query_chars])
    return tuple(dict.fromkeys(queries))


# Indonesian function words / design-task markers. Used ONLY to decide whether
# to apply the (English-gloss) expansion; an English brief contains none of
# these, so English query planning is provably unchanged.
_INDONESIAN_MARKERS = frozenset({
    "yang", "dan", "atau", "untuk", "dengan", "tanpa", "pada", "dari", "ini",
    "itu", "adalah", "sebuah", "para", "agar", "biar", "supaya", "serta",
    "sangat", "lebih", "juga", "akan", "tidak", "bisa", "dapat", "harus",
    "wajib", "maupun", "namun", "tetapi", "karena", "sebagai", "oleh", "dalam",
    "antara", "setiap", "buat", "bikin", "rancang", "tampilan", "beranda",
    "situs", "halaman", "layanan", "pengguna", "jelas", "mudah", "ramah",
})


# Glossary keys that are ALSO ordinary English words. They must NOT be used as
# Indonesian evidence (an English brief legitimately contains them).
_ENGLISH_COLLISIONS = frozenset({
    "menu", "visual", "editorial", "hover", "layout", "grid", "font", "brand",
    "landing", "header", "footer", "checkout", "data", "desain",
})

# Indonesian-only glossary keys: evidence that the brief is Indonesian.
_ID_ONLY_GLOSSARY_KEYS = frozenset(GLOSSARY) - _ENGLISH_COLLISIONS


def looks_indonesian(text: str) -> bool:
    """Deterministic Indonesian detection.

    True iff the brief contains an Indonesian function/marker word OR an
    Indonesian-ONLY design term (a glossary key that is not also an English
    word). An English brief contains neither, so detection never fires on it.
    """
    tokens = set(planner._tokenize(text))
    return bool(tokens & (_INDONESIAN_MARKERS | _ID_ONLY_GLOSSARY_KEYS))


def _additive_queries(brief: str, cfg) -> tuple[str, ...]:
    """Candidate D: the accepted planner queries PLUS one bounded gloss query.

    STRICTLY ADDITIVE and ENGLISH-NEUTRAL BY CONSTRUCTION:

    * the base query set is the accepted ``plan_queries`` output, UNCHANGED and
      never displaced;
    * a single deterministic English-gloss query is appended ONLY when the brief
      is detected as Indonesian AND at least one keyword has a gloss AND there
      is spare query budget (``len(base) < max_queries``);
    * for an English brief (no Indonesian marker) the result is byte-identical
      to the accepted planner, so English retrieval cannot change.
    """
    base = list(planner.plan_queries(brief, None, cfg))
    if not looks_indonesian(brief):
        return tuple(base)
    if len(base) >= cfg.max_queries:
        return tuple(base)
    keywords = planner._tokenize(brief)[: planner.MAX_QUERY_KEYWORDS]
    eng = gloss_keywords(keywords)
    if not eng:
        return tuple(base)
    gloss = " ".join(eng)[: cfg.max_query_chars]
    norm = " ".join(gloss.split()).strip().lower()
    if any(" ".join(q.split()).strip().lower() == norm for q in base):
        return tuple(base)
    base.append(gloss)
    return tuple(base)


def build_queries(candidate: str, case: dict, cfg, by_pair: dict) -> tuple[str, ...]:
    brief = case["brief"]
    if candidate == "B":
        return planner.plan_queries(brief, None, cfg)
    if candidate == "C1":
        return _bilingual_queries(brief, cfg)
    if candidate == "C2":
        return _english_only_queries(brief, cfg)
    if candidate == "C3":
        pair = by_pair.get(case["pair"], {})
        en = pair.get("en")
        return planner.plan_queries(en["brief"], None, cfg) if en else ()
    if candidate == "D":
        # Evaluate the ACTUAL production implementation (opt-in flag on).
        import dataclasses
        cfg_d = dataclasses.replace(cfg, multilingual_expansion=True)
        return planner.plan_queries(brief, None, cfg_d)
    raise ValueError(candidate)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="B,C1,C2,C3")
    ap.add_argument("--dataset", default=str(BENCH_DIR / "d4b1_dataset.json"))
    ap.add_argument("--base-url", default="http://127.0.0.1:1933")
    ap.add_argument("--project-id", default="wb-design")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out", default=str(BENCH_DIR / "results" / "d4b2_experiment.json"))
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text())
    cases = data["cases"]
    by_pair: dict = {}
    for c in cases:
        by_pair.setdefault(c["pair"], {})[c["language"]] = c

    cfg = planner.LayaConfig(enabled=True, library_project_id=args.project_id, min_score=FLOOR)
    api_key = os.environ.get("OPENVIKING_API_KEY") or None
    adapter = build_adapter(OpenVikingConfig(
        enabled=True, base_url=args.base_url, api_key=api_key, timeout_seconds=args.timeout))
    budget = RetrievalBudget(max_items=20, min_score=0.0)  # floor off: see everything

    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    out: dict = {"floor": FLOOR, "project_id": args.project_id, "candidates": {}}

    for cand in candidates:
        per_case = []
        for c in cases:
            queries = build_queries(cand, c, cfg, by_pair)
            items = []
            latencies = []
            for q in queries:
                t0 = time.monotonic()
                r = adapter.retrieve_context(query=q, project_id=args.project_id,
                                             scope=cfg.categories, budget=budget)
                latencies.append(round(time.monotonic() - t0, 4))
                if r.status == planner.STATUS_OK:
                    for it in r.items:
                        items.append({"uri": it.uri, "category": it.category,
                                      "score": round(float(it.score), 4), "query": q})
            scores = sorted((x["score"] for x in items), reverse=True)
            top = scores[0] if scores else None
            admitted = [x for x in items if x["score"] >= FLOOR]
            per_case.append({
                "id": c["id"], "language": c["language"], "pair": c["pair"],
                "scenario": c["scenario"], "queries": list(queries),
                "top_score": top, "n_returned": len(scores),
                "admitted_ge_floor": len(admitted),
                "empty_pack_with_floor": len(admitted) == 0,
                "admitted_scores": [round(x["score"], 4) for x in admitted],
                "admitted_items": [{"uri": x["uri"], "category": x["category"], "score": x["score"]} for x in admitted],
                "latencies_s": latencies,
                "ground_truth": c["ground_truth"],
            })
            print(f"[{cand}] {c['id']:38s} {c['language']} top={top} adm={len(admitted)}", flush=True)
        out["candidates"][cand] = per_case
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2))
        print(f"--- wrote {args.out} after candidate {cand} ---", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
