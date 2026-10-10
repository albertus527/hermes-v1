# D4b.1 — Real Upstream Laya: Benchmark, Integration Gate & Acceptance

Status: engineering record for Batch **D4b.1**.
Branch: `web-design`.
Baseline SHA: `4557ebce83812ca6730e8b44dd3afa9f322ca81b`
(D4b accepted: `D4B_READY_FOR_INTEGRATION`).

This batch corrects the D4b architectural mismatch: the original requirement was
to evaluate the **real upstream Laya project** (`NandhaKishorM/laya`) and its
official multilingual checkpoint, benchmark it, and integrate it **only** if
objective acceptance gates pass. D4b's custom deterministic context preparer is
useful and remains accepted, but it is **not** upstream Laya and is not
presented as such anywhere below.

> **Headline.** The real upstream Laya package and checkpoint were **verified**
> (source, version, revision, hash, license). The benchmark dataset (50 briefs),
> rubric, thresholds and the full non-paid benchmark harness were built and
> **executed** against the accepted D4b deterministic baseline (real D4a
> OpenViking retrieval). The **substantial download** (isolated venv +
> 644 MB checkpoint) and the **paid FAST downstream comparison** both require the
> mandatory operator approval checkpoint, which was **not obtained** (the approval
> form timed out). Per the mission, execution therefore stops safely at
> **`D4B1_AWAITING_APPROVAL`**. No production integration was performed, no
> homegrown engine is substituted for upstream Laya, and no PASS is claimed.

---

## 1. Baseline and preconditions (verified, not assumed)

| Item | Value | Evidence |
|---|---|---|
| Branch | `web-design` | `git rev-parse --abbrev-ref HEAD` |
| Working tree at start | clean | `git status --short` → empty |
| Baseline commit | `4557ebce83812ca6730e8b44dd3afa9f322ca81b` | `git rev-parse HEAD` |
| `feature/website` | untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646` | branch list; not merged |
| D4a.1 verdict | `D4A1_READY_FOR_LAYA` | `docs/D4A1_OPENVIKING_LIVE_ACCEPTANCE.md` §20 |
| D4b verdict | `D4B_READY_FOR_INTEGRATION` | `docs/D4B_LAYA_CONTEXT_PREPARATION_ACCEPTANCE.md` §18 |
| OpenViking `/health` | `{"status":"ok","healthy":true,"version":"0.4.23","auth_mode":"api_key"}` | live `curl` |
| OpenViking unit | `openviking-website.service` → `active`, `enabled` | `systemctl --user` |
| Focused D4b tests | **77 passed** | `pytest tests/test_laya_*.py` |
| Hermes Trade | running (tmux `trade` present) | `tmux ls` |

All prerequisites satisfied. No merge, rebase, or force-push was performed.

---

## 2. Real upstream Laya — verification (part of the mission)

| Field | Value | How verified |
|---|---|---|
| Repository | `https://github.com/NandhaKishorM/laya` | GitHub API |
| License | **Apache-2.0** | GitHub API + `pyproject.toml` |
| Default branch | `main`; tags up to `v0.4.1` | GitHub API `/tags` |
| **Pinned release tag** | **`v0.4.1`** → commit **`1adc59f7e371deb601fcfa18a14e25db238addcc`** | `git clone --branch v0.4.1` |
| PyPI distribution | **`laya==0.4.1`**, `requires_python>=3.10`, Apache-2.0 | PyPI JSON API |
| Public API | `from laya import Router; Router().predict(state, questions)` → typed `choice` / `score` / `noul` decisions + `routing` | source `laya/router.py`, `laya/agent.py` |
| Dependencies | `torch>=2.0.0`, `transformers>=4.48.0`, `safetensors>=0.4.0`, `huggingface_hub>=0.20.0`, `numpy>=1.20.0` | `pyproject.toml` |
| CPU support | `device="cpu"` supported (auto-select order CUDA→MPS→XPU→CPU); CPU runs fp32 | README + `laya/agent.py` |
| Multilingual | `laya-multilingual`, mmBERT-base (322M), 100+ languages | model card |
| Official checkpoint | **`convaiinnovations/laya-multilingual`** | HF API |
| **Pinned checkpoint revision** | **`e4e9ddf21a7b1903b7acffd8814ad4307bf63a67`** | HF API `/revision/…` |
| Weights | `model.safetensors`, **sha256 `9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204`**, **643,835,514 bytes** | HF LFS pointer |
| Checkpoint license | Apache-2.0 | model card |
| Calibration | **uncalibrated** — `temperature: [1.0, 1.0, 1.0]` | `rl_agent_config.json` |
| Supply-chain pinning | package ships `PINNED_REVISIONS` + optional `LAYA_REVISION`/`LAYA_SHA256_DIGESTS` | `laya/revisions.py` |

