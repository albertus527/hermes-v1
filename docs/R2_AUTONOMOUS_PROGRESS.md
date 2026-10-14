# R2 Autonomous Progress Ledger

> Durable, session-spanning progress record for the Hermes Website R2 N1–N5
> redesign. **A new session MUST reload this ledger and reconcile it with the
> actual Git state before continuing.** Do not trust an in-conversation
> checkpoint across sessions.

## Identity

| Field | Value |
|---|---|
| Plan | `hermes-website-r2-n1-n5` v1 (5 batches) |
| Roadmap (hash-pinned) | `autodev/plans/roadmap_n1_n5.md` |
| Plan manifest | `autodev/plans/PLAN_MANIFEST.json` |
| Development worktree | `/home/albertus527/hermes-website-r2` |
| Required branch | `feature/website-r2-redesign` |
| Remote | `origin` → `https://github.com/albertus527/hermes-v1.git` |
| Push target | `origin/feature/website-r2-redesign` (fast-forward only) |

## Plan-input integrity (verify on every session start)

| File | Expected SHA-256 |
|---|---|
| `autodev/plans/roadmap_n1_n5.md` | `e9fabccce845abc4f4479964e4a21e3ed425c76f3aaafe3a8272d6bb9674e1e1` |
| `autodev/plans/batches_n1_n5.json` | `fbecdc25daf1090ca349402dd91106c4fe2a58eaa101b389af78238daae0f25b` |

Verification command:
```
sha256sum autodev/plans/roadmap_n1_n5.md autodev/plans/batches_n1_n5.json
```
If either hash differs, **STOP** and escalate — the approved roadmap has changed.

## Current status

| Field | Value |
|---|---|
| Current batch | **ALL COMPLETE — N1–N5 pushed & verified** |
| Phase | Final batch complete; final report delivered |
| Batch status | N1 **DONE** · N2 **DONE** · N3 **DONE** · N4 **DONE** · N5 **DONE** |
| Last verified Git SHA (pre-N1 baseline) | `d0016be5a0cb7a860251f338d84e00ac688242f6` |
| N1 commit | `1e4f488023befa22c26a427929562e08cdb4c46c` |
| N2 commit | `830fd83b114eef0fe461fe7aa368ca9eae3f94af` |
| N3 commit | `7c9daa42c8a84d7c84fa8525d223f8abb8412190` |
| N4 commit | `08d094510a3110463da189fa62b9ef675900ade3` |
| N5 commit | `c2c36849fd46111fcdefb1c71117da648345e03b` |
| Remote SHA | `c2c36849fd46111fcdefb1c71117da648345e03b` — **matches local, verified** |
| Working tree | clean except untracked `autodev/` and `tests/autodev/` (preserved, never staged) |

## Latest tests

| Batch | Command | Result |
|---|---|---|
| N1 focused | `python -m pytest website-builder/tests/test_document_contracts.py -q` | **31 passed, 8 subtests passed** |
| N1 mutation | `python website-builder/tools/mutation_check_n1.py` | **13/13 guards killed** |
| N1 gate | `scripts/run_tests.sh website-builder/tests` | **126 files, 4118 passed, 20 failed, 2 skipped** (exit 1) |
| N2 focused | `pytest tests/test_requirement_creator.py` + `tests/test_requirement_creator_build.py` | **30 + 7 passed** |
| N2 mutation | `python tools/mutation_check_n2.py` | **11/11 guards killed** |
| N2 gate | `scripts/run_tests.sh website-builder/tests` | **128 files, 4155 passed, 20 failed, 2 skipped** (exit 1) |
| N2 vs baseline | failure node-id set diff | **IDENTICAL** ⇒ no new failures |
| N3 focused | `pytest tests/test_decision_engine.py` | **27 passed, 4 subtests** |
| N3 mutation | `python tools/mutation_check_n3.py` | **12/12 guards killed** |
| N3 gate | `scripts/run_tests.sh website-builder/tests` | **129 files, 4182 passed, 20 failed, 2 skipped** (exit 1) |
| N4 focused | `pytest tests/test_document_guidance.py` + `tests/test_requirement_creator_build.py` | **13 + 13 passed** |
| N4 mutation | `python tools/mutation_check_n4.py` | **10/10 guards killed** |
| N4 gate (first attempt) | `scripts/run_tests.sh website-builder/tests` | **130 files, 4179 passed, 40 failed** — 20 NEW failures (BLOCKER) |
| N4 gate (after fix) | `scripts/run_tests.sh website-builder/tests` | **130 files, 4201 passed, 20 failed, 2 skipped** — failure set IDENTICAL to baseline |
| N5 focused | `pytest tests/test_r2_n5_regression.py` | **19 passed** |
| N5 mutation | `python tools/mutation_check_n5.py` | **9/9 guards killed** |
| N5 gate (final) | `scripts/run_tests.sh website-builder/tests` | **131 files, 4220 passed, 20 failed, 2 skipped** — failure set IDENTICAL to baseline |
| N1 gate — pristine-HEAD baseline | same command on `git archive HEAD` copy | **125 files, 4087 passed, 20 failed, 2 skipped** (exit 1) |
| **Delta (N1→N5)** | — | **+6 files, +133 passed, +0 failed** ⇒ no regressions |

