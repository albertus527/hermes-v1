# p20 FRONTEND Failure — Deep Bug-Hunt Report + Fix Plan

**Evidence source:** p20 (`tg-6329821361-p20`, invocation `1749b2664dd244c69392bf8a7183bebf`).
**Terminal outcome:** `FRONTEND_HARD_TIMEOUT` at 2700.2s.
**Scope of this document:** investigation + fix plan. **Nothing is implemented here.**

Constraints honoured throughout: no canonical-URL changes, no watchdog timeout-semantics
changes, no convergence behavior added, no Batch D.

---

## 1. Executive diagnosis

p20 was **not** a liveness failure. The watchdog was correct: 2294/2294 progress events
advanced, zero keepalives, longest forward-progress gap 17.0s against a 180s idle bound.
The child was genuinely working, for 45 minutes, and never produced the deliverable.

**The defect is that nothing in the system distinguishes "busy" from "converging."**

Three facts, each independently proven by code, compose into the failure:

1. **The only machine check that `src/App.tsx` was actually implemented**
   (`_has_complete_frontend_artifacts`, `adapter.py:1362-1395`) is consulted in exactly
   **two** places: the observational probe (`adapter.py:676`) and the **timeout-recovery
   branch** (`adapter.py:1346`). It is **never consulted on the success path.** There is no
   `DESIGN_DONE`, no implementation-started marker, no phase boundary anywhere in the repo.
2. **The agent loop has no budget of any kind.** `max_iterations: int = sys.maxsize`
   (`run_agent.py:456`) and `hermes_cli/oneshot.py:648-678` never overrides it. The only
   enforced per-tool caps cover `web_search` and `delegate_task`
   (`tool_guardrails.py:202-203`) — neither is in FRONTEND's 9-tool set.
3. **A refused tool call is indistinguishable from a successful one** on the progress
   channel. `tool_executor.py:1816-1817` emits `tool completed: write_file (0.1s) (error)`,
   which classifies as `TOOL/completed` → `advance=True`
   (`progress_events.py:105,145-155`). The receipt keeps **no tool-error counter at all**.

