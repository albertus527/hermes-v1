# D4b.2 — Multilingual Retrieval Hardening: Benchmark Report

Status: engineering record for Batch **D4b.2** (benchmark + candidate evaluation).
Branch: `web-design`.
Baseline commit at mission start: `b203c20f425c412818a4fae1acb81efdb14e9ae8`
(D4b.1 accepted record committed).
Companion acceptance report: `docs/D4B2_MULTILINGUAL_RETRIEVAL_ACCEPTANCE.md`.

This report covers the **benchmark, root-cause analysis, candidate evaluation,
and held-out methodology**. The final verdict, gates, security review, and
rollback procedure are in the acceptance report.

> **Headline.** The Indonesian empty-pack rate is **not** primarily a
> threshold-calibration problem. The reviewed `wb-design` corpus is **100 %
> English**, and the embedding model is English-centric, so an Indonesian brief
> scores systematically **~0.047 lower** than its English semantic equivalent
> (measured on 25 paired ID/EN briefs). The **smallest safe change** that fixes
> this without touching the corpus, the embedding model, the relevance floor, or
> any English behaviour is a **deterministic Indonesian→English gloss query**
> appended to the accepted D4b query plan, **disabled by default**. On the frozen
> 50-brief benchmark it moves Indonesian empty packs **15 → 6** (true retrieval
> failures **10 → 1**), Indonesian recall **0.50 → 0.95**, and leaves **English
> byte-identical** (7 empty packs, recall 0.85, precision 0.944, F1 0.895 — all
> unchanged). The held-out set confirms generalisation.

---

## 1. Frozen baseline (verified, not assumed)

The frozen D4b.1 artifacts were **not modified**. Their SHA-256 digests are
byte-identical to the D4b.1 record:

| Artifact | SHA-256 |
|---|---|
| `tools/benchmark/d4b1_dataset.json` | `9da94ae5fe609238a7e0ec28a8e2053ac3580594c516985f50af829fbbfd42cd` |
| `tools/benchmark/d4b1_thresholds.json` | `dfa932b4810f79629bf3b586883aa8be22a10fbb37b78e8b26493a02ccc51965` |

Re-running the accepted D4b path (`tools/benchmark/d4b1_benchmark.py --candidates B`)
reproduces the frozen metrics **byte-for-byte**:

| Metric | Frozen value | Re-measured |
|---|---|---|
| `relevant_categories` micro-F1 (all) | 0.672 | **0.6716** |
| `relevant_categories` micro-F1 (ID) | 0.526 | **0.5263** |
| `relevant_categories` micro-F1 (EN) | 0.779 | **0.7792** |
| `retrieval_beneficial` accuracy | 0.720 | **0.720** |
| empty packs (all / ID / EN) | 22 / 15 / 7 | **22 / 15 / 7** |
| ID top-similarity mean | 0.611 | **0.6109** |
| EN top-similarity mean | 0.658 | **0.6576** |

Live environment at mission start: OpenViking `/health` → `ok, 0.4.23,
auth_mode api_key`; `/ready` → `vectordb: ok, embedding: ok`; systemd unit
`active`+`enabled`; `tmux` sessions `[trade, website]` present; host
`MemAvailable` ≈ 6.2 GB; load ≈ 0.7. No service was restarted or reconfigured.

---

## 2. Root-cause analysis

### 2.1 The retrieval path (traced, not guessed)

```
USER BRIEF
  → IntakeProcessor.process
  → LayaContextPreparer.prepare_context
      → plan_queries (DETERMINISTIC, ≤3 queries, ≤200 chars)   [app/core/laya_context.py]
      → OpenVikingRetrievalAdapter.retrieve_context            [app/core/openviking_retrieval.py]
          → LiveOpenVikingBackend.find                         [app/core/openviking_live.py]
              → POST /api/v1/search/find  (127.0.0.1:1933)     [OpenViking 0.4.23]
                  → embedding: 9router → openrouter/text-embedding-3-small (1536-d)
              → attach application provenance (sidecar), drop unprovenanced
          → relevance floor (min_score) drop; category allowlist; credential/isolation checks
      → dedup, rank, classify_quality, bound to ≤6000 chars
  → render_laya_context_block → FAST (lower-trust REFERENCE DATA)
```

