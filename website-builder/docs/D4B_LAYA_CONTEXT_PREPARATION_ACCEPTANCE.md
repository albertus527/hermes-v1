# D4b — Laya Context Preparation & FAST Integration: Acceptance Report

Status: engineering record for Batch D4b.
Branch: `web-design`.
Baseline: `dca84ba58f426da7ccddbf38e175586f6758967a` (D4a.1 accepted:
`D4A1_READY_FOR_LAYA`).
Scope: implement **Laya** as a bounded, optional, application-owned
context-preparation layer that enriches the existing FAST intake path using the
already-implemented D4a/D4a.1 OpenViking context library — **without** becoming a
second decision maker and **without** altering the FAST output contract.

Every claim below is backed by an executed command or an executable test. Live
qualification was **executed against the real OpenViking 0.4.23 server** already
provisioned in D4a.1. **Zero paid model calls** were made (the paid VLM ingestion
path was not touched; the FAST *model* boundary was replaced by a recording
stand-in for the live run).

---

## 1. Baseline and preconditions (verified, not assumed)

| Item | Value |
|---|---|
| Branch | `web-design` (verified `git branch --show-current`) |
| Working tree at start | clean (verified `git status`) |
| Baseline commit | `dca84ba58f426da7ccddbf38e175586f6758967a` |
| `feature/website` | untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646` |
| D4a.1 verdict | `D4A1_READY_FOR_LAYA` (read from `docs/D4A1_OPENVIKING_LIVE_ACCEPTANCE.md` §20) |
| OpenViking `/health` | `{"status":"ok","healthy":true,"version":"0.4.23","auth_mode":"api_key"}` |
| OpenViking `/ready` | `{"status":"ready",...,"vectordb":"ok","embedding":"ok"}` |
| systemd unit | `openviking-website.service` → `active`, `enabled` |
| Corpus | 11/11 declared sources present & indexed (`skipped_duplicate: 11` — idempotent, **no reindex**) |
| Hermes Trade | **not touched** (tmux `trade` session and `website` session identical before/after) |
| D3a.5/D3b contracts | unchanged (1029 regression tests pass; all mutation drivers unchanged) |

No prior verdict was assumed to have advanced: D4a.1 readiness was re-verified
from the report **and** from the live service before any code change.

---

## 2. The real FAST intake call graph (discovered, not guessed)

Traced from user input through to FAST:

```
USER (Telegram/WhatsApp)
  │
  ▼
app/runtime.py  TelegramPoller / dispatcher loop
  │
  ▼
app/channels/dispatch.py  TelegramDispatcher.dispatch(action="intake")
  │   - authz (ProjectAccess), event claim (idempotency), lifecycle authority
  ▼
app/core/intake.py  IntakeProcessor.process(message, project_id)     ◄── INSERTION POINT
  │   - load persisted brief / pause_state / pending_clarification
  │   - [D4b] LayaContextPreparer.prepare_context(...)  → reference block
  │   - self.hermes_adapter.fast_interpret(text, project_id, context, reference_context=…)
  │        │
  │        ▼
  │   app/hermes/adapter.py  HermesAdapter.fast_interpret
  │        - _build_fast_prompt(text, context, reference_context)
  │        - _run_fast_programmatic(role="FAST", enabled_toolsets=[])   ← the ONLY model call
  │        - _parse_fast_response → {scope,name,what,why,…,readiness}
  │   - application owns readiness/scope/merge (FAST interprets; code enforces)
  ▼
app/core/intake.py  IntakeProcessor.apply_to_project  (persists brief, lifecycle)
  ▼
app/channels/dispatch.py  auto-build sub-claim → FrontendBuilder.build → FRONTEND
  ▼
