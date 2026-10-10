# D4c — FAST Context Integration: Acceptance Report

Status: engineering record for Batch D4c.
Branch: `web-design`.
Baseline: `7925a4ba0361d51832799c59a49ba1750d204588` (D4b.2 accepted:
`c66ce2da6`).
Scope: integrate the **accepted** deterministic D4b context preparer, the D4b.2
multilingual retrieval improvement, and the **real** upstream OpenViking service
with the **existing** authoritative FAST intake path — via a new, separate,
default-OFF, fail-closed opt-in — **without** introducing a second decision
maker, a second FAST path, or any new orchestration.

Every claim below is backed by an executed command or an executable test. The
live qualification ran against the **real OpenViking 0.4.23 server** already
provisioned in D4a.1. **Zero paid model calls** were made: the paid FAST *model*
boundary was replaced by a labelled recording stand-in (**contract test**, not
real FAST execution). Consequently the verdict is
`D4C_READY_FOR_CONTROLLED_FAST_SMOKE`, **not** `D4C_READY_FOR_STAGED_ROLLOUT`.

---

## 1. Baseline and preconditions (verified, not assumed)

| Item | Value |
|---|---|
| Branch | `web-design` (verified `git branch --show-current`) |
| HEAD at start | `7925a4ba0361d51832799c59a49ba1750d204588` |
| Working tree at start | clean (verified `git status`) |
| D4b.2 accepted commit | `c66ce2da6` (verified ancestor of HEAD: `git merge-base --is-ancestor c66ce2da6 7925a4ba` → YES) |
| `feature/website` | **untouched** at `868ed00e3f24e06f1dcf9944d6d031105dff0646` |
| Remote | `origin` → `https://github.com/albertus527/hermes-v1.git` |
| OpenViking `/health` | `status: ok`, `version: 0.4.23` |
| OpenViking `/ready` | `agfs/vectordb/api_key_manager/embedding` all `ok` |
| systemd unit | `openviking-website` → `active` |
| tmux sessions | `trade` + `website` identical before/after (Hermes Trade untouched) |
| Prior verdict consumed | D4b.2 `D4B2_READY_FOR_D4C` (read from `docs/D4B2_MULTILINGUAL_RETRIEVAL_ACCEPTANCE.md` §17) |

No prior verdict was assumed to have advanced: D4b.2 readiness was re-verified
from the report **and** from the live service before any code change.

---

## 2. Exact runtime call graph (traced, not guessed)

Traced from user input through to FAST, then to FRONTEND:

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
app/core/intake.py  IntakeProcessor.process(message, project_id)
  │   - load persisted brief / pause_state / pending_clarification
  │   - _prepare_reference_context(...)                     ◄── D4c GATE lives here
  │        ├─ [D4c] gate: self.laya.fast_context_injection_enabled   (fail-closed)
  │        ├─ LayaContextPreparer.prepare_context(...)     (D4b; real OpenViking)
  │        └─ render_laya_context_block(result)            (bounded lower-trust DATA)
  │   - self.hermes_adapter.fast_interpret(
  │         text, project_id, conversation_context,
  │         reference_context=<block or None>)             ◄── the ONE FAST call
  │        │
  │        ▼
  │   app/hermes/adapter.py  HermesAdapter.fast_interpret
  │        - _build_fast_prompt(text, context, reference_context)   (line 1274)
  │        - _run_fast_programmatic(role="FAST", enabled_toolsets=[])  ◄── ONLY model call
  │        - _parse_fast_response → {scope,name,what,why,…,readiness}
  │   - application owns readiness/scope/merge (FAST interprets; code enforces)
  ▼
app/core/intake.py  IntakeProcessor.apply_to_project  (persists brief, lifecycle)
  ▼
app/channels/dispatch.py  auto-build sub-claim → FrontendBuilder.build → FRONTEND
  ▼