The embedding model is **`openrouter/text-embedding-3-small` (1536-dim)** via the
9router loopback proxy. The corpus is the 11 reviewed English reference files
declared in `app/core/openviking_corpus.py`.

### 2.2 Separating retrieval failures from correct abstentions

The mission requires that retrieval failures be separated from correct
abstentions. Joining the frozen probe
(`results/d4b1_retrieval_probe.json`) with the frozen ground truth:

| Language | Empty packs | **Correct abstentions** (rb=false) | **True retrieval failures** (rb=true) |
|---|---|---|---|
| Indonesian | 15 | **5** | **10** |
| English | 7 | **4** | **3** |
| All | 22 | **9** | **13** |

So of the 22 empty packs, **9 are correct** (out-of-scope / adversarial /
too-vague briefs) and **13 are genuine failures**. The Indonesian problem is
**10 true failures**, not 15 — the baseline was under-reporting quality on the
9 correct abstentions and over-reporting failures on the 5 that were correct.

### 2.3 The decisive experiment: the corpus is English, the queries are Indonesian

The dataset contains **25 paired ID/EN briefs** that are semantic equivalents
(e.g. `S02-corporate-consulting` ↔ `S02-corporate-konsultan`). Running each
pair's own-language brief through the accepted planner and comparing the top
similarity (floor off):

| Pair | ID top | EN top | Δ (EN−ID) |
|---|---|---|---|
| S01 | 0.6410 | 0.6618 | +0.021 |
| S02 | 0.5658 | 0.6431 | +0.077 |
| S07 | 0.6204 | 0.7261 | +0.106 |
| S08 | 0.6457 | 0.7449 | +0.099 |
| S09 | 0.5669 | 0.6505 | +0.084 |
| S11 | 0.5932 | 0.7005 | +0.107 |
| S21 | 0.7045 | 0.7525 | +0.048 |
| … | … | … | … |
| **mean** | **0.6109** | **0.6576** | **+0.047** |

**23 of 25 pairs** score higher in English, with a mean gap of **+0.047**. This
is a genuine **cross-lingual embedding penalty**: the corpus is English and the
embedding model places an Indonesian query farther from its English documents
than the English paraphrase of the same query.

### 2.4 The floor is NOT the root cause (it cannot separate the classes)

Lowering the relevance floor was evaluated against the frozen ground truth. The
Indonesian classes **overlap in score**:

* ID **true failures** (rb=true) score **0.566 – 0.705**;
* ID **correct abstentions** (rb=false) score **0.553 – 0.613**.

| Floor | ID TP | ID FP | ID FN | ID recall |
|---|---|---|---|---|
| 0.62 (baseline) | 10 | 0 | 10 | 0.50 |
| 0.60 | 13 | 1 | 7 | 0.65 |
| 0.58 | 17 | 3 | 3 | 0.85 |
| 0.56 | 20 | 4 | 0 | 1.00 |

Lowering the floor raises recall **only by admitting false positives** (the
rb=false briefs at 0.553–0.613), and at 0.56 it admits every one of them. A
single global floor cannot separate the two Indonesian classes. **Cause B
(Indonesian query formulation / cross-lingual embedding mismatch) dominates;
cause C (the floor) is a symptom, not the disease.**

### 2.5 Cause classification

| Candidate cause | Verdict | Evidence |
|---|---|---|
| A. Embedding mismatch (ID vs EN) | **CONFIRMED (dominant)** | paired-brief gap +0.047; corpus 100 % EN; English-oracle recovers all of it (§3) |
| B. Indonesian query formulation | **PARTIAL** | the deterministic planner emits Indonesian queries against an English corpus |
| C. English-calibrated relevance floor | **SYMPTOM, not cause** | lowering it admits false positives; ID classes overlap |
| D. Corpus language distribution | **CONTRIBUTING** | corpus is entirely English (11/11 sources) |
| E. Category selection | **NOT the cause** | category filtering is not the bottleneck; empty packs occur even when categories match |
| F. Multiple interacting causes | **YES** | A + B + D interact; the remedy addresses the cross-lingual mismatch |

