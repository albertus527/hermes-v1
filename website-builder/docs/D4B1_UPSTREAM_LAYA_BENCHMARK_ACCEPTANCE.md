# D4b.1 — Real Upstream Laya: Benchmark, Integration Gate & Acceptance

Status: engineering record for Batch **D4b.1** (RESUME — real upstream execution).
Branch: `web-design`.
Resume baseline commit: `00e1f81c5bc9721a355e3d748975333e0165dfb3`
(D4b.1 harness accepted at `D4B1_AWAITING_APPROVAL`; D4b accepted:
`D4B_READY_FOR_INTEGRATION`).

This resume executes the part of D4b.1 that the prior record left **approval-gated**:
the operator has now authorized the isolated environment, the pinned CPU-only
dependencies, the official multilingual checkpoint, and local CPU inference on
the frozen 50-brief dataset. This document replaces the prior "NOT_RUN" /
"AWAITING_APPROVAL" status with **real measured upstream inference**.

> **Headline.** The real upstream Laya package (`laya==0.4.1`, source `v0.4.1`)
> and the official checkpoint (`convaiinnovations/laya-multilingual`) were
> installed into an isolated venv and **executed** on all 50 briefs. Checkpoint
> SHA-256 and size **match the pinned LFS values exactly**. Upstream Laya
> **passes the design-intent gates** (T1/T1b/T2/T3) but **fails every category /
> retrieval gate** (T4, T5, T6, T7, T10) and the regression/improvement gates
> (T11/T12) against the accepted deterministic D4b baseline. Its
> `retrieval_beneficial` decision is **worse than chance** (accuracy 0.20 vs a
> 0.80/0.20 base rate), and a controlled phrasing probe shows this is a genuine
> model property, not a harness artifact. Resource-wise Laya is **safe but
> heavy** (~1.85 GB steady, ~2.35 GB peak, ~2.1 s/brief, ~13–19 s cold).
> **Verdict: `D4B1_BENCHMARK_FAILS_AVAILABLE_GATES`.** The deterministic D4b
> path is **preserved unchanged**; upstream Laya is **not** integrated, and no
> permanent runtime is installed. The paid FAST-only comparison (Candidate A)
> remains **BLOCKED** (no paid calls authorized).

---

## 1. Baseline and preconditions (verified, not assumed)

| Item | Value | Evidence |
|---|---|---|
| Branch | `web-design` | `git rev-parse --abbrev-ref HEAD` |
| Working tree at start | clean | `git status --short` → empty |
| Resume baseline commit | `00e1f81c5bc9721a355e3d748975333e0165dfb3` | `git rev-parse HEAD` |
| `feature/website` | **untouched** at `868ed00e3f24e06f1dcf9944d6d031105dff0646` | proof guardrail §14 |
| D4a.1 verdict | `D4A1_READY_FOR_LAYA` | `docs/D4A1_OPENVIKING_LIVE_ACCEPTANCE.md` §20 |
| D4b verdict | `D4B_READY_FOR_INTEGRATION` | `docs/D4B_LAYA_CONTEXT_PREPARATION_ACCEPTANCE.md` §18 |
| OpenViking `/health` | `{"status":"ok","healthy":true,"version":"0.4.23","auth_mode":"api_key"}` | live `curl` |
| OpenViking `/ready` | `{"status":"ready",...,"vectordb":"ok","embedding":"ok"}` | live `curl` |
| Hermes Trade | tmux `trade` present before **and** after; untouched | `tmux ls` |
| Frozen dataset hash | `9da94ae5…bbfd42cd` (matches D4b.1 record) | `sha256sum` |
| Frozen thresholds hash | `dfa932b4…ccc51965` (matches D4b.1 record) | `sha256sum` |

The frozen dataset and threshold files were **not modified**; their SHA-256 digests
are byte-identical to the values recorded in the prior D4b.1 acceptance record.

---

## 2. Real upstream Laya — verification (unchanged, re-confirmed)

