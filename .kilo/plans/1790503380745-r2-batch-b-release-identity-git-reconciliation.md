# Website Builder R2 Batch B — Release Identity + Git Publication Reconciliation

Repo: `albertus527/hermes-v1` · **Do not commit, push, or deploy in this batch.**

### Branch safety check (re-verified during the spec patch)

```
git rev-parse --abbrev-ref HEAD   -> batch_B
git log --oneline -1               -> d6880ef7e Migrate the pre-R2 Vercel bypass secret store at startup (R2-B3)
git status --porcelain             -> only ?? .kilo/plans/1790503380745-r2-batch-b-release-identity-git-reconciliation.md
```

- **Observed working branch: `batch_B`.** Both `batch_B` and `feature/website` exist locally.
- The task text names `feature/website`; the tree is on `batch_B`. This is reported, not reconciled.
- **Do not switch branches** unless the user instructs it. Do not `git add`/`commit`/`push` as part of this spec patch or the implementation that follows.

## Problem

GitHub branch HEAD is not proof of what is LIVE. Release A is live, publication B is pushed, Vercel promotion for B fails — the branch now reads B while production serves A. A future revision must not treat B as the live source.

R1 makes this structurally possible in the other direction too: `_publish_live_source` (promote.py:1040) runs **after** the LIVE transition, so the release identity is assembled from two records (`deployment.last_live_deployment`, `state.repository`) with no contract binding them, and `publish_project_branch` (git_output.py:340-366) re-parents onto *any* unrecognized remote head.

## Locked decisions (confirmed with user)

1. **Reorder** — Git publication moves before production: `PREPARED → GIT_CONFIRMED → PRODUCTION_CONFIRMED → SMOKE_PASSED → COMMITTED`. Consequence: a Git failure now means production is never touched. This inverts R1's "git push failure still goes live" guarantee — that inversion is the point.
2. **Strict A/B/C/D** — delete the re-parent path; classify the remote head and fail closed on C/D.
3. **Bind release identity in application state only** — no change to `VercelAdapter._meta` (`wbOwner/wbOperation/wbRevision/wbArtifact`); `wbOperation` already ties `deployment_id` to the release.
4. **Keep `last_live_deployment` as a derived projection; retire `state.repository`.** Legacy on-disk state is migrated lazily in `ProjectState.from_dict`.

## Schema / state changes

All new records live in `state.deployment` (same namespace as `promotion_intent` / `last_live_deployment`). No new top-level `ProjectState` field.

### `deployment.publication_head` — branch parent authority

Advanced at `GIT_CONFIRMED`, i.e. the last publication commit we have **confirmed on the remote branch**, independent of whether the release committed. This is what lets a release C chain onto a git-confirmed-but-not-live B instead of colliding with it.

```json
{ "commit": "<40hex>", "branch": "financeadvisory", "confirmed_at": 1735.0 }
```

Resolved parent for any new publication: `publication_head.commit` (or `None` for the first release). Replaces R1's `state.repository.publication_commit`.

### `deployment.pending_publication` — the in-flight publication

```json
{
  "operation_id": "op-7", "source_revision": 7,
  "stage": "PREPARED", "outcome": null,
  "reconciliation_required": false,
  "created_at": 1734.0, "updated_at": 1734.0,
  "source_sha256": "<64hex>", "artifact_sha256": "<64hex>",
  "publication": {
    "configured": true, "repo": "owner/name", "branch": "financeadvisory",
    "tested_commit": "<40hex>", "tested_tree": "<40hex>",
    "intended_commit": "<40hex>", "intended_parent": "<40hex>|null",
    "intended_tree": "<40hex>",
    "status": "PENDING", "confirmed_at": null
  },
  "production": { "deployment_id": null, "promoted_deployment_id": null, "confirmed_at": null },
  "smoke": { "status": null, "at": null },
  "last_error_code": null
}
```

`publication.status ∈ {PENDING, CONFIRMED, NOT_CONFIGURED, FAILED}`.
`outcome ∈ {null, TERMINAL_FAILED, RECONCILIATION_REQUIRED}`.

#### Stage table — one linear machine, no second graph

`stage` always advances in exactly this order; it never skips and never goes backwards:

| # | Stage | Entered when | `publication.status` on entry | Git network activity |
|---|---|---|---|---|
| 1 | `PREPARED` | branch + deterministic commit built and persisted with `promotion_intent` | `PENDING` (or `NOT_CONFIGURED` if publication is not configured) | none |
| 2 | `GIT_CONFIRMED` | intended commit is on the remote branch — **or**, when publication is not configured, immediately on entry to stage 2 | `CONFIRMED` (or `NOT_CONFIGURED`) | one push, **or zero when not configured** |
| 3 | `PRODUCTION_CONFIRMED` | `promote_deployment` succeeded; `deployment_id` + `promoted_deployment_id` recorded | unchanged | none (Vercel only) |
| 4 | `SMOKE_PASSED` | production smoke passed against the canonical URL; evidence recorded | unchanged | none |
| 5 | `COMMITTED` | one atomic locked write; `pending_publication` cleared, `last_live_release` written | `CONFIRMED` \| `NOT_CONFIGURED` | none |

