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
| Current batch | **N3 — Decision Engine & OpenViking** |
| Phase | N1, N2 pushed & verified; starting N3 |
| Batch status | N1 **DONE** · N2 **DONE** · N3 IN PROGRESS |
| Last verified Git SHA (pre-N1 baseline) | `d0016be5a0cb7a860251f338d84e00ac688242f6` |
| N1 commit | `1e4f488023befa22c26a427929562e08cdb4c46c` |
| N2 commit | `830fd83b114eef0fe461fe7aa368ca9eae3f94af` |
| Remote SHA | `830fd83b114eef0fe461fe7aa368ca9eae3f94af` — **matches local, verified** |
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
| N1 gate — pristine-HEAD baseline | same command on `git archive HEAD` copy | **125 files, 4087 passed, 20 failed, 2 skipped** (exit 1) |
| **Delta (N1→N2)** | — | **+2 files, +37 passed, +0 failed** ⇒ no regressions |

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

1. **N1 + N2 are pushed.** Remote = `830fd83b114eef0fe461fe7aa368ca9eae3f94af` (verified).
2. Begin **N3 — Decision Engine & OpenViking** (deliverables: replaceable
   DecisionProvider interface; Decision Intake classification; OpenViking
   candidate retrieval + Decision Design ranking; bounded retrieval, provenance,
   score validation, abstention, fallback; Laya retirement gated on parity).
3. After N3: run the full suite, compare to the fingerprint above, then commit +
   push + verify remote SHA.

## Batch log

| Batch | Depends on | Status | Gate | Commit SHA | Remote SHA |
|---|---|---|---|---|---|
| N1 | — | **DONE (pushed)** | `scripts/run_tests.sh website-builder/tests` → 20 baseline failures (documented exception) | `1e4f48802` | `1e4f48802` ✓ verified |
| N2 | N1 | **DONE (pushed)** | `scripts/run_tests.sh website-builder/tests` → +37 passed, 20 baseline failures (identical set) | `830fd83b1` | `830fd83b1` ✓ verified |
| N3 | N1,N2 | IN PROGRESS | `scripts/run_tests.sh website-builder/tests` | — | — |
| N4 | N1,N2,N3 | NOT STARTED | `scripts/run_tests.sh website-builder/tests` | — | — |
| N5 | N1,N2,N3,N4 | NOT STARTED | `scripts/run_tests.sh website-builder/tests` | — | — |

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