| Field | Value | How verified |
|---|---|---|
| Repository | `https://github.com/NandhaKishorM/laya` | GitHub API |
| License | **Apache-2.0** | GitHub API + `pyproject.toml` |
| **Pinned release tag** | **`v0.4.1`** → commit **`1adc59f7e371deb601fcfa18a14e25db238addcc`** | `git clone --branch v0.4.1` |
| PyPI distribution | **`laya==0.4.1`** (installed and imported: `laya.__version__ == "0.4.1"`) | live import |
| Public API | `from laya import Router; Router().predict(state, questions)` → typed `choice` / `score` / `noul` decisions + `routing` | source + live call |
| Official checkpoint | **`convaiinnovations/laya-multilingual`** | HF API |
| **Pinned checkpoint revision** | **`e4e9ddf21a7b1903b7acffd8814ad4307bf63a67`** | HF API + `laya/revisions.py` `PINNED_REVISIONS` |
| Encoder | `jhu-clsp/mmBERT-base` (mmBERT-base, 322M) | `rl_agent_config.json` (live) |
| Calibration | **uncalibrated** — `temperature: [1.0, 1.0, 1.0]` | live verify script |

The installed package's own `PINNED_REVISIONS["convaiinnovations/laya-multilingual"]`
equals `e4e9ddf21a7b1903b7acffd8814ad4307bf63a67` — the same immutable revision the
harness pins, so the supply-chain pin is independently corroborated by the
upstream source, not only by our harness.

**Documented upstream limitations (not reproduced by us).** The model card states
the multilingual checkpoint is *weaker on English* (0.619 vs 0.684 macro),
*near chance on typed-decisions zero-shot* (0.342 vs 0.318 random), and *ships
uncalibrated* (mean ECE 0.314). Upstream latency (33 ms) is a **T4-GPU** figure.
None of these upstream numbers was used as ground truth or as a threshold; the
frozen thresholds were written before any candidate ran.

---

## 3. Installation verification (actual bytes, not projections)

Isolated environment, created by `tools/benchmark/d4b1_laya_setup.sh`:

| Item | Location | Measured |
|---|---|---|
| Isolated venv | `~/.website-builder/laya/venv` | **1.2 GB** |
| Checkpoint | `~/.website-builder/laya/models` | **647 MB** |
| HF metadata | `~/.website-builder/laya/hf` | ~100 KB |
| **Total disk** | `~/.website-builder/laya` | **1.8 GB** (budget 3.5 GB) |

Isolation guarantees held: **no global site-packages**, **no systemd unit**, **no
persistent service**, **no PATH change**, **no Hermes Trade modification**,
**no paid API call**. Rollback is `bash tools/benchmark/d4b1_laya_setup.sh --uninstall`.

**One setup correction (found and fixed during this resume).** The pinned stack
resolved `huggingface_hub 2.2.0`, which depends on `httpx2` rather than `httpx`.
The accepted D4b OpenViking adapter imports `httpx` **lazily at call time**, so
the first isolated-venv benchmark run reported `BACKEND_FAILURE` for **every**
Candidate-B brief (see §4.1). The setup script now installs the real `httpx`
client explicitly; the fix is committed and the venv re-verified. This is an
environment-completeness fix only — no production code was changed.

### 3.1 Checkpoint hash verification (independent of the download)

```
$ bash tools/benchmark/d4b1_laya_setup.sh --verify
laya 0.4.1
{
  "weights_path": ".../laya/models/model.safetensors",
  "size_bytes": 643835514,
  "size_ok": true,
  "sha256": "9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204",
  "sha256_ok": true,
  "encoder": "jhu-clsp/mmBERT-base",
  "temperature": [1.0, 1.0, 1.0],
  "calibrated": false,
  "verdict": "PASS"
}
```

The downloaded `model.safetensors` is **643,835,514 bytes** and its SHA-256 is
**`9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204`** — both
**exactly** the pinned values in the mission and the prior acceptance record.

---

## 4. Real Candidate-C inference evidence

Candidate C is the **real upstream API** — no custom inference engine:

```python
from laya import Router
router = Router(models={"multilingual": "~/.website-builder/laya/models"},
                revision="e4e9ddf21a7b1903b7acffd8814ad4307bf63a67",
                device="cpu", default="multilingual", max_loaded=1, preload=False)
res = router.predict(brief, questions, model="multilingual")   # 6 typed questions
```

A first real call (single brief, cold) returned a well-formed typed payload:

```
model=laya-rl-agent
design_intent   choice=editorial   P=0.9718  confidence=0.9427
primary_category choice=design_dna  P=0.8905
motion_relevant noul=0.267  component_relevant noul=0.5231
retrieval_beneficial noul=0.1005   ambiguous noul=0.116
usage={input_tokens:485, state_tokens:19, truncated:false}
routing={model:multilingual, reason:"explicit model='multilingual'"}
```

The harness records, per brief: the raw typed decisions (`choice` / `noul` /
`score`), `probabilities`, `confidence` / `answer_confidence`, `action`,
`routing`, `usage`, per-brief latency, and any inference error. **All 50 briefs
produced a real answer** (0 inference errors; coverage 1.00 on every Candidate-C
label). Raw confidence is reported but **never treated as a calibrated
probability** (the checkpoint ships uncalibrated).

### 4.1 A measurement bug found and corrected before the result was trusted

The **first** isolated-venv A,B,C run produced Candidate-B micro-F1 **0.000** for
every brief — i.e. the accepted D4b baseline looked broken. Root cause: the
isolated venv lacked `httpx`, so the D4b OpenViking adapter failed closed with
`BACKEND_FAILURE`. After installing `httpx` (§3), Candidate B **reproduced the
frozen D4b baseline byte-for-byte** (micro-F1 0.672 all / 0.526 ID / 0.779 EN;
FPR 0.100, FNR 0.325; empty packs 22/50). This confirms the B path is the real
accepted system and that the corrected run is the valid one. Both runs are
preserved in `tools/benchmark/results/` (`d4b1_ABC.json` is the corrected run).

---

## 5. Candidate B vs Candidate C — shared normalized-label comparison

Identical 50 briefs, labels, corpus, and evaluation rules. Candidates are scored
only on labels they emit; coverage is reported. Candidate B is the **real
accepted D4b planner + real live OpenViking** (unchanged). Candidate C is the
**real upstream Laya** typed decisions.

| Label | Metric | Candidate B (D4b) | Candidate C (Laya) | Better |
|---|---|---|---|---|
| relevant_categories | micro-F1 | **0.672** | 0.544 | B |
| relevant_categories | exact-match | **0.440** | 0.340 | B |
| retrieval_beneficial | accuracy | **0.720** | 0.200 | B |
| retrieval_beneficial | F1 | **0.794** | 0.000 | B |
| motion_relevant | accuracy / F1 | 0.940 / **0.824** | 0.920 / 0.667 | B |
| component_relevant | accuracy / F1 | 0.580 / **0.618** | 0.540 / 0.378 | B |
| design_intent | macro-F1 / acc | *(not emitted)* | 0.639 / 0.620 | C only |
| ambiguous | accuracy | *(not emitted)* | 0.880 | C only |

Paired bootstrap 95% CIs and McNemar tests (`d4b1_analyze.py`, n=50):

| Label | A/B/C | Result |
|---|---|---|
| relevant_categories | B 0.440 vs C 0.340 | McNemar p=0.383 → **INCONCLUSIVE** (B nominally ahead) |
| retrieval_beneficial | B 0.720 vs C 0.200 | McNemar p=0.0000 → **B significantly better** |
| motion_relevant | B 0.940 vs C 0.920 | p=1.000 → **INCONCLUSIVE** |
| component_relevant | B 0.580 vs C 0.540 | p=0.832 → **INCONCLUSIVE** |
| ambiguous | A 0.400 vs C 0.880 | p=0.0000 → **C significantly better** |

**Reading.** Laya's typed decisions are **not better than the deterministic D4b
planner on any label the planner serves**, and are **significantly worse** on
`retrieval_beneficial`. Laya does emit two labels the deterministic path does not
(`design_intent`, `ambiguous`) and is strong on those — the only positive signal.

### 5.1 `retrieval_beneficial` is inverted — and it is a genuine model property

Candidate C answered "retrieval is **not** beneficial" for almost every brief,
including the 40/50 whose ground truth is `true` — so its accuracy (0.200) is
**below the 0.80 majority-class base rate** and its F1 is **0.000**. Because the
`noul` decoding convention matters here, the harness rule was validated against
the upstream source: `agent.py` states *"the returned `noul` value is always
P(true)"* and decodes `noul = p[1]` over the option pair `[false, true]` — so
`noul >= 0.5 ⇒ true` is **correct**.