**NOT_CONFIGURED is a stage-2 no-op, never a shortcut past stage 2.** When publication is not configured:

- `publication.configured = false`, `publication.status = NOT_CONFIGURED` is set **at PREPARED**;
- the machine advances `PREPARED → GIT_CONFIRMED` with **zero** Git subprocesses (no `ls-remote`, no push, no `update-ref`);
- `publication_commit` / `publication_tree` / `publication_parent` / `publication_repo` / `publication_branch` and `tested_commit` / `tested_tree` **stay `null`** — they are null *by design*, not by failure;
- `publication_head` is left unchanged (there is no new branch authority);
- execution then proceeds to `PRODUCTION_CONFIRMED` exactly as in the configured case.

Rationale: one graph means resume, reconciliation, and failure bookkeeping have a single code path. A NOT_CONFIGURED record sitting at `GIT_CONFIRMED` must look exactly like a configured one to every reader of `pending_publication`, so resume never has to ask "which graph was this?".

### `deployment.last_live_release` — the authoritative LIVE release

```json
{
  "release_id": "op-7", "source_revision": 7, "operation_id": "op-7",
  "tested_commit": "<40hex>", "tested_tree": "<40hex>",
  "publication_commit": "<40hex>|null", "publication_tree": "<40hex>|null",
  "publication_parent": "<40hex>|null", "publication_branch": "financeadvisory"|null,
  "publication_repo": "owner/name"|null,
  "source_sha256": "<64hex>", "artifact_sha256": "<64hex>",
  "deployment_id": "dpl_...", "production_url": "https://slug.vercel.app/",
  "deployment_url": "https://dpl-....vercel.app",
  "smoke": { "status": "PASSED", "at": 1736.0, "target_host": "slug.vercel.app",
             "target_path": "/", "failure_classification": null },
  "committed_at": 1736.5,
  "completeness": "COMPLETE" | "LEGACY_PARTIAL"
}
```

Advances **only** when: exact publication confirmed (or confirmed as `NOT_CONFIGURED`) → exact tested artifact reached production → canonical production smoke passed.

#### `completeness` — two explicit validator modes

`validate_last_live_release(record)` takes an explicit `mode` argument. There is no inference and no "best effort" mode.

**`completeness == "COMPLETE"`** — the record is a release this system committed under R2 and every field below must be present and well-formed:

*Publication identity (required when `publication.configured == true`):*

- `publication_commit` — 40-hex
- `publication_tree` — 40-hex
- `publication_parent` — 40-hex, or `null` **only** for a root publication (first release on the branch)
- `publication_repo` — `owner/name`
- `publication_branch` — friendly branch name

*Tested / source / artifact identity (always required):*

- `tested_commit` — 40-hex; `tested_tree` — 40-hex
- `source_sha256` — 64-hex; `artifact_sha256` — 64-hex
- `release_id`, `operation_id`, `source_revision` — non-empty; `committed_at` — positive number

*Deployment + smoke identity (always required):*

- `deployment_id` — non-empty; `deployment_url` / `production_url` — the two recorded hosts only
- `smoke.status == "PASSED"`, `smoke.at` positive, `target_host` == the canonical production host, `target_path` non-empty, `failure_classification is None`

*Not-configured exception:* when the release's publication was `NOT_CONFIGURED`, the five publication-identity fields above are `null` **by design** and the record is still `COMPLETE` provided every non-publication requirement holds. A `COMPLETE` record with `publication_commit = null` is legal **only** when the operation recorded `publication.configured == false`; a configured release with a null publication commit is a validation failure, not a `COMPLETE` record.

**`completeness == "LEGACY_PARTIAL"`** — only for state lazily derived from a pre-R2 `deployment.last_live_deployment` / `state.repository` record:

- preserve **only** the facts R1 actually persisted (deployment id / url / commit, and the R1 `repository.publication_commit` when present);
- every field R1 did not persist stays `null` — no placeholders, no sentinels, no "likely" values;
- **never** fabricate `publication_tree`, `tested_tree`, `source_sha256`, `artifact_sha256`, `publication_parent`, `publication_repo`, or smoke evidence;
- the validator accepts it **only** in a `previous-release` / compatibility-identity role (rollback target, "what was live before" lookups). It is rejected as a new release identity and rejected by `commit_release`;
- `LEGACY_PARTIAL` is **never** emitted by `commit_release()` — a new R2 release is always `COMPLETE` or the commit fails;
- `LEGACY_PARTIAL` is **never** silently upgraded to `COMPLETE`. Upgrade is allowed only when every required field above is genuinely present on disk (a real re-hydration of a complete record), and it is an explicit, operation-bound write, not a lazy side effect of a read;
- callers that need "is the current LIVE release known?" must read `completeness` rather than infer it from the presence of `publication_commit`;
- **Hydration is out of scope** and must treat an unknown publication identity on a `LEGACY_PARTIAL` record as *unknown*, never as a conflict and never as "assume it matches the branch".