---

## 3. Candidate implementations and provenance

All candidates use the **real live OpenViking server** and the **real accepted
D4b planner**. No paid model call is made by any candidate.

| Candidate | Description | Provenance / cost |
|---|---|---|
| **A** | FAST-only baseline | paid model; **NOT run** (no paid approval) — out of scope here |
| **B** | Accepted D4b planner (unchanged) | the frozen baseline |
| **C1** | Bilingual: base Indonesian query **+** gloss query (replaces category queries) | deterministic glossary, $0 |
| **C2** | English-only: gloss query first, Indonesian fallback (replaces category queries) | deterministic glossary, $0 |
| **C3** | **English-oracle**: the paired English brief's queries (perfect translation upper bound) | requires a translator → **approval-gated ceiling probe**, not implementable |
| **D** | **Strictly additive** gloss: the accepted `plan_queries` output **plus one** gloss query, **Indonesian-gated**, disabled by default | deterministic glossary, $0 |

The glossary (`_ID_EN_GLOSSARY`, ~90 terms) is a **general design lexicon**, not
derived from any benchmark brief, so enabling it leaks no ground truth. It
contains **no model call, no network I/O, and no new dependency**.

---

## 4. Candidate comparison (frozen 50-brief benchmark)

Case-level decision: a **non-empty pack** = the candidate asserts retrieval is
beneficial. Metrics against the frozen ground truth. Floor = 0.62.

| Cand | Lang | Empty | Top mean | Precision | Recall | F1 | FPR | FNR | Correct abst. | True fail |
|---|---|---|---|---|---|---|---|---|---|---|
| **B** | all | 22/50 | 0.6343 | 0.964 | 0.675 | 0.794 | 0.100 | 0.325 | 9 | 13 |
| **B** | **id** | **15/25** | 0.6109 | 1.000 | **0.500** | 0.667 | 0.000 | 0.500 | 5 | **10** |
| **B** | en | 7/25 | 0.6576 | 0.944 | 0.850 | 0.895 | 0.200 | 0.150 | 4 | 3 |
| **C1** | all | 12/50 | 0.6538 | 0.974 | 0.925 | 0.949 | 0.100 | 0.075 | 9 | 4 |
| **C1** | id | 6/25 | 0.6493 | 1.000 | 0.950 | 0.974 | 0.000 | 0.050 | 5 | 1 |
| **C1** | en | 6/25 | 0.6584 | 0.947 | 0.900 | 0.923 | 0.200 | 0.100 | 4 | 2 |
| **C3** | id | 7/25 | 0.6576 | 0.944 | 0.850 | 0.895 | 0.200 | 0.150 | 4 | 3 |
| **C3** | en | 7/25 | 0.6576 | 0.944 | 0.850 | 0.895 | 0.200 | 0.150 | 4 | 3 |
| **D** | all | 13/50 | 0.6534 | 0.973 | 0.900 | 0.935 | 0.100 | 0.100 | 9 | 4 |
| **D** | **id** | **6/25** | 0.6492 | 1.000 | **0.950** | 0.974 | 0.000 | 0.050 | 5 | **1** |
| **D** | **en** | **7/25** | **0.6576** | **0.944** | **0.850** | **0.895** | **0.200** | **0.150** | **4** | **3** |

**Reading.**

1. **C3 (English-oracle) is the ceiling.** Its Indonesian metrics become
   **identical** to English (empty 7, top mean 0.6576, recall 0.850). This proves
   the cross-lingual penalty is fully recoverable by translating the query — the
   ceiling is *not* higher than English, and no local model is needed to reach it.
2. **C1 improves both languages** — but because it *replaces* the accepted
   category queries, it also perturbs English (7→6 empty, precision 0.944→0.947).
   That is a change to English behaviour, which the mission forbids.
3. **D matches C1's Indonesian gains while leaving English byte-identical.**
   D's English row is **exactly** B's English row, because D never changes an
   English query plan (§5). This is the decisive property.

**Category micro-F1 (from admitted item categories):**