A controlled probe (`/tmp` diagnostic, 6 retrieval-beneficial=true briefs) then
ruled out a phrasing artifact:

| Brief | 6-question call | single q (same phrasing) | **negated** phrasing | explicit true/false criteria |
|---|---|---|---|---|
| S01 EN | 0.008 | 0.008 | 0.015 | 0.048 |
| S01 ID | 0.000 | 0.000 | 0.029 | 0.004 |
| S02 EN | 0.011 | 0.011 | 0.030 | 0.146 |
| S02 ID | 0.002 | 0.002 | 0.082 | 0.200 |
| S03 EN | 0.089 | 0.089 | 0.179 | 0.273 |
| S03 ID | 0.001 | 0.001 | 0.019 | 0.007 |

The model returns P(true) ≈ 0.00–0.09 under the harness phrasing, does **not**
flip under a negated phrasing, and stays low even with explicit `true`/`false`
criteria. This is consistent with the upstream card's *"near chance on
typed-decisions zero-shot"* and is reported as a **genuine weakness of the
uncalibrated checkpoint**, not a harness defect.

---

## 6. Indonesian / English retrieval investigation (priority)

Read-only probe (`tools/benchmark/d4b1_retrieval_probe.py`) issuing the **same
planner queries** the accepted D4b path issues, with the relevance floor
**disabled** so rejected references and their scores are visible. The frozen
corpus, thresholds, and ground truth were **not** modified.

| Metric | Indonesian (25) | English (25) | All (50) |
|---|---|---|---|
| Empty pack at floor 0.62 | **15** | **7** | **22** |
| Empty-pack rate | **0.600** | 0.280 | 0.440 |
| Top similarity score — mean | **0.611** | 0.658 | — |
| Top similarity score — min / max | 0.553 / 0.705 | 0.594 / 0.753 | — |
| Cases with no return at all | 0 | 0 | 0 |

**Findings.**

1. **The bottleneck is the embedding/relevance layer, not the typed-decision
   layer.** Every brief *does* return candidate references (0 cases with no
   return); Indonesian briefs simply cluster **below the 0.62 floor** (top-score
   mean 0.611, i.e. *below* the floor on average). The reference embeddings for
   Indonesian briefs are systematically weaker than for English.
2. **Laya's typed decisions do not improve retrieval planning.** Candidate C's
   `relevant_categories` micro-F1 is *worse* than the deterministic planner on
   both languages (ID 0.415 vs 0.526; EN 0.656 vs 0.779), and its
   `retrieval_beneficial` decision is inverted (§5.1). Laya therefore **cannot**
   be credited with improving multilingual retrieval.
3. **Laya *can* read Indonesian** (design_intent ID macro-F1 0.579 vs EN 0.659 —
   a real but modest multilingual capability), which only reinforces that the
   multilingual **retrieval** failure is an embedding/relevance problem, not a
   language-comprehension problem.
4. **The 0.62 floor is the operative cause of the empty packs.** With the floor
   off, Indonesian top scores (0.553–0.705) largely straddle 0.62; the floor
   discards the majority of Indonesian references while admitting most English
   ones. Lowering the floor would raise recall but also admit false positives
   (the floor was calibrated to separate relevant 0.66–0.77 from unrelated
   0.54–0.58 on the *English* corpus).

**Documentation discrepancy (reported honestly).** The prior D4b record stated
"22/50 empty, 18 of them Indonesian". The measured split — identical across the
frozen artifacts `d4b1_B_only.json`, `d4b1_AB.json`, the new `d4b1_ABC.json`, and
this independent probe — is **15 Indonesian + 7 English = 22/50**. The **total
matches**; the per-language split in the prior prose was inaccurate. The measured
split is authoritative. (The ID/EN category micro-F1 *metrics* themselves are
byte-identical to the frozen values: 0.526 / 0.779.)

**Proposed future fix (NOT implemented in this benchmark).** A separate,
evidence-driven multilingual retrieval mission should evaluate: a per-language or
recalibrated relevance floor; a multilingual embedding model for the corpus; and
category-scoped retrieval. This is out of scope for D4b.1 and is **not** built
here.

---

## 7. Resident (Mode A) vs short-lived worker (Mode B) lifecycle