### N4 blocker and fix (recorded, not hidden)

The first N4 gate produced **20 NEW failures** (`test_r1_full_story`,
`test_r1_phase_g_full_story`, `test_bypass_leakage`, `test_self_contained`,
`test_r1_failure_matrix`, `test_r1_revision_crash`, `test_r1_simulation_baseline`,
`test_r1_smoke_failure_recovery`). Root cause: the N4 design threaded a new
`document_guidance=` kwarg through five `frontend_build` call sites, and the R1
test harness's fake adapter
(`tests/r1_harness.py::frontend_build`) does not accept it → `TypeError` inside
the Phase-7 build, which the build's outer handler converted to a FAILED
lifecycle.

Fix (a genuine design improvement, not a test edit): the adapter **derives**
guidance from the `workspace` it is already handed
(`guidance_for_workspace(workspace)` inside `HermesAdapter.frontend_build`), so
no call site changes and every caller — including the pre-N4 harness — gets
consistent guidance. The call-site kwargs were reverted; `revise.py` is pristine.
Re-run: failure set **identical** to baseline.

The 20 failures are **pre-existing and environmental**, proven by reproducing the
identical failure set on a pristine `HEAD` copy with **zero N1 files present**:

| File | Failing | Root cause (verified) |
|---|---|---|
| `test_r2_hydration.py` | 18 | Git fixture references objects absent from this clone (`git cat-file` → "not a valid object"); `git commit-tree` exits 128 |
| `test_design_resources.py` | 1 | `test_runtime_does_not_start_when_a_required_capability_is_missing` — `composition` is `None` under the hermetic test env |
| `test_suite_hygiene.py` | 1 | same class; passes in isolation, fails under the parallel gate on both trees |

None of these files import `document_contracts`; the N1 module has **no production
importer** (verified: only `tests/test_document_contracts.py` and
`tools/mutation_check_n1.py`).

## Baseline failure fingerprint (MANDATORY — compare after every batch)

Exact, reproducible fingerprint of the 20 pre-existing full-suite failures.
**A new failure, a changed failure mode, or a changed count is a BLOCKER.**
Do NOT change existing tests to hide these; do NOT silently redefine the baseline.

| # | Test node id | Failure cause | Environment condition |
|---|---|---|---|
| 1–18 | `test_r2_hydration.py::<18 nodes>` (see below) | Git fixture builds commits via `git commit-tree <sha> …` where the referenced tree/object is **absent from this clone** (`git cat-file -t <sha>` → *"could not get object info"*); `git commit-tree` exits 128 → `subprocess.CalledProcessError` | This clone lacks the fixture's referenced objects (not shallow: `git rev-parse --is-shallow-repository` = false) |
| 19 | `test_design_resources.py::test_runtime_does_not_start_when_a_required_capability_is_missing` | Under the hermetic test env, `app/runtime.py main()` gets `composition = None`; `reconcile_stranded_projects(composition.store)` → `AttributeError: 'NoneType' object has no attribute 'store'` before the test's expected exit path | Hermetic `env -i` test environment / runtime deps absent |
| 20 | `test_suite_hygiene.py::test_the_escape_scanner_would_catch_a_real_one` | The canonical runner uses **Python 3.11.16**, where an invalid escape sequence emits `DeprecationWarning`; the scanner records only `SyntaxWarning` (Python 3.12 behaviour), so its self-test finds nothing | Interpreter: runner venv = CPython 3.11.16; ambient = 3.12.3 |