| Cand | Lang | Precision | Recall | F1 |
|---|---|---|---|---|
| B | id | 0.750 | 0.395 | 0.517 |
| D | id | 0.717 | **0.868** | **0.786** |
| B | en | 0.638 | 0.789 | 0.706 |
| D | en | 0.638 | 0.789 | **0.706** |

Indonesian category recall **0.395 → 0.868**; English **unchanged**.

---

## 5. Why Candidate D is English-neutral by construction

Candidate D appends **one** gloss query to the accepted `plan_queries` output
**only when the brief is detected as Indonesian**:

* **Strictly additive** — the accepted base queries are preserved as a prefix;
  nothing is displaced.
* **Indonesian-gated** — `looks_indonesian()` fires on an Indonesian
  function/marker word **or** an Indonesian-only glossary key. Glossary keys that
  are also ordinary English words (`menu`, `grid`, `layout`, `brand`, `mode`,
  `jam`, …) are **excluded** from the evidence set, so an English brief is never
  detected as Indonesian.
* **Bounded** — the extra query is appended only when `len(base) < max_queries`
  (≤3) and is truncated to `max_query_chars` (200).

Verified offline on the frozen and held-out sets:

| Set | EN plans identical to accepted | EN misdetected as ID | ID plans glossed |
|---|---|---|---|
| frozen (50) | **25 / 25** | **0** | 20 / 25 |
| held-out (16) | **8 / 8** | **0** | 7 / 8 |

---

## 6. Held-out evaluation

A **separate, frozen** held-out set (`tools/benchmark/d4b2_heldout.json`, 16
cases: 8 ID + 8 EN, paired) was authored **before** final acceptance and is
**never** used for threshold tuning. It covers editorial, landing pages,
portfolio, product showcase, motion, component selection, design DNA, and an
out-of-scope case, with labels grounded in the actual corpus references.

| Cand | Lang | Empty | Precision | Recall | F1 | FPR | FNR | Correct abst. | True fail |
|---|---|---|---|---|---|---|---|---|---|
| B | id | 3/8 | 1.000 | 0.714 | 0.833 | 0.000 | 0.286 | 1 | 2 |
| B | en | 1/8 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 1 | 0 |
| **D** | **id** | **2/8** | 1.000 | **0.857** | **0.923** | 0.000 | 0.143 | 1 | **1** |
| **D** | **en** | **1/8** | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 1 | 0 |

Candidate D generalises: held-out Indonesian recall **0.714 → 0.857**, empty
packs **3 → 2**, with **English unchanged** and **zero false positives**. The
one remaining held-out ID failure (`H02-landing-saas-notulen-ai`, top 0.610) is
a genuine hard case — a SaaS landing brief whose Indonesian wording shares few
glossary terms.

---

## 7. Precision / recall and false-positive trade-off

Candidate D **does not** trade false positives for recall:

* false-positive rate is **unchanged at 0.100** (all) and **0.000** (Indonesian);
* the **9 correct abstentions** (out-of-scope, adversarial, too-vague) are
  **preserved exactly** — D still abstains on S13/S14/S23/S25 and the ambiguous
  S10, because their Indonesian briefs yield no relevant reference above the
  floor even with the gloss;
* the improvement comes **only** from recovering genuine retrieval failures
  (ID true failures **10 → 1**).

This is the honest improvement the mission asks for: recall rises **because
relevant references genuinely exist**, not because the floor was lowered.

---

## 8. Resource and cost measurements

| Metric | Value |
|---|---|
| Additional model calls (Candidate D) | **0** (deterministic planner) |
| Additional paid cost | **$0.00** (embedding path metered $0.00; no paid call) |
| Queries per Indonesian brief | ≤ 3 (base plan + ≤1 gloss) |
| Per-query latency (frozen, n=87) | p50 **2.82 s**, p95 **3.11 s** (baseline p50 2.81 s) |
| Per-brief latency (frozen) | p50 **5.54 s**, p95 **8.43 s** (baseline p95 8.30 s) |
| Benchmark process RSS | ≈ 55 MB (no resident model) |
| Host `MemAvailable` during benchmark | ≈ 6.2 GB (never below ~5.5 GB) |
| OpenViking service RSS | ≈ 368 MB (unchanged) |
| Host load average during benchmark | ≤ ~1.2 of 4 vCPU |
| Swap activity | none (`si/so` ≈ 0; swap usage unchanged) |
| Hermes Trade | **untouched** (`tmux trade` identical before/after) |
| OpenViking service | **not restarted/reconfigured** (`/health` ok throughout) |