### Mode A — resident model (`d4b1_benchmark.py`, Candidate C)

| Measurement | Value | Threshold | Verdict |
|---|---|---|---|
| Cold start (load + first forward) | **19.26 s** | R1 ≤ 180 s | PASS |
| Warm p50 / brief | **2.090 s** | R2 ≤ 3.0 s | PASS |
| Warm p95 / brief | **2.254 s** | R3 ≤ 8.0 s | PASS |
| Steady-state RSS | **~1.82–1.85 GB** | R5 ≤ 2.0 GB | PASS |
| Peak additional RSS | **2.35 GB** | R4 ≤ 3.0 GB | PASS |
| Total benchmark wall time | **2:02** (A+B+C) | — | — |
| CPU | 190 % (user 229 s / wall 122 s) | — | ~1.9 of 4 vCPU |

### Mode B — short-lived isolated worker (`d4b1_worker.py` + `d4b1_lifecycle.py`)

One fresh process **per brief**, concurrency = 1, representative cold-start
samples (5, spread across the dataset), each worker loading the real checkpoint,
running real inference, printing a bounded JSON result, and exiting.

| Measurement | Value |
|---|---|
| Workers OK / exited | **5 / 5**; `worker_pid_gone_after_exit = true` for all |
| Import cost | ~0.07 s |
| Cold-start overhead (lazy load + first forward) | **p50 12.62 s** (12.25–12.96 s) |
| End-to-end latency / brief | **p50 14.32 s** (13.87–14.62 s) |
| Peak process-tree RSS | **max 2.32 GB** (2.05–2.32 GB) |
| **Memory reclaimed after exit** | **`post_exit_tree_rss = 0.0 MB`** — the OS fully reclaimed the worker image; host `MemAvailable` returned to within ~60 MB of pre-run level |
| Swap used during workers | ≤ **457 MB** (unchanged from the pre-run baseline — **no new swap**) |
| Host load during workers | ≤ **1.95** |

**Reclaim proof.** A `Router.unload()` call is *not* offered as proof: the
lifecycle driver re-reads the process tree after reaping the child and confirms
the PID is gone **and** its tree RSS is **0.0 MB**, then confirms host available
memory recovered. This is an OS-level reclaim measurement, not an API claim.

**Mode comparison.** Mode A amortises the ~13–19 s cold start over many briefs
(2.09 s/brief warm) but holds ~1.85 GB resident continuously. Mode B pays the
full ~13 s cold start **per brief** (14.3 s e2e) but holds **zero** steady-state
memory between briefs. For a low-frequency advisory seam, Mode B bounds
steady-state footprint at the cost of latency; neither mode is adopted (see §12).

---

## 8. Peak and steady-state memory

| State | Value | Evidence |
|---|---|---|
| Pre-run host `MemAvailable` | 6.28 GB | `resource_trace.csv` |
| Lowest host `MemAvailable` during Mode A | **3.86 GB** | `resource_trace.csv` |
| Peak additional RSS (Mode A) | **2.35 GB** | `/usr/bin/time -v` `Maximum resident set size` |
| Steady-state RSS (Mode A) | ~1.85 GB | 50 samples |
| Peak worker RSS (Mode B) | 2.32 GB | live `/proc` sampling |
| Memory reclaimed after worker exit (Mode B) | **full (tree RSS 0.0 MB)** | post-reap measurement |

A 2.35 GB peak on a host with ~6.2 GB available leaves ~3.9 GB headroom, enough
for Hermes Trade, OpenViking, and Hermes Website (see §10). It is, however, a
material new footprint, not free.

## 9. CPU and swap impact

| Metric | Value |
|---|---|
| CPU during Mode A | 190 % (~1.9 of 4 vCPU) |
| Host load average (Mode A) | ≤ 1.98 |
| Host load average (Mode B) | ≤ 1.95 |
| Swap **activity** during runs | `vmstat` si/so ≈ 0 |
| Swap **used** during Mode A | 1,590 MB free throughout (no change) |
| Swap **used** during Mode B | ≤ 457 MB (unchanged) |
| Swap **used**, post-benchmark idle | ~680 MB (**+222 MB vs the 458 MB pre-run baseline, not returned**) |

