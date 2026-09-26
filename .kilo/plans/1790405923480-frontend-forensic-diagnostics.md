# FRONTEND forensic diagnostics (Website Builder)

Instrument one supervised FRONTEND invocation so the next 45-minute run answers
*which* non-termination mode occurred. Observation only: no behaviour, prompt,
timeout, toolset, skill, or convergence change.

## Verdict on the 45-minute failure

**Provable from the code, independent of any log:**

1. A 45-minute kill can only be `FRONTEND_HARD_TIMEOUT` — the other bounds are
   180s idle (`watchdog.py:68`), 900s in-flight backstop (`:78`), 900s legacy
   (`:83`). 2700s (`:72`, `config/default.yaml:58`) is the only 45-minute bound.
2. The hard fuse is reachable **only** when `progress_channel_confirmed` is true
   (`watchdog.py:613-635`); otherwise the 900s legacy bound fires first.
3. The idle-timeout predicate is `no_progress >= 180 and not in_flight`, where
   `is_in_flight` is `active_operation is not None and no_progress < 900`
   (`:637-642`, `:212-221`). In-flight protection can therefore suppress the 180s
   rule for up to `FRONTEND_MAX_SINGLE_OPERATION_SECONDS` (900s). A hard timeout
   does **not** prove there was no 180-second gap in progress: it proves only
   that at no earlier tick did the predicate hold — i.e. either progress had
   arrived within 180s, or an operation was in flight and progress had arrived
   within 900s. A run of repeated long operations, each under the 900s in-flight
   bound, can reach the fuse with real multi-minute silences between events.
   This run was therefore *not* hung, *not* blocked on a dead subprocess, and
   *not* unattested — but it was not necessarily continuously active either.

**Undecidable from what is kept today.** `FrontendInvocation.diagnostics()`
(`watchdog.py:223-238`) retains `last_progress_kind` — a *single last value* —
plus one `progress_event_count` scalar. Kind/phase distribution is discarded,
`desc` is dropped at `watchdog.py:450-453`, and nothing about the workspace is
recorded. No category below can be separated from the surviving record. That is
the exact gap this plan closes.

**Category G — real, not in the A-F list.** The watchdog measures *activity*, not
*convergence*. `receiving stream response → STREAM/active`
(`progress_events.py:82`) and `UNKNOWN/active` both refresh liveness
indefinitely (`watchdog.py:179-192`). Nothing in the event vocabulary expresses
"this is not forward progress". A run can be 100% active and 0% converged and
only the hard fuse ends it. Companion **H**: only 7 description prefixes are
mapped (`progress_events.py:75-94`); any other keep-alive driver (retry backoff,
compression, subagent) becomes `UNKNOWN`, still refreshes liveness, and is
currently invisible.

**A–F: none is excluded.** On the corrected predicate, B, G, and the repeated
long-operation pattern lead. A, C, D, and **E** all remain live: conflicting
guidance is itself a plausible driver of sustained model/tool activity — the
prompt's "Do NOT run npm ci, npm run build, or npm run typecheck"
(`adapter.py:1284-1285`) against a skill steering toward verification, or the
declarative Design DNA contract (`adapter.py:1293-1298`) against a polishing
instinct — and nothing distinguishes it until runtime evidence exists. **F is
also not excluded**; it is classified from the combined workspace-mutation and
model/tool signature, never from artifact completeness alone.

**Evidence caveat.** No runtime evidence exists on this host: `~/.hermes`,
`~/.hermes-website`, `~/.website-builder` and all log dirs are absent. 45 minutes
is the *configured* ceiling, not a captured run. Per the operator, only the
timeout was observed — whether the build then recovered via the artifact path
(`adapter.py:1193`) is unknown. The receipt records artifact completeness at
**first** observation and **at end**, which answers that fork retroactively.

## Preserved (explicit non-goals)

FRONTEND toolsets `["file","terminal","skills"]`, the three profile skills, the
prompt, all four timeout values, the artifact-recovery gate
(`adapter.py:1193-1194`), and `_has_complete_frontend_artifacts` itself
(`adapter.py:1209-1242`). No convergence guard. No early termination. No new
core-repo surface — see "Zero core changes" below.

## Zero core changes

