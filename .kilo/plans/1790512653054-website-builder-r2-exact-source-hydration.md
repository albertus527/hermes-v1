# Website Builder R2 — Batch C: exact-source hydration, LIVE/DRAFT revision base, R1→R2 admission

Scope: `website-builder/` only. Batch A (credential isolation) and Batch B (release identity) assumed accepted.
**SPEC/PLAN ONLY. Do not implement from this document. Do not commit, push, deploy, or switch branches.**

---

## 1. Repo-verified starting state

| Fact | Location |
|---|---|
| `reserve()` order: writer lock → `require_mutating_role` → F6 re-drive → `seq != expected` gate → lifecycle gate → **mutation** | `app/projects/revise.py:113-171` |
| `apply()` order: load → authz → `queued_revision_seq` → `revision_seq >= seq` → delivered-unapplied reconciliation → lifecycle check → `acquire_project` → **one** writer block containing compose, workspace resolution, `QUEUED`→`RUNNING`, `source_revision += 1`, invalidation, `save`, then `frontend_build` | `app/projects/revise.py:240-385` |
| **The only production caller of `revise.apply()` is the dispatcher, and it never passes `workspace=`.** | `app/channels/dispatch.py:443-459`; `app/runtime.py:895-901` |
| `apply()` currently does `workspace = workspace or self.runner.create_workspace(project_id)` | `app/projects/revise.py:327` |
| **`_fail()` takes `error_code` and returns it on `RevisionResult`, but the persisted `state.failure` has only `phase`, `seq`, `error`, `failed_at`.** All 8 call sites already pass the code, so persisting it needs no call-site change. | `app/projects/revise.py:563-577`; call sites 381, 390, 397, 426, 438, 461, 506, 536 |
| `create_workspace()` resolves a path, `mkdir`s it, then creates `.hermes/`, `.browser/`, `.runtime/` **under that resolved path** | `app/sandbox/runner.py:201-211` |
| `create_workspace()`, `run_command()`, `start_background()` **each independently** call `project_workspace_path(...)` | `app/sandbox/runner.py:203, 240, 354` |
| `_build_project_env` returns `credentials.build_env(project_id, workspace, extra)`, which sets `HERMES_HOME = <workspace>/.hermes` and `WORKSPACE_ROOT = str(workspace)` | `app/sandbox/runner.py:309`; `app/core/credentials.py:579-580` |
| `_migrate_release_identity()` mutates `data["deployment"]` **in place** and only ever sets `last_live_release`, `publication_head`, and the D8′ projection; other keys are untouched. Ends with `data["repository"] = {}` on every load. | `app/core/state.py:62-104` |
| `ReleaseCoordinator.commit_release()` owns the **single** writer-locked COMMITTED save; all validation precedes `save(state)` | `app/projects/release.py:857-938` |
| `OutputGitRepository.commit()` builds the tree as `dict(snapshot.source)` then adds `dist/<name>` | `app/deploy/git_output.py:161-163` |
| Git env role-scoped; hooks disabled (`-c core.hooksPath=<devnull>`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<devnull>`) | `app/deploy/git_output.py:129-152` |
| `_run(..., extra=...)` is the `_ssh_env` seam; `credentials_env` exists but the test harness subclass does **not** accept it | `app/deploy/git_output.py:129`; `tests/test_r1_live_publish_finishing.py:140` |
| `EXCLUDED` covers `.git, node_modules, dist, qa, .hermes, .browser, .runtime` | `app/deploy/snapshot.py:15` |
| `FAILED` has **no** edge to `REVISION_REQUESTED`; F6 re-drive requires `lifecycle == REVISION_REQUESTED` | `app/core/lifecycle.py:85-99`; `app/projects/revise.py:130-145` |
| `tests/test_r2_release_identity.py:670` asserts `set(record) == set(RELEASE_FIELDS)` on a COMPLETE record | that file |

**Path mapping (settled).** `read_tree(workspace, EXCLUDED)` skips a top-level `dist`, so `snapshot.source` can never hold a `dist/`-prefixed key.
- **LIVE** materialization is **identity**: commit `dist/index.html` → `<staging>/dist/index.html`; `src/App.tsx` → `<staging>/src/App.tsx`.
- **DRAFT** materialization is **not** identity: `tested_snapshot["source"]` keys are root-relative and `tested_snapshot["dist"]` keys are `dist/`-relative.

**Always-hydrate is safe.** At `PREVIEW_READY` the workspace is byte-identical to `tested_snapshot`; at `LIVE` it is byte-identical to what was published.

---

## 2. Locked decisions

Preserved: **D1** pointer-file indirection, operation-specific `.ops/rev-<seq>` · **D2** local-first, exact pinned remote fallback · **D3** verdict + reservation refusal only, no operator CLI or repair workflow · **D5** base kind keyed on lifecycle only · **D6** verdicts in `deployment.canonical_source`, never inside `last_live_release` · **D9** partial hydration never usable · **D10** no leftover mutable-workspace reconstruction · **D11** unsupported submodule/symlink fail-closed · **D12** blob-exact `ls-tree` + `cat-file` + Python writes · **D14** pointer swap is the commit boundary · **D15** hydration failure → `FAILED` · **D16** post-swap `READY`-save failure is same-operation re-drivable · **D17** R1 `LEGACY_PARTIAL` LIVE projects intentionally non-revisable · **D4′** `publication_commit` materialized, `tested_commit` only corroborates · **D8′** legacy evidence projected and popped · **D19** foreign current-target op preserved · **D20** `workspace=` is a test-only seam · **D21** one pointer-aware resolver · **D22** DRAFT traverses the same four stages · **D23** `fetched_locally` semantics · **D24** foreign `READY` supersede · **D25** `_fail()` persists `error_code` · **D26** runtime subdirs inside the resolved workspace.

| # | Decision | Status |
|---|---|---|
| **D13** | `current` holds **only** a validated op token (`rev-<digits>`), never a commit SHA; no commit/branch/repo identity is inferred from it | preserved |
| **D13‴ → superseded by D27** | ~~Legacy fallback gated on `hydration.state == READY` alone~~ | **superseded** — unsound under D24; see D27 |
| **D27** | **`deployment["pointer_mode"] = True` is a monotonic durable marker** that the project has crossed the pointer commit boundary at least once. It is the *only* historical witness the resolver consults. It is **never** reset to `False` and never re-inferred from `current` absence. Smallest possible state surface: one boolean. | **new** |
| **D28** | **DRAFT hydration records carry `None` for every LIVE-only identity field.** No dummy SHA, fake repo, fake branch, placeholder tree, or fake corroboration value. Per-kind validators enforce this both directions. | **new** |
| **D29** | **LFS detection covers exactly the implemented canonical pointer grammar** and nothing more. Files outside it are ordinary committed bytes. Digest verification is a content-integrity check, **not** a general LFS detector. | **new** |

### Why D27 is required (the concrete failure it closes)

`deployment.hydration` is a **single** operation record, and D24 lets a later operation supersede it:

```
rev-7:  hydration.state = READY,   current = rev-7
rev-8:  hydration.state = RESERVED, current = rev-7      (D24 supersede)
        current deleted
```

A resolver keyed on `hydration.state == READY` sees `RESERVED`, concludes "never hydrated", and returns the legacy mutable R1 workspace — resurrecting precisely the source-of-truth behavior this batch exists to remove. `pointer_mode` is unaffected by supersede, so it closes this.

### The one residual, stated rather than engineered around

`pointer_mode` is persisted **after** the atomic pointer swap, so there is an irreducible window in which the filesystem says "pointer mode" and the durable state does not. That ordering is deliberate: the reverse (persist `pointer_mode` first, then swap) would let a crash between the two leave `pointer_mode = True` with no `current` — and since `pointer_mode` is monotonic and legacy fallback is permanently forbidden, that is an **unrecoverable wedge**. Swap-then-save is the only ordering whose failure mode is recoverable.

The residual requires **two independent faults**: a crash after the swap *and* loss of `current` before the `READY`/`pointer_mode` save lands. Nothing in this codebase deletes `current` (it is written only by `write_pointer`), so reaching it requires external filesystem tampering — already an operator-repair scenario. Documented, not designed around. Test 10 covers the single-fault case.

---

## 3. Canonical-source verdict

| # | Verdict | Fires when | Domain |
|---|---|---|---|
| 1 | `NO_LIVE_RELEASE` | `deployment.last_live_release` absent or not a dict | any |
| 2 | **`SOURCE_SYNC_REQUIRED`** | `completeness == LEGACY_PARTIAL` **and** `legacy_source_sync.sync_status == "SOURCE_SYNC_REQUIRED"` **exactly** | R1 |
| 3 | `LEGACY_RELEASE_IDENTITY` | `completeness == LEGACY_PARTIAL` with no more specific durable evidence | R1 |
| 4 | `PUBLICATION_NOT_CONFIGURED` | `completeness == COMPLETE` and `publication_configured is False` | R2 |
| 5 | `NO_PUBLICATION_COMMIT` / `PUBLICATION_PARENT_UNRESOLVED` / `REMOTE_IDENTITY_UNRESOLVED` | `completeness == COMPLETE` with a missing/malformed identity field; also the landing spot for a `completeness` that is neither COMPLETE nor LEGACY_PARTIAL | R2 |
| 6 | `READY` | — | R2 |

Verdicts 2–3 apply **only** inside the `LEGACY_PARTIAL` branch; 4–5 only inside `COMPLETE`; the linear order holds *within* each branch. The branch gate is load-bearing: a project that was R1 and later earned a genuine R2 `COMPLETE` release would still carry stale `legacy_source_sync`. Two independent defenses — the gate, and D8′'s `commit_release` pop. `SOURCE_SYNC_REQUIRED` fires only on the exact string; `SYNCED` or any other value falls to verdict 3. **Absence is unknown, never `False`.** `canonical_source_verdict()` is a pure read; `revise.reserve()` is its only caller. `DRAFT_SNAPSHOT_UNAVAILABLE` is **not** in this set.

---

## 4. LIVE identity chain

`tested_commit` is the immutable **tested snapshot** identity; `publication_commit` is the **publication** identity. Batch B proves tree equality when the release is committed. The immutable `preview/<proj-sha>/<snapshot-sha>` ref points at **`tested_commit`**, never at `publication_commit`.

1. the immutable `preview/…` ref still points at `tested_commit`;
2. `tested_commit^{tree}` == recorded `tested_tree`;
3. `publication_commit^{tree}` == recorded `publication_tree`;
4. recorded `tested_tree` == recorded `publication_tree`;
5. `publication_repo` / `publication_branch` == recorded, and the repo name derived from the operator-configured `source_repo_url` matches `publication_repo`;
6. the object materialized is **exactly** the recorded `publication_commit`.

Step 5 is the **same** `verify_repository_identity` call as T3.5, *hoisted* ahead of the fetch so the refusal is cheap and offline; hoisting a pure read out of the logical order changes no outcome.

If `publication_commit` is **not** locally available: pinned fetch of **exactly** `publication_commit` into `refs/hydrate/<40hex>`; **never** branch HEAD; **never** materialize `tested_commit` in its place. Re-assert 3, 4, 5 after the fetch; steps 1–2 become best-effort, recorded as `tested_commit_corroborated: false`.

**Content identity is proven by bytes, not by the chain.** The authority is the `source_sha256` / `artifact_sha256` comparison at `VERIFIED`.

---

## 5. `pointer_mode` semantics

| Value | Meaning | Legacy fallback |
|---|---|---|
| absent / `False` | the project has never successfully committed a pointer swap | allowed when `current` is absent |
| `True` | the project has crossed the pointer commit boundary **at least once** | **permanently forbidden** |

Rules:

1. **Monotonic.** absent/`False` → `True` only. Never reset to `False`, never deleted, never inferred backward from `current` absence.
2. Set **atomically with the first successful pointer promotion**, in the same recovery-safe sequence as `READY` (§8).
3. It lives in the freeform `deployment` bag. `_migrate_release_identity` mutates that dict in place but only ever sets `last_live_release`, `publication_head`, and `legacy_source_sync`, so a load/migration round-trip preserves it untouched (test 12).
4. **D24:** later operations may replace `deployment.hydration` freely; `pointer_mode` stays `True` forever.
5. No larger workspace-state object. One boolean is sufficient because the only question it answers is "has a swap ever committed?".

---

## 6. Pointer resolver contract (final)

`current` holds **only** a validated op token, e.g. the single line `rev-7`.

| # | Condition | Behavior |
|---|---|---|
| **1** | `current` **absent** AND `pointer_mode` is not `True` | Return the legacy `<workspace_root>/<project_id>` path. The R1 / never-hydrated compatibility case. |
| **2** | `current` **absent** AND `pointer_mode` is `True` | **FAIL CLOSED** → `WORKSPACE_POINTER_MISSING_AFTER_HYDRATION`. |
| **3** | `current` **exists** with a valid `rev-<digits>` token | Resolve exactly `<project_id>/.ops/<token>`; validate containment; refuse symlink escape. |
| **4** | `current` **exists** but is empty, multi-line/multi-token, 40-hex, otherwise invalid, malformed, or a symlink; or names a missing op dir, or a symlinked/escaping op dir | **FAIL CLOSED** → `WORKSPACE_POINTER_INVALID`. |

**No other legacy fallback exists.** Rule 1 is safe only for a project that has genuinely never entered pointer mode; rule 2 permanently forbids resurrecting the mutable workspace once a swap has committed, regardless of what `hydration` currently says.

States `RESERVED` / `FETCHED` / `VERIFIED` with `current` absent are rule 1 — the swap has not committed, so the legacy workspace is still legitimately current. This is exactly the D16 case (b) resume.

`sweep_ops()` preserves the pointer target **unconditionally**, independent of hydration state.

---

## 7. LIVE and DRAFT hydration flows

Both traverse the same enum and the same order (D22).

### 7.1 Common

1. **RESERVED** — `assert_immutable`: the on-disk **reservation's** `base` must equal the in-memory one → else `HYDRATION_BASE_DRIFT`.
2. **FETCHED** — kind-specific acquisition (§7.2 / §7.3).
3. **Materialize** into `<project_id>/.ops/rev-<seq>/` (fresh; exists-and-not-ours → `HYDRATION_STAGING_UNAVAILABLE`).
4. **VERIFIED** — create `.hermes/`, `.browser/`, `.runtime/` **inside the staging workspace** (D26); then in order: `source_fingerprint == base.source_sha256`; `digest(read_tree(staging/'dist')) == base.artifact_sha256`; and an independent `read_tree` re-walk of the real filesystem confirming §9's name checks against reality. Runtime subdirs are covered by `EXCLUDED`, so digests are unaffected. **Until this write succeeds, staging is unreachable by every component** (D9).
5. **READY** — §8.

### 7.2 LIVE `FETCHED` (real source acquisition)

1. `verify_repository_identity` — **offline, before any network**.
2. Resolve the object: local when the `tested_commit` ref and both trees resolve, else pinned fetch of `publication_commit` exactly.
3. `materialize_commit(commit, expected_tree, staging)` — §9.

### 7.3 DRAFT `FETCHED` (local no-op acquisition)

**No Git subprocess, no network.** Validate and load the frozen `tested_snapshot`: `TestedSnapshot.from_dict` must parse, its `source_sha256` / `artifact_sha256` must equal the frozen `base`, and `snapshot.identity` must equal the frozen `snapshot_identity`. `fetched_locally = True` (D23) — the durable `tested_snapshot` is local and no network acquisition occurs. On failure: `HYDRATION_SOURCE_MISMATCH` / `HYDRATION_ARTIFACT_MISMATCH` / `DRAFT_SNAPSHOT_UNAVAILABLE`, staging discarded, pointer untouched.

Then materialize: `source` at the root, `dist/<name>` under `dist/`, under the same path predicate and per-entry containment check as LIVE. `last_live_release` is never read on this path.

### 7.4 DRAFT hydration record schema (D28)

| Field | LIVE | DRAFT |
|---|---|---|
| `commit` | `publication_commit` | `None` |
| `repo` | `publication_repo` | `None` |
| `branch` | `publication_branch` | `None` |
| `expected_tree` | `publication_tree` | `None` |
| `tested_commit_corroborated` | `True` / `False` | `None` |
| `fetched_locally` | `True` / `False` | `True` |
| `base_kind` | `"LIVE"` | `"DRAFT"` |
| `op_dir`, `op_token`, `expected_source_sha256`, `expected_artifact_sha256`, `operation_id`, `state`, `updated_at` | identical | identical |

DRAFT source authority is the frozen `snapshot_identity`, the frozen `source_sha256` / `artifact_sha256`, and the durable `tested_snapshot`. Per-kind validators enforce: LIVE requires all five identity fields non-`None` and 40-hex where applicable; DRAFT requires all five to be exactly `None` and `fetched_locally is True`. A violation is `HYDRATION_RECORD_INVALID` at load, not a silent coercion.

`hydrated_from` diagnostics mirror the same shape: `{"kind", "commit", "fetched_locally", "tested_commit_corroborated"}`, with `None` for the DRAFT identity fields.

---

## 8. READY transition and the D16 window (final)

```
BEFORE the swap:   staging is discardable; pointer_mode unchanged.
AFTER atomic swap: the workspace is committed/current;
                   it must NEVER be deleted or rolled back.
THEN persist `pointer_mode = True` and `hydration.state = "READY"` in **one** `store.save()` under a single writer lock — never as two saves:
                   deployment["pointer_mode"] = True
                   deployment["hydration"]["state"] = "READY"
THEN:              safe stale-op cleanup.
```

If that durable save fails: pointer and target remain intact; **no rollback, no delete**; next same-operation resume persists `pointer_mode = True` + `READY` with **no re-materialization and no rollback** (D16).

### 8.1 Entry / recovery table

| On-disk `deployment.hydration` | Action |
|---|---|
| **ours** + `READY` | idempotent success |
| **ours** + `VERIFIED` | two-case resume based on the `current` pointer (§8.2) |
| **ours** + `FETCHED` | discard staging; re-materialize from §7.2/§7.3 using the frozen base |
| **ours** + `RESERVED` | discard staging; restart acquisition/materialization from the frozen base |
| **foreign** + `READY` | **D24:** normal previous completed hydration. Preserve the current pointer target. **Do not** return `HYDRATION_RECOVERY_REQUIRED`. Allow the new operation to supersede this record with its own `RESERVED`. Keep the current target in the sweep keep-set until the new swap commits. `pointer_mode` remains `True` (D27.4) |
| **foreign** + non-`READY` + `current` **points at** the foreign op | **D19:** never delete or sweep that op dir; never start a new hydration over it; **hold** with `HYDRATION_RECOVERY_REQUIRED` |
| **foreign** + non-`READY` + `current` does **not** point at it | abandoned staging; safe to sweep after containment and symlink checks |

The §6 pointer resolve runs first, independently of this table.

Superseding loses no base identity: a resumed operation's base always comes from **its own unapplied `pending_revisions` entry** (which `prune_bounded_ledgers` keeps), never from the hydration record.

### 8.2 The two resume cases

Ask one question: *does `current` name our op token?*

- **(a) yes** — the swap already happened. **Preserve** the directory: no rebuild, no re-materialization, no delete. Persist `pointer_mode = True` + `READY`, then safe stale-op cleanup.
- **(b) no** — the swap did not happen; staging is still discardable. **Re-verify both digests before making it current** (it has sat on disk across a restart and nothing protects it), then complete the swap, then persist `pointer_mode = True` + `READY`, then clean up.

Treating (a) as (b) rebuilds an already-current workspace; treating (b) as (a) strands a complete staging directory forever. Both are tested.

---

## 9. Blob and LFS policy

`materialize_commit(commit, expected_tree, staging)` — LIVE only; DRAFT uses the same predicate and containment checks on snapshot keys.

1. `rev-parse <commit>^{tree}` must equal `expected_tree` → else `HYDRATION_TREE_MISMATCH`.
2. `ls-tree -r -z <commit>`, parsed strictly as `<mode> SP <type> SP <sha> TAB <path> NUL`. Unparseable → `HYDRATION_UNSAFE_ENTRY`.
3. Accept only ordinary blob modes `100644` and `100755` with type `blob`.
4. Refuse, each with its own `reason` and the offending path named: `120000` (`symlink`), `160000` (`submodule`), non-blob (`non_blob`), any other mode (`mode`).
5. Refuse unsafe paths with the same predicate `commit()` uses (`git_output.py:170-172`): leading `/`, empty/`.`/`..`/`.git` components, `\`, `:`, NUL (`unsafe_path`).
6. **Containment check before every write**: destination strictly under the resolved `staging`; `staging` not a symlink; no intermediate component an existing symlink. Re-checked **per entry**.
7. **Inspect blob bytes before writing.** Canonical Git-LFS pointer content — at minimum the header `version https://git-lfs.github.com/spec/v1` plus the expected pointer structure — is refused with **`HYDRATION_UNSUPPORTED_LFS`**, naming only the safe repository path.
8. Otherwise write the exact bytes with **Python**; set the executable bit from the mode for `100755` only.

Never used: `checkout`, `checkout-index`, `read-tree` as a worktree materializer, `tar`, any shell pipeline, `filter.*`, clean/smudge, hooks.

### 9.1 LFS contract and its exact boundary (D29)

- Batch C explicitly recognizes and refuses the **canonical Git-LFS pointer grammar its detector implements** — the canonical `version https://git-lfs.github.com/spec/v1` header plus the expected pointer structure.
- Files **outside that recognized grammar** are treated as ordinary committed bytes.
- Batch C does **not** claim to detect arbitrary, malformed, adversarial, or non-standard LFS-like encodings, and does not attempt to.
- Digest verification at `VERIFIED` is a **content-integrity check, not a general LFS detector.** It is explicitly *not* a safety net for unrecognized LFS encodings: if such bytes were themselves what was tested and published, the recorded digest matches them and hydration legitimately succeeds with the pointer text as the content. That is a recorded, accepted limitation — the batch's contract is the canonical grammar, not a general guarantee.
- No `.gitattributes` evaluator. `.gitattributes` stays inert project bytes. An optional extra refusal signal is permitted **only** if a simple, deterministic, non-executing textual check spots an explicit `filter=lfs` line; step 7 is the authoritative minimum.
- On detection: fail closed; do not execute Git LFS, run filters, invoke clean/smudge, or fetch the LFS object.

**Performance (deferred).** One `cat-file` per blob — tens to low hundreds of files, inside the existing 128 MiB / 10 000-file bounds and dwarfed by `npm ci`. `cat-file --batch` is a permitted later optimization **only** if it preserves identical parse/validation/fail-closed behavior.

---

## 10. Workspace layout and resolver authority

### Pointer mode
```
<workspace_root>/<project_id>/
  current                     # one line: the op token, e.g. "rev-7". Never a commit.
  .ops/
    rev-7/
      .hermes/                # runtime dirs live INSIDE the resolved workspace
      .browser/
      .runtime/
      <project source>
      dist/
```

### Legacy mode (rule 1)
```
<workspace_root>/<project_id>/
  .hermes/  .browser/  .runtime/
  <project source>  dist/
```

1. `resolve_workspace(project_id)` is the **single** authority, implementing §6 rules 1–4. It never returns the legacy path once `pointer_mode` is `True`.
   - **State read constraint:** `ProjectRunner` already holds `self.state_store` (`runner.py:194`), so the resolver reads `pointer_mode` with a plain `state_store.load(project_id)`. It must **never** acquire the writer lock — `build.py` calls `create_workspace` from inside a writer block, and reaching for a lock in the resolver would invert lock order. `load()` is a bare file read plus the in-memory migration, so the nested read is safe.
   - **`load()` raising `ProjectNotFoundError`** (no state file) means the project has no `pointer_mode` and therefore never crossed the boundary: treat `pointer_mode` as absent and fall to rule 1. A workspace with no state file cannot have a hydrated op dir.
2. `create_workspace(project_id)` → resolve **first**, then `mkdir` the resolved workspace, then create the three runtime subdirs **inside it**. It is never `mkdir` the legacy root and then discover the op dir.
3. `run_command` and `start_background` call the **same** resolver, use the resolved path for cwd containment (`WorkspaceError` when cwd escapes it), and pass it to `_build_project_env` — so `WORKSPACE_ROOT` and `HERMES_HOME` both bind to the op dir.
4. **No component may hydrate into `.ops/rev-N` while another runtime helper creates or uses the runtime dirs under the legacy root.** All four entry points resolve first; there is no second path.
5. `write_pointer` — one line, `rev-<digits>` only; refuses a symlinked `current` and a symlinked `.ops`.
6. `sweep_ops(project_id, keep=…)` — deletes op dirs not in `keep`; **preserves the pointer target unconditionally** (D24: the old current stays until the new swap commits).
7. With rule 1 satisfied, every path is byte-for-byte compatible with R1.

---

## 11. R1 admission semantics

Every historical R1 LIVE release migrates to `LEGACY_PARTIAL`; such a project is refused at `reserve()`. This batch adds no operator CLI and no user-facing repair workflow, therefore **Batch C guarantees no path by which an R1 LIVE project becomes revisable**.

1. R1 `LEGACY_PARTIAL` LIVE projects are **intentionally NON-REVISABLE** through the new exact-source revision path.
2. **Retrying does not help** — the refusal is a property of durable state, not a transient condition.
3. Batch C **does not** reconstruct canonical source from a leftover mutable workspace.
4. Batch C **does not** fabricate a `COMPLETE` identity from a `LEGACY_PARTIAL` one.
5. A future batch or out-of-band operator workflow may establish admission. **Out of scope here.**
6. A project that does obtain a genuine `COMPLETE` R2 release through another valid path reads `READY` afterwards. Batch C does not guarantee that path exists for every R1 project, and neither copy nor docs may imply one.

**Copy must match.** `CANONICAL_SOURCE_*` refusals state that the revision cannot start safely, that the site and production are unchanged, and that a new verified publication is required. No "try again", no implied automatic recovery, no transient-error framing.

---

## 12. `reserve()` / `apply()` ordering

### 12.1 `reserve()`
1. writer lock; 2. `require_mutating_role`; 3. F6 re-drive branch; 4. sequence gate and lifecycle gate; 5. **compute and freeze `base`** — after admission is proven, **before** any mutation; 6. mutate: `queued_revision_seq = seq`, lifecycle transition, append with `base`, prune, save.

A refusal leaves durable state byte-equivalent apart from unavoidable filesystem metadata: **no** sequence bump, **no** lifecycle mutation, **no** reservation append, **no** `canonical_source` cache write. F6 re-drive returns the existing reservation unchanged and **must not** recompute or overwrite `base`.

- **LIVE:** `canonical_source_verdict(state)`; if not `READY`, return the specific code with zero mutation. Otherwise build `base` from `last_live_release`.
- **DRAFT:** require `tested_snapshot` present, parseable, and its digests equal to `deployment.checked`; else `DRAFT_SNAPSHOT_UNAVAILABLE`, same zero-mutation guarantee.
- `prune_bounded_ledgers()` keeps every unapplied reservation, so the frozen base survives. **Verify with a test.**

### 12.2 `apply()`
1. existing read / authz / reservation / delivered-unapplied reconciliation guards; 2. acquire the `MAX_WORKERS=1` slot; 3. **hydrate** using the reservation's frozen base; 4. **only after hydration succeeds**, acquire the existing mutation writer block; 5. re-check reservation and authz under that writer; 6. compose instructions; 7. transition `QUEUED` → `RUNNING`; 8. increment `source_revision`; 9. invalidate `checked` / QA / preview / approval state; 10. FRONTEND and the existing remainder of the pipeline.

Hydration refusal therefore occurs **before** step 4's state mutation.

**On hydration failure:** no FRONTEND invocation, no build, no QA, no preview, no Vercel/provider deployment call, no production mutation, and no `source_revision` bump. The classified failure is recorded and the project transitions to **`FAILED`** through the existing `_fail()` (D15). The reservation and its frozen base stay durable and unapplied.

**Permitted exception — narrow.** Hydration itself **may already have performed one narrowly-scoped Git network operation**: `fetch exactly the recorded publication_commit from the configured remote`, when the object was unavailable locally. That is part of hydration and is permitted under D2. "No remote call" is **wrong** and must not appear in code, tests, or comments. What the failure guarantees is the **absence of application/provider remote effects**, not the absence of a permitted exact Git fetch. The exact-commit / no-HEAD rule is unchanged.

**Documented consequence:** `FAILED` has no edge to `REVISION_REQUESTED`, so the same `seq` is not re-drivable after a hydration failure. Pre-existing lifecycle contract, unchanged.

**The one exception (D16):** a post-swap failure to persist `READY` / `pointer_mode` is a *durability* failure, not a hydration failure. Do not call `_fail()` and do not transition; return `HYDRATION_STATE_UNPERSISTED`, leave the lifecycle at `REVISION_REQUESTED`, leave the promoted workspace intact. The next same-principal `reserve(seq)` hits the existing F6 re-drive branch and `apply()` resumes into **§8.2 case (a)**: pointer already correct, so it persists `pointer_mode = True` + `READY` and cleans up, then proceeds. No double `source_revision` bump.

### 12.3 The `workspace=` seam (D20)
`apply(workspace: Optional[Path] = None)` is a **narrowly-scoped compatibility/test seam**:
- normal/production dispatch never supplies it (sole production caller `app/channels/dispatch.py:443-459`);
- when `workspace is None`, **hydration is mandatory and there is no fallback** — the current `workspace or self.runner.create_workspace(project_id)` implicit fallback is removed, because that fallback *is* "silently use a leftover mutable workspace";
- **before implementation, re-verify every production call site.** If any non-test production caller supplies `workspace=`, migrate it to hydration or fail the implementation review;
- architecture comment at the parameter plus a test asserting dispatch never exercises it;
- **no new public bypass flag**; no caller may choose an arbitrary leftover workspace in the normal runtime path.

### 12.4 Persisted failure classification (D25)
`_fail(project_id, seq, error, error_code)` gains `"error_code": error_code` in the persisted `state.failure` dict. All 8 existing call sites already pass it, so **no call-site change is required**. The returned `RevisionResult.error_code` and the persisted value must agree; lifecycle semantics unchanged; callers not inspecting it are unaffected; **no new failure subsystem**.

---

## 13. State shape

```python
# D27 — monotonic, one boolean, never reset
state.deployment["pointer_mode"] = True      # absent/False = never crossed the boundary

# appended to the existing pending_revisions entry for the reserved seq
state.pending_revisions[i]["base"] = {
    "base_kind": "LIVE" | "DRAFT",
    "reserved_at": <float>, "revision_seq": <int>,
    "requirements_version": <int>,   # revisions.requirements_version at reserve time
    "design_dna_version": <int>,     # revisions.design_dna_version at reserve time
    "source_revision": <int>,        # LIVE: last_live_release.source_revision
                                      # DRAFT: revisions.source_revision
    # LIVE only
    "publication_commit": "<40 hex>", "publication_tree": "<40 hex>",
    "tested_commit": "<40 hex>", "tested_tree": "<40 hex>",
    "publication_repo": "owner/name", "publication_branch": "<friendly>",
    # both
    "source_sha256": "<64 hex>", "artifact_sha256": "<64 hex>",
    # DRAFT only
    "snapshot_identity": "<64 hex>",
}

# exactly one record; superseded by the next operation's RESERVED (D24).
# Per-kind identity fields: LIVE populated, DRAFT exactly None (D28).
state.deployment["hydration"] = {
    "operation_id": "rev-<seq>",
    "state": "RESERVED" | "FETCHED" | "VERIFIED" | "READY",   # same enum/order both kinds
    "base_kind": "LIVE" | "DRAFT",
    "commit": "<40 hex>" | None,               # publication_commit | None
    "repo": "owner/name" | None,
    "branch": "<friendly>" | None,
    "expected_tree": "<40 hex>" | None,       # publication_tree | None
    "tested_commit_corroborated": True | False | None,
    "fetched_locally": True | False,           # DRAFT is always True
    "op_dir": "<absolute op path>", "op_token": "rev-<seq>",
    "expected_source_sha256": "<64 hex>", "expected_artifact_sha256": "<64 hex>",
    "updated_at": <float>, "error_code": None | str,
}

# D8′: projected before repository is emptied; popped only by a successful
# COMPLETE R2 COMMITTED save
state.deployment["legacy_source_sync"] = {"sync_status": "SOURCE_SYNC_REQUIRED" | ...}

# D25: existing record, one field added
state.failure = {"phase": "revision", "seq": <int>, "error": <str>,
                 "error_code": <str>, "failed_at": <float>}
```

---

## 14. Module map and task list

| File | Change |
|---|---|
| **NEW** `app/deploy/hydrate.py` | `BaseKind`, `RevisionBase` (build / validate / assert-immutable), `HydrationRecord` (per-kind validators, D28), `WorkspaceHydrator`, `HYDRATION_*` codes |
| `app/projects/release.py` | **+** `CANONICAL_SOURCE_*` constants, `canonical_source_verdict(state)`, the `legacy_source_sync` pop inside `commit_release` |
| `app/core/state.py` | **+** the `legacy_source_sync` projection inside `_migrate_release_identity`, before `data["repository"] = {}`. Must not touch `pointer_mode` |
| `app/projects/revise.py` | `reserve()` freezes the base (§12.1); `apply()` reordered (§12.2); implicit workspace fallback removed; D20 seam documented; **`_fail()` persists `error_code`** |
| `app/sandbox/runner.py` | **ONE** pointer-aware resolver (§6, §10) used by `create_workspace`, `run_command`, `start_background`; `op_dir_for`, `read_pointer`, `write_pointer`, `sweep_ops`; `WORKSPACE_POINTER_INVALID`, `WORKSPACE_POINTER_MISSING_AFTER_HYDRATION`; runtime subdirs inside the resolved workspace |
| `app/deploy/git_output.py` | **+** `has_tested_snapshot_commit`, `fetch_pinned_commit`, `materialize_commit` (§9) |
| `app/runtime.py` | `compose()` injects the hydrator; **+** `ERROR_MESSAGES` entries |
| **NEW** `tests/test_r2_hydration.py` | §16 matrix |
| **NEW** `tests/test_r2_canonical_source.py` | §16 group E |

### T1 — Durable evidence and the verdict
1. `release.py`: `CANONICAL_SOURCE_*` constants and `canonical_source_verdict(state)` per §3 — branch gate, exact-string rule, absent-is-unknown, unusable-`completeness` → `NO_PUBLICATION_COMMIT`. Pure read.
2. `state.py`: inside `_migrate_release_identity`, **before** `data["repository"] = {}`, set `deployment["legacy_source_sync"]` only when `repository["sync_status"]` is a non-empty string. **Never overwrite; never write `False`; never touch `pointer_mode`.**
3. `release.py`: inside `commit_release`'s existing writer-locked block, `state.deployment.pop("legacy_source_sync", None)` **after all validation succeeded and before `save(state)`**.
4. Do not compute the verdict during load; a read path must not write.

### T2 — `reserve()` freezes the base
Per §12.1, inserted between the lifecycle gate and the first mutation. Plus `ERROR_MESSAGES` per code, obeying §11's honesty rules, in the existing Indonesian `PUBLISH_*` register. Nothing into `_INTENTIONALLY_UNHELPFUL`.

### T3 — Git-side recovery and blob-exact materialization
1. `has_tested_snapshot_commit(tested_commit)`.
2. `fetch_pinned_commit(commit, url, *, ssh_key, extra_env)` — literal 40-hex into `refs/hydrate/<40hex>`; never `FETCH_HEAD`, never a branch, never HEAD; refuses non-40-hex. Uses `extra` via `_ssh_env`; **do not pass `credentials_env`** (`test_r1_live_publish_finishing.py:140`).
3. `materialize_commit(...)` — §9 all eight steps, per-entry containment re-check, canonical LFS detection in step 7.
4. The §4 chain with `tested_commit_corroborated` recorded.
5. `verify_repository_identity` — offline, before any fetch.

### T4 — `WorkspaceHydrator`
Per §7 and §8: the §6 pointer resolve first (all four rules, including the `pointer_mode` gate); the D24 foreign-`READY` supersede; the D19 foreign-current-target hold; `sweep_ops` preserving the pointer target; the two resume cases; the four transitions shared by both base kinds; swap-then-`pointer_mode`+`READY`-then-cleanup; swap-bounded failure semantics. DRAFT's `FETCHED` is §7.3's local no-op acquisition.

### T5 — `apply()` reordering and failure classification
Per §12.2 and §12.4. `RevisionResult` gains `base_kind` and `hydrated_from` (`{"kind", "commit", "fetched_locally", "tested_commit_corroborated"}`, DRAFT identity fields `None`).

### T6 — One pointer-aware workspace resolver
Per §6 and §10: `read_pointer`, `resolve_workspace`, `op_dir_for`, `write_pointer`, `sweep_ops`. `create_workspace` resolves first then creates the three runtime subdirs **inside** the resolved workspace; `run_command` / `start_background` use the same resolver for containment and for `_build_project_env`.

### T7 — Tests
Real `ProjectStateStore`, real `OutputGitRepository`, real `git`. Reuse `_bare_remote` / `_mirror_env` / `_remote_git`; add a `_HydratingRepo` recording every `_run` argv so tests assert what hydration does **not** do (`clone`, `pull`, `checkout`, `checkout-index`, `archive`, `tar`, `push`). No network, no credentials, no provider. Full matrix in §16.

### T8 — Validation
```
cd website-builder
py -3 -m pytest tests/test_r2_hydration.py tests/test_r2_canonical_source.py -q -p no:randomly
py -3 -m pytest tests/test_r2_release_identity.py tests/test_r2_publication_reconciliation.py -q -p no:randomly
py -3 -m pytest tests/test_r2_credential_isolation.py tests/test_bypass_legacy_migration.py tests/test_role_preflight.py -q -p no:randomly
py -3 -m pytest tests/test_revise.py tests/test_r1_revision_crash.py tests/test_crash_recovery.py tests/test_r1_live_publish_finishing.py tests/test_runner.py tests/test_build.py -q -p no:randomly
py -3 -m pytest tests -q -p no:randomly        # full suite
```
`scripts/run_tests.sh` needs a venv this machine does not have (Batch A §6); use the `py -3 -m pytest` invocation, the established equivalent.

**Mutation-check every new guard** (Batch A §6c precedent). Disable, one at a time: the pointer-token validation; resolver rule 4 (malformed); **resolver rule 2 (the `pointer_mode` gate)**; the `pointer_mode` monotonicity assertion; per-entry containment re-check; LFS canonical detection; the `ls-tree` mode gate; the `read_tree` re-walk; the `base` immutability assert; the non-admitted-LIVE refusal; the D8′ `commit_release` pop; the D19 foreign-op hold; the D24 foreign-`READY` supersede; the D16 no-rollback/no-delete rule; `sweep_ops` pointer preservation; the D25 `error_code` persistence; the D28 per-kind field validators; the D26 resolve-before-mkdir order. Each must turn the suite red.

---

## 15. Error codes

| Code | Meaning | Retry helps? |
|---|---|---|
| `CANONICAL_SOURCE_NO_LIVE_RELEASE` | LIVE revision with no release record | No |
| `CANONICAL_SOURCE_SYNC_REQUIRED` | R1 evidence proves publication was attempted and not synced | No |
| `CANONICAL_SOURCE_LEGACY_IDENTITY` | R1 `LEGACY_PARTIAL` with no more specific durable evidence | No |
| `CANONICAL_SOURCE_NOT_PUBLISHED` | R2 release committed with publication not configured | No |
| `CANONICAL_SOURCE_COMMIT_MISSING` | COMPLETE record missing a required identity field, or unusable `completeness` | No |
| `CANONICAL_SOURCE_PARENT_UNRESOLVED` | non-null `publication_parent` is not a commit id | No |
| `CANONICAL_SOURCE_REPO_UNRESOLVED` | missing/malformed `publication_repo` or `publication_branch` | No |
| `CANONICAL_SOURCE_REPO_MISMATCH` | recorded repo != configured remote's repo | No (operator config) |
| `DRAFT_SNAPSHOT_UNAVAILABLE` | PREVIEW_READY with no consistent `tested_snapshot` / `checked` | No |
| `WORKSPACE_POINTER_INVALID` | `current` exists but is unreadable, malformed, or names an unusable op dir | No |
| `WORKSPACE_POINTER_MISSING_AFTER_HYDRATION` | `current` absent while `pointer_mode is True` — prior pointer mode; legacy must not be resurrected | No |
| `HYDRATION_BASE_DRIFT` | the reservation's recorded base changed under us | No |
| `HYDRATION_COMMIT_UNAVAILABLE` | the pinned `publication_commit` is absent locally and from the remote | Maybe (remote) |
| `HYDRATION_TREE_MISMATCH` | the commit's tree != the recorded `publication_tree` | No |
| `HYDRATION_REPO_MISMATCH` | hydrate-time repository identity check failed | No |
| `HYDRATION_SOURCE_MISMATCH` / `HYDRATION_ARTIFACT_MISMATCH` | hydrated bytes != the recorded digests | No |
| `HYDRATION_UNSAFE_ENTRY` | traversal / symlink / submodule / non-blob / bad mode, with a `reason` | No |
| `HYDRATION_UNSUPPORTED_LFS` | a blob matches the canonical Git-LFS pointer grammar | No |
| `HYDRATION_STAGING_UNAVAILABLE` | staging exists and is not ours, or is a symlink | No |
| `HYDRATION_RECORD_INVALID` | a persisted hydration record violates its per-kind field contract (D28) | No |
| `HYDRATION_RECOVERY_REQUIRED` | a foreign operation's op dir is the current pointer target; that operation must finish first | **Yes — by the owning operation's same-seq resume only** |
| `HYDRATION_STATE_UNPERSISTED` | **post-swap**: the workspace is current and valid, but `READY` / `pointer_mode` was not written | **Yes — same-operation re-drive/resume only** |

`HYDRATION_STATE_UNPERSISTED`, precisely: an arbitrary new operation does **not** fix it; the same reserved seq by the same principal follows the existing F6 re-drive; the pointer and the promoted workspace remain intact; the resume persists `pointer_mode = True` + `READY` and continues; no re-materialization, no rollback. Copy must say nothing changed and the operation completes on the next same-operation attempt.

All 23 codes need `ERROR_MESSAGES` entries obeying §11.

---

## 16. Test matrix

### A. Pointer resolver and `pointer_mode`
| # | Case | Assertion |
|---|---|---|
| 1 | never-hydrated project, `current` absent, `pointer_mode` absent/`False` | legacy path returned; R1 behavior unchanged |
| 2 | rev-7 reaches `READY` | `deployment["pointer_mode"] is True` durably persisted (A) |
| 3 | rev-8 starts and replaces `hydration` with its own `RESERVED` | `pointer_mode` remains `True` while `hydration` shows rev-8 (B) |
| 4 | during rev-8, `current` is deleted | **fail closed** with `WORKSPACE_POINTER_MISSING_AFTER_HYDRATION`; the legacy workspace is **never returned or read** (C) |
| 5 | steady state: `current` absent, `pointer_mode is True` | fail closed |
| 6 | `current` malformed / 40-hex / empty / multi-line / symlink / missing op dir / symlinked op dir | fail closed with `WORKSPACE_POINTER_INVALID`; legacy never returned |
| 7 | `current` removed after `READY` while a **tampered legacy root** exists | the tampered legacy root is **never returned, read, or touched** |
| 8 | D16 crash: `state == VERIFIED`, `current` already swapped | resolves to rule 3; resume case (a) proceeds — no false fail-closed |
| 9 | `state` in {`RESERVED`, `FETCHED`, `VERIFIED`} with `current` absent | rule 1 — legacy still correct (swap not committed); resume case (b) proceeds |
| 10 | **D16 with `pointer_mode`:** pointer swapped, `READY`/`pointer_mode` save fails | resume persists **`pointer_mode = True` + `READY`** with **no re-materialization, no rollback, no delete** (E) |
| 11 | `pointer_mode` across later revisions | no later revision, hydration, or sweep path resets it to `False` or removes it (F, part 1) |
| 12 | `pointer_mode` across a load + `_migrate_release_identity` round-trip | preserved unchanged (F, part 2) |

### B. State machine, crash, foreign records
| # | Case | Assertion |
|---|---|---|
| 13 | **DRAFT exact stage sequence** | `RESERVED → FETCHED → VERIFIED → READY` recorded in order; **zero** Git subprocesses, zero network |
| 14 | LIVE exact stage sequence | `RESERVED → FETCHED → VERIFIED → READY` in the same enum and order |
| 15 | **DRAFT persisted record schema** | `commit`, `repo`, `branch`, `expected_tree`, `tested_commit_corroborated` are all exactly `None`; `fetched_locally is True`; no dummy SHA / fake repo / placeholder tree; survives a `to_dict`/`from_dict` round-trip; the four-stage traversal is unchanged |
| 16 | crash after `FETCHED`, before `VERIFIED` | record is `FETCHED`; `current` unchanged; current workspace fingerprint unchanged; next `apply()` discards staging and completes |
| 17 | crash after `VERIFIED`, before swap — case (b) | staging passes both digests but `current` names the previous token, so the partial hydration is **unusable**; resume re-verifies both digests, completes the swap, persists `pointer_mode` + `READY`; the previous workspace disappears only after the swap |
| 18 | post-swap save lost — case (a) | pointer still names the new token; workspace **not** deleted; no rollback; lifecycle still `REVISION_REQUESTED`; same-principal `reserve(seq)` returns `redriven: True`; `apply()` persists `pointer_mode = True` + `READY` and proceeds with **no double `source_revision` bump** |
| 19 | the two cases are not confused | (a) performs no re-materialization; (b) does not treat staging as already current |
| 20 | stale staging | `.ops/rev-99/` from an abandoned op is swept; the pointer target is never swept; `.ops/` bounded after success |
| 21 | foreign non-`READY` + `current` points at it | no sweep, no delete, no new hydration over it; new operation holds with `HYDRATION_RECOVERY_REQUIRED`; current workspace intact |
| 22 | foreign non-`READY` + `current` does not point at it | safely swept after containment/symlink checks |
| 23 | **foreign `READY` (rev-7) then rev-8** | no `HYDRATION_RECOVERY_REQUIRED`; rev-7 stays current throughout rev-8's `RESERVED`/`FETCHED`/`VERIFIED`; rev-8's pointer swap occurs **only** at the `READY` boundary |
| 24 | old current op dir vs. sweep timing | rev-7 is **not** swept before rev-8's pointer swap and before rev-8's `READY` is durably persisted |
| 25 | per-kind record validation | a LIVE record with a `None` identity field, or a DRAFT record with a populated one, is rejected with `HYDRATION_RECORD_INVALID` rather than coerced |

### C. LIVE identity
| # | Case | Assertion |
|---|---|---|
| 26 | healthy local chain | all six §4 steps established; `fetched_locally is True`; `tested_commit_corroborated is True` |
| 27 | `tested_commit` tree != `tested_tree` | refused |
| 28 | locally-absent `tested_commit` | `tested_commit_corroborated is False`; still fully verified by digest |
| 29 | locally-absent `publication_commit` | pinned fetch for **exactly** `publication_commit`; `fetched_locally is False` |
| 30 | never materialize `tested_commit` as `publication_commit` | delete the object locally **and** its whole remote ref → `HYDRATION_COMMIT_UNAVAILABLE`; no fallback to `tested_commit` |
| 31 | wrong repository identity | `publication_repo` != the repo derived from the configured `source_repo_url` → refusal **before any fetch** |
| 32 | branch HEAD newer than the recorded commit | a foreign commit is pushed onto the friendly branch; hydration still yields the recorded commit's bytes; the `current` pointer holds the **op token, not a commit id** |
| 33 | remote commit missing | absent locally and remotely → `HYDRATION_COMMIT_UNAVAILABLE`; pointer untouched; project not damaged |
| 34 | missing local workspace | the whole `<project_id>` tree is deleted; a LIVE revision still hydrates and `source_fingerprint == last_live_release.source_sha256` |
| 35 | pinned fetch then hydration failure | the exact-pinned `git fetch` **is** observed for the recorded commit (never HEAD), and there are **zero** FRONTEND / build / QA / preview / Vercel / provider effects and no `source_revision` bump — the two are explicitly distinguished |

### D. Blob, LFS, and safety
| # | Case | Assertion |
|---|---|---|
| 36 | canonical LFS pointer blob whose bytes **would** match the recorded `source_sha256` | `HYDRATION_UNSUPPORTED_LFS`; pointer untouched; **bytes are never accepted merely because the digest matches** |
| 37 | LFS filter non-execution | `filter.lfs.smudge=<sentinel script>` in the bare repo's local config, a pointer blob + `.gitattributes` committed; the sentinel **never ran**, no clean/smudge ran, no LFS object was fetched |
| 38 | ordinary text mentioning "LFS" | **not** rejected; hydrates normally |
| 39 | **non-canonical, LFS-like bytes** (malformed or non-standard encoding) | treated as **ordinary committed bytes** and hydrated normally — matching the stated D29 boundary. The test asserts the *narrow* contract: no claim of general LFS detection, and **no assertion that the digest catches it** |
| 40 | submodule / symlink / non-blob / bad mode | `160000`, `120000`, a non-blob type, `120755` each fail closed with the specific `reason` and the offending path named; none silently skipped |
| 41 | tree/path safety | `..`, `.git`, a NUL-bearing name, a leading `/` refused; containment re-checked per entry (make `staging` a symlink mid-operation and assert refusal) |
| 42 | no hooks executed | `core.hooksPath` set in the bare repo's local config to a sentinel directory; no sentinel fires |
| 43 | no worktree/filter materialization | recorded argv contains **no** `checkout`, `checkout-index`, `archive`, `tar`; the workspace is byte-equal to the commit's blobs |
| 44 | inert instruction files | a committed hostile `AGENTS.md` / `CLAUDE.md` hydrates as inert bytes and changes no decision |
| 45 | no leftover mutable-workspace reconstruction | a tampered workspace marker absent from the snapshot is **absent** from the hydrated workspace |

### E. Admission and migration
| # | Case | Assertion |
|---|---|---|
| 46 | one test per §3 verdict | including the ordering proof that `sync_status: SOURCE_SYNC_REQUIRED` yields `SOURCE_SYNC_REQUIRED`, **not** `LEGACY_RELEASE_IDENTITY` |
| 47 | `SYNCED` and absent sync evidence | both fall to `LEGACY_RELEASE_IDENTITY`; absence is unknown, never `False` |
| 48 | R1 project later earning a genuine COMPLETE R2 release | reads `READY` — proving both the branch gate and the `commit_release` pop |
| 49 | Batch B record shape | `set(record) == set(RELEASE_FIELDS)` still holds for a COMPLETE record |
| 50 | refusal leaves no trace | the state file is byte-identical before and after a non-admitted LIVE `reserve()` |
| 51 | **legacy evidence lifecycle** | `legacy_source_sync` **retained** on a failed/refused `commit_release`; removed **only** by a successful COMPLETE R2 COMMITTED save |
| 52 | `legacy_source_sync` writes | never written for an empty/absent `sync_status`; never overwritten; migration never touches `pointer_mode` |

### F. Reservation, lifecycle, seam, layout
| # | Case | Assertion |
|---|---|---|
| 53 | base frozen at reservation | F6 re-drive returns the reservation unchanged and does not recompute `base` |
| 54 | concurrent same-project reservation | same seq, different principals → second refused `OUT_OF_ORDER_REVISION`; first's `base` byte-unchanged; a different project interleaved at every step is unaffected |
| 55 | base must not drift | after `reserve()`, mutate `last_live_release`, publish a newer release, swap `tested_snapshot`; `apply()` uses the reserved base and fails closed if the reserved commit is no longer obtainable |
| 56 | hydration failure → FAILED | digest mismatch injected; no `source_revision` bump, no FRONTEND/QA/preview invocation, lifecycle `FAILED`, reservation still unapplied with its frozen `base` intact, same-seq re-reservation refused |
| 57 | persisted classification — `HYDRATION_SOURCE_MISMATCH` | `state.failure.error_code` == returned `RevisionResult.error_code` |
| 58 | persisted classification — `WORKSPACE_POINTER_INVALID` | same agreement, reached through the resolver refusal path |
| 59 | backward compatibility — one existing non-hydration revision failure | `state.failure.error_code` populated and equal to the returned code; lifecycle semantics unchanged; callers not inspecting it unaffected |
| 60 | `HYDRATION_STATE_UNPERSISTED` retry semantics | same-operation F6 re-drive of the same seq + principal **succeeds**; an **arbitrary replacement operation does not**; pointer and promoted workspace intact; no re-materialization, no rollback |
| 61 | explicit `workspace` seam | production dispatch never supplies it; with `workspace=None` hydration is mandatory and no fallback exists; a re-scan of production call sites finds no non-test `apply(..., workspace=…)` caller |
| 62 | legacy-mode layout | runtime dirs remain under `<project_id>/` when `current` is absent |
| 63 | pointer-mode layout | `.hermes/`, `.browser/`, `.runtime/` exist **inside `.ops/rev-N/`**; command containment is based on that directory |
| 64 | resolver authority | after `current=rev-N`, `run_command`, `start_background`, and `_build_project_env` all resolve `.ops/rev-N`; `WORKSPACE_ROOT` and `HERMES_HOME` equal that directory |
| 65 | no legacy root use in pointer mode | no command can use the legacy root once `current` exists |

### G. Unaffected paths
| # | Case | Assertion |
|---|---|---|
| 66 | new project unaffected | `source_revision == 0` still builds through `build.py`'s `_copy_starter` path; no hydration, no verdict, no `.ops/`, pointer absent, `pointer_mode` absent |
| 67 | draft continuation | PREVIEW_READY + revise → the FRONTEND workspace is byte-identical to the prior `tested_snapshot` **including prior draft edits**; a distinctive prior-draft marker survives; `last_live_release` is never read |

---

## 17. Risks and non-goals

1. **`requirements_version` is never incremented anywhere today** — a `RevisionState` field with no writer. Record it as required; the test asserts equality with the value at reserve time, never a literal.
2. **R1 LIVE projects are knowingly left non-revisable** (§11). A product-visible consequence, deliberately recorded.
3. **`FAILED` has no edge back to `REVISION_REQUESTED`.** A hydration failure is terminal for that `seq` — a pre-existing lifecycle property matching FRONTEND-failure behavior, not a new restriction.
4. **A missing or corrupt `current` now hard-stops a hydrated project** where R1 would have limped along on the legacy directory. That is the intended trade. Both pointer codes' copy must say the project needs operator repair.
5. **Residual: a crash after the pointer swap but before the `pointer_mode` + `READY` save, combined with loss of `current`, would still allow a legacy read.** Documented in §2 with the ordering rationale; it requires two independent faults and external filesystem deletion, since nothing in the codebase deletes `current`. Accepted, not engineered around — the alternative ordering creates an unrecoverable wedge.
6. **In pointer mode, `HERMES_HOME` becomes `<project_id>/.ops/rev-N/.hermes`.** It moves per revision rather than living at the project root. Still inside the generation workspace, never inside a Hermes profile, so Batch A's profile guards are unaffected; `EXCLUDED` covers it, so digests are unaffected. Worth stating because Batch A records the profile path verbatim in a skill.
7. **LFS detection is a bounded canonical-grammar check, not a general detector** (D29). Files outside the recognized grammar are ordinary committed bytes and may hydrate successfully even when they encode an LFS object, because the recorded digest may match those exact bytes. This is the stated contract, not a defect, and the digest is explicitly **not** a fallback LFS safety net.
8. **`HYDRATION_RECOVERY_REQUIRED` is narrow, not a framework.** If it were ever allowed to block unrelated projects or spawn its own recovery loop, it would become exactly the "speculative infrastructure" the repo's rubric rejects. In the normal flow it is unreachable: a foreign current-target op implies `REVISION_REQUESTED`, which the lifecycle gate already refuses for `seq+1`.
9. **Hydration is on the critical path of every revision** and is not free: LIVE is `ls-tree` + one `cat-file` per blob; DRAFT is a base64 decode plus writes. Both local, both inside the existing 128 MiB / 10 000-file bounds; `npm ci` already dominated.
10. **Not in scope:** LFS support; submodules (refused); a `.gitattributes` evaluator; detection beyond the implemented canonical grammar; `cat-file --batch`; a cross-restart garbage collector (the per-project sweep suffices); deleting a pre-pointer legacy tree; promoting `LEGACY_PARTIAL` to `COMPLETE`; any generic migration or recovery framework; any new public bypass flag; any larger workspace-state object.
11. **Do not touch** `last_live_release`'s shape, `ReleaseRecordError` semantics, or the stage graph. `test_r2_release_identity.py:670` is a change-detector assertion by the repo's own rubric; it is correct today and this batch keeps it passing. A later batch adding a release field should convert it to an invariant, not loosen it.
12. **Batch A's unresolved risk 10 (same-user execution isolation)** is unchanged and remains a prerequisite before exposing Website Builder to untrusted users.
13. **Open questions: none.** The `dist/` mapping is settled by reading `commit()` and pinned by tests. Only the wording of the 23 user-facing messages remains, constrained by §11.

---

## 18. Ready to implement

- [ ] D27: `deployment["pointer_mode"]` monotonic boolean, set with the first successful swap, never reset, never inferred backward, untouched by migration
- [ ] §6 resolver: rule 1 (absent + not `pointer_mode` → legacy), rule 2 (absent + `pointer_mode` → fail closed), rule 3 (valid token), rule 4 (malformed → fail closed). No other legacy fallback
- [ ] §8 READY order: swap → persist `pointer_mode = True` + `READY` → safe cleanup; §8.2 two-case resume; post-swap never deletes or rolls back
- [ ] D24 foreign `READY` supersedes without `HYDRATION_RECOVERY_REQUIRED`; old current kept until the new swap commits and the save lands
- [ ] D19 hold for a foreign current-target op; `sweep_ops` preserves the pointer target unconditionally
- [ ] One resolver used by `create_workspace`, `run_command`, `start_background`, and `WORKSPACE_ROOT` / `HERMES_HOME` binding; D26 resolve-before-mkdir
- [ ] D22/D23: DRAFT traverses the same four stages; `FETCHED` is a local no-op acquisition
- [ ] D28: DRAFT records `None` for all LIVE-only identity fields; per-kind validators enforce both directions; round-trip safe
- [ ] §4 six-step chain, `tested_commit_corroborated` recorded, no `tested_commit`-for-`publication_commit` fallback
- [ ] §9 blob-exact materialization; canonical-grammar LFS refusal; no `checkout`, `checkout-index`, `archive`, `tar`, filter, or hook
- [ ] §12.1 base frozen after admission and before mutation; F6 re-drive never recomputes
- [ ] §12.2 one hydration-failure lifecycle (`FAILED`, no bump, no FRONTEND/QA/preview/provider effect), D16 exception stays `REVISION_REQUESTED`
- [ ] Failure copy permits a pinned Git fetch but claims no application/provider remote effect
- [ ] §12.4 `_fail()` persists `error_code`; returned and persisted values agree; no call-site changes needed
- [ ] D20 seam documented; production call sites re-verified; no new bypass flag
- [ ] All 67 test rows implemented; every new guard mutation-checked red
- [ ] Full suite green; nothing committed, pushed, or deployed