**Honest swap note.** During the runs, swap *activity* stayed idle and swap
*usage* did not grow. After the benchmark the host's swap **residency** had risen
by ~222 MB and did not fully return at idle; `si/so` remains ≈ 0 and the swap
file is only ~⅓ used. This is a small, non-destabilising side effect of the
memory-heavy run and is reported rather than hidden. Services were unaffected
(§10).

## 10. Service stability (R6) — verified

| Service | Before | After |
|---|---|---|
| OpenViking `/health` | `ok`, 0.4.23 | **`ok`, 0.4.23** |
| OpenViking unit | active | **active** |
| Hermes Trade (tmux `trade`) | present | **present, unchanged** |
| Hermes Website (tmux `website`) | present | **present, unchanged** |
| Lingering Laya processes after run | — | **none** |

No service was restarted, reconfigured, or killed to free memory. **Hermes Trade
was never touched.**

---

## 11. Browser QA / Strix capacity policy

The future production runtime must accommodate Hermes Website, Hermes Trade,
OpenViking, FRONTEND execution, Chromium/browser QA, and Strix scanning. No
dedicated Browser-QA or Strix resource-telemetry file exists in the repository,
so **Browser QA and Strix RAM were not measured** and are **not** assumed. The
only observed browser-adjacent process on the host is a leftover `vite preview`
node process (~6 MB) from a prior smoke test.

Conservative runtime policy (recommended, not activated):

* Laya inference **concurrency = 1**;
* **no permanently resident Laya model by default** (Mode B if ever used);
* **no overlapping Laya inference with resource-heavy Browser QA or Strix
  scanning** unless separately qualified — a 2.35 GB Laya peak plus an unmeasured
  Chromium/Strix peak could exhaust the ~3.9 GB headroom;
* preserve RAM headroom for Hermes Trade;
* if headroom is insufficient, or Laya times out / fails to load / exceeds
  limits, use the **deterministic D4b fallback**;
* **never kill Hermes Trade to make room for Laya.**

---

## 12. Threshold evaluation (all applicable frozen gates)

| ID | Metric | Op | Value | Observed | Verdict |
|---|---|---|---|---|---|
| T1 | design_intent macro-F1 (50) | ≥ | 0.50 | 0.639 | **PASS** |
| T1b | design_intent accuracy (50) | ≥ | 0.60 | 0.620 | **PASS** |
| T2 | design_intent macro-F1 (ID 25) | ≥ | 0.40 | 0.579 | **PASS** |
| T3 | design_intent macro-F1 (EN 25) | ≥ | 0.50 | 0.659 | **PASS** |
| T4 | relevant_categories micro-F1 | ≥ | 0.70 | 0.544 | **FAIL** |
| T5 | retrieval_beneficial accuracy | ≥ | 0.80 | 0.200 | **FAIL** |
| T6 | motion_relevant F1 | ≥ | 0.70 | 0.667 | **FAIL** |
| T7 | component_relevant F1 | ≥ | 0.70 | 0.378 | **FAIL** |
| T8 | ambiguous accuracy | ≥ | 0.75 | 0.880 | **PASS** |
| T9 | false-positive retrieval rate | ≤ | 0.15 | 0.000 | **PASS** |
| T10 | false-negative retrieval rate | ≤ | 0.25 | 1.000 | **FAIL** |
| T11 | no material regression vs baselines | ≥ | −0.10 | worst −0.794 | **FAIL** |
| T12 | ≥1 core metric improves | ≥ | 1 | 0 improved | **FAIL** |
| R1 | cold start | ≤ | 180 s | 19.26 s | **PASS** |
| R2 | warm p50 / brief | ≤ | 3.0 s | 2.09 s | **PASS** |
| R3 | warm p95 / brief | ≤ | 8.0 s | 2.25 s | **PASS** |
| R4 | peak additional RSS | ≤ | 3.0 GB | 2.35 GB | **PASS** |
| R5 | steady-state RSS | ≤ | 2.0 GB | 1.85 GB | **PASS** |
| R6 | no service destabilization | == | true | true | **PASS** |
| C1 | additional Laya-path cost | ≤ | $0.00 | $0.00 (local) | **PASS** |
| C2 | paid FAST calls | ≤ | 150 | 0 (comparison not run) | **BLOCKED** |
| S1 | fail-open security regressions | == | 0 | 0 | **PASS** |