The 18 `test_r2_hydration.py` nodes:
```
test_a_symlinked_staging_root_is_refused
test_an_executable_blob_is_written_and_accepted
test_blob_submodules_are_refused
test_blob_symlinks_are_refused
test_blob_unsafe_paths_are_refused[../escape.txt]
test_blob_unsafe_paths_are_refused[..]
test_blob_unsafe_paths_are_refused[./relative.txt]
test_blob_unsafe_paths_are_refused[.git/config]
test_blob_unsafe_paths_are_refused[.git]
test_blob_unsafe_paths_are_refused[a/../../escape.txt]
test_blob_unsafe_paths_are_refused[a/.git/hooks/pre-commit]
test_blob_unsafe_paths_are_refused[back\\slash.txt]
test_blob_unsafe_paths_are_refused[colon:name.txt]
test_canonical_lfs_pointer_is_refused_before_the_bytes_hit_disk
test_inert_instruction_files_are_ordinary_committed_bytes
test_lfs_is_never_executed_or_filtered
test_non_canonical_lfs_like_bytes_are_ordinary_committed_bytes
test_ordinary_text_mentioning_lfs_is_accepted
```

**Baseline exception (documented, not a claim of green):** the full suite is
**NOT** green on this host. Baseline = `20 failed, 2 skipped` with the fingerprint
above. A batch is acceptable iff its run yields **the same 20 failures** (same
node ids, same causes) and **no new** failure.

## Pending decisions / escalations

| ID | Question | Options | Status |
|---|---|---|---|
| D1 | Gate exits 1 due to 20 pre-existing environmental failures; commit + push, or hold? | (a) commit+push · (b) commit local, hold push · (c) hold all · (d) no commit | **RESOLVED — operator chose (b), then authorized the push under D2.** |
| D2 | Authorize the held N1 push + autonomous N2–N5? | — | **RESOLVED — operator authorized non-force fast-forward push + autonomous continuation.** |

## Next action

1. **N1–N4 pushed.** N4 commit + remote SHA recorded after push.
2. Begin **N5 — Regression, Replay & Recovery** (deliverables: full new-website
   and revision lifecycle tests; Forma Atelier baseline replay; failure-injection
   coverage — provider, document, retrieval, candidate, Git, crash, QA; exact
   release identity + user approval verification; final engineering/quality
   report).
3. After N5: run the full suite, compare to the fingerprint above, then commit +
   push + verify remote SHA.

## Batch log

| Batch | Depends on | Status | Gate | Commit SHA | Remote SHA |
|---|---|---|---|---|---|
| N1 | — | **DONE (pushed)** | `scripts/run_tests.sh website-builder/tests` → 20 baseline failures (documented exception) | `1e4f48802` | `1e4f48802` ✓ verified |
| N2 | N1 | **DONE (pushed)** | +37 passed, 20 baseline failures (identical set) | `830fd83b1` | `830fd83b1` ✓ verified |
| N3 | N1,N2 | **DONE (pushed)** | +27 passed, 20 baseline failures (identical set) | `7c9daa42c` | `7c9daa42c` ✓ verified |
| N4 | N1,N2,N3 | **DONE (pushed)** | +19 passed, 20 baseline failures (identical set; 20-new-failure blocker fixed) | `08d094510` | `08d094510` ✓ verified |
| N5 | N1,N2,N3,N4 | **DONE (pushed)** | +19 passed, 20 baseline failures (identical set) | `c2c36849f` | `c2c36849f` ✓ verified |

## Final outcome

All five batches are implemented, feature-flagged (new behaviour default-OFF or
shadow-mode), independently tested, and mutation-verified. **+133 passing tests,
0 new failures** across the whole redesign. No security or release invariant was
relaxed. Production deployment still requires explicit human approval, which this
run never granted.

## N6 — Architecture Completion (AUDIT → DESIGN → IMPLEMENT)

N6 completes the roadmap's intended architecture: pre-build Design Guidance,
Decision Engine modes, per-invocation guidance snapshots, and QA/VISION parity.
Approved decisions: D-N6-1..6 (one `decision_engine` block; separate
`DesignGuidance.md`; active intake affects routing only; `RevisionState` +
existing metadata for versions; reuse D2 selector; activate
`build_design_context_pack` behind active-mode gating).