Every requested counter is derivable from what is **already on the wire**:
`oneshot.py:279-295` emits `{run_id, event, kind, phase, desc}`, and `desc` is
already built from tool name and duration only —
`f"executing tool: {function_name}"` (`tool_executor.py:1040`) and
`f"tool completed: {name} ({dur}s)"` (`:1815`). **No changes to `run_agent.py`,
`agent/progress_events.py`, `hermes_cli/oneshot.py`, `toolsets.py`, or any
skill.** The work is confined to `website-builder/app/hermes/`.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Write location | `<hermes_home>/diagnostics/<project_id>/<invocation_id>.json` | Sibling of `runs/`, so it survives `runs_dir.rmdir()` (`adapter.py:595-599`) and `progress_path.unlink()` (`watchdog.py:667-670`) |
| Written by | The supervisor itself, on **both** success and timeout | Covers all four call sites at once; they currently drop `invocation` at `build.py:544`, `orchestrator.py:450`, `revise.py:380` |
| Durability | `mkstemp` + `fsync` + `os.replace`, prune to newest 20 per project | In-repo precedent `app/core/state.py:226-236`; bounded disk |
| Completeness probe | `artifacts_probe: Optional[Callable[[], bool]]` injected by the adapter, wrapping `self._has_complete_frontend_artifacts(workspace)` | Reuse, never duplicate (AGENTS.md "extend, don't duplicate") |
| Probe is observational | Never consulted by the timeout decision | This is the deferred convergence guard; wiring it in would change behaviour |
| Workspace fingerprint | Deterministic **metadata** fingerprint — a sha256 digest over sorted `(relpath, size, mtime_ns)` for `design-dna.json` + `src/**` | File contents are **never read** and never stored, so the "no source contents" constraint holds absolutely. It is a change detector, not a content or source hash |
| Sample cadence | Baseline at t≈0, then every 30s; cap 256 samples | 256 × 30s = 128 min, covers the 2 × 45 min worst case |
| File cap | 2000 files, `node_modules`/dotdirs excluded, `truncated: true` on overflow | A pathological tree must never stall the supervisor loop |
| Desc safety | Re-clamp to 120 chars, then redact URLs, absolute paths, and >32-char hex/base64 runs | `bound_activity_description` (`session_activity.py:19`) clamps but does **not** redact; other `_touch_activity` sites emit free-form text |
| Addition beyond the listed fields | `longest_activity_gap_seconds`, `distinct_fingerprints`, `tool_names`, `mutated_paths` (relative only) | `longest_activity_gap_seconds` is the direct discriminator between continuous activity and in-flight-protected silence (see corrected predicate above); `tool_names` separates A from B. All bounded, no arguments, no contents |
| Retention / surfacing | Operator-only. Not added to the user-facing error reply | `runtime.py` render path stays untouched |

## Tasks

### 1. `website-builder/app/hermes/watchdog.py` — counters and ring buffer

Extend `FrontendInvocation` (`:154`) with: `model_started`, `model_completed`,
`tool_started`, `tool_completed`, `stream_active`, `first_activity_at`,
`longest_activity_gap_seconds`, a `deque(maxlen=20)` of
`{offset, kind, phase, desc}`, and a bounded `tool_name_counts` dict (cap 32
distinct names, then set `tool_name_counts_truncated`).

In `_read_progress_events` (`:407-453`), after the existing
`note_activity`/`note_operation` calls, increment the matching counter from
`kind` + `phase` and push the normalized desc. Reuse the existing constants
(`_KIND_UNKNOWN` `:125`); add `_KIND_MODEL`, `_KIND_TOOL`, `_KIND_STREAM`,
`_PHASE_STARTED`, `_PHASE_COMPLETED` as mirrored literals, matching the file's
existing "duplicated as literals" convention at `:120-125`.

`longest_activity_gap_seconds` updates on every accepted event as
`now - last_activity_at`, before `last_activity_at` is reset. Compute it for
**every** accepted event including `UNKNOWN` and `STREAM`, so it measures the
true silence between any two progress signals. Track tool names by parsing the
two known desc prefixes (`executing tool: `, `tool completed: `) before
redaction — a tool name is not an argument.

### 2. `website-builder/app/hermes/watchdog.py` — redaction helper