No threshold was changed after observing results. T9 passes only because Laya
never asserts retrieval is beneficial (its 0.000 FPR is a by-product of the
inverted decision, **not** a quality signal — T10 fails correspondingly).

### 12.1 T11 / T12 detail (the harness left them BLOCKED; computed here)

* **T11 (no regression ≥ −0.10).** Candidate C vs the better baseline, per shared
  label: `relevant_categories` −0.128, `retrieval_beneficial` −0.794,
  `motion_relevant` −0.157, `component_relevant` −0.240, `ambiguous` −0.211 →
  **FAIL** (four of five exceed the −0.10 allowance).
* **T12 (≥1 core metric improves).** Candidate C `relevant_categories` micro-F1
  0.544 is **below** D4b's 0.672; Candidate C performs no retrieval, so the
  retrieval-relevance arm is not applicable → **FAIL**.

## 13. Remaining paid FAST blockers

* **Candidate A (real FAST-only) is BLOCKED.** The real baseline is the paid
  FAST model (`openrouter/z-ai/glm-5.3-flash`); paid calls were **not**
  authorized, so no FAST call was made. The harness's `deterministic_standin_fast_only`
  is reported separately and is **never** represented as the real FAST baseline.
* **C2 (paid FAST downstream comparison) is BLOCKED.** The comparison of the
  Laya-enriched prompt against the FAST baseline reading that prompt requires
  paid calls (hard cap 150) and remains behind the operator approval gate.
* **Consequence.** The three-way benchmark is **incomplete** (A missing). This
  document does **not** claim a full three-way PASS; it evaluates every gate that
  is reachable **without** paid calls and reports the rest BLOCKED.

---

## 14. Security and regression results

**Regression battery (`tools/d4b_final_proof.py`) — 19/19 checks PASS:**

```
[1/7] focused D4b tests ....................... PASS  77 passed
[2/7] focused D4a/D4a.1 tests ................. PASS  150 passed
[3/7] full offline suite (network BLOCKED) .... PASS  4008 passed, 2 skipped, 4 deselected, 60 subtests
[4/7] D3a.5/D3b regression tests .............. PASS  1029 passed, 1 skipped, 19 subtests
[5/7] D3a.5 + D3b + D4a + D4a.1 mutation drivers PASS 16/16, 73/73, 39/39, 36/36, 18/18, 14/14, 17/17, 9/9 killed
[6/7] D4b mutation driver ..................... PASS  10/10 guards killed
[7/7] guardrails .............................. PASS  6/6
========================================================================
19/19 checks passed
VERDICT: PASS
```

* **D4b.1 benchmark tests:** **29 passed** (`tests/test_d4b1_benchmark.py`).
* **D4b.1 mutation driver:** **6/6 guards killed** (dataset-size, 25/25 split,
  upstream version pin, "refuse without isolated env", 40-char revision pin,
  frozen-thresholds flag).
* **Full offline suite:** **4008 passed**, 2 skipped, 4 deselected, 60 subtests,
  **no failures**. This resume added **no** new test files (only benchmark tooling
  and evidence), so no existing assertion was weakened and no count is attributed
  to new tests.
* **Guardrails confirm:** branch `web-design`; `feature/website` untouched at
  `868ed00e3f24e06f1dcf9944d6d031105dff0646`; no OpenViking/Laya wiring into
  FRONTEND/QA/Design-DNA; no global install / memory-plugin invocation; **Laya
  disabled by default** in `config/default.yaml`.

**Security checklist (all held):**

| Property | Result |
|---|---|
| No credentials exposed | ✔ (result JSONs scanned: secret-free) |
| No global package changes | ✔ (isolated venv only) |
| No cross-project context contamination | ✔ (retrieval scoped to `wb-design`) |
| No prompt-injection promotion | ✔ (references are inert DATA) |
| No unbounded model retries | ✔ (one predict per brief; no retry loop) |
| No unbounded subprocess creation | ✔ (Mode B concurrency = 1, 5 workers) |
| No second FAST orchestration path | ✔ (guardrail) |
| No modification to Hermes Trade | ✔ (tmux unchanged) |
| No production Laya feature activation | ✔ (`laya.enabled: false`) |