So: FRONTEND wrote `design-dna.json` at t≈90s (as instructed), then spent the remaining
**2610 seconds — 97% of the run — producing 476 completed tool calls and 282 model calls
with zero `src/` mutation**, and nothing in the system could notice. Prompt-only enforcement
was already tried for exactly this and failed: commit `573dff3c6` ("bound frontend design
discovery and extend build timeout") added the current prompt wording and raised 300s→900s.
p20 is the counter-example to that fix.

**Corrected reading of the p20 counters.** The quoted tool histogram is **not** raw call
counts. `watchdog.py:623-629` increments `tool_name_counts` on *every* TOOL event and
`forensic_tool_name` matches both prefixes (`watchdog.py:299`), so each sequentially-executed
call contributes **2** (start + completion). The arithmetic closes exactly:
`952 = 476 + 476 = tool_completed × 2`, and `tool_started 563 = 476 matched + 87 concurrent
batch events` (unparseable). Likewise `model_started 566 / model_completed 282 ≈ 2.0`, because
`MODEL/started` has four emitters (`progress_events.py:94-99`) against one
`MODEL/completed`. **Real figures: ~476 tool invocations, 282 model calls.** Any convergence
metric built on today's counters would be wrong by 2×.

---

## 2. Findings A–E

### A. Convergence / phase-stall — **ROOT CAUSE, BLOCKER**

**A1 — What tells FRONTEND to move from DNA to implementation.**
Only prompt text, `adapter.py:1429-1461` (task 4 "IMMEDIATELY after writing
design-dna.json, edit the website source"; rules "Once Design DNA is written, immediately
implement the website", "The task is incomplete until the starter placeholder in src/ has
actually been replaced"). Added by `573dff3c6`. **No test enforces behavior, only string
presence** (`test_hermes_adapter.py::test_frontend_prompt_requires_source_implementation_after_design_dna`).

**A2 — Machine-enforced phase boundary: none exists.**
The convergence guard is an *explicitly deferred* change with tripwire tests already in place:
`tests/test_frontend_watchdog.py:1338-1354` — *"BOUNDARY LOCK: this is the convergence guard,
and it is not implemented… If this test ever fails, someone has wired the probe into the
supervision decision — which is the deferred change, not this one."* Mirror test at `:1357-1370`.

**A3 — Yes, the agent may continue arbitrary turns forever.** No bound (A5, C2). The prompt's
"Keep the build sequential" is unenforced.

**A4 — Why no `src/` mutation despite 2294 advancing events.**
Because advancing ≠ converging. `description_advances` (`progress_events.py:145-155`) returns
True for everything not in `_KEEPALIVE_PREFIX_RULES`; a completed `read_file` advances the
clock identically to a completed `write_file`. 476 completed reads kept a run alive that had
not produced a byte of source.

**A5 — Stuck planning/inspecting/validating rather than executing.**
The workspace sampler proves the *shape*: `distinct_fingerprints = 3` (baseline + 2 changes),
both changes to `design-dna.json` (created ≈90s, rewritten ≈2010.9s), **zero** changes under
`src/**` in 2610 seconds spanning ~470 tool calls. The agent was in design mode for the entire
run. Whether it was *deliberately* re-deriving or *reacting* to its own artifact cannot be
resolved from the receipt — see §7 open evidence.

**A6 — Can tool results or system prompts redirect it back into design exploration after DNA exists? Yes, three channels:**
- The `website-builder-design-dna` skill is a **pure field contract with zero sequencing
  instructions** (`.hermes/skills/website-builder-design-dna/SKILL.md`, 75 lines — no
  "read/inspect/iterate/re-derive/validate" instruction anywhere). Sequencing lives *only* in
  the prompt, so it is the single point of failure.
- `read_file` on `design-dna.json` returns the file's own content into history (see E1 — the
  wrapper shape is self-evidently odd to a model reading it back).
- `skill_view` **is** in FRONTEND's toolset (`toolsets.py:176-180`), and `skill_manage` triggers
  a creation nudge every 10 iterations (`conversation_loop.py:2102-2106`,
  `agent_init.py:1991`) — reachable inside a stuck read/terminal loop at iteration 10–20.
- `compose_project_instructions` (`composition.py:22-32`) appends reference / direction /
  contact-form blocks to the same prompt, all requirement-shaped, none sequencing-shaped.

**The exact loop that permitted `design-dna.json` + 282 model completions + 476 completed
tools + zero `src/` mutation:** `supervise_frontend_run`'s poll loop
(`watchdog.py:1572-1638`) evaluates *only* `cancel_event`, `progress_channel_confirmed`,
`elapsed >= hard_max_runtime`, and `no_progress_for >= idle`. Every one of those inputs is
satisfied by a read loop. `WorkspaceSampler` sits in the same loop at `watchdog.py:1578` and is
commented *"Observation only… strictly after the progress read so the workspace can never gate
the liveness decision."* It therefore cannot stop anything. That is the whole mechanism.

---

### B. Phase regression / Design DNA mutability — **SYMPTOM of A + one independent latent defect, MEDIUM**

**B1 — How many times was `design-dna.json` written? Exactly twice.** `distinct_fingerprints=3`
= launch baseline + 2 changes (`watchdog.py:880-885`); `source_mutation_count=2`; only
`design-dna.json` in `mutated_paths`; `first_mutation_offset_seconds=90.0`,
`last_mutation_offset_seconds=2010.9`.

**B2 — Which turns caused the rewrite? Unknown from the receipt.** `recent_activity` holds only
the last 20 normalized descriptions (bounded at `watchdog.py:517-519`). The rewrite is a
filesystem fact at t=2010.9s with no surviving turn attribution.

**B3 — Is there a `DESIGN_DONE` / implementation-started concept? No.** Repo-wide, the strings
`DESIGN_DONE`, `implementation_started`, `converge`, `no_deliverable`, `deliverable` return
**zero** hits in `website-builder/app/`. The only "convergence" in the codebase is
`registry.converge_display_name` (identity, unrelated).

**B4 — Intentional or regression?** The rewrite is **not a regression** — nothing regressed,
because implementation never started. Per `573dff3c6`'s intent ("Creating design-dna.json
alone does not complete this task"), one rewrite *before* implementation is legitimate design
refinement. What is unintended is that the run was still pre-implementation 33 minutes later.

**B5 — Immutability, versioning, or mutable? Mutable, and versioned only *after the fact*.**
`state.revisions.design_dna_version` is written **after** the invocation returns
(`build.py:937`, `revise.py:640-641`, `build.py:608-610`). During the invocation it carries no
information, so it provides **zero** intra-invocation protection. Downstream consumers treat
DNA as continuously mutable by design: the skill's Rule 2 is literally "`design_dna_version`
increments on each update."

**B6 — Can repeated rewrites reset planning and block implementation?** Plausible but
**unproven**. The mechanism would be: rewrite → `read_file` returns a fresh, differently-shaped
artifact → model re-derives. Requires the transcript to confirm.

**Verdict: contributing factor / symptom, not an independent bug** — with one independent
latent defect attached (B5: DNA is unversioned within an invocation).

---

### C. Runaway tool loop / missing progress budget — **ROOT CAUSE (independent of A), BLOCKER**

**C1 — The histogram is inflated 2×.** Proven above. Real: **~476 tool invocations**
(~275 `read_file`, ~196 `terminal`, ~3 `search_files`, ~2 `write_file`) across **282 model
calls** — ~1.7 tool calls per model call, ~9.6s per model call.

**C2 — Iteration budget: there is none.**
`run_agent.py:456` → `max_iterations: int = sys.maxsize` ("Default: unlimited tool-calling
iterations"). `hermes_cli/oneshot.py:648-678` passes neither `max_iterations` nor
`iteration_budget`. `agent_init.py:679` → `IterationBudget(sys.maxsize)`. The loop condition
(`conversation_loop.py:2022`) is therefore unbounded. **The watchdog's 2700s hard fuse was the
only bound that existed.** (Note: `AGENTS.md` and `agent/iteration_budget.py:4-6` both document
"default 500" — **both are stale relative to the code.**)

**C3 — Identical files/commands repeatedly read/run? Partially, and every detector is blind to it.**
| Detector | Location | Why it didn't fire |
|---|---|---|
| Runaway loop caps | `tool_guardrails.py:202-203,659-724` | Only `web_search` + `delegate_task` branches exist |
| Hard-stop blocking | `tool_guardrails.py:388-389` | Dead: `hard_stop_enabled: False` (`config_defaults.py:774`) |
| Identical-call streak | `tool_guardrails.py:579-591` | **Strictly consecutive** — a `read_file`/`terminal` alternation resets it to 1 every call |
| `terminal` no-progress | `tool_guardrails.py:41-60,492-497,522-525` | `terminal` is *mutating* ⇒ not idempotent, and a success **clears its own counters** ⇒ 196 successful calls produce **zero** signal |
| `read_file` repeat block | `file_tools.py:1923-1986` | Blocks at 4, but keyed on exact `("read", path, offset, limit)`; **different `offset`/`limit` = different key = full content, `consecutive` never exceeds 1.** Wiped entirely on compression (`conversation_compression.py:4654-4661`) |
| `read_file` dedup stub | `file_tools.py:1788-1842` | Same exact-tuple + mtime key; returns a stub only on byte-identical repeat |
| Intra-message dedup | `run_agent.py:5095-5120` | `seen` set is **local to one assistant turn** |

**C4 — Is tool output fed back redundantly? Yes, unconditionally.**
`messages.append(tool_message)` at `tool_executor.py:1853` (concurrent) / `:2768` (sequential),
with no cross-turn comparison anywhere. `read_file` is **pinned to never spill**:
`budget_config.py:11-13` `PINNED_THRESHOLDS = {"read_file": float("inf")}`, honored at `:102-103`
— deliberate, to prevent persist→read→persist loops. It returns up to
`_DEFAULT_MAX_READ_CHARS = 100_000` chars (`file_tools.py:65`) verbatim ≈ 25–35K tokens, *every time*.

**C5 — Per-phase budget for reads/searches/terminal? None exists.** The per-turn byte budget
(`turn_budget = 200_000` chars, `budget_config.py:18`) bounds one turn's tool output; it never
bounds call *count* and never bounds cumulative history. `proactive_prune_tokens: 0`
(`config_defaults.py:848`) — the config key that exists for precisely this failure mode is off.

**C6 — Can hundreds of non-writing operations run with no required deliverable mutation? Yes.**
There is no invariant coupling tool activity to artifact production anywhere.

**C7 — Do retries/self-checks/skills/tool descriptions encourage repeated inspection?**
The three profile skills do **not** (all three are declarative; verified line by line). But two
description-level conflicts exist and are worth recording as conflicts, not causes:
- `terminal_tool.py:1135` positively assigns builds to `terminal` ("Reserve terminal for: builds,
  installs, git, processes…"), directly contradicting `adapter.py:1437-1438` ("Do NOT run npm ci,
  npm run build, or npm run typecheck").
- `skill_manage` nudge every 10 iterations (C/A6).
No repo skill encourages build loops. `ui-ux-pro-max` (referenced at `adapter.py:1431`) is
**not vendored in this repo** — it exists only in `~/.hermes-website/skills`, unreadable here.

**C8 — Terminal usage: unrecoverable.** `tools/terminal_tool.py` has **no** per-command state,
history, or "already ran this" check anywhere in 4213 lines. Whether the 196 calls were
productive (`ls`, `cat`) or repetitive is **not derivable from the receipt** — the receipt stores
tool *names* only, never arguments (`watchdog.py:311-317`, by design).

**Quantifying useful vs repeated work — the honest limit.**
The receipt deliberately stores names, never arguments (`watchdog.py:311-317`), so *no*
per-file or per-command repetition analysis is possible from it. What **is** provable:
~2 of ~476 calls mutated anything; 0 of ~476 mutated `src/`; the run held one deliverable
artifact hostage for 2610 seconds. **The smallest meaningful progress metric is therefore not
a repetition ratio — it is "deliverable mutation," i.e. the workspace fingerprint the sampler
already computes.** A crude total-tool-count kill is explicitly the wrong instrument: it would
fire on a legitimately long build and it would still not distinguish a rejection loop.

---

### D. Context growth / trimming / model metadata — **CONTRIBUTING FACTOR + one independent metadata bug, MEDIUM/HIGH**

**D1 — Exact source of the warning.** `agent/model_metadata.py:395-408`
`_warn_context_length_fallback(model, base_url)`, emitted once per `(model, base_url)`.
`DEFAULT_FALLBACK_CONTEXT = CONTEXT_PROBE_TIERS[0] = 256_000` (`model_metadata.py:373-387`).

**D2 — What is `frontend`? The literal config value, never resolved.** It is
`website_builder.models.FRONTEND.model` from `~/.hermes-website/config.yaml`
(`adapter.py:279-292`), passed verbatim as `--model`. **Proven defect:** the adapter *always*
emits `--provider` (`adapter.py:503-506`; `_role_selection` raises on an empty provider,
`adapter.py:288-291`), so `effective_provider is never None`, so the
`DIRECT_ALIASES` (`model_aliases:`) resolution branch at **`oneshot.py:571-590` never
executes**. Hence `agent.model == "frontend"`. (Verified firsthand, `oneshot.py:569-602`.)

**D3 — Is model metadata wrong or missing? Metadata is CORRECT; the name never reached it.**
`agent/model_metadata.py:533` → `"glm-5.3": 1_048_576`. Longest-key-first substring matching
(`model_metadata.py:3442-3451`) means `glm-5.3-flash` / `z-ai/glm-5.3-flash` all resolve to
1,048,576. No `DEFAULT_CONTEXT_LENGTHS` key is a substring of `"frontend"`, so step 9 returns
256,000. **The 256K fallback is caused solely by the unresolved alias string.**

**D4 — Actual trimming threshold.** Compression is **ON** in oneshot: `agent_init.py:2155`
(`enabled` default `True`) / `:2811`, `config_defaults.py:800`, `threshold: 0.50`
(`config_defaults.py:813`), `max_attempts: 3` (`:843`). `oneshot.py:648-678` disables nothing.
Trigger = `turn_context.py:1052-1110` (turn-start preflight) and
`conversation_loop.py:7592-7616` (post-tool). With `context_length=256_000`:
`_effective_threshold_percent` floors 0.50 → **0.75** (`context_compressor.py:3279-3281`,
`_SMALL_CTX_WINDOW_LIMIT=512_000`, `_SMALL_CTX_THRESHOLD_PERCENT=0.75` at `:1356-1357`), and
`_compute_threshold_tokens` (`:3283-3323`) yields **192,000 tokens**.

**D5 — Did trimming trigger during p20? Cannot be determined — and that is itself a defect.**
330–350K > 192,000, so compaction *should* have fired. Suppression states exist and are
verified: `context_compressor.py:3834-3864` `_automatic_compression_blocked{,_locally}`
(summary-LLM cooldown, structural backoff, `ineffective` after two sub-10% attempts).
**The forensic receipt contains no compression counter, no context-size sample, and no
compaction event** — so "never attempted", "attempted and blocked", and "attempted and
ineffective" are indistinguishable in the record.

**D6 — Does history accumulate without compaction? Yes, structurally.**
Per-result persistence (`tool_executor.py:1820-1826`) is bypassed for `read_file`
(`PINNED_THRESHOLDS = inf`). Per-turn aggregate (`enforce_turn_budget`, 200K chars) is
per-turn only. `proactive_prune_tokens: 0`. Every compression wipes `read_file`'s dedup
(`conversation_compression.py:4654-4661`), handing back a fresh dedup budget each cycle.

**D7 — Do repeated reads duplicate large content? Yes.** Dedup key is
`(resolved_path, offset, limit)` + mtime (`file_tools.py:1788-1842`); any write between reads
bumps mtime → full re-append, and **the prior full copy stays in history** (nothing is
retroactively removed).

**D8 — Auxiliary/title calls: irrelevant.** `title_generator.py:383-410` sends
`user_snippet[:1000]` with `max_tokens=64` and no history. The only auxiliary call carrying
the large context is the compression summarizer itself — a consequence of D4/D6, not an
independent leak.

**D9 — Is 256K enforced? No — advisory only.** A repo-wide grep for
`context_length >`/`>=`/`<=` finds **no** site that trims, aborts, or rejects a request. The
only over-window behavior is a warning (`run_agent.py:1073-1091`). An under-reported window
silently *defers* compaction; it never blocks an oversized request.

**D10 — Does the provider accept more than the runtime believes?** Unknown from this repo, but
the direction is established: with a correctly-resolved 1,048,576 window, `threshold_tokens`
would be 524,288 — *higher* than today's 192,000. **So the 256K fallback did not cause the
blow-up by making the trigger too low; it made the reported number wrong and the derived
tool-budget floor inert** (`budget_for_context_window(256_000)` clamps back to exactly
`DEFAULT_BUDGET` — 100K/result, 200K/turn).

**D11 — Classification.**
- The unresolved alias → **metadata/config bug** (real, independent, one-line surface).
- The 256K advisory fallback warning → **misleading warning**, correctly self-describing.
- Unbounded tool-result accumulation + no prune + no counters → **observability bug**.
- 330–350K input tokens → **not proven to be a correctness bug**; the provider accepted them.

---

### E. Design DNA schema / parser compatibility — **PROVEN latent incompatibility, HIGH**

**E1 — The contract shape and the validated shape are different documents.**

*Contract* — `.hermes/skills/website-builder-design-dna/SKILL.md:28-60`, a YAML block with
**everything nested under a top-level `design_dna:` key**. This is exactly the live shape:
`{"design_dna": {...}, "verified_content": {}, "unresolved_facts": [...]}`.

*Application* — `adapter.py:1876-1883` loads the **whole file** as the DNA object:
```python
dna_path = workspace / "design-dna.json"
if dna_path.exists():
    with dna_path.open("r", encoding="utf-8") as f:
        design_dna = json.load(f)      # <-- the wrapper IS the DNA
```
*Validation* — `composition.py:57-62` `validate_composed_dna` → `validate_typography`
(`design_dna.py:15-20`, reads `dna["typography"]`) and `validate_reference_synthesis`
(`references.py:268-272`, reads `dna["reference_synthesis"]`).
*Every test and the harness* — `r1_harness.py:1109-1122` `_design_dna()` returns the
**unwrapped** object; `test_revision_frontend_failure.py:53-56` likewise.

**E2 — Consequence matrix for the wrapper shape** (all proven by reading the validators):

| Consumer | Wrapper shape behaviour | Severity |
|---|---|---|
| `validate_composed_dna` → `isinstance(dna, dict) and dna` | **passes** (non-empty dict) | — |
| `validate_typography` → `dna.get("typography")` | `None` → `set()` → **silently passes** | **typography enforcement bypassed** |
| `validate_reference_synthesis` (when references exist) | `dna.get("reference_synthesis")` is `None` → raises `"Missing or invalid reference evidence"` | **Phase 7 hard-fails with a misleading message** |
| `state.design_dna = design_dna` (`build.py:938`) | persists a wrapper | VISION prompt (`adapter.py:1562,1576`) receives a nested blob |
| `design_dna_version` (`build.py:937`, `revise.py:640`) | `.get("version")` misses → falls back to increment | silent |
| `check_design_dna` (`qa/deterministic.py:17-27`) | valid JSON → passes | — |

**E3 — FRONTEND prompt contract.** `adapter.py:1432-1433`: *"Create a Design DNA document at
{workspace}/design-dna.json following the minimal Design DNA contract."* The only definition of
"the minimal Design DNA contract" is the skill — i.e. the wrapper. **The model followed the
contract correctly.** The defect is that the contract and the validator disagree.

**E4 — `_parse_frontend_response`.** `adapter.py:1849-1928`. Authoritative on `success`/`error`;
loads DNA from the workspace unconditionally; returns `{"success": True, "design_dna": None}`
when no declaration is parseable (`:1909-1910`). It **never inspects the DNA's shape.**

**E5 — Other validators:** `design_dna.py` (typography only, 42 lines total),
`references.py:227-233` `validate_characteristics` (exact role-set match),
`qa/deterministic.py:17-32` (existence + valid JSON + `src/App.tsx` is a file).
There is **no schema/type validator** for the DNA document anywhere.

**E6 — Build/revision/QA consumers:** `build.py:718` and `:596` (compile repair),
`revise.py:622`, `qa/orchestrator.py:216` → all via `validate_composed_dna`.

**E7 — Verdict: PROVEN latent incompatibility, not valid by design.** Two independent
confirmations: (i) every consumer reads top-level keys that the contract shape does not have;
(ii) `r1_harness.py:1109-1122` — the repo's own integration harness — writes the *unwrapped*
shape, so the test suite has never exercised the contract shape.

**Did it contribute to p20?** **Not as a failure** — p20 timed out before any parse. As a
**plausible contributor to the loop**: a model that `read_file`s its own
`{"design_dna": {...}}` artifact sees a shape that does not match "a Design DNA document," which
is a natural trigger for the 2010.9s rewrite and for further re-reading. **Hypothesis, not
proven** — requires the transcript.

**E8 — The missing regression test (verbatim, as specified):**
> "a Design DNA artifact produced according to the FRONTEND contract must round-trip through
> the application parser and `validate_composed_dna`."

**It does not exist.** A repo-wide grep for `validate_composed_dna` in `tests/` returns **3
hits, all in `test_qa.py:263-309`, all patching it** to capture the `state` argument — none
exercise a persisted artifact.

---

## 3. Evidence classification

**Proven by code + p20 counters (high confidence):**
- `_has_complete_frontend_artifacts` is unreachable on the success path (A1/A4).
- No phase boundary / `DESIGN_DONE` concept exists (A2/B3).
- `max_iterations = sys.maxsize`, never overridden on the oneshot path (C2).
- Loop caps cover only `web_search`/`delegate_task`; `hard_stop_enabled` default `False`; `terminal` structurally exempt (C3).
- `tool completed: … (error)` advances the progress clock; receipt has no tool-error counter (RC3).
- `tool_names` is 2× inflated; `model_started` is ~2× inflated (C1).
- `read_file` pinned to never spill, 100K chars verbatim; per-turn budget is per-turn only; `proactive_prune_tokens: 0` (C4/C5).
- `design-dna.json` wrapper bypasses typography validation and hard-fails reference projects (E2).
- `frontend` alias is never resolved because `--provider` is always passed; `"glm-5.3" = 1_048_576` exists; 256K is advisory-only (D2/D3/D9).
- Compression is ON in oneshot with `threshold_tokens = 192_000` under the 0.75 small-window floor (D4).
- DNA written exactly twice; zero `src/**` mutation in 2610s (A5/B1).

**Hypothesis — needs the p20 transcript / workspace (must not be treated as cause):**
- Why the agent read 275 times instead of implementing (A5).
- Which turn rewrote the DNA at 2010.9s and why (B2/B6).
- Whether the 4 `write_file` calls were *refused* (the `(error)` suffix is invisible today — this is exactly what RC3's missing counter would reveal).
- Whether compaction ran, was blocked, or was `ineffective` (D5).
- Whether the wrapper shape triggered the rewrite (E7).

**Contradiction with the task brief — resolved:**
The brief's tool histogram reads as raw call counts. It is a 2× event-occurrence histogram.
**`read_file = 550` is not 550 `read_file` calls.** The corrected figures are ~476 invocations
and ~275 reads. This does not change any conclusion — it strengthens C (the real numbers are
still far past any sane bound) — but it must be corrected before any metric is built on it.

---

## 4. Root cause vs contributing factor vs unrelated

| # | Finding | Status | Independent? |
|---|---|---|---|
| RC1 | No machine-enforced convergence toward the deliverable (A) | **ROOT CAUSE** | Yes |
| RC2 | No budget of any kind on the loop (C2/C3/C5) | **ROOT CAUSE** | Yes — independent of RC1 |
| RC3 | Progress clock + receipt cannot see a *refused* tool call; counters 2× inflated | **ROOT CAUSE (observability)** — makes RC1 unfixable-blind today | Yes |
| RC4 | `design-dna.json` contract shape ≠ validated shape (E) | **INDEPENDENT BUG** — would have failed Phase 7 on any reference project | Yes |
| RC5 | `frontend` alias never resolved ⇒ 256K advisory fallback (D1–D3) | **INDEPENDENT METADATA/CONFIG BUG** | Yes |
| RC6 | Unbounded tool-result accumulation, no prune, no counters (D6/C4) | **CONTRIBUTING** to slowness and to context size | Partly RC3 |
| B5 | DNA unversioned within an invocation | **LATENT DEFECT** | Yes (small) |
| B1/B4 | DNA rewritten at 2010.9s | **SYMPTOM of RC1** | No |
| terminal-schema vs prompt conflict (C7) | **UNRELATED** — report only | No |
| 330–350K input tokens | **NOT PROVEN A BUG** — provider accepted them | No |

**RC1 and RC2 are both necessary.** A convergence guard with no budget is a stopwatch; a budget
with no convergence guard is a leash with no destination. Neither alone fixes p20.

---

## 5. Severity

| Finding | Severity |
|---|---|
| RC1 no convergence enforcement | **BLOCKER** — every initial build can burn 45 min and fail; and a FRONTEND that *lies* about success is accepted |
| RC2 no loop budget | **BLOCKER** — unbounded by construction |
| RC3 refused-tool invisibility + 2× counters | **BLOCKER (observability)** — hides the failure mode and corrupts any metric built on it |
| RC4 DNA schema mismatch | **HIGH** — silent enforcement bypass + misleading hard-fail |
| RC5 alias never resolved | **HIGH** — corrupts every derived number for every role, plus a recurring misleading warning |
| RC6 unbounded tool-result accumulation | **MEDIUM** — real cost/latency driver |
| B5 DNA unversioned in-invocation | **MEDIUM** |
| B1/B4 DNA rewrite | **LOW** (symptom) |
| terminal-schema/prompt conflict | **LOW** (documentation conflict only) |

---

## 6. Smallest safe fix for each proven defect

**S1 — Receipt counters stop double-counting; add a tool-error counter.** *(RC3, first)*
`website-builder/app/hermes/watchdog.py`. Gate the `tool_name_counts` increment on
`phase == _PHASE_COMPLETED` (one event per invocation) or add a parallel
`tool_names_by_phase`; add `tool_error_count` by detecting the `" (error)"` suffix the emitter
already appends (`tool_executor.py:1816-1817`); add an explicit `model_call_count` alias for the
already-exact `model_completed_count`. Observation only — **changes no bound and no decision.**
Because changing `tool_names` semantics (not merely adding) is incompatible under the module's
own rule (`watchdog.py:196`), bump `FORENSICS_SCHEMA` to `frontend_forensics/4` and update
`test_build.py:1781`'s pinned literal.

**S2 — Resolve the role model through `model_aliases`; fail closed on a fallback window.**
*(RC5)* `website-builder/app/hermes/adapter.py` **only — do not touch `hermes_cli/oneshot.py`.**
In `_run_hermes_cli`, resolve `model` through `hermes_cli.model_switch.DIRECT_ALIASES` before
appending `--model`, so the child receives the real slug and the existing `oneshot.py` gate
becomes irrelevant. Add to `_validate_role_configuration` (`adapter.py:360-443`, already
fail-closed and already real-resolves every role): resolve the effective context length and
**fail closed when it equals `DEFAULT_FALLBACK_CONTEXT`** for a model that is not a known
provider id. Report the role name only, never the value.
*This is not a context-limit change* — it makes the resolver see the truth instead of
overriding a number. Satisfies the "do not change model context limits" constraint.

**S3 — One Design DNA loader; fix the contract; defensive unwrap.** *(RC4)*
Add `load_persisted_design_dna(path)` to `app/core/design_dna.py` as the **single** loader; use
it from `adapter.py:1876-1883` and `qa/deterministic.py:17-27`. It returns the **unwrapped**
object, unwrapping a top-level `design_dna` key when present. Correct
`.hermes/skills/website-builder-design-dna/SKILL.md:28-60` to show the **unwrapped** object —
the shape every consumer, validator, and the harness already assume. On a detected wrapper,
record it (do not silently accept). Do **not** teach the validators to read through a wrapper:
that would legitimise two shapes.

**S4 — Fail closed on a never-implemented declared success.** *(RC1, smallest slice; NOT a
convergence guard, NO new timeout)*
In `frontend_build`'s success path (`adapter.py:1359-1360`), if the summary declared success but
`not self._has_complete_frontend_artifacts(workspace)`, return
`FRONTEND_IMPLEMENTATION_MISSING` instead of accepting. This is a **postcondition check on an
already-declared result** — no time-based behaviour, no watchdog change. Reuses the existing
single definition of "complete."

**S5 — Give the loop a budget.** *(RC2, split by blast radius)*
- *Now, zero repo change:* in `~/.hermes-website/config.yaml` (operator config, **outside this
  repo** — needs an explicit go-ahead) set `tool_loop_guardrails.hard_stop_enabled: true` and
  `compression.proactive_prune_tokens: 48000`.
- *Later, core repo:* extend `LoopCapConfig` (`tool_guardrails.py:202-203`) with
  `max_read_file_per_turn` / `max_terminal_per_turn`, enforced in `_check_loop_cap`
  (`:659-724`) mirroring the existing `web_search` branch. Own batch, own test matrix.
- *Never:* a crude total-tool-count kill.

**S6 — Add the two missing `WorkspaceSampler` facts** *(prerequisite for any future
convergence guard; pure observation, changes nothing)*
`design_dna_first_seen_offset_seconds`, `first_src_mutation_offset_seconds`,
`last_deliverable_mutation_offset_seconds` — each one-to-three lines beside the existing
`first_complete_offset_seconds` (`watchdog.py:957-975`). Needed so the guard's predicate has
phase-relative inputs.

---

## 7. Required tests

**Missing today, must be added:**

1. **Design DNA contract round-trip** (the exact test named in the brief, E8): build an artifact
   *from the SKILL.md contract*, write it to `design-dna.json`, run it through
   `_parse_frontend_response` and `validate_composed_dna`; assert success **and** that
   `validate_typography` actually observes the typography. Must fail on current code.
2. **Wrapper must not bypass typography enforcement** — assert a 3-font wrapper raises
   `DESIGN_DNA_TYPOGRAPHY_VIOLATION`, not silence.
3. **Wrapper + references** must raise a dedicated `DESIGN_DNA_SHAPE_INVALID`, not
   `"Missing or invalid reference evidence"`.
4. **Receipt counter fidelity** — scripted N sequential tool calls ⇒ `tool_names` total == N
   (not 2N), `tool_started`/`tool_completed` still exact.
5. **`(error)` suffix classification** — `tool completed: write_file (0.1s) (error)` increments
   `tool_error_count` and is not counted as a successful completion.
6. **Alias resolution** — a profile whose `website_builder.models.FRONTEND.model` is a
   `model_aliases:` key puts the **resolved** slug on argv; preflight fails closed when the
   context length falls back.
7. **Success-path postcondition** — a FRONTEND `success: true` summary with an unimplemented
   `src/App.tsx` is rejected as `FRONTEND_IMPLEMENTATION_MISSING`.
8. *(Deferred to the convergence batch)* guard arms only on a fresh workspace; never on compile
   repair (`build.py:559-565`) or QA repair (`qa/orchestrator.py:192-193`, same `workspace`
   object); never while `src/**` is changing; the two BOUNDARY LOCK tripwires
   (`test_frontend_watchdog.py:1338-1370`) must be **inverted deliberately**, with their
   recorded intent preserved in the replacement tests.

---

## 8. Suggested implementation order

**This batch — no convergence, no new timeouts, no core-repo change:**

1. **S1** receipt counters + `tool_error_count`, schema bump to `/4`. Tests 4, 5.
2. **S3** one Design DNA loader + SKILL.md contract fix + defensive unwrap. Tests 1, 2, 3.
3. **S4** fail closed on a never-implemented declared success. Test 7.
4. **S2** alias resolution + preflight fail-closed. Test 6.
5. **Re-run p20 and read the corrected receipt** before designing anything further. S1 is
   deliberately first: until tool errors are visible, hypothesis-vs-cause for the 275 reads and
   the 2 writes cannot be settled.

**Next batch (explicitly not this one):** S6 sampler facts, then the convergence guard, then S5's
core-repo caps.

Rationale for the order: S1 is zero-risk and unblocks diagnosis; S3 and S4 are small, independent,
and each prevents a *silent wrong success*; S2 is a correctness fix with a fail-closed preflight.
RC1's guard goes last because it must be built on counters that are correct and a predicate whose
inputs exist.

---

## 9. Convergence guard — specification for the deferred batch

Recorded now so the next batch does not re-derive it. **Not implemented here.**

**Outcome code:** `FRONTEND_NO_CONVERGENCE`, a member of `TIMED_OUT_OUTCOMES` (exit 124).
Rationale: it *is* a supervision termination, so `timed_out=True` gating artifact recovery at
`adapter.py:1346` stays semantically correct — and because the predicate requires *incomplete*
artifacts, recovery can never actually admit it. It must never be confused with a hang, hence a
distinct code + an `ERROR_MESSAGES` entry per `runtime.py:1148`.

**Predicate — arm only when all three hold, evaluated on the existing 30s sample cadence:**
1. `design-dna.json` exists, **and**
2. `src/App.tsx` is byte-identical to `templates/frontend-starter/src/App.tsx`, **and**
3. neither `design-dna.json` nor any file under `src/**` has changed for `convergence_seconds`
   (S6's `last_deliverable_mutation_offset_seconds`).

**Why this cannot kill a healthy long build — four structural properties:**

1. **Self-disarming on every non-initial path.** Compile repair (`build.py:559-565`) and QA
   repair (`qa/orchestrator.py:192-193`, the *same* `workspace` object) both invoke
   `frontend_build` on a workspace where `src/App.tsx` already differs from the starter, so
   `complete_at_end` is True at sample 0 and the guard never arms. Only the initial build
   (`build.py:684-696`: fresh `create_workspace` + `_copy_starter`) can arm it. This is
   structural, not a heuristic.
2. **Never kills a progressing build.** Condition 3 requires *zero* deliverable change for the
   full window. Any `src/` write at all resets it, so a slow iterative implementer is safe.
3. **Structurally independent of liveness.** Every input comes from
   `WorkspaceSampler._scan` — stat-only `(relpath, size, mtime_ns)` — and **no progress event is
   read**. It cannot be refreshed by model activity, and it cannot be confused with the
   watchdog's `last_activity_at` / `last_progress_at`. This is precisely the alive-vs-converging
   separation the brief demands.
4. **Fails in the safe direction.** The fingerprint's documented blind spot (a same-size rewrite
   inside one filesystem mtime tick, `watchdog.py:797-799`) makes the guard *less* likely to
   fire, never more.

**Residual risk, stated honestly:** a healthy model that legitimately spends more than
`convergence_seconds` between writing the DNA and its first `src/` write would be killed. p20's
own DNA→deadline was 2610s with zero mutation, but that number comes from a *broken* run and
cannot calibrate the healthy case. Mitigations: (i) a large default, `convergence_seconds = 900`,
matching `max_single_operation_seconds`; (ii) calibrate against a known-good run's receipt before
tightening; (iii) the two existing BOUNDARY LOCK tripwires must be inverted deliberately, not
quietly; (iv) a distinct outcome code so it is never mistaken for a hang.

**How genuine progress is defined for convergence — without reusing liveness semantics.**
**Deliverable progress** = a change to the set of files the task must produce, measured from the
filesystem on the sampler's own cadence, keyed on `(relpath, size, mtime_ns)` over `src/**` +
`design-dna.json`. Three facts suffice, all already existing or one-to-three-line additions
(S6): `design_dna_first_seen_offset_seconds`, `first_src_mutation_offset_seconds`,
`last_deliverable_mutation_offset_seconds`. The predicate is a pure function of those offsets and
the sample clock. **It reads no progress event.** Liveness and convergence are different inputs
to different decisions — which is the property that must not be conflated.

---

## 10. Explicitly NOT to be changed

- **Watchdog bound semantics.** idle 180 / hard 2700 / max_single_operation 900 / legacy 900 /
  startup grace 60 (`config/default.yaml:73-95`). No new timeout values.
- **`max_single_operation_seconds` semantics** — max *no-genuine-forward-progress* while an
  operation is in flight, not max operation duration (`watchdog.py:167-173,726-738`).
- **The `advance` classification** — `_KEEPALIVE_PREFIX_RULES`, the fail-safe-forward default,
  and the explicit-`advance` requirement at stream emitters (`agent/progress_events.py`).
- **Forensic privacy contract** — no source, prompts, tool arguments, URLs, secrets, or absolute
  paths in the receipt (`watchdog.py:311-322`).
- **`_has_complete_frontend_artifacts` as the single definition of "complete"** — reuse, never
  duplicate a second completeness notion.
- **Canonical URL, `promote.py`, `release.py`, `hydrate.py`, `registry.py`** — R1 LIVE publish and
  R2 canonical source are untouched.
- **The three profile skills' ownership and scope**, except the `SKILL.md` schema correction in S3.
- **Core-repo files** — `hermes_cli/oneshot.py`, `run_agent.py`, `agent/progress_events.py`,
  `agent/stream_shapes.py`, `toolsets.py`, `agent/tool_guardrails.py`, `agent/context_compressor.py`,
  `agent/model_metadata.py`. **No core change in this batch.** S2 fixes the alias on the
  website-builder side precisely so core needs no edit.
- **`read_file`'s `PINNED_THRESHOLDS = inf`** — deliberate, prevents persist→read→persist loops
  (`budget_config.py:9-13`). Do not "fix" it.
- **The `terminal`-schema vs prompt conflict** — report it; do not resolve it here.
- **No crude total-tool-count kill.**
- **Batch D.**

**Open evidence needed before the convergence batch** (all from the VPS, not this repo):
`~/.hermes-website/diagnostics/tg-6329821361-p20/1749b2664dd244c69392bf8a7183bebf.json` (the
corrected receipt after S1) and the p20 workspace's actual `design-dna.json` + the child
transcript, to settle the four hypotheses in §3.