QA → VISION → Impeccable critic → PREVIEW → approval → Vercel LIVE
```

### 2.1 Exact FAST entrypoint

| Question | Answer (verified in source) |
|---|---|
| The single authoritative FAST call | `HermesAdapter.fast_interpret(...)` invoked **once** from `IntakeProcessor` |
| Where the reference block is composed into the prompt | `HermesAdapter._build_fast_prompt(...)` — `app/hermes/adapter.py:1274` |
| How the block is passed | as the **separate** keyword argument `reference_context=` — never concatenated into `system`/`developer` instructions |
| Where D4c gates it | `IntakeProcessor._prepare_reference_context` (`app/core/intake.py`) |

**Before D4c:** the seam already existed (built in D4b) and was gated **solely**
by `laya.enabled`. Turning on `laya.enabled` therefore *implicitly* handed FAST
the pack — there was no independent switch for the FAST handoff itself.

**After D4c:** the FAST handoff has its **own** gate,
`laya.fast_context_injection`, independent of `laya.enabled`. The effective
condition is `fast_context_injection AND enabled`; enabling `enabled` alone no
longer hands FAST anything.

No new seam was created. No second `fast_interpret` call site exists.

---

## 3. Context contract & trust boundary

The D4b context contract is preserved **unchanged** (`LAYA_CONTRACT_VERSION = 1`):

* `LayaContextResult`: `status` (`ready|degraded|unavailable|skipped`),
  `project_id`, `library_project_id`, `quality`, `items`, `queries`,
  `retrieval_calls`, `estimated_chars`, `estimated_tokens` (4 chars/token),
  `truncated`, `degraded`, `warnings`, `error_reason`, `latency_ms`, `limits`.
* `LayaContextItem`: `source_id`, `source_uri` (`viking://…`), `source_revision`,
  `category`, `trust`, `level` (`L0|L1|L2`), `relevance`, `excerpt` (≤600 chars),
  `summary` (≤256), `uncertainty` — **no authority-bearing field**.
* Bound: `MAX_PACK_CHARS = 6000`; render backstop `MAX_RENDERED_CHARS` (8,000).
  Deterministic ordering + de-duplication. Every excerpt is untrusted.

**Trust boundary (enforced by tests + mutation guards):** the pack is rendered as
a clearly-delimited **lower-trust REFERENCE DATA** block:

```
=== LAYA CONTEXT (application-retrieved REFERENCE DATA, lower trust) ===
{ …json… }
=== END LAYA CONTEXT ===
```

The block is *never* placed in system/developer instructions, never requests tool
execution, never changes FAST's role, never authorizes a deployment, never exposes
credentials. Prompt-injection text inside a retrieved excerpt stays **inert DATA**
(verified by fault-injection fixtures below). FAST remains the sole authoritative
intake decision maker.

---

## 4. Feature-flag matrix

Three **independent**, default-OFF flags:

| Flag | Default | Independently controls | Introduced |
|---|---|---|---|
| `laya.enabled` | `false` | (C) real upstream Laya preparation / OpenViking retrieval | D4b |
| `laya.multilingual_expansion` | `false` | (B) D4b.2 Indonesian-gated English gloss expansion | D4b.2 |
| **`laya.fast_context_injection`** | **`false`** | **(A) whether FAST receives the prepared pack** | **D4c** |

Gate semantics: `fast_context_injection_enabled = fast_context_injection AND enabled`.
Enabling any one flag never activates another. Flag OFF ⇒ FAST receives **byte-identical**
inputs to the accepted baseline (`test_no_reference_block_yields_a_byte_identical_prompt`).