**Upstream benchmark limitations (documented by upstream, not reproduced by us).**
The model card states the multilingual checkpoint is *weaker on English*
(0.619 vs 0.684 macro), *near chance on typed-decisions zero-shot* (0.342 vs
0.318 random), and *ships uncalibrated* (mean ECE 0.314 → 0.106 only after
refitting one temperature per question type). Upstream latency (33 ms) is a
**T4-GPU** figure; it is explicitly **not** assumed to reproduce on this CPU-only
VPS. None of these upstream numbers were used as ground truth or as thresholds.

**Conclusion of verification:** upstream Laya is a real, Apache-2.0,
pip-installable, CPU-capable multilingual typed-decision engine with a public
`Router.predict` API and a real published checkpoint. An exact released version
(`laya==0.4.1`) and an immutable source revision (`v0.4.1`) plus an immutable
checkpoint revision are pinned. **No GitHub-`main` install is used.**

---

## 3. VPS resource preflight (measured)

| Metric | Value |
|---|---|
| CPU | 4 vCPU (AMD EPYC 7B12); load avg 0.59 / 0.36 / 0.16 |
| RAM total / available | 7,933 MB / ~6,247 MB |
| Swap total / used | 2,047 MB / 392 MB; `vmstat` si/so ≈ 0 (idle) |
| Disk free | 65 GB (96 GB, 34% used) |
| Hermes Website RSS | ~648 MB (`hermes`) |
| OpenViking RSS | ~294 MB (settled ≈435 MB per D4a.1) |
| Hermes Trade | tmux `trade` present; gateway up |
| Existing 9router | up (embedding/VLM provider path) |

**Incremental estimate for the upstream Laya benchmark (approval-gated):**

| Item | Estimate |
|---|---|
| Isolated venv (torch CPU + transformers + deps + laya) | ≈ 2.5 GB disk |
| Checkpoint download (multilingual only) | 644 MB disk (643,835,514 B) |
| Peak additional RSS | ≈ 2.5 GB (322M fp32 weights ≈1.3 GB + torch runtime + activations) |
| Steady-state RSS | ≈ 1.6 GB (`max_loaded=1`) |
| Cold start | ≈ 30–90 s (checkpoint load + first forward pass) |
| Warm inference | ≈ 1–3 s/brief on 4 vCPU |
| Financial | **$0** (local inference) |

**Isolation plan.** Dedicated venv `~/.website-builder/laya/venv`; dedicated HF
cache `~/.website-builder/laya/models`; no global packages, no systemd unit, no
PATH change, no modification to the WB venv or Hermes Trade's environment.
**Rollback:** delete those two directories (`d4b1_laya_setup.sh --uninstall`).

---

## 4. Mandatory approval checkpoint — outcome

Before the substantial download and before any paid call, the operator was
presented (one form, two questions):

1. **Isolated env + 644 MB checkpoint download** — disk ~3.1 GB, RAM ~2.5 GB
   peak / ~1.6 GB steady, $0, reversible.
2. **Paid FAST downstream comparison** — hard cap 150 calls, or skip and mark
   BLOCKED.