New module-level `normalize_forensic_desc(raw: str) -> str`, next to
`bound_activity_description` usage. Collapse whitespace, re-clamp to
`ACTIVITY_DESCRIPTION_MAX`, then replace `scheme://…`, Windows drive-letter and
POSIX absolute paths, and runs of 32+ hex/base64-ish characters with a
`<redacted>` placeholder. Pure function, no I/O, no raising.

### 3. `website-builder/app/hermes/watchdog.py` — workspace sampler

New small class `WorkspaceSampler`, constructed per invocation with the workspace
root (`cwd`) and the injected `artifacts_probe`.

- `sample(now)` — walks `design-dna.json` and `src/**`, skipping any path
  segment starting with `.` and the name `node_modules`, up to
  `WORKSPACE_SAMPLE_MAX_FILES`. Computes `metadata_fingerprint` from
  `(relpath, size, mtime_ns)` only, never opening a file for reading. Compares
  against the previous fingerprint to derive `source_mutation_count`,
  `unique_source_files_mutated`, `mutated_paths` (relative, capped 50),
  `first_mutation_offset_seconds`, `last_mutation_offset_seconds`. Calls
  `artifacts_probe` at most once per sample and records
  `first_complete_offset_seconds` on the first `True` only.
- Every public method wrapped so **no** exception escapes. The sampler runs
  synchronously inside the poll loop; keep it allocation-light and never let it
  raise into supervision.

Add named constants beside the existing ones (`:60-109`):
`WORKSPACE_SAMPLE_INTERVAL_SECONDS = 30.0`, `WORKSPACE_SAMPLE_MAX_FILES = 2000`,
`MAX_SAMPLE_SERIES = 256`, `MAX_MUTATED_PATHS = 50`,
`MAX_TOOL_NAME_DISTINCT = 32`, `FORENSICS_SCHEMA = "frontend_forensics/1"`.
Add `diagnostics_dir`, `artifacts_probe`, and `workspace` keyword parameters to
`supervise_frontend_run` (`:532-547`), defaulting to `None` so every existing
test and caller is unaffected.

### 4. Persist the receipt

At the end of `supervise_frontend_run`, after the poll loop and tree cleanup,
build the receipt (schema below) and, when `diagnostics_dir` is set, write it
atomically and prune to the newest 20 files in that project directory. Write it
for **every** terminal path: success, `FRONTEND_IDLE_TIMEOUT`,
`FRONTEND_HARD_TIMEOUT`, `FRONTEND_LEGACY_TIMEOUT`. Wrap the whole write in
`try/except` — a failed receipt must never change the supervision result. Add
`"forensics_receipt": <path or null>` to the returned `SupervisedRun.diagnostics`
(`:223-238`, `:695-703`).

The in-memory `diagnostics()` dict keeps its existing 12 keys and gains the
forensics block, so the existing durable channel at `build.py:703-704` carries it
on the initial-build path without being load-bearing.

### 5. `website-builder/app/hermes/adapter.py` — wire it up

In `_run_hermes_cli_supervised` (`:536-617`), pass to `supervise_frontend_run`:
`diagnostics_dir=self.hermes_home / "diagnostics" / project_id`,
`workspace=cwd`, and
`artifacts_probe=lambda: _safe(self._has_complete_frontend_artifacts, cwd)`.
`cwd` is already the workspace for every FRONTEND call
(`adapter.py:1175`). No other edit; `supervise=True` remains the only caller
(`adapter.py:1180`).

Also pass the same `artifacts_probe` into `_run_hermes_cli_legacy` (`:619-650`)
so a degraded-mode timeout still records `channel_confirmed: false` and
`outcome: FRONTEND_LEGACY_TIMEOUT`.

### Receipt schema