**Memory/service stability:** no destabilization; a small persistent swap
residency increase (~222 MB) is reported in §9.

---

## 15. Final recommendation

**Recommendation: D — No production integration** (deterministic D4b remains the
production path).

Reasoning, against the mission's §10 decision rule:

1. Upstream source and checkpoint — **verified**, hashes **exact** (§2–§3). ✔
2. All 50 benchmark cases execute with real inference — **yes** (§4). ✔
3. Primary quality gates — **FAIL** (T4–T7, T10; T11, T12) (§12). ✘
4. No material regression vs the accepted baseline — **FAIL** (T11). ✘
5. Retrieval relevance improves — **no** (Laya performs no retrieval and its
   planning labels are worse) (T12). ✘
6. Resource budget — **PASS** but heavy (~1.85 GB steady / 2.35 GB peak) (§8). ~
7. Cost — **$0** local; paid FAST comparison **BLOCKED** (§13). ~
8. Security — **maintained**; nothing integrated (§14). ✔

**Rejected alternatives.** *A. Short-lived local worker* and *B. Resident local
worker* are both feasible resource-wise, but neither is justified while Laya
fails the quality gates — a heavy new runtime would add cost and risk for no
measured decision-quality gain. *C. External inference host* would remove the RAM
concern but not the quality failure, and would add a network dependency and a
possible paid path. Only **D** follows the evidence.

**Preserved:** the accepted deterministic D4b context preparer, OpenViking,
Hermes Trade, 9router, all D3/D4a/D4b guarantees, the frozen dataset, rubric, and
thresholds. **No permanent Laya runtime** is installed (the isolated venv is
opt-in and removable).

**Optional future path (not started).** Laya's **design_intent** and **ambiguous**
decisions are the only gates it passes and the only labels the deterministic path
does not emit. A narrow, separately-gated future evaluation *could* test Laya as
an **advisory design-intent label only** (no retrieval, no authority), behind the
existing disabled flag, and only after the paid-FAST comparison is authorized.
This is **not** D4c and is **not** begun here.

## 16. Verdict

```
D4B1_BENCHMARK_FAILS_AVAILABLE_GATES
```

The real upstream Laya project was installed and executed on the frozen 50-brief
dataset. It passes the design-intent gates but fails the category/retrieval gates
and the regression/improvement gates against the accepted deterministic baseline;
its `retrieval_beneficial` decision is worse than chance by a phrasing-robust
model property. Resources are safe but heavy. The paid FAST-only baseline and the
paid downstream comparison remain **BLOCKED**. Per the mission, **the
deterministic D4b path is preserved and upstream Laya is not integrated**; no
permanent runtime is installed.

## 17. Limitations (stated plainly)

* **Candidate A is a stand-in**, not the paid FAST model; the three-way
  comparison is incomplete.
* **Laya confidence is uncalibrated**; no probability threshold was trusted.
* **Browser QA and Strix RAM were not measured**; the §11 policy is conservative
  by design, not by measurement.
* **The 0.62 relevance floor was calibrated on English** and is the operative
  cause of the Indonesian empty packs; a multilingual retrieval fix is proposed
  (§6) but **not implemented**.
* **Upstream latency (33 ms) is a GPU figure**; the measured 2.1 s/brief is
  CPU-only and is the relevant number here.
* The prior D4b record's **"18/25 Indonesian"** empty-pack split is superseded by
  the measured **15/25 ID + 7/25 EN** (§6).

## 18. Final proof record

* Resume baseline: `00e1f81c5bc9721a355e3d748975333e0165dfb3`
* `feature/website`: untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646`
* Battery: `tools/d4b_final_proof.py` → **19/19 checks passed, VERDICT: PASS**
* D4b.1 tests: 29 passed; D4b.1 mutation driver: 6/6 guards killed
* Real artifacts: `tools/benchmark/results/d4b1_ABC.json` (corrected run),
  `d4b1_lifecycle.json`, `d4b1_retrieval_probe.json`, `d4b1_ABC_analysis.json`
* Commit SHA: recorded at commit time (see §19)
* Push: `origin/web-design` (fast-forward, no force)

## 19. Commit and push status

Recorded at commit time — see the commit that adds this revision of the file to
`origin/web-design`.