**Outcome: the approval form timed out with no response.** Per the mission's
mandatory checkpoint, approval is **unavailable**, so the disciplined action is
to **not** download and **not** spend. Execution pauses at
**`D4B1_AWAITING_APPROVAL`**. All non-gated work (dataset, rubric, thresholds,
harness, real deterministic baseline, regression battery) was completed so that
resumption is a single command.

---

## 5. Benchmark dataset and ground-truth methodology

Frozen dataset: `tools/benchmark/d4b1_dataset.json`
(sha256 `9da94ae5fe609238a7e0ec28a8e2053ac3580594c516985f50af829fbbfd42cd`).

* **Exactly 50 briefs**: 25 Indonesian + 25 English, arranged as **25 paired
  scenarios** (one ID + one EN per scenario) so language is not a confounding
  factor.
* Coverage: editorial, corporate, portfolio, SaaS landing, florist/botanical,
  restaurant, minimalist, motion-heavy, component-rich, ambiguous,
  existing-project revision, conflicting instructions, in-scope vs out-of-scope,
  and adversarial instructions.
* **Ground truth is authored from a documented rubric** in the dataset file
  (`rubric` key), **before** any candidate was run. Labels: design intent,
  relevant OpenViking categories, retrieval beneficial, motion relevant,
  component relevant, ambiguous, clarification required (plus a coarse scope
  class for coverage).
* **No candidate output was used as ground truth**, and **no candidate ever
  receives a label** — candidates see only the brief text. This is guarded by
  tests (`test_candidate_a_never_reads_ground_truth`,
  `test_dataset_declares_label_leakage_control`).
* Ambiguity is marked explicitly (6/50) rather than manufactured into certainty.

Dataset distribution: design intent `{editorial:2, corporate:4, portfolio:4,
saas_landing:4, botanical:4, restaurant:8, minimalist:4, motion_creative:4,
component_rich:4, other:12}`; scope `{in_scope:40, revision:4, out_of_scope:2,
adversarial:4}`; retrieval-beneficial true 40/50; motion true 8/50; component
true 30/50; ambiguous 6/50.

---

## 6. Predeclared acceptance thresholds

Frozen plan: `tools/benchmark/d4b1_thresholds.json`
(sha256 `dfa932b4810f79629bf3b586883aa8be22a10fbb37b78e8b26493a02ccc51965`),
written **before** observing any candidate result (`frozen_before_testing: true`).
Every numeric limit carries its rationale. Highlights:

| ID | Metric | Op | Value |
|---|---|---|---|
| T1 | design_intent macro-F1 (all 50) | ≥ | 0.50 |
| T1b | design_intent accuracy | ≥ | 0.60 |
| T2 | design_intent macro-F1 (Indonesian 25) | ≥ | 0.40 |
| T3 | design_intent macro-F1 (English 25) | ≥ | 0.50 |
| T4 | relevant_categories micro-F1 | ≥ | 0.70 |
| T5 | retrieval_beneficial accuracy | ≥ | 0.80 |
| T6 | motion_relevant F1 | ≥ | 0.70 |
| T7 | component_relevant F1 | ≥ | 0.70 |
| T8 | ambiguous accuracy | ≥ | 0.75 |
| T9 | false-positive retrieval rate | ≤ | 0.15 |
| T10 | false-negative retrieval rate | ≤ | 0.25 |
| T11 | no material regression vs baselines | ≥ | −0.10 |
| T12 | ≥1 core metric improves | ≥ | 1 |
| R1 | cold start | ≤ | 180 s |
| R2 | warm p50 / brief | ≤ | 3.0 s |
| R3 | warm p95 / brief | ≤ | 8.0 s |
| R4 | peak additional RSS | ≤ | 3.0 GB |
| R5 | steady-state RSS | ≤ | 2.0 GB |
| R6 | no service destabilization | == | true |
| C1 | additional Laya-path cost | ≤ | $0.00 |
| C2 | paid FAST calls | ≤ | 150 (approval-gated) |
| S1 | fail-open security regressions | == | 0 |

With n=50, all headline metrics are reported with bootstrap 95% CIs and paired
McNemar tests; a CI straddling zero is **inconclusive**, never an improvement.
No threshold was changed after testing.