The extra gloss query adds at most one retrieval round-trip (~2.8 s) for an
Indonesian brief; the p95 per-brief latency stays under 9 s, well inside the
adapter's 10 s timeout and the G11 gate (12 s). Concurrency was **1**.

---

## 9. Security findings

No security regression. The change is confined to **query construction** in
`plan_queries`; it does not touch the adapter, the corpus, the trust model, or
the pack boundary.

* Retrieved content remains **untrusted DATA** (the renderer is unchanged).
* **No cross-project retrieval** — the library scope stays the application
  constant `wb-design`; the gloss query only changes the query *text*.
* **No unbounded expansion** — at most one extra query, ≤200 chars, within the
  existing `max_queries` ceiling.
* **No new network call** — the glossary is a static in-process dict; the planner
  is verified to open no socket.
* **No credential/isolation/provenance change** — those checks live in the
  adapter, untouched.
* The glossary contains no secret, no path, and no benchmark-case identifier.

A dedicated mutation driver (`tools/mutation_check_d4b2.py`, **7/7 guards
killed**) proves the disabled default, the shipped-config default, the
Indonesian gate, the additive property, the English-collision exclusion, the
config read, and real detection all fail closed when reverted.

---

## 10. Regression and mutation test results

| Suite | Result |
|---|---|
| Focused D4b.2 tests (`tests/test_d4b2_multilingual.py`) | **45 passed** |
| Focused D4b tests (Laya context/integration/composition) | 77 passed |
| Focused D4a/D4a.1 tests | 150 passed |
| D4b.1 benchmark tests | 29 passed |
| Full offline suite (network blocked) | **4053 passed**, 2 skipped, 4 deselected |
| D3a.5/D3b regression subset | 1029 passed |
| D4b mutation driver | 10/10 guards killed |
| D4b.1 mutation driver | 6/6 guards killed |
| **D4b.2 mutation driver** | **7/7 guards killed** |

The full offline suite grew from **4008** (D4b.1 baseline) to **4053** — exactly
the **+45** new D4b.2 tests. No existing assertion was weakened.

---

## 11. Reproducibility

```bash
# root-cause probe (read-only, floor off)
./.venv/bin/python tools/benchmark/d4b1_retrieval_probe.py

# candidate experiment (real live OpenViking; needs the OV user key)
export OPENVIKING_API_KEY="$(sed -n 's/^OPENVIKING_USER_KEY=//p' ~/.website-builder/openviking/openviking.env)"
./.venv/bin/python tools/benchmark/d4b2_experiment.py --candidates B,C1,C2,C3
./.venv/bin/python tools/benchmark/d4b2_experiment.py --candidates B,D \
    --out tools/benchmark/results/d4b2_BD.json
./.venv/bin/python tools/benchmark/d4b2_experiment.py --candidates B,D \
    --dataset tools/benchmark/d4b2_heldout.json \
    --out tools/benchmark/results/d4b2_heldout_BD.json
./.venv/bin/python tools/benchmark/d4b2_analyze.py   # + d4b2_summary.json
./.venv/bin/python tools/benchmark/d4b2_gate_eval.py # gate verdicts
```

Artifacts: `results/d4b2_experiment.json`, `results/d4b2_BD.json`,
`results/d4b2_heldout_BD.json`, `results/d4b2_summary.json`.

---

## 12. Conclusion

The Indonesian retrieval gap is a **cross-lingual embedding mismatch** (English
corpus + English-centric embedding model), **not** a mis-set relevance floor. The
smallest safe fix that adds **no model, no cost, and no English change** is a
**deterministic, Indonesian-gated, strictly-additive English-gloss query**,
disabled by default. It passes every frozen acceptance gate on the tuning set
and generalises to the held-out set. See the acceptance report for the verdict
and the remaining operator approval gate.