### Unchanged / retired

- `deployment.last_live_deployment` — written in the same atomic `COMMITTED` save as a derived projection. `domain.py:69` and promote.py:456/875 keep working untouched.
- `state.repository` — retired. `ProjectState` keeps the dataclass field (so `from_dict(**data)` never breaks) but migration drops its contents. No production code reads it; only ~20 assertions in `test_r1_live_publish_finishing.py` do.

## Publication state machine

```
   ┌─ resolve branch, build deterministic commit (no network)
   │  persist promotion_intent + pending_publication  [PREPARED]
   ▼
 GIT STAGE
   ├─ publication not configured ─► status = NOT_CONFIGURED
   │                                 (already set at PREPARED)
   │                                 advance as a local no-op: no ls-remote,
   │                                 no push, no update-ref, no publication_head
   │                                          │
   └─ publication configured ───► push intended commit
                                          │
                    ┌─────────────────────┼─ push ACCEPTED (fast-forward)
                    │                     │     → persist GIT_CONFIRMED directly.
                    │                     │       NO ls-remote here. A successful
                    │                     │       non-force push IS the proof.
                    │                     │
                    └─ push REJECTED ────┴─► A/B/C/D classify (see matrix)
                                          │
                              A_ADOPT ─────┴─► GIT_CONFIRMED
                              B_RETRY ────────► push once ─► GIT_CONFIRMED
                              C/D ────────────► reconciliation-required, stop
   │
   ▼  [always, configured or not]
 GIT_CONFIRMED   advance publication_head (only when configured & confirmed)
   │
   ▼
 promote / adopt   ──► PRODUCTION_CONFIRMED  (record deployment_id + promoted id)
   │
   ▼
 canonical URL ──► production smoke ──► SMOKE_PASSED  (record smoke evidence)
   │
   ▼
 COMMITTED: one atomic locked write — lifecycle LIVE, revisions.live_revision,
            production_url, last_live_deployment projection, last_live_release
            (completeness=COMPLETE), pending_publication cleared
   │
   ▼
 Telegram LIVE message
```

Every stage transition is a writer-locked `save()` that raises `StaleOperationIntent` when the record on disk belongs to a different `operation_id` (mirrors `_update_intent`, promote.py:1277).

**Supersede rule:** a *new* operation may not begin while `pending_publication.reconciliation_required` is true → `PUBLICATION_SUPERSEDE_FORBIDDEN`, checked at the top of `_promote_authorized` before the approval/lifecycle gates. A terminal `pending_publication` is supersedable and is replaced by the new PREPARED record.

#### Remote-read contract (unambiguous)

**A successful exact fast-forward push is sufficient proof. `ls-remote` is never run on the ordinary successful path.**

After an accepted push, `_confirm_git` persists `GIT_CONFIRMED` **immediately**, with no remote read of any kind. The push already proved the remote's previous head was exactly `intended_parent`; re-reading it would be a redundant network call and a second source of truth for something the push result already settled.

The remote head is re-read in exactly two situations, both of which run the same A/B/C/D classifier:

1. **Resume / recovery of an operation already persisted at `GIT_CONFIRMED`** (or later) — the state was written by an earlier process, so this process has no push receipt and must re-derive the fact.
2. **Reconciling a rejected or ambiguous push** — the push failed, so its non-acceptance carries no information about what the remote holds.

Resume at `GIT_CONFIRMED` resolves as:

| Observed on resume | Verdict | Action |
|---|---|---|
| `remote_head == intended_commit` | `A_ADOPT` | continue to `PRODUCTION_CONFIRMED`; no push |
| `remote_head` is any other valid 40-hex | `C_CONFLICT` | fail closed `PUBLICATION_HEAD_CONFLICT` |
| `ls-remote` non-zero exit / unparseable | `D_UNAVAILABLE` | fail closed `PUBLICATION_HEAD_UNREADABLE` |

A NOT_CONFIGURED record resuming at `GIT_CONFIRMED` does **not** read the remote — there is nothing to confirm, so it continues straight to `PRODUCTION_CONFIRMED`.

Net invariant, preserved from R1 and asserted by a test: **zero remote reads on the ordinary happy path** (no `ls-remote`, no fetch/clone/pull/archive, ever).

## Git reconciliation matrix (`OutputGitRepository.reconcile_publication_head`)