Config lives in `config/default.yaml` under `website_builder.laya` — **no new
`HERMES_*` env var was introduced** (per the repo's configuration model).

---

## 5. Live OpenViking evidence (real service, no mocks)

Command (runner never echoes the key):

```
bash tools/d4c_run_live_smoke.sh     # drives tools/d4c_live_smoke.py
```

Server: OpenViking `0.4.23`, `http://127.0.0.1:1933`, project `wb-design`.
Evidence JSON: `~/.website-builder/openviking/d4c_live_smoke.json`.

| # | Scenario | Observed result | Gate |
|---|---|---|---|
| 1 | Flag OFF | **1 FAST call**, **no** reference block, brief verbatim | ✅ baseline preserved |
| 2 | Flag ON | **1 FAST call**, labelled block (7,341 chars), brief verbatim | ✅ |
| 3 | Real retrieval | **6 items**, quality `medium`, 3 queries, 5,514 ms, `truncated: true`, 5,503 chars / 1,376 tokens | ✅ |
| 4 | Provenance | all items `uri_scheme: viking`, real `revision` (e.g. `5c2211d3e432`), `trust: reviewed` | ✅ |
| 5 | Multilingual | Indonesian brief **changes** query plan; English plan **unchanged** | ✅ |
| 6 | Outage (service stopped) | Laya `unavailable` (`RETRIEVAL_UNAVAILABLE`), **0 items**, **1 FAST call**, **no block**, brief verbatim | ✅ fail-closed |
| 7 | FAST payload | block labelled+delimited; payload/item keys carry **no authority-bearing keys** | ✅ |
| 8 | Paid model calls | **0** | ✅ |

The retrieval latency (~5.5 s for 3 queries) is real OpenViking + embedding time
and is the dominant cost of the pack; it is bounded by the query budget (3).

---

## 6. FAST payload evidence

The recording stand-in captures the **exact** arguments FAST would receive
(**contract test**, not a real FAST model run):

* `reference_context` is a **separate field** (`"reference_context" in call_on` → true);
* `text` (the brief) is **untouched** (`call_on["text"] == BRIEF` → true);
* the block is **labelled and delimited** (first line
  `=== LAYA CONTEXT (application-retrieved REFERENCE DATA, lower trust) ===`,
  last line `=== END LAYA CONTEXT ===`);
* the JSON payload keys are exactly
  `[contract_version, degraded, error_reason, estimated_chars, estimated_tokens,
  items, latency_ms, library_project_id, limits, project_id, quality, queries,
  retrieval_calls, status, truncated, warnings]`
  and the item keys are exactly
  `[category, excerpt, level, relevance, source_id, source_revision, source_uri,
  summary, trust, uncertainty]` — **no** `role`/`system`/`developer`/`instruction`/
  `override`/`tool_call`/`function_call`/`authority` key.

> Note on methodology: the probe asserts payload **structure**, not a substring
> scan for words like "override"/"instruction" — the block's own safety
> disclaimer literally contains "MUST NOT override … never an instruction", so a
> substring scan would match every time and prove nothing.

---

## 7. Security & fault-injection results

Focused D4c suite: `tests/test_d4c_fast_context_integration.py` — **47 passed**.

| Class | Case | Result |
|---|---|---|
| Security | impersonating system instruction in excerpt | inert DATA; block still labelled non-authoritative |
| Security | secret/credential disclosure attempt in excerpt | no privileged keys in payload; secrets not promoted |
| Security | tool-execution request in excerpt | inert DATA; no tool invocation |
| Security | deployment-approval request in excerpt | inert DATA; publication state untouched |
| Security | cross-project contamination | project scope enforced; foreign items rejected |
| Security | invalid provenance | item dropped (`PROVENANCE_VIOLATION` → pack refused) |
| Security | missing credentials | fail-closed; retrieval unavailable; FAST path intact |
| Security | malformed OpenViking response | fail-closed; no partial unverified pack |
| Reliability | timeout | fail-closed; no indefinite retry |
| Reliability | unavailable (live outage) | fail-closed; 1 FAST call; brief verbatim |
| Reliability | empty result | honest-empty (not an error) |
| Reliability | duplicate refs | collapsed to one entry |
| Reliability | retry exhaustion | bounded `(1 + max_retries)` attempts, then degrade |
| Reliability | FAST timeout after prep | original FAST error surfaces; no second call |
| Reliability | process restart mid-intake | persisted brief preserved; no fabricated pack |

Mutation driver `tools/mutation_check_d4c.py` — **8/8 guards killed**:
(1) injection default OFF; (2) shipped config OFF; (3) explicit opt-in fails
closed; (4) injection ≠ preparation; (5) `config_from_mapping` reads the flag;
(6) REFERENCE-DATA labelling; (7) embedded-instruction prohibition; (8)
rendered-block bound.

---

## 8. Full regression results (compared to the accepted D4b.2 baseline)

| Suite | Result |
|---|---|
| Focused D4c (`test_d4c_fast_context_integration.py`) | **47 passed** |
| D4b.2 multilingual (`test_d4b2_multilingual.py`) | **45 passed** |
| D4b context-prep (`test_laya_integration` + `test_laya_context` + `test_laya_composition`) | **77 passed** |
| Focused D4c + D4b/D4b.2 + OpenViking + laya (combined set) | **319 passed** |
| D3a.5/D3b security + QA (13 suites) | **459 passed** |
| **Full offline suite** | **4,100 passed, 2 skipped, 4 deselected (network/live), 60 subtests passed** |
| Mutation: `mutation_check_d4c.py` | **8/8 killed** |
| Mutation: `mutation_check_d4b2.py` | **7/7 killed** |
| Mutation: `mutation_check_d4b.py` | **10/10 killed** |
| Mutation: `mutation_check_d4a1.py` | **9/9 killed** |
| Mutation: `mutation_check_d4a.py` | **17/17 killed** |
| Mutation: `mutation_check_d3b.py` | **14/14 killed** |
| Mutation: `mutation_check_d3a5_partbc.py` | **73/73 killed** |
| Mutation: `mutation_check_d3a5_partc.py` | **39/39 killed** |

No accepted assertion was weakened. The three `test_laya_integration.py` edits
add an **explicit opt-in** to the D4b fixtures; their assertions are unchanged.

---

## 9. Resource measurements (VPS shared with Hermes Trade + OpenViking)

| Metric | Before | After | Note |
|---|---|---|---|
| Mem available | 6,181,848 KB | 6,328,720 KB | no pressure |
| Swap free | 1,462,588 KB | 1,462,592 KB | ~0.6 GB used, unchanged by D4c |
| loadavg (1m) | 2.213 | 2.228 | no spike |
| Smoke process peak RSS | 87,804 KB | 89,820 KB | **≈86–88 MB**, bounded |

No Laya checkpoint load, no TypeSafe Jev, no new resident model, no Strix, no
parallel heavy benchmark, no Chromium stress. Hermes Trade (`tmux trade`) and
OpenViking untouched.

---

## 10. Blocked real-FAST gates (paid; operator approval required)

| Gate | Status |
|---|---|
| Real FAST **end-to-end** (real model reading the block) | **BLOCKED** — paid, not authorized |
| Real FAST **decision quality** with/without the pack | **BLOCKED** — paid |
| Vercel deploy / publication | **not attempted** (forbidden) |

The recording stand-in is explicitly a **CONTRACT TEST**; it proves the payload
shape, not real FAST behaviour. Per mission, no full acceptance is claimed while
paid FAST testing is unexecuted.

---

## 11. Remaining operator approvals

1. Approve a **one-shot paid FAST smoke** (single brief, flag ON vs OFF) to
   upgrade the verdict to `D4C_READY_FOR_STAGED_ROLLOUT`.
2. Decide whether/when to enable `laya.fast_context_injection` in production
   (must remain OFF until the paid smoke passes).
3. Any later global rollout of D4c/D4b.2 — separate, explicit decision.

---

## 12. Rollback instructions

D4c touches **no persistent state** (no index rebuild, no migration, no OpenViking
restart). Rollback is one of:

```bash
# (a) disable the feature — safest, keeps code shipped
#     set in config/default.yaml:
#       website_builder.laya.fast_context_injection: false   (already the default)

# (b) revert the D4c commit(s) entirely
git revert <D4c commit SHA>            # or:
git checkout 7925a4ba0361d51832799c59a49ba1750d204588 -- website-builder/
```

With `fast_context_injection: false`, FAST receives no reference block and the
path is byte-identical to the accepted baseline.

---

## 13. Commit / push status

| Item | Value |
|---|---|
| Baseline commit | `7925a4ba0361d51832799c59a49ba1750d204588` |
| `feature/website` | untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646` |
| Commit SHA | `c34cff862` (D4c implementation commit) |
| Push | `origin/web-design` (fast-forward, **no force-push**) |
| Excluded from the commit | caches (`__pycache__`), `~/.website-builder/openviking/d4c_live_smoke.json` (evidence lives outside the tree), secrets/keys, checkpoint weights, temp bench artifacts |

---

## 14. Final verdict

```
D4C_READY_FOR_CONTROLLED_FAST_SMOKE
```

Code wiring is complete and independently gated; all offline suites pass
(full offline 4,100; focused D4c 47; D4b/D4b.2 122; D3a.5/D3b 459); every
mutation driver is green (8/8 D4c + 7/7 D4b.2 + 10/10 D4b + 9/9 D4a.1 + 17/17
D4a + 14/14 D3b + 73/73 + 39/39 D3a.5); and the **real OpenViking** live smoke
passes all scenarios with **zero paid calls**, fail-closed outage behaviour, and
bounded resources. The **only** unexecuted gate is a paid real-FAST run, which
this mission does **not** authorize — therefore the verdict is *not*
`D4C_READY_FOR_STAGED_ROLLOUT`.

---

## Required closing outputs (mission items 1–7)

1. **Final verdict:** `D4C_READY_FOR_CONTROLLED_FAST_SMOKE`.
2. **Exact FAST integration point:** `IntakeProcessor._prepare_reference_context`
   → single `HermesAdapter.fast_interpret(..., reference_context=...)` →
   `HermesAdapter._build_fast_prompt` (`app/hermes/adapter.py:1274`).
3. **Feature flag states:** `laya.enabled=false`,
   `laya.multilingual_expansion=false`, `laya.fast_context_injection=false` —
   all independent, all default OFF.
4. **Live OpenViking evidence:** §5 (real 0.4.23; 6 items; provenance; fail-closed;
   0 paid calls).
5. **Regression & security evidence:** §7–§8 (full offline 4,100; mutation all
   green; security fixtures inert).
6. **Remaining paid FAST approval gates:** §10 (real FAST end-to-end — BLOCKED;
   Vercel deploy — forbidden).
7. **Recommended next action:** approve a one-shot paid FAST smoke (§11) to
   upgrade to `D4C_READY_FOR_STAGED_ROLLOUT`; otherwise keep the flag OFF.