---

## 7. FAST-only benchmark (Candidate A)

The **real** FAST baseline is the paid Hermes FAST model
(`openrouter/z-ai/glm-5.3-flash`). Without paid approval it cannot be run, so the
harness runs a **documented deterministic stand-in** that mirrors the FAST
prompt's documented contract (scope vocabulary + NAME/WHAT/WHY readiness). It is
labelled `deterministic_standin_fast_only`, never sees ground truth, and its
scope-class perfect score is a **stand-in artifact** — the real FAST baseline is
**BLOCKED**.

| Label | Accuracy | 95% CI |
|---|---|---|
| scope_class (stand-in) | 1.000 | [1.000, 1.000] |
| ambiguous | 0.400 | [0.260, 0.540] |
| clarification_required | 0.840 | [0.740, 0.940] |

**Status: real FAST-only baseline BLOCKED (paid calls not approved).** No FAST
outcome is fabricated.

---

## 8. Deterministic D4b benchmark (Candidate B) — REAL, executed

This is the **real accepted D4b code** (`app/core/laya_context.py`) driving the
**real D4a OpenViking adapter** against the live server (`127.0.0.1:1933`,
OpenViking 0.4.23), on all 50 briefs. No model call. Read-only retrieval.

| Label | Metric | All 50 | Indonesian | English |
|---|---|---|---|---|
| relevant_categories | micro-F1 | 0.672 | 0.526 | 0.779 |
| relevant_categories | exact-match | 0.440 | — | — |
| retrieval_beneficial | accuracy | 0.720 | 0.600 | 0.840 |
| motion_relevant | accuracy / F1 | 0.940 / 0.824 | 0.960 | 0.920 |
| component_relevant | accuracy / F1 | 0.580 / 0.618 | 0.520 | 0.640 |

Per-class category F1: `design_dna 0.719`, `components 0.618`, `motion 0.667`.

Retrieval FP/FN: `retrieval_beneficial` FPR 0.100, FNR 0.325;
`motion_relevant` FPR 0.048, FNR 0.125; `component_relevant` FPR 0.400, FNR 0.433.

**Key honest finding — the deterministic floor is English-biased.**
Candidate B returned an **honest-empty** pack (0 items) for **22/50** briefs, and
**18 of those 22 were Indonesian**. The corpus is retrieved through
`openrouter/text-embedding-3-small` with an operational relevance floor of
0.62, and Indonesian design briefs systematically fall below it. So the real D4b
context layer today **fails to retrieve references for most Indonesian briefs** —
the single most important measured fact this benchmark surfaces. (Category
"precision" is high where it does retrieve — e.g. `design_dna` precision 0.958 —
because it rarely over-claims; its recall is what suffers.)

Resources (real, in-process): RSS ≈ 47–55 MB; host available dropped ≈ 128 MB
during the run; retrieval latency p50 **1.82 s**, p95 **5.03 s**, max 24.3 s
(a few long briefs + 3 embedding round-trips each). Additional model calls: 0.
Additional cost: **$0**.

---

## 9. Real upstream Laya benchmark (Candidate C) — NOT RUN (approval-gated)

The real upstream candidate is **implemented and ready** but was **not executed**
because its isolated environment + checkpoint download were not approved.

* Candidate C is built on the **real upstream API**:
  `laya.Router(models={"multilingual": …}, revision=<pinned>, device="cpu",
  max_loaded=1)` then `Router.predict(brief, questions, model="multilingual")`,
  using the documented typed questions `choice`/`score`/`noul`.
* `build_candidate_c()` **refuses to run** without the isolated environment (it
  imports the real `laya` package and checks `laya.__version__ == "0.4.1"`); it
  **never** falls back to a heuristic. A missing environment is reported as
  `UNAVAILABLE`, never silently downgraded to a stand-in.
* The harness records raw typed decisions, confidence, probabilities, routing
  metadata, per-brief latency, and cold/warm performance separately.