Per-operation, run **only** on resume at `GIT_CONFIRMED`+ or on a rejected/ambiguous push — **never** on the happy path (a successful fast-forward push already proves head == expected parent, preserving R1's "no remote read on the normal path" invariant).

The matrix is **authoritative** for error codes. C and D are distinct and must not be merged:

| Observed remote state | Verdict | Action | Error code |
|---|---|---|---|
| head == `intended_commit` | `A_ADOPT` | advance to `GIT_CONFIRMED`, no push | — |
| head == `intended_parent` | `B_RETRY` | push `intended_commit` exactly once | — |
| branch absent, `intended_parent is None` | `B_RETRY` (root) | push `intended_commit` | — |
| branch absent, `intended_parent` set | `C_CONFLICT` | fail closed | `PUBLICATION_HEAD_CONFLICT` |
| any other 40-hex (even one we hold locally) | `C_CONFLICT` | fail closed | `PUBLICATION_HEAD_CONFLICT` |
| `ls-remote` non-zero exit / unparseable output | `D_UNAVAILABLE` | fail closed | `PUBLICATION_HEAD_UNREADABLE` |

**C vs D — the distinction is intentional and load-bearing:**

- **C (`PUBLICATION_HEAD_CONFLICT`)** — the remote was read *successfully* and contains a state we did not intend. We know exactly what is there; it is simply not ours to touch. The remote is left byte-for-byte unchanged.
- **D (`PUBLICATION_HEAD_UNREADABLE`)** — the remote state could not be *trusted* or read at all. Transport failure, auth failure, unparseable output, or an ambiguous/absent-vs-unreadable collapse. Nothing is asserted about what is there, so nothing is done.

Every implementation site, test, docstring, and error message must use these exact codes. An earlier draft of this plan carried a note mapping the unavailable-remote case to `PUBLICATION_HEAD_CONFLICT`; that was **wrong** and is corrected here. D is `PUBLICATION_HEAD_UNREADABLE`, full stop.

Two R1 defects this fixes:
- `_remote_head` (git_output.py:249) returns `None` for both a transport failure and an absent branch. Split: non-zero returncode → `D_UNAVAILABLE`; zero exit with no matching line → branch-absent (which is only `B_RETRY` root or `C_CONFLICT`, never `D`).
- The re-parent accepted any head ≠ expected parent. Re-parenting is now unnecessary: the intended commit is deterministic (pinned author/committer/date in `_run`, fixed message in `_publication_message`) and persisted *before* the push, so a lost `GIT_CONFIRMED` write yields a byte-identical commit → case A.

## Vercel contract (unchanged, locked)

- Production is still reached by `promote_deployment` → `_promote_by_creation` (`deploymentId` inheritance, adapters.py:1341) or `_promote_by_alias_remap`. The create request carries **no** `files`, **no** `builds`, **no** `gitSource`/`gitRepo`/`gitRef`/`gitCommitRef`.
- No new provider meta. `wbOperation/wbRevision/wbArtifact` + `wbOwner` remain the whole provider-side identity; `_complete_identity` and `_validated_deployment` are untouched.
- Release identity is bound in application state only.
- No native Vercel Git deployment path is introduced.

## Failure / rollback semantics

| Failure point | stage | lifecycle | `last_live_release` | `pending_publication` | `publication_head` |
|---|---|---|---|---|---|
| no trusted tested commit (PREPARED build) | — | FAILED | unchanged | not created | unchanged |
| publication not configured (not a failure) | `GIT_CONFIRMED` | LIVE on commit | `COMPLETE`, publication fields `null` | cleared at COMMITTED | unchanged |
| push terminal failure (auth/hook) | `PREPARED` | FAILED | unchanged | `PREPARED`, `TERMINAL_FAILED`, publication `FAILED` | unchanged |
| push rejected → C | `PREPARED` | PUBLISHING (held) | unchanged | `PREPARED`, `RECONCILIATION_REQUIRED`, `PUBLICATION_HEAD_CONFLICT` | unchanged |
| push rejected → D | `PREPARED` | PUBLISHING (held) | unchanged | `PREPARED`, `RECONCILIATION_REQUIRED`, `PUBLICATION_HEAD_UNREADABLE` | unchanged |
| promote terminal failure | `GIT_CONFIRMED` | FAILED | unchanged | `GIT_CONFIRMED`, `TERMINAL_FAILED` | advanced |
| promote ambiguous | `GIT_CONFIRMED` | PUBLISHING (held) | unchanged | `GIT_CONFIRMED`, `RECONCILIATION_REQUIRED` | advanced |
| canonical URL unresolved | `PRODUCTION_CONFIRMED` | FAILED | unchanged | `PRODUCTION_CONFIRMED`, `TERMINAL_FAILED` | advanced |
| production smoke failure (+ rollback) | `PRODUCTION_CONFIRMED` | FAILED | unchanged | `PRODUCTION_CONFIRMED`, `TERMINAL_FAILED`, smoke evidence | advanced |
| crash at any point | preserved | preserved | preserved | preserved | preserved |

No Vercel call is made from the `PREPARED` or `GIT_CONFIRMED` rows, and no Git network activity is made from the `PRODUCTION_CONFIRMED`/`SMOKE_PASSED` rows. That separation is the batch's central invariant.

Rollback semantics are untouched (`_rollback_and_fail`, promote.py:1443): re-promote the persisted `previous_production` identity, record the exact observed outcome, never guess a target. The Git branch is **never** rolled back — it is durable history, so after a smoke failure the branch legitimately holds B while `last_live_release` still describes A.

New error codes: `PUBLICATION_HEAD_CONFLICT` (C only), `PUBLICATION_HEAD_UNREADABLE` (D only), `PUBLICATION_PUSH_FAILED`, `PUBLICATION_SUPERSEDE_FORBIDDEN`. Existing `NO_TRUSTED_TESTED_COMMIT` retained. `PUBLICATION_HEAD_CONFLICT` and `PUBLICATION_HEAD_UNREADABLE` are never substituted for one another.

## Task list

**T1 — `app/deploy/git_output.py`**
- Add `prepare_publication(tested_commit, branch, url, *, source_revision, source_sha256, artifact_sha256, parent, extra_env)` → validated deterministic identity, no network, no push. Reuse the existing URL/branch/commit/ref validation and the tree-equality assertion from `publish_project_branch`.
- Add `reconcile_publication_head(url, branch, intended_commit, intended_parent, extra)` → `OperationResult` with `data["verdict"] ∈ {A_ADOPT, B_RETRY, C_CONFLICT, D_UNAVAILABLE}` and `data["error_code"]`, per the matrix. `C_CONFLICT` → `PUBLICATION_HEAD_CONFLICT`; `D_UNAVAILABLE` → `PUBLICATION_HEAD_UNREADABLE`. These two codes are not interchangeable and are asserted separately.
- Add `push_prepared_publication(url, commit, branch, extra)` → existing `_push_publication` plus rejection classification (non-fast-forward vs anything else). **On an accepted push it returns success with no remote read** — no post-push `ls-remote`, no verification round-trip.
- Fix `_remote_head` to separate transport failure (non-zero returncode) from branch-absent (zero exit, no matching line). Return a discriminated result, not a bare `None`.
- **Delete** `publish_project_branch` and the re-parent block. It is called only from promote.py:1164 and the R1 test harness, both of which are being rewritten.

**T2 — new `app/projects/release.py`**
- Stage constants `PREPARED / GIT_CONFIRMED / PRODUCTION_CONFIRMED / SMOKE_PASSED / COMMITTED` + an ordering guard (`_assert_stage_advances(from, to)`) that rejects skips and regressions. NOT_CONFIGURED is *not* a stage; it is a `publication.status` value that occupies the normal `GIT_CONFIRMED` slot.
- `resolve_branch_parent(state)`.
- `build_pending_publication` / `build_last_live_release` + the two-mode validator `validate_last_live_release(record, mode)` with `mode ∈ {"new_release", "previous_release"}` mapping onto `COMPLETE` / `LEGACY_PARTIAL` exactly as specified above (all SHAs, no secrets, no URLs beyond the two recorded hosts).
- `commit_release(...)` — the single atomic `COMMITTED` write (lifecycle + `live_revision` + `production_url` + `last_live_deployment` projection + `last_live_release` + clear pending). It **always** writes `completeness: "COMPLETE"` and raises on a record that would be `LEGACY_PARTIAL`.
- `advance_stage(project_id, operation_id, stage, **fields)` — writer-locked, `StaleOperationIntent` on mismatch.
- `mark_terminal_failure(...)` / `mark_reconciliation_required(...)`.

**T3 — `app/core/state.py`**
- In `ProjectState.from_dict` (which already does legacy `roles` handling): derive `last_live_release` from `last_live_deployment` + `state.repository` with `completeness: "LEGACY_PARTIAL"`; seed `publication_head` from `repository.publication_commit`; drop `state.repository` contents. **Never** invent a `last_live_release` when there is no `last_live_deployment`.
- Preserve **only** facts R1 actually persisted. Unknown fields stay `null` — no fabricated `publication_tree`, `tested_tree`, `source_sha256`, `artifact_sha256`, `publication_parent`, `publication_repo`, or smoke evidence. A derived record is validated in `previous_release` mode; `new_release` mode must reject it.
- Never promote a derived record to `COMPLETE` as a side effect of loading it.

**T4 — `app/projects/promote.py`**
- Resolve the publication branch at PREPARED (not after LIVE) via the existing `source_branch_for`.
- Split the locked PREPARED write to persist `promotion_intent` **and** `pending_publication` together.
- Insert `_confirm_git` between the PREPARED write and `promote_deployment`. It is the **only** place a push happens. On an accepted push it calls `advance_stage(..., GIT_CONFIRMED)` immediately with **no remote read**; on rejection it calls `reconcile_publication_head` and applies A/B/C/D exactly as the matrix says.
- When publication is not configured, `_confirm_git` is a local no-op that still advances `PREPARED → GIT_CONFIRMED` (with `publication.status = NOT_CONFIGURED`, all publication identity fields `null`, and **zero** Git subprocesses), then falls through to the same production path.
- Rework `_post_promote` to: canonical URL → smoke → `commit_release` → Telegram. Remove the `_publish_live_source` call and the post-LIVE git work.
- Delete `_publish_live_source`, `_record_source_sync`, `_record_source_sync_required`. Keep `_tested_commit_for` and `_source_repo_name`.
- Add the supersede guard at the top of `_promote_authorized`.
- Extend the LIVE idempotent no-op (promote.py:455) to match on `last_live_release.operation_id` and return the release payload with `already_live: True`. A `LEGACY_PARTIAL` record there is a valid idempotency match, but its payload is labelled `completeness: "LEGACY_PARTIAL"` — it is never dressed up as a full release.
- Extend `is_resume` / `resume_publish` to require an intact `pending_publication` (accepting `LEGACY_PARTIAL` for pre-upgrade states) and to reconcile the git stage before touching Vercel. Resuming at `GIT_CONFIRMED` or later performs exactly one remote head read: `== intended_commit` → continue; other valid head → `C_CONFLICT` / `PUBLICATION_HEAD_CONFLICT`; unreadable → `D_UNAVAILABLE` / `PUBLICATION_HEAD_UNREADABLE`. Resuming a NOT_CONFIGURED record performs no read.
- Result payload: replace `source_sync` with a `release` block; keep `production_url` / `deployment_url` / `deployment_id` / `operation_id` / `reconciled`.

**T5 — `app/runtime.py`**
- Pass the release coordinator into `PromotionOrchestrator`. `--reconcile-publish <project_id> --as <principal>` (runtime.py:2601) needs no change — it already routes to `resume_publish`, which now reconciles the git stage too.

## Tests

New: `website-builder/tests/test_r2_release_identity.py`, `website-builder/tests/test_r2_publication_reconciliation.py` (reuse the real bare-remote + `insteadOf` mirror harness from `test_r1_live_publish_finishing.py` so every push / fast-forward / ancestry claim is observed, not simulated). Fault injection via a `ProjectStateStore` subclass whose `save()` silently drops the stage identified by a test-set flag — deterministic, no mocking of the code under test.

1. Success — Git confirmed → production confirmed → smoke passed → `last_live_release` advances; `pending_publication` cleared; `publication_head` == `publication_commit`.
2. Push succeeds, `GIT_CONFIRMED` write lost → fresh orchestrator over the same state root → case A adopts, **no second push**, no duplicate logical publication.
3. Push succeeds, Vercel publish fails → remote branch contains B, `last_live_release` still A, `pending_publication` at `GIT_CONFIRMED` with `TERMINAL_FAILED`, `publication_head` == B.
4. Production promotion succeeds, smoke fails → rollback performed as in R1, `last_live_release` unchanged, smoke failure evidence retained on `pending_publication`.
5. Branch HEAD newer than LIVE → state still identifies the correct LIVE release; assert on state only, never read the branch to decide.
6. Unrelated remote advance → case C → `PUBLICATION_HEAD_CONFLICT`, no force, no fetch, remote head untouched.
7. Duplicate approve/publish: (a) duplicate after COMMITTED returns the existing release with `already_live`; (b) duplicate while in-flight resumes the same operation with no second push or promote; (c) duplicate Telegram `update_id` still dedups in `dispatch_events`.
8. `publication_commit^{tree} == tested_commit^{tree} == publication_tree` for every release, and `pending_publication.publication.tested_tree` matches.
9. The exact tested artifact is what Vercel gets: `promote_deployment` receives the approval's `artifact_sha256`/`source_sha256`, and (adapter-level) the production create body carries `deploymentId` and none of `files` / `builds` / `gitSource` / `gitRepo` / `gitRef` / `gitCommitRef`.

#### 10. Remote-read contract (contradiction 1, locked)

- `test_happy_path_performs_zero_remote_reads` — instrument the `OutputGitRepository` subprocess seam; run a full successful promote (configured publication) and assert `ls-remote` is invoked **zero** times, along with zero fetch/clone/pull/archive, while the push is invoked exactly once and `GIT_CONFIRMED` is persisted.
- `test_accepted_push_persists_git_confirmed_without_verification_round_trip` — the `GIT_CONFIRMED` write is observed on the state store immediately after the push, with no intervening read.
- `test_resume_at_git_confirmed_adopts_without_pushing` — resume at `GIT_CONFIRMED` where the remote head is `intended_commit` → exactly one `ls-remote` for exactly one ref (`refs/heads/<branch>`), zero pushes, continues to production.
- `test_resume_at_git_confirmed_with_other_head_is_case_c` → `PUBLICATION_HEAD_CONFLICT`, zero pushes.
- `test_resume_at_git_confirmed_with_unreadable_remote_is_case_d` → `PUBLICATION_HEAD_UNREADABLE`, zero pushes.
- `test_rejected_push_reconciles_exactly_once` — a non-fast-forward rejection triggers exactly one `ls-remote`, never two, and never a fetch.

#### 11. NOT_CONFIGURED stage semantics (contradiction 2, locked)

- `test_not_configured_advances_through_git_confirmed` — with publication unconfigured, the recorded stage sequence is exactly `PREPARED → GIT_CONFIRMED → PRODUCTION_CONFIRMED → SMOKE_PASSED → COMMITTED`. There is no `PREPARED → PRODUCTION_CONFIRMED` shortcut anywhere in the code or the state.
- `test_not_configured_performs_no_git_network_activity` — zero git subprocesses of any kind during the run; `publication.status == "NOT_CONFIGURED"`; `publication_commit`/`publication_tree`/`publication_parent`/`publication_repo`/`publication_branch` and `tested_commit`/`tested_tree` are all `null`; `publication_head` unchanged.
- `test_not_configured_resume_needs_no_remote_read` — a crash after `GIT_CONFIRMED` on a NOT_CONFIGURED record resumes with zero `ls-remote` and completes.
- `test_not_configured_release_is_complete_with_null_publication_identity` — the committed `last_live_release` has `completeness == "COMPLETE"` and null publication identity, and passes `COMPLETE` validation.

#### 12. COMPLETE vs LEGACY_PARTIAL (contradiction 3, locked)

- `test_commit_release_rejects_legacy_partial` — `commit_release` cannot emit `LEGACY_PARTIAL`; a record that would be partial raises.
- `test_legacy_migration_preserves_only_r1_facts` — from a seeded R1 state, the derived `last_live_release` carries exactly the persisted R1 values; `publication_tree`, `tested_tree`, `source_sha256`, `artifact_sha256`, `publication_parent`, `publication_repo`, and smoke evidence are all `null` (asserted as null, not merely absent).
- `test_legacy_partial_rejected_as_new_release_identity` — `validate_last_live_release(record, "new_release")` raises for a `LEGACY_PARTIAL` record; `validate_last_live_release(record, "previous_release")` accepts it.
- `test_legacy_partial_is_not_upgraded_on_read` — loading and re-saving state does not change `completeness`; the only accepted upgrade is an explicit operation-bound write with all required evidence genuinely present.
- `test_complete_requires_publication_identity_when_configured` — a `COMPLETE` record with `configured == true` and any of the five publication-identity fields null/invalid fails validation.
- `test_no_invented_live_release_without_last_live_deployment` — a non-LIVE project with no `last_live_deployment` gets no `last_live_release` at all.

#### 13. C vs D error codes (contradiction 4, locked)

| Scenario | verdict | asserted `last_error_code` |
|---|---|---|
| remote has an unrelated valid 40-hex head | `C_CONFLICT` | `PUBLICATION_HEAD_CONFLICT` |
| remote branch absent, `intended_parent` set | `C_CONFLICT` | `PUBLICATION_HEAD_CONFLICT` |
| `ls-remote` non-zero exit | `D_UNAVAILABLE` | `PUBLICATION_HEAD_UNREADABLE` |
| `ls-remote` unparseable output | `D_UNAVAILABLE` | `PUBLICATION_HEAD_UNREADABLE` |
| resume at `GIT_CONFIRMED`, head moved | `C_CONFLICT` | `PUBLICATION_HEAD_CONFLICT` |
| resume at `GIT_CONFIRMED`, remote unreachable | `D_UNAVAILABLE` | `PUBLICATION_HEAD_UNREADABLE` |

- `test_c_and_d_never_collapse` — an explicit guard that no code path maps a transport failure to `PUBLICATION_HEAD_CONFLICT`; the two codes are raised by distinct branches and asserted to be distinct.

R1 tests to update in `test_r1_live_publish_finishing.py` (contracts intentionally inverted):
- `test_failed_publish_never_moves_the_branch` → now the branch **does** move; assert `last_live_release` unchanged.
- `test_publication_failure_keeps_the_site_live` → inverted to `test_publication_failure_never_touches_production` (no Vercel call at all).
- `test_preview_intent_of_another_operation_is_never_published` and `test_a_commit_outside_the_tested_snapshot_refs_is_never_pushed` → assert the publish fails closed with no Vercel call.
- `test_rejected_push_reconciles_the_remote_head_once` → replaced by the case-A test (#2); the re-parent scenario is now case C.
- `test_no_github_read_back_in_r1` → still zero remote reads on the happy path; `ls-remote` is now permitted exactly once on rejection/resume, for exactly one ref. No fetch/clone/pull/archive ever.
- `test_unavailable_remote_head_fails_closed_without_forcing` → still passes; error code is `PUBLICATION_HEAD_UNREADABLE` (**not** `PUBLICATION_HEAD_CONFLICT` — the earlier note in this plan was wrong). Add a sibling `test_conflicting_remote_head_fails_closed_without_forcing` asserting `PUBLICATION_HEAD_CONFLICT` for the valid-but-unexpected head.
- All `state.repository[...]` assertions → read the release record instead.

### Validation

```
scripts/run_tests.sh website-builder/tests/test_r2_release_identity.py -q
scripts/run_tests.sh website-builder/tests/test_r2_publication_reconciliation.py -q
scripts/run_tests.sh website-builder/tests/test_r1_live_publish_finishing.py website-builder/tests/test_promote.py website-builder/tests/test_promote_parity.py website-builder/tests/test_publish_dedup.py website-builder/tests/test_r1_smoke_failure_recovery.py -q
scripts/run_tests.sh website-builder/tests/ -q
```

**No test baseline was established in this session** — the sandbox permission layer blocked `python -m pytest`, so no command run here produced a pass/fail result. The implementer must record the pre-change `test_r1_live_publish_finishing.py` result before touching source, so the inverted-contract churn is attributable.

## Risks

- **The reorder is a real behaviour change.** Any install relying on "site goes live even when GitHub is unreachable" stops publishing. That is the accepted trade and the reason for the contract; call it out in the release notes for the batch.
- **The branch legitimately ends up ahead of production** after a production or smoke failure. That is intended. `publication_head` is what keeps the next publication chainable.
- **Legacy `LEGACY_PARTIAL` releases** may lack `publication_commit` / `publication_tree` (R1 persisted only the publication commit and the deployment identity). They are valid **only** as the *previous* release identity (rollback target), they are never re-committed as a new release, and they are never silently upgraded to `COMPLETE`. Callers that need a complete identity must check `completeness` rather than testing for `publication_commit`. Hydration (out of scope) must treat an unknown publication identity on a partial record as *unknown*, not as a conflict and not as "assume it matches the branch".

## Out of scope

Hydration, design-stack changes, Laya, Hostinger, Strix, Impeccable, and any Vercel Git-integration provisioning. No commit, no push, no deploy.

## Ready to implement — checklist

- [ ] Confirm the working branch before any edit: expected `batch_B` (task text says `feature/website`). Do **not** switch; do not commit or push.
- [ ] Record the pre-change baseline for `website-builder/tests/test_r1_live_publish_finishing.py` (and `test_promote.py`, `test_promote_parity.py`, `test_publish_dedup.py`, `test_r1_smoke_failure_recovery.py`) so the inverted-contract churn is attributable.
- [ ] T1 `git_output.py`: `prepare_publication`, `push_prepared_publication` (no post-push read), `reconcile_publication_head` with A/B/C/D + `error_code`, `_remote_head` transport-vs-absent split, delete `publish_project_branch` + re-parent.
- [ ] T2 `release.py`: 5 stage constants, `_assert_stage_advances` (no skips, no regressions), `validate_last_live_release(record, "new_release" | "previous_release")`, `advance_stage` with `StaleOperationIntent`, `commit_release` (always `COMPLETE`), `mark_terminal_failure`, `mark_reconciliation_required`.
- [ ] T3 `state.py`: lazy `LEGACY_PARTIAL` derivation from `last_live_deployment` + `repository.publication_commit`; `publication_head` seeding; drop `repository` contents; null (never fabricate) every unknown field; no upgrade on read.
- [ ] T4 `promote.py`: branch resolved at PREPARED; combined `promotion_intent` + `pending_publication` write; `_confirm_git` (push only, or a no-op advancing to `GIT_CONFIRMED` when unconfigured); delete `_publish_live_source` / `_record_source_sync*`; supersede guard; `is_resume` / `resume_publish` git reconciliation before any Vercel call; `release` payload replaces `source_sync`.
- [ ] T5 `runtime.py`: wire the release coordinator; `--reconcile-publish` unchanged.
- [ ] Tests: sections 1–13 above, including `test_happy_path_performs_zero_remote_reads`, the NOT_CONFIGURED stage suite, the COMPLETE/LEGACY_PARTIAL suite, and the C-vs-D code table.
- [ ] Green run: the four `scripts/run_tests.sh` invocations in **Validation**, including the full `website-builder/tests/` suite.
- [ ] No commit, no push, no deploy at the end of the batch.