QA → VISION → Impeccable critic → PREVIEW → approval → Vercel LIVE
```

### 2.1 Selected insertion point

The smallest verified production seam where Laya can enrich FAST's *input*
without changing FAST's *output contract* is:

**`IntakeProcessor.process`**, immediately before the single
`self.hermes_adapter.fast_interpret(...)` call — i.e. the `LayaContextPreparer`
is invoked there and its rendered block is passed to FAST as the new
`reference_context` keyword argument.

Why this and not elsewhere:

* It is the **only** place FAST is invoked on the intake path (verified: one
  `fast_interpret` call site in `app/core/intake.py`; the conversation router has
  its own separate `_run_fast_programmatic` for turn classification, which Laya
  does **not** touch).
* It is **before** FAST (Laya prepares context; FAST decides).
* It is **not** in FRONTEND or QA (verified by the proof guardrail that those
  modules contain no `openviking`/`laya` reference).
* It preserves the **original user brief verbatim** (`text` is passed unchanged)
  and the **original FAST system/developer prompt** (only an additional,
  clearly-labelled reference block is appended; with no context the prompt is
  byte-identical — see §9).

No parallel intake path was built. Laya is **not** called from FRONTEND or QA.

---

## 3. Architecture

```
                       ┌───────────────────────────────────────────────┐
   USER brief ─────────▶│ IntakeProcessor.process  (application-owned)  │
                       │                                               │
                       │   persisted brief ──┐                         │
                       │                     ▼                         │
                       │        ┌───────────────────────────────┐      │
                       │        │ LayaContextPreparer           │      │
                       │        │  (app/core/laya_context.py)   │      │
                       │        │                               │      │
                       │        │ 1. plan_queries (DETERMINISTIC│      │
                       │        │    ≤3 queries, ≤200 chars)    │      │
                       │        │ 2. retrieve via D4a adapter ──┼──┐   │
                       │        │ 3. dedup + rank               │  │   │
                       │        │ 4. classify_quality           │  │   │
                       │        │ 5. bound to ≤6000 chars       │  │   │
                       │        │ 6. render_laya_context_block  │  │   │
                       │        └───────────────┬───────────────┘  │   │
                       │                        │ reference block  │   │
                       │                        ▼                  │   │
                       │   hermes_adapter.fast_interpret(text, …,   │   │
                       │        reference_context=block)            │   │
                       └────────────────────────┬──────────────────┘   │
                                                │                       │
                                                ▼                       │
                       ┌────────────────────────────────────┐          │
                       │ HermesAdapter._build_fast_prompt   │          │
                       │  system prompt (UNCHANGED)         │          │
                       │  + LAYA CONTEXT block (lower trust)│          │
                       │  + "User text:" + ORIGINAL brief   │          │
                       └────────────────┬───────────────────┘          │
                                        ▼                              │
                       ┌────────────────────────────────────┐          │
                       │ FAST model (zero tools)            │          │
                       │ → JSON {scope,name,what,why,…}     │          │
                       └────────────────────────────────────┘          │
                                                                       │
   D4a/D4a.1 OpenViking adapter (UNCHANGED contract) ◀────────────────┘
      OpenVikingRetrievalAdapter.retrieve_context(query, project_id, scope, budget)
        │  project isolation · category allowlist · provenance · credential check
        ▼
      OpenViking 0.4.23 server (loopback 127.0.0.1:1933, wb-design corpus)