| Checkpoint | Status | Tests | Commit SHA | Remote SHA |
|---|---|---|---|---|
| N6-A config/modes | **DONE (pushed)** | 17 passed | `196525f03` | `196525f03` ✓ |
| N6-B intake modes | **DONE (pushed)** | 16 passed; 5/5 killed | `2a4fd3694` | `2a4fd3694` ✓ |
| N6-C DesignGuidance | **DONE (pushed)** | 17+5 passed; 6/6 killed | `5b3e8f84e` | `5b3e8f84e` ✓ |
| N6-D FRONTEND handoff + pack | **DONE (pushed)** | 13 passed; 6/6 killed | `3a971ebf6` | `3a971ebf6` ✓ |
| N6-E snapshot + QA/VISION parity | **DONE (pushed)** | 17+2 passed; 6/6 killed | `a3840adf6` | `a3840adf6` ✓ |

**N6 gate:** `scripts/run_tests.sh website-builder/tests` → **137 files, 4307
passed, 20 failed, 2 skipped**; failure set IDENTICAL to the baseline
fingerprint (18 hydration + 1 design_resources + 1 suite_hygiene). N6 as a whole:
**+87 passed, +0 new failures**. Docs: `website-builder/docs/N6_ARCHITECTURE_COMPLETION.md`.

N6 safety posture: both modes default `disabled`; active mode is NOT enabled on
production and must not be until a separate live E2E qualification passes
(plan in the N6 report). Mocked tests are not production readiness.

## N6 live-integration fix (OpenViking design leg) — DONE (pushed)

The N6 design leg was verified against a LIVE OpenViking and found to deliver
**zero** candidates (silent abstention). Four independent defects were confirmed
and fixed on `feature/website-r2-redesign`:

| # | Defect | Fix |
|---|--------|-----|
| A | `openviking_decision` passed the library **project id** as the retrieval **scope**; `normalize_scope` refused it (expects category names) and the provider swallowed the error | pass `DESIGN_RETRIEVAL_CATEGORIES` (validated subset of the library's closed category set); project id goes to `project_id` |
| B | `_to_candidate` read `item.source_uri`; the real `ContextItem` field is **`uri`**, so every item mapped to `None` | map `uri`→`source_uri`, preserve genuine `source_revision`/`source_id`; no fabrication |
| C | `library_project_id` was never supplied to the pre-build design context | new config `decision_engine.design_library_project_id` (application constant, grammar-validated, never user input) passed into `decide_design` |
| D | a configured-but-unavailable provider silently resolved to the deterministic fallback and was reported as a clean success | engine reports it as a **degraded fallback** (`source=fallback`, `degraded=True`), for both intake and design |

**Verification (live):** real retrieval returned 6 provenance-bearing candidates;
`refero_typography@5c2211d3e432` reached `decide_design`, the `DesignGuidance.md`
reference line, and the FRONTEND pre-build pack. Mode matrix (disabled/shadow/
active, outage, invalid provenance, scoring bounds, fallback) PASS.

**Gate:** `scripts/run_tests.sh website-builder/tests` → **138 files, 4323
passed, 20 failed, 2 skipped**; failure set byte-identical to the baseline
(18 hydration + 1 design_resources + 1 suite_hygiene). Delta: +1 file, +16
passed, **+0 new failures**. Mutation: N3 11/12 killed (1 pre-existing anchor
drift in `intake.py`, not modified), N6-C/D/E 18/18 killed.

## Standing safety constraints (never relax)

- Never modify `/home/albertus527/hermes-website` (original worktree) or Hermes Trade.
- Never change global Hermes config, credentials, active services, Docker config, or system permissions.
- Never merge, force-push, rewrite remote history, or deploy to production.
- Never weaken security policy, dependency controls, Git/Vercel release invariants, or the website-only product boundary.
- Never modify, delete, stage, or commit `autodev/` or `tests/autodev/`.
- Never invoke `python -m autodev run` (parked reference only).
- Never run destructive Git commands or bulk-delete untracked files.
- Never claim a test PASS unless the command actually ran successfully.

## Session resume procedure

1. `git -C /home/albertus527/hermes-website-r2 branch --show-current` → must be `feature/website-r2-redesign`.
2. `git -C /home/albertus527/hermes-website-r2 status --short` → expect only untracked `autodev/`, `tests/autodev/` (plus any in-progress N-batch work).
3. `git -C /home/albertus527/hermes-website-r2 rev-parse HEAD` and compare to "Last verified Git SHA" above.
4. Re-verify the plan-input hashes.
5. Reconcile: if Git state and this ledger disagree, **trust Git and escalate** — never resume from assumption.