**Status: `NOT_RUN` / `UNAVAILABLE` — approval-gated.** No simulated value is
presented as upstream inference.

---

## 10. Three-way comparison (as far as it can go without approval)

| Dimension | A. FAST-only | B. D4b deterministic | C. Upstream Laya |
|---|---|---|---|
| design intent | not emitted | not emitted | **not run** |
| relevant categories | not emitted | micro-F1 0.672 | **not run** |
| retrieval beneficial | not emitted | acc 0.720 | **not run** |
| motion relevant | not emitted | acc 0.940 / F1 0.824 | **not run** |
| component relevant | not emitted | acc 0.580 / F1 0.618 | **not run** |
| ambiguity | acc 0.400 (stand-in) | not emitted | **not run** |
| scope | acc 1.000 (stand-in) | not emitted | **not run** |

The three candidates intentionally emit **different subsets** of the shared label
layer, so raw schemas are **not** compared directly; comparison is at the shared
normalized-label layer, and a candidate is scored only on labels it emits
(coverage is reported). The comparison is **incomplete without Candidate C**:
the design-intent and Indonesian/English thresholds (T1–T3) and the improvement
test (T12) can only be evaluated once real upstream Laya runs. **No integration
decision can be finalized yet.**

---

## 11. Resource, latency, and cost assessment

| Metric | Candidate B (real) | Candidate C (projected, unmeasured) |
|---|---|---|
| Additional model calls | 0 | 0 (local) |
| Additional API cost | $0 | $0 |
| RSS | ~54 MB | ~1.6 GB steady / ~2.5 GB peak |
| Latency | p50 1.82 s, p95 5.03 s | ~1–3 s warm (projected) |
| Cold start | n/a | ~30–90 s (projected) |
| Service impact | none; OpenViking healthy throughout | to be measured |

Candidate C's resource/latency/cost numbers are **projections, not measurements**
and are labelled as such. They must be replaced by real measurements before any
threshold R1–R6 / C1 can be marked PASS.

---

## 12. Integration decision and justification

**Decision: no integration. Stop at the approval boundary.**

Reasoning, against the §8 gate:

1. Official upstream source and checkpoint — **verified** (§2). ✔
2. All 50 benchmark cases execute — **only Candidate B executed**; C did not. ✘
3. Primary quality threshold (T1) — **cannot be evaluated** (needs C). ✘
4. Indonesian minimum (T2) — **cannot be evaluated**. ✘
5. No regression vs baselines — **cannot be evaluated** (needs C). ✘
6. Retrieval relevance improves meaningfully — **unknown**; the measured D4b
   baseline is itself weak on Indonesian. ✘
7. Peak/steady RAM within budget — **unmeasured for C**. ✘
8. CPU stability — **unmeasured for C**. ✘
9. Latency within limits — **unmeasured for C**. ✘
10. Cost within limits — **$0 local projected**, unconfirmed. ~
11. Security maintained — existing guarantees **unchanged** (no integration). ✔

Because the benchmark **cannot be completed** without the approval-gated
download (and the paid FAST comparison), the outcome is **BLOCKED**, not PASS and
not FAIL. The accepted D4b architecture is preserved unchanged. Upstream Laya is
**not** integrated, and — critically — the custom deterministic planner is
**not** presented as upstream Laya.

---

## 13. Security and regression results

**Security (unchanged, since nothing was integrated):** the D4b fail-closed
invariants remain in force (isolation, provenance, credential, category); the
OpenViking feature flag stays disabled by default; the Laya advisory flag stays
disabled by default; no second FAST orchestration path; no global memory plugin;
no modification to Hermes Trade. The upstream candidate is **inert** until the
isolated environment exists, and it refuses to run without it.

**Regression battery (executed on the working tree):**

* New D4b.1 benchmark tests: **29 passed** (`tests/test_d4b1_benchmark.py`).
* Focused D4b + D4a/D4a.1 tests: **253 passed**
  (`test_laya_*`, `test_openviking_*`, `test_d4b1_benchmark`).