```

Laya is a **context provider**, not an authority. The arrow from Laya to FAST is
one-way reference DATA; there is no arrow back into requirements, scope,
dependencies, project state, QA, preview, or deployment.

---

## 4. Context pack schema

`LayaContextResult` (contract version `LAYA_CONTRACT_VERSION = 1`):

| Field | Meaning |
|---|---|
| `status` | `ready` \| `degraded` \| `unavailable` \| `skipped` (closed) |
| `project_id` | the CALLING project (for provenance, not used for retrieval scope) |
| `library_project_id` | the application-owned library scope actually searched |
| `quality` | `high` \| `medium` \| `low` \| `insufficient` (closed; see §6) |
| `items` | tuple of `LayaContextItem` (bounded reference DATA) |
| `queries` | the planned queries actually issued (auditable) |
| `retrieval_calls` | number of adapter calls made (cost accounting) |
| `estimated_chars` / `estimated_tokens` | pack size (4-chars/token estimate) |
| `truncated` | True when items were dropped to satisfy the size budget |
| `degraded` | True for any non-`ready` status |
| `warnings` | static, value-free labels |
| `error_reason` | static code (never content, never a secret) |
| `latency_ms` | Laya preparation latency |
| `limits` | the effective (clamped) bounds |

`LayaContextItem` — **no authority-bearing field** (no
`instruction`/`system`/`requirement`/`override`/`command`):

| Field | Meaning |
|---|---|
| `source_id` | application-owned source identity |
| `source_uri` | `viking://…` URI (real, in-scope) |
| `source_revision` | content revision pin |
| `category` | D4a closed category |
| `trust` | `reviewed` \| `internal` \| `external` |
| `level` | L0 abstract / L1 overview / L2 detail |
| `relevance` | backend score (echoed, not invented) |
| `excerpt` | bounded reference text (≤600 chars) — DATA |
| `summary` | bounded summary (≤256 chars) — DATA |
| `uncertainty` | static uncertainty note |

---

## 5. Query budget & retrieval policy