```json
{
  "schema": "frontend_forensics/1",
  "invocation_id": "…", "project_id": "…", "build_operation_id": "…",
  "pid": 12345, "outcome": "FRONTEND_HARD_TIMEOUT", "returncode": -15,
  "elapsed_seconds": 2700.4, "idle_for_seconds": 1.2, "channel_confirmed": true,
  "policy": {"idle": 180.0, "hard": 2700.0, "max_single_operation": 900.0},
  "counters": {
    "model_started": 412, "model_completed": 411,
    "tool_started": 180, "tool_completed": 180,
    "stream_active": 900, "unknown_active": 12,
    "total_progress_events": 2095, "stale_events_discarded": 0
  },
  "activity": {
    "first_offset_seconds": 3.1, "last_offset_seconds": 2698.7,
    "longest_gap_seconds": 812.4, "last_kind": "TOOL", "last_phase": "started",
    "active_operation_at_end": "TOOL"
  },
  "workspace": {
    "sample_interval_seconds": 30.0, "samples": 90,
    "distinct_fingerprints": 57,
    "source_mutation_count": 56, "unique_source_files_mutated": 3,
    "first_mutation_offset_seconds": 60.0, "last_mutation_offset_seconds": 2640.0,
    "mutated_paths": ["design-dna.json", "src/App.tsx"],
    "design_dna_present": true, "app_tsx_present": true, "truncated": false
  },
  "artifacts": {
    "probe_available": true, "complete": true,
    "first_complete_offset_seconds": 210.5, "complete_at_end": true
  },
  "tool_names": {"write_file": 140, "read_file": 38},
  "tool_names_truncated": false,
  "recent_activity": [
    {"offset_seconds": 2690.0, "kind": "TOOL", "phase": "started",
     "desc": "executing tool: write_file"}
  ],
  "forensics_receipt": "…/diagnostics/<project_id>/<invocation_id>.json"
}
```

Never present: prompt text, model responses, tool arguments, file contents,
absolute paths, URLs, credentials. `mutated_paths` are workspace-relative only.

## Reading the receipt

| Signature | Verdict |
|---|---|
| `longest_gap_seconds` large (hundreds) with `active_operation_at_end` set and hard outcome | **Long-operation pattern** — repeated operations each under the 900s in-flight bound, suppressed from the idle rule. No progress gap under 180s is required |
| `longest_gap_seconds` small, `stream_active` dominant, `tool_started` ≈ 0 | **G** stream liveness without progress |
| `unknown_active` dominant, ring buffer shows a non-model/tool driver | **H** unmapped keep-alive (retry backoff / compression / subagent) |
| `model_started` ≫ `tool_started` (>5×), `source_mutation_count` 0 | **B** model-request loop |
| `tool_started` high, `unique_source_files_mutated` 1–2, fingerprint changes most samples, artifacts complete early | **A** repetitive file/tool loop |
| `tool_started` high, unique files many, fingerprint changes most samples, ring buffer shows repeated near-identical edits | **D** repeated polishing |
| `model_started` in the hundreds **and** any of the loop shapes above | **C** context accumulation as accelerant |
| `unique_source_files_mutated` grows steadily, `tool_names` diverse, model/tool ratio ≈1, mutations spread across the whole run | **F** genuine implementation — the 45 min ceiling is too tight, not a loop |
| Alternating MODEL/TOOL on the same one or two tools, no fingerprint change, no mutation growth | **E** instruction/skill conflict driving repeated unresolvable attempts |

**Artifact completeness (answers the operator's build-outcome question, and
does not classify F).** `first_complete_offset_seconds == null` means only that
**no complete deliverable was observed before the timeout**; genuinely slow
implementation (F) is equally consistent with that, so completeness alone never
classifies F. For the *build outcome*: `artifacts.complete` false at the end
means the recovery gate at `adapter.py:1193-1194` could not have fired, so the
build failed outright; `complete: true` with a small
`first_complete_offset_seconds` means the work finished early and the run failed
to stop.

**Known limits, to state when reading a receipt:** `active`-phase events are
coalesced to ≤1 per 5s by the emitter (`oneshot.py:244`), so `stream_active` and
`unknown_active` are lower bounds while `model_*`/`tool_*` are exact (boundary
events are never coalesced). A 30s sampler collapses any rewrite burst inside
one window into a single mutation, so `source_mutation_count` is a lower bound.
The fingerprint tracks `(relpath, size, mtime_ns)`, so a same-size rewrite
within one filesystem mtime tick is invisible.

## Tests

Extend `website-builder/tests/test_frontend_watchdog.py`, reusing the existing
`Harness` (`:114-182`), `FakeClock`, `FakeChild`, and `ProgressScript` (`:74-111`).
The only harness change needed: let `ProgressScript.at()` take an optional
`desc` (it currently hardcodes `f"{kind.lower()}:{phase}"` at `:101`).