* Full battery: **19/19 checks passed** — full offline suite **4005 passed**,
  2 skipped, 4 deselected, 60 subtests; D3a.5/D3b regressions **1029 passed**;
  every mutation driver (D3a.5 16/73/39/36/18, D3b 14/14, D4a 17/17,
  D4a.1 9/9, D4b 10/10) green; guardrails 6/6 (§14).

**New mutation guards (`tools/mutation_check_d4b1.py`): all 6 killed** by the
focused D4b.1 tests — dataset-size guard, 25/25 language-split guard, upstream
version pin, "refuse without isolated env", 40-char revision pin, frozen-
thresholds flag. No existing assertion was weakened.

**Offline/CI safety:** the new tests never download a checkpoint and never call a
paid model; the real-checkpoint path is isolated behind the approval-gated venv
and is explicitly reported as `UNAVAILABLE`/`NOT_RUN` when absent.

---

## 14. Final proof record

```
$ ./.venv/bin/python tools/d4b_final_proof.py
========================================================================
D4b FINAL PROOF
========================================================================
[1/7] focused D4b tests ...                       PASS  77 passed
[2/7] focused D4a/D4a.1 tests ...                 PASS  150 passed
[3/7] full offline suite (network BLOCKED) ...    PASS  4005 passed, 2 skipped, 4 deselected, 60 subtests
[4/7] D3a.5/D3b regression tests ...              PASS  1029 passed, 1 skipped, 19 subtests
[5/7] D3a.5 + D3b + D4a + D4a.1 mutation drivers  PASS  16/16, 73/73, 39/39, 36/36, 18/18, 14/14, 17/17, 9/9 killed
[6/7] D4b mutation driver ...                     PASS  10/10 guards killed
[7/7] guardrails ...                              PASS  6/6
========================================================================
19/19 checks passed
VERDICT: PASS
========================================================================
```

The guardrails confirm the architectural boundary held: `feature/website`
untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646`; **no OpenViking/Laya
wiring into FRONTEND/QA/Design-DNA**; no global install / memory-plugin
invocation; Laya still disabled by default. The full offline suite grew to
**4005 passed** (D4b baseline 3979), and no existing assertion was weakened.

---

## 15. Limitations (stated plainly)

* **Upstream Laya was never executed.** The real inference numbers do not exist
  yet; every Candidate-C figure is projected and labelled.
* **The FAST-only baseline is a stand-in**, not the paid model.
* **The paid FAST downstream comparison is BLOCKED** (no approval).
* **Candidate B's Indonesian retrieval is weak** (honest-empty for most ID
  briefs) — a real property of the accepted system, not a benchmark artifact.
* **Upstream confidence is uncalibrated** by the vendor's own admission, so any
  future probability threshold must be validated before it is trusted.
* Latency for Candidate C is projected from CPU hardware, **not** the upstream
  T4-GPU figure.

---

## 16. Final verdict

```
D4B1_AWAITING_APPROVAL
```

The mandatory approval checkpoint (§3) was not obtained, so execution paused
safely before the substantial download and before any paid call. All non-gated
work is complete and committed: upstream verification, the frozen 50-brief
dataset and rubric, the frozen thresholds, the executed real D4b deterministic
baseline, the new benchmark tests and mutation guards, the regression battery,
the approval-gated setup/verify tooling, and this acceptance record. **No
production integration was performed; the deterministic planner is not
represented as upstream Laya.**

To resume, the operator grants the two approvals in §4; then:

```bash
bash tools/benchmark/d4b1_laya_setup.sh
~/.website-builder/laya/venv/bin/python tools/benchmark/d4b1_benchmark.py --candidates A,B,C
./.venv/bin/python tools/benchmark/d4b1_analyze.py --in tools/benchmark/results/d4b1_results.json
```

and the gate in §12 is evaluated with real Candidate-C data.

* Baseline: `4557ebce83812ca6730e8b44dd3afa9f322ca81b`
* `feature/website`: untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646`
* Commit SHA: recorded at commit time (see §17).