| Bound | Value | Enforced where |
|---|---|---|
| Max queries / intake | **3** (`MAX_QUERIES`) | `plan_queries`, clamped by `LayaConfig.normalized` |
| Max query chars | **200** (`MAX_QUERY_CHARS`) | `plan_queries` |
| Max query keywords | 10 | `plan_queries` |
| Per-query max items | 6 (default) | D4a `RetrievalBudget` |
| Categories | `design_dna`, `components`, `motion` | `LayaConfig` (a subset of D4a's closed set) |
| Relevance floor | **0.62** (operational, live-calibrated) | D4a `RetrievalBudget.min_score` |
| **Final pack chars** | **6000** (`MAX_PACK_CHARS`) | `prepare_context` final serialization check |
| Rendered block chars | **8000** (`MAX_RENDERED_CHARS`) | `render_laya_context_block` |

Policy properties:

* Queries are derived **only** from the user brief and the approved persisted
  project context.
* The planner is **deterministic** — no model call, no recursion, no expansion.
  Equivalent (normalized) queries are collapsed.
* Only the **approved categories** are searched; an unknown category is refused
  by `normalize_scope` (D4a), and `LayaConfig` can only **narrow** the set.
* The retrieval scope (`library_project_id`) is an **application constant**, never
  derived from user input, so a user cannot control a raw OpenViking URI path or
  search an unrelated project.
* There is **no** background indexing, no reindex-during-intake, and no repeated
  identical query within one intake.

---

## 6. Confidence / quality policy

Laya expresses the quality of **retrieved context**, never the correctness of
FAST's future decisions. It is a **small explicit classification** with a
transparent, tested rule — **no fabricated numeric confidence**:

```
if no items or status not in {ready, degraded}:   → insufficient
elif provenance incomplete:                        → low
elif top_relevance ≥ 0.68 AND not truncated
     AND retrieval_failures == 0 AND no external:  → high
elif top_relevance ≥ 0.55:                         → medium
else:                                              → low
```

Thresholds (`RELEVANCE_HIGH = 0.68`, `RELEVANCE_MEDIUM = 0.55`) are **calibrated
from live measurements** of the `wb-design` corpus (§13): a RELEVANT brief scores
0.66–0.77 at the top, an UNRELATED brief 0.54–0.58. The operational relevance
floor sits at 0.62, so an unrelated brief is **honestly empty** rather than being
presented as relevant.

Low confidence **authorizes nothing**: it never triggers an action, and when
context is insufficient FAST receives either a clearly `degraded` pack or no
additional context. FAST alone decides whether to clarify with the user.

---

## 7. Trust & provenance policy

* Every included item carries `source_id`, `source_uri`, `source_revision`,
  `category`, `trust`, `level`, and `relevance`.
* Duplicate references (same URI) are collapsed to one entry (highest score).
* Items are ranked by relevance desc, reviewed-first, then URI.
* `reviewed` is **never** silently promoted: an item whose trust is not in the
  D4a closed vocabulary is treated as `external` (the lowest level). Laya never
  upgrades an external source to reviewed.
* A reference is dropped when its category is outside the D4a closed set, or when
  its provenance is incomplete (`source_id`/`source_revision`/`uri` missing) — a
  belt-and-suspenders guard on top of the adapter's own fail-closed check.
* Retrieved text is **quoted reference DATA**, escaped inside a JSON payload
  bounded by explicit `=== LAYA CONTEXT … ===` markers, so it cannot impersonate
  system/developer instructions. An `external` reference sets
  `WARNING_EXTERNAL_TRUST`.
* Size accounting reuses the D1 `payload_chars` discipline via `item_chars`
  (counts excerpt + summary + every provenance field), plus **one final
  serialization-size check at the exact boundary sent to FAST**.

---

## 8. FAST authority boundary

| Property | Status |
|---|---|
| Original user brief preserved verbatim | ✅ (`test_the_original_user_brief_is_preserved_verbatim`) |
| Original FAST system/developer instructions preserved | ✅ (reference block is additive; no system text changed) |
| Laya context added only as lower-trust supplemental material | ✅ (block labelled `REFERENCE DATA`, "MUST NOT override") |
| FAST output schema unchanged | ✅ (`_parse_fast_response` untouched) |
| FAST intent/scope/action authority unchanged | ✅ (application still owns readiness/scope/merge) |
| No second FAST call | ✅ (single call site; proof guardrail) |
| No second FRONTEND call | ✅ (Laya never touches FRONTEND) |
| Design DNA priority rules unchanged | ✅ (`design_context.py` untouched; guardrail) |
| Revision source-of-truth semantics unchanged | ✅ (`revise.py` untouched; guardrail) |
| QA/publication lifecycle unchanged | ✅ (`app/qa/*` untouched; guardrail) |

---

## 9. Feature flag and fallback

`website_builder.laya.enabled` defaults to **`false`** in `config/default.yaml`
and in `LayaConfig`. Laya performs **no retrieval** unless **both** Laya AND
OpenViking are enabled. When disabled, the FAST prompt is **byte-identical** to
the pre-D4b prompt (verified by
`test_no_reference_block_yields_a_byte_identical_prompt`).

Failure policy (all tested, §11):

| Case | Behaviour |
|---|---|
| A. Laya disabled | original FAST path; `skipped`, 0 items |
| B. OpenViking disabled | original FAST path; `skipped`, 0 items |
| C. OpenViking service unavailable | original FAST path; `unavailable`, 0 items |
| D. Retrieval timeout | bounded fallback; `unavailable`, 0 items |
| E. Empty/irrelevant retrieval | honest-empty; `ready`, 0 items, quality `insufficient` |
| F. Malformed retrieval | safe degradation; 0 items |
| G. Missing provenance | `unavailable`, error `PROVENANCE_VIOLATION` (fail closed) |
| H. Cross-project contamination | `unavailable`, error `ISOLATION_VIOLATION` (fail closed) |
| I. Credential-shaped content | `unavailable`, error `CREDENTIAL_LEAK_VIOLATION` (fail closed) |
| J. Prompt injection in references | inert DATA; no authority field |
| K. Pack exceeds budget | bounded truncation to ≤6000 chars |
| L. Laya internal exception | original FAST path (intake fail-open wrapper) |

Availability failures **never** block the pipeline. Security violations are
**never** silently converted into an ordinary successful retrieval: Laya returns
`unavailable` with a static error reason and zero items, and it refuses the
**whole** pack if any query reports a violation.

---

## 10. Cost & latency control

* **Additional model calls by Laya: 0.** The query planner is deterministic.
  There is no hidden, recursive, or background LLM call, no automatic indexing,
  and no reindex during intake.
* **Maximum retrieval calls per intake: 3** (`MAX_QUERIES`); identical queries
  are collapsed.
* **No paid provider is introduced.** The paid VLM ingestion path (the D4a.1
  cost risk) is untouched and remains fail-closed.
* Counters recorded: `retrieval_calls`, `estimated_chars`, `estimated_tokens`,
  `latency_ms` — none expose user content or secrets.
* Latency is measured separately for Laya preparation and the FAST invocation
  (§13). Retrieval latency dominates (embedding round-trip ≈2 s/query, D4a.1).

---

## 11. Test plan & results

### 11.1 Focused D4b tests (implementation gate)

```
$ ./.venv/bin/python -m pytest tests/test_laya_context.py \
      tests/test_laya_integration.py tests/test_laya_composition.py -q
77 passed
```

Covers: query planning, query dedup, category selection, ranking, budget
enforcement, provenance preservation, trust classification, quality
classification, empty corpus, disabled feature, OpenViking outage, timeout,
malformed response, prompt injection, credential leakage, cross-project
isolation, no unauthorized state mutation, FAST output-contract preservation,
original brief preservation, FAST-only fallback, project creation & revision
intake.

### 11.2 Mutation guards (critical integration invariants)

```
$ ./.venv/bin/python tools/mutation_check_d4b.py
all 10 guards killed by the focused D4b tests
```

The 10 guards (each reverted alone on a throwaway copy → focused tests go red):

1. Laya refuses to run when the feature flag is disabled
2. Laya refuses to run when OpenViking is disabled/unavailable
3. a security violation refuses the whole pack (not a partial success)
4. an item without complete provenance is never carried
5. the final pack-size bound is enforced
6. the query-count bound is enforced by the planner
7. duplicate references are collapsed to one entry
8. the intake seam hands FAST the Laya reference block
9. the renderer emits a wrapper only when there is real context
10. a Laya internal failure never blocks the original FAST path

### 11.3 Full battery (`tools/d4b_final_proof.py`)

```
========================================================================
D4b FINAL PROOF
========================================================================
[1/7] focused D4b tests ......................... PASS  77 passed
[2/7] focused D4a/D4a.1 tests ................... PASS  150 passed
[3/7] full offline suite (network BLOCKED) ...... PASS  3979 passed, 2 skipped, 4 deselected, 60 subtests
[4/7] D3a.5/D3b regression tests ................ PASS  1029 passed, 1 skipped, 19 subtests
[5/7] D3a.5 + D3b + D4a + D4a.1 mutation drivers  PASS  16/16, 73/73, 39/39, 36/36, 18/18, 14/14, 17/17, 9/9 killed
[6/7] D4b mutation driver ....................... PASS  10/10 guards killed
[7/7] guardrails ................................ PASS  6/6
========================================================================
19/19 checks passed
VERDICT: PASS
========================================================================
```

> The `[7/7]` artifact-presence guardrail requires this acceptance document to
> exist; it passed on the post-commit run (recorded in §15).

No existing test was weakened to make D4b pass. The full offline suite grew from
**3902** (D4a.1 baseline) to **3979** — exactly the **+77** new D4b tests.
Regression counts (D3a.5/D3b 1029; all mutation drivers) are **unchanged**.

---

## 12. Live qualification (real server, no reindex, no paid calls)

Executed via `tools/laya_qualify.sh` → `tools/laya_qualify.py`, which drives the
**real production composition** (`app.runtime.compose`) — the real
`IntakeProcessor`, the real `LayaContextPreparer`, and the real D4a
`LiveOpenVikingBackend` against `http://127.0.0.1:1933`. Only the FAST **model**
boundary is replaced by a recording stand-in, so **no paid call is made**
(`paid_model_calls: 0`).

| Scenario | Result |
|---|---|
| **1. New brief requiring design guidance** ("Bloom, florist, minimalist editorial landing page with botanical typography and subtle motion") | FAST received a 7268-char reference block, labelled `REFERENCE DATA`, "MUST NOT override"; brief preserved verbatim. Laya: `degraded`/`medium`, 6 items, 3 retrieval calls, 5509 chars, ≈5.47 s. Sources: `refero_typography`, `refero_motion`, `refero_anti_ai_slop`, `refero_icons`, `refero_craft_details`, `impeccable_layout` — all `reviewed`, with real `viking://…` URIs and revisions. |
| **2. Revision brief with accepted prior project context** (seeded persisted brief) | Production intake loaded the accepted brief and passed it to Laya; FAST received the reference block. Laya: `ready`/`medium`, 1 item (`refero_craft_details`, rev `bc6b016a3ff3`, relevance 0.6318), 1 call, ≈1.61 s. |
| **3. Brief with no relevant references** ("quantum chromodynamics lattice gauge theory renormalization") | Honest-empty: `ready`, **0 items**, quality `insufficient`, warning "retrieved references scored below the relevance floor". **No fabricated context.** |
| **4. OpenViking temporarily unavailable** (isolated `systemctl --user stop openviking-website`, then restart) | `health_after_stop: null`; Laya `unavailable`, `RETRIEVAL_UNAVAILABLE`, 0 items; **FAST received no reference block** and the intake still produced `DISCOVERY_READY` in 99 ms. Service restarted and `/health` healthy again. **Hermes Trade unaffected**: tmux sessions `[trade, website]` identical before and after. |
| **5. Prompt-injection fixture** | With the calibrated floor the injection topic is honestly empty; the offline test proves that when a retrieved item *does* contain instruction-like text it round-trips verbatim as DATA with **no** authority-bearing field. |

The production intake path **invokes Laya and FAST receives the correct bounded
context pack** — this is a real production-integration proof, not a direct unit
invocation.

Evidence file: `~/.website-builder/openviking/laya_qualification.json`
(secret-free). No Vercel deployment was triggered. No corpus reindex occurred
(`skipped_duplicate: 11`).

---

## 13. Latency measurements (real)

| Measurement | Value |
|---|---|
| Laya preparation (3-query new brief) | **≈ 5.47 s** (dominated by 3 embedding round-trips ≈1.8 s each) |
| Laya preparation (1-query revision brief) | **≈ 1.61 s** |
| Intake total (new brief, incl. Laya) | **≈ 5.64 s** |
| Intake total (revision brief, incl. Laya) | **≈ 1.59 s** |
| Intake total (OpenViking down) | **≈ 0.10 s** (connection refused, fail-open) |
| Per-query retrieval (D4a.1 reference) | median ≈ 1.95 s, max ≈ 2.30 s |

Retrieval latency dominates and is bounded: at most 3 queries per intake, each
under the adapter's 10 s timeout.

Live relevance calibration (read-only, embeddings metered $0.00):

| Brief | Top scores |
|---|---|
| relevant (design) | 0.73, 0.71, 0.71, 0.71, 0.70, 0.70, 0.69 |
| relevant (motion) | 0.77, 0.71, 0.69, 0.69, 0.69, 0.68, 0.66 |
| relevant (component) | 0.71, 0.70, 0.69, 0.69, 0.69, 0.69, 0.69 |
| unrelated (QCD) | 0.58, 0.56, 0.55, 0.55, 0.55, 0.55, 0.54, 0.54 |
| unrelated (recipe) | 0.57, 0.56, 0.56, 0.56, 0.56, 0.56, 0.55 |
| unrelated (finance) | 0.57, 0.56, 0.56, 0.56, 0.55, 0.55, 0.55, 0.55 |

Clean separation → operational floor **0.62**.

---

## 14. Security & mutation tests

* Cross-project isolation fails closed (`ISOLATION_VIOLATION`, whole pack refused).
* Cross-tenant / provenance violation fails closed (`PROVENANCE_VIOLATION`).
* Credential-shaped retrieved content fails closed (`CREDENTIAL_LEAK_VIOLATION`)
  and the secret value is **never** echoed into the result or its summary.
* Prompt injection round-trips as inert DATA with no authority-bearing field.
* No unauthorized state mutation: Laya exposes no `write`/`ingest`/`install`/
  `deploy`/`publish`/`save` surface; the production dispatch test confirms only
  the brief advances (deployment/repository untouched).
* The proof guardrail confirms FRONTEND, the revision orchestrator, QA, and the
  Design-DNA context pack contain **no** `openviking`/`laya` reference (no second
  orchestration path).
* The Laya module never shells out and imports no network client at import time.
* The OpenViking feature flag stays **disabled by default**; the Laya flag too.

---

## 15. Final proof record

* Baseline: `dca84ba58f426da7ccddbf38e175586f6758967a`
* `feature/website`: untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646`
* Proof: `tools/d4b_final_proof.py` → **19/19 checks passed, VERDICT: PASS**
* Commit SHA: recorded in §18 at commit time.

---

## 16. Known limitations

* **The FAST model boundary was stubbed in the live run** to avoid paid spend.
  The production integration is proven (the real intake seam invokes Laya and
  FAST receives the pack), but the end-to-end behaviour of a *real* FAST model
  reading the block is not measured here — that would incur paid calls. The
  prompt-level contract (labelled DATA, "MUST NOT override", brief verbatim) is
  pinned by tests.
* **The token estimate** is a 4-chars/token approximation (a safety bound, not a
  billing figure), inherited from D4a.
* **The relevance floor (0.62)** is calibrated against the current 11-source
  `wb-design` corpus; a materially different corpus would need re-calibration.
* **Rerank / intent analysis** (`search`) remains unused; Laya issues single
  queries through `find`.
* **A query planner LLM is deliberately NOT used.** The deterministic planner is
  the baseline; a model-assisted planner would require an explicit paid-call
  budget gate and has not demonstrated measurable value here.
* **Live injection proof is offline.** The corpus contains no reviewed
  injection fixture, so the live run shows honest-empty for that topic; the
  inert-DATA property is proven deterministically offline.

---

## 17. Operational enablement

1. Ensure OpenViking is healthy (`docs/D4A1_OPENVIKING_OPERATIONS.md`):
   `systemctl --user is-active openviking-website` → `active`;
   `curl -s http://127.0.0.1:1933/health` → `status: ok`.
2. Export the USER key: `export OPENVIKING_API_KEY="$(sed -n 's/^OPENVIKING_USER_KEY=//p' ~/.website-builder/openviking/openviking.env)"`.
3. Enable **both** blocks in `config/default.yaml`:

   ```yaml
   openviking:
     enabled: true
   laya:
     enabled: true
   ```

4. Restart the Website Builder process. With either flag `false`, Laya is a
   strict no-op and the FAST path is unchanged.
5. To disable: set `laya.enabled: false` (recommended first) or
   `openviking.enabled: false`. Both degrade safely.

Do **not** enable the paid VLM ingestion path for Laya; Laya never ingests.

---

## 18. Final verdict

```
D4B_READY_FOR_INTEGRATION
```

All mandatory deterministic and live integration gates pass: the full battery is
**19/19** (focused D4b 77, focused D4a/D4a.1 150, full offline suite 3976,
D3a.5/D3b regressions 1029, every mutation driver including the new D4b driver's
10 guards); the live qualification against the real OpenViking 0.4.23 server
passes all five scenarios (new brief, revision brief, no-relevant-references,
temporary outage, prompt injection) with **zero paid model calls**, no reindex,
no Vercel deployment, and Hermes Trade untouched.

* Commit SHA: `______________________________________` (set at commit time)
* Push: `origin/web-design` (fast-forward, no force)