1. Counters classify correctly from a scripted MODEL/TOOL/STREAM/UNKNOWN stream;
   `model_started`/`tool_completed` exact.
2. Ring buffer holds at most 20, oldest evicted, each entry carrying
   offset/kind/phase/desc.
3. `normalize_forensic_desc` redacts a URL, a Windows absolute path, a POSIX
   absolute path, and a 40-char hex run; preserves `executing tool: write_file`.
4. Unchanged tree → constant `metadata_fingerprint`, `mutation_count == 0`. One
   file touched → fingerprint changes, `mutation_count == 1`,
   `unique_source_files_mutated == 1`, that relative path in `mutated_paths`.
5. The fingerprint is a function of `(relpath, size, mtime_ns)` only — assert it
   is unchanged when file **content** is rewritten to a different same-length
   value with the mtime pinned, and that no file is ever opened for reading.
6. Sampler sets `truncated: true` past the file cap and still returns.
7. A raising sampler or a raising `artifacts_probe` does not change the
   supervision outcome (run still completes normally).
8. `artifacts.first_complete_offset_seconds` is set on the first `True` and never
   overwritten; stays `null` when never complete.
9. **No convergence guard (explicit boundary test):** artifacts complete at
   t=300s with activity continuing → the run is still `None` outcome at t=300s
   and is not terminated. Fails if anyone wires the probe into the decision.
10. **In-flight suppression (corrected predicate):** a scripted 800s silence
    inside a started operation records `longest_gap_seconds` ≈ 800, does *not*
    raise `FRONTEND_IDLE_TIMEOUT` at 180s, and keeps running — pinning that
    in-flight protection legitimately suppresses the idle rule up to 900s.
11. Receipt is written on **success** and on each of the three timeout codes, and
    the file still exists after `runs_dir.rmdir()` / `progress_path.unlink()`.
12. Receipt is bounded: 300 scripted events + 300 samples produce a file under a
    stated size ceiling, with the sample series capped at 256.
13. Pruning keeps the newest 20 per project.
14. Receipt write failure (injected raising writer) leaves the run's outcome and
    returncode untouched.
15. Receipt contains none of: the prompt string, an absolute path, a URL.

In `website-builder/tests/test_hermes_adapter.py`: assert the adapter passes
`diagnostics_dir`, `workspace`, and a working `artifacts_probe`, and that
`artifacts_probe()` reflects a real `_has_complete_frontend_artifacts` result.
The existing `test_frontend_build_requests_extended_timeout` and the
`timeout_seconds` default/override tests are untouched — the non-watchdog path is
unchanged.

## Risks

- **The probe is the convergence guard's shape.** One line of wiring turns
  observation into early termination. Mitigation: task 5 passes it only as a
  sampler input, and test 9 locks it.
- The sampler runs in the supervisor poll loop. Mitigated by the 2000-file cap,
  the 30s cadence, and the no-raise wrapper.
- `mutated_paths` reveals relative source layout. Relative-only, capped at 50,
  and never contents — judged acceptable for an operator diagnostic. Strike this
  field if unwanted; the counts survive without it.
- `tool_names` is one field beyond the requested list. Tool names are not
  arguments. Strike it if the scope must stay literal; the A-vs-B split then
  relies on the `model_*`/`tool_*` ratio alone, which is weaker but sufficient.
- `longest_activity_gap_seconds` is a second field beyond the requested list, but
  without it the receipt cannot distinguish continuous activity from
  in-flight-protected silence — the corrected verdict above rests on it.

## Validation

```bash
bash scripts/run_tests.sh website-builder/tests/test_frontend_watchdog.py -q --file-retries=0
bash scripts/run_tests.sh website-builder/tests/test_hermes_adapter.py -q --file-retries=0
HERMES_TEST_WORKERS=1 bash scripts/run_tests.sh website-builder/tests -q --file-retries=0
```

Serial for the full suite — the parallel run has frozen Windows before. Do not
commit, push, or deploy.

## Out of scope

The convergence guard itself; any change to the four timeout values, the
prompt, the toolsets, the skills, the artifact-recovery gate, or
`_has_complete_frontend_artifacts`; surfacing forensics in the user-facing error
reply; and making `invocation` readable by any API or UI. Surfacing the
write-only `state.failure["invocation"]` (`build.py:703-704`) is a separate,
later change.
