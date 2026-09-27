# Batch B — Final Focused Review (R2 release identity + Git reconciliation)

Review only. No code changed, no commit/push/deploy.
Reviewed: `website-builder/app/{core/state.py,deploy/git_output.py,projects/promote.py,projects/release.py,runtime.py}` + 4 test files.

**No test run was possible** — the sandbox permission layer blocked `python -m pytest`, so every finding below is static analysis. Batch B is **not** ready to commit: 3 HIGH and 4 MEDIUM findings remain.

---

## HIGH

### H1 — `OperationResult.fail(..., data=...)` raises `TypeError` in both fail-closed input branches

`git_output.py:465-477` (`reconcile_publication_head`):

```python
return OperationResult.fail(
    'Invalid intended publication commit',
    error_code='PUBLICATION_HEAD_CONFLICT',
    data={'verdict': 'C_CONFLICT'},
)
```

`OperationResult.fail` (`app/core/contracts.py:45-56`) is
`fail(cls, error, error_code=None, retryable=False)` — there is **no `data` parameter**.
Both calls raise `TypeError` at runtime.

The module's own `_verdict` helper (`git_output.py:38-50`) documents this exact
constraint ("`OperationResult.fail` has no `data` parameter, so the result is
constructed directly rather than losing the verdict on the way") and is used
everywhere else. These two call sites bypass it.

Consequence: a branch that is supposed to **fail closed** instead raises. The
`TypeError` escapes `reconcile_publication_head` → `_reconcile_head` →
`_push_git`/`_resume_git` → `_confirm_git` → `_promote_authorized` uncaught, so no
`mark_terminal_failure` / `mark_reconciliation_required` is written: the pending
record is stranded at `PREPARED` with `publication.status == "PENDING"`, no error
code, and a lifecycle that the dispatcher decides rather than the state machine.

**Fix:** route both through `_verdict('C_CONFLICT', ERROR_HEAD_CONFLICT)`.
Add a direct unit test calling `reconcile_publication_head` with a non-40-hex
`intended_commit` and with a non-40-hex `intended_parent` (the orchestrator never
produces either, which is exactly why this survived).

### H2 — Crash between `SMOKE_PASSED` and `COMMITTED` is permanently unrecoverable

`_post_promote` step 1 (`promote.py`, after the resume-reconcile blocks) is
unconditional:

```python
self.releases.ensure_stage(project_id, operation_id,
                           release_contract.STAGE_PRODUCTION_CONFIRMED, production={...})
```

`ReleaseCoordinator.ensure_stage` (`release.py:669-672`) refuses a record ahead of
the target:

```python
elif current > target:
    raise ReleaseStageError("...cannot re-enter the earlier stage...")
```

A crash after the `SMOKE_PASSED` write and before `commit_release` therefore leaves
a record that **can never commit**: every resume re-enters `_post_promote`, raises,
and calls `_fail(..., "PROMOTION_STAGE_LOST")`. `_fail` writes
`state.failure = {"phase": "promotion", ...}` (`promote.py:1828-1833`), so `is_resume`
stays true and every operator retry fails identically — an infinite recovery loop
with no forward path.

State at that point lies in the exact way the batch exists to prevent: the branch
holds release B, Vercel production serves B, and `last_live_release` still names A.

Violates: *"crash at any point — preserved"*, *"same-operation resume must remain
possible"*, and the plan's failure table (there is no row for this).

**Not covered by any test.** `test_ensure_stage_still_refuses_a_regression`
(`test_r2_release_identity.py:691-697`) locks in the half of the behaviour that
causes the hole.

**Fix:** in `_post_promote`, only call `ensure_stage(PRODUCTION_CONFIRMED)` when the
record is behind it (or make `ensure_stage` idempotent for a *re-entry into an
already-completed* stage pair). Add a fault-injection test that drops the
`COMMITTED` write (or seeds a record at `SMOKE_PASSED` with a terminal outcome),
runs `resume_publish`, and asserts the release commits with **no** second promote
POST and **no** second push.

### H3 — `publication_configured` is **not** persisted: the flagged defect is real

`build_last_live_release` (`release.py:538-560`) computes `configured` locally,
passes it to the validator as an argument, and never stores it:

```python
configured = publication.get("configured") is True
record = { ... }                       # no publication_configured key
return validate_last_live_release(record, MODE_NEW_RELEASE,
                                  publication_configured=configured)
```

- `RELEASE_FIELDS` (`release.py:126-134`) omits it.
- `legacy_live_release` omits it.
- `test_build_last_live_release_reads_the_publication_it_commits`
  (`test_r2_release_identity.py:558`) asserts `set(record) == set(RELEASE_FIELDS)` —
  the test **locks the absence in**.
- The docstring forbids it outright (`release.py:281-290`): *"It is a validation
  input, never a stored field: the release record itself must not be able to
  declare itself unconfigured in order to pass."*

That is the inverse of the accepted contract. Read-side validation
(`release.py:350-358`) then infers configured/not-configured from **exactly** the
prohibited signal:

```python
if publication_configured is None:
    if record.get("publication_commit") is None:
        raise ReleaseRecordError("A complete release with no publication commit "
                                 "needs the operation's publication_configured fact")
    publication_configured = True
```

**Observed effect:** a correctly committed, fully `COMPLETE`, `NOT_CONFIGURED`
release cannot be read back. `release_is_known(record)` returns `False` for it.
That is the module's one helper whose entire job is answering exactly this
question, and `test_a_not_configured_release_is_complete_with_null_git_identity`
(`test_r2_release_identity.py:518-527`) asserts `assert not release_is_known(record)`
— a test that passes **vacuously against the contract** by encoding the defect.
`test_a_null_publication_commit_without_the_operation_fact_is_refused` (line 530)
encodes it a second time.

**Fix (keeps the tamper-resistance the docstring is protecting):**

1. Add `publication_configured` to `RELEASE_FIELDS`, to `build_last_live_release`,
   and to `legacy_live_release` (legacy: derive from whether R1's
   `repository.publication_commit` was present, and it must be a real `bool`).
2. In `commit_release`, keep deriving `configured` from the **on-disk pending
   record inside the writer lock** (it already does, `release.py:781-787`) and
   additionally assert `record["publication_configured"] == configured`. The
   persisted flag is then corroborated, never self-declared.
3. In `validate_last_live_release`, when `publication_configured` is `None`, read it
   from `record["publication_configured"]` and require it to be a `bool`; refuse a
   record that lacks it entirely. Only the legacy call path may still fall back to
   the commit-presence heuristic — and it should say so explicitly.
4. Invert the two tests: `release_is_known(_not_configured_release())` must be
   `True`, and read-side validation of that record must succeed.
5. `RELEASE_FIELDS` is also the shape contract in
   `test_persisted_release_record_carries_no_credentials` — add the key there.

---

## MEDIUM

### M1 — Unparseable `ls-remote` output collapses into `ABSENT`, not `D_UNREADABLE`

`_remote_head` (`git_output.py:307-321`): zero exit, then a loop that `continue`s
past every line it cannot parse, falling through to
`return ('ABSENT', None)`.

A remote that answers zero-exit with garbage (proxy, wrapper, corrupted transport)
is therefore classified as *branch absent*:
- with an `intended_parent` set → `C_CONFLICT` / `PUBLICATION_HEAD_CONFLICT`
  (claims "we read it and it is wrong");
- with no parent → `B_RETRY` → **a push at a branch whose state is unknown**.

The plan's locked table requires `D_UNAVAILABLE` / `PUBLICATION_HEAD_UNREADABLE`
for unparseable output, and explicitly forbids C/D collapsing — the exact class
`_remote_head` was split to fix.

The new D test is **vacuous with respect to this path**:
`test_case_d_is_unreadable_not_conflicting` (`test_r2_publication_reconciliation.py:475`)
fakes D by overriding `reconcile_publication_head` and returning
`self._verdict("D_UNAVAILABLE", ...)` directly. `_remote_head`'s own classification
is never exercised.

**Fix:** in `_remote_head`, distinguish "no line at all" (genuine ABSENT) from
"lines present but none parseable as a ref for this branch" (UNREADABLE). Add a
test that drives real `ls-remote` with garbage stdout through the unmocked
`reconcile_publication_head` and asserts `PUBLICATION_HEAD_UNREADABLE`.

### M2 — Ambiguous same-operation resume does not hold the pending publication; supersede guard bypassed

`promote.py:829-838` — the ambiguous `reconcile_production_deployment` branch inside
`if is_same_operation:` — calls `_fail_reconciliation_required(...)` and returns
**without** `_mark_publication_reconciliation_required(...)`.

The parallel ambiguous path at `promote.py:925-927` does mark it.
`_fail_reconciliation_required_locked` (`promote.py:1855-1870`) never touches
`pending_publication` at all.

So on this path `pending_publication.reconciliation_required` stays `False`,
`ReleaseCoordinator.is_reconciliation_required()` returns `False`, and the
supersede guard at `promote.py:500` lets a **new** operation start on top of a
publication the contract says must be held open.

Violates: *"new operations cannot supersede a reconciliation-required
publication"*. `test_a_held_publication_refuses_a_new_operation` only covers the
case-C Git path, not this Vercel-ambiguity path.

**Fix:** call `self._mark_publication_reconciliation_required(project_id, operation_id,
"PROMOTION_RECONCILIATION_REQUIRED")` before `_fail_reconciliation_required` in the
resume branch. Add a test: same-operation resume whose Vercel reconcile is ambiguous
→ assert `pending.reconciliation_required is True` and that a new operation is
refused with `PUBLICATION_SUPERSEDE_FORBIDDEN`.

### M3 — Resume at `GIT_CONFIRMED`+ can push, contradicting the locked resume table

`_resume_git` treats any successful non-`A_ADOPT` verdict as `B_RETRY` and issues a
second `push_prepared_publication`.

The plan's resume table is **A / C / D only** — *"`remote_head` is any other valid
40-hex → `C_CONFLICT` → fail closed `PUBLICATION_HEAD_CONFLICT`"* — and T4 repeats
it: *"`== intended_commit` → continue; other valid head → `C_CONFLICT`; unreadable
→ `D_UNAVAILABLE`"* (no push). Today a remote head rewound to `intended_parent`
(operator force-reset, reverted push) is silently **re-published** instead of being
reported as a conflict.

`test_case_matrix_from_a_resume_at_git_confirmed` parametrizes
`("parent", None, 1)` — one push — locking in the deviation.

**Needs a decision before the fix:** the matrix reading (current) or the
resume-table reading (plan). Recommend the resume-table reading: a record already
at `GIT_CONFIRMED`+ asserts the push already landed, so a head that is *not* the
intended commit is by definition unexpected, and re-pushing here re-establishes a
publication the record already owns. If the matrix reading is kept, it must be an
explicit, documented deviation and the plan must be amended.

### M4 — Missing recovery test: push succeeded, `GIT_CONFIRMED` write lost

The scaffolding exists and is **unused** (verified by grep — zero call sites):

- `_DropStageStore` — `test_r1_live_publish_finishing.py:180-199`
- `Live(..., store_factory=...)` — `test_r1_live_publish_finishing.py:264-266`

This is the plan's test #2 and the only path that exercises `A_ADOPT` reached from
a **rejected push at `PREPARED`** (record still `PENDING`, remote already holds
`intended_commit`). `test_retry_after_a_publication_failure_replays_the_same_commit`
covers the *nothing-landed* retry, not this one.

**Fix:** use `_DropStageStore` with `drop_stage="GIT_CONFIRMED"`, publish, assert
`len(push_argvs()) == 2` (the original + the rejected retry), exactly one
`ls-remote`, branch still at `rev-list --count == 1`, and a `COMPLETE`
`last_live_release` whose `publication_commit` equals the remote head.

---

## LOW

- **L1 — vacuous force-check.** `test_case_c_leaves_the_remote_untouched_and_holds_publishing`
  (`test_r2_publication_reconciliation.py:462-465`) asserts
  `push_argvs() == []` and then loops over `push_argvs()` checking `--force` / `+`.
  The loop body never executes. The "never force" requirement is unasserted there
  (covered only incidentally by the empty list).
- **L2 — tautological assertion.** `test_r2_publication_reconciliation.py:611`:
  `assert release["source_sha256"] == release["source_sha256"]`. The line's stated
  intent ("everything the operation actually did is present") is unverified.
- **L3 — test does not test its name.** `test_accepted_push_persists_git_confirmed_without_a_round_trip`
  (`test_r2_publication_reconciliation.py:276-286`) reads post-`COMMITTED` state,
  where `pending_publication` is already cleared. The zero-read half is real (also
  covered by `test_happy_path_performs_zero_remote_reads`); the "persists
  GIT_CONFIRMED" half is not observed. Use `_StageRecordingStore.stages` (already
  available) to assert `GIT_CONFIRMED` appears between `PREPARED` and
  `PRODUCTION_CONFIRMED`.
- **L4 — mislabelled local validation as C.** `git_output.py:465-477` map a
  corrupt local `intended_commit`/`intended_parent` to `C_CONFLICT`. C is defined as
  "the remote was read successfully and holds a state we did not intend". An
  operator triaging `PUBLICATION_HEAD_CONFLICT` will inspect GitHub instead of
  local state. (Same lines as H1 — fix both together.)
- **L5 — incomplete retirement.** `_migrate_release_identity` (`state.py:70-72`)
  returns early for an already-migrated record, so `data["repository"] = {}` is
  never reached for it. Inert today (nothing reads it), but the retirement is
  partial.
- **L6 — unclassified escape after a successful push.** `push_prepared_publication`
  runs `self._run(['update-ref', ...])` (`git_output.py:438`) outside any `try`. A
  local write failure there propagates unhandled, leaving the remote published and
  the record at `PREPARED`. Recoverable via `A_ADOPT` on resume, but unclassified.
- **L7 — dead parameter.** `_confirm_git(..., recovery: bool = False)` accepts
  `recovery` and never reads it.
- **L8 — `commit_release` does not refuse `PENDING`.** It refuses
  `reconciliation_required` and `publication.status == FAILED`; a hand-seeded
  `SMOKE_PASSED` record with `status == "PENDING"` would commit. Unreachable
  through the orchestrator.

---

## Contract checklist

| Contract item | Verdict |
|---|---|
| One linear stage machine (5 stages, no skips/regressions) | ✅ `STAGE_ORDER`, `assert_stage_advances` |
| Git publication before production | ✅ `_confirm_git` at `promote.py:782`, before any Vercel call |
| Zero remote reads on the ordinary successful push path | ✅ push → `confirm_publication`, no `ls-remote`; asserted by `test_happy_path_performs_zero_remote_reads` |
| Strict A/B/C/D reconciliation | ⚠️ matrix present, but see M1 (unparseable → ABSENT) and M3 (resume B_RETRY pushes) |
| C → `PUBLICATION_HEAD_CONFLICT` | ✅ except H1 (TypeError) and M1 |
| D → `PUBLICATION_HEAD_UNREADABLE` | ⚠️ M1; D test is vacuous |
| `last_live_release` authoritative | ✅ `commit_release` is the single write |
| `last_live_deployment` derived | ✅ `release.py:833-842` |
| `state.repository` retired | ✅ no production reads; ⚠️ L5 partial |
| `LEGACY_PARTIAL` never fabricated into `COMPLETE` | ✅ |
| Resume/recovery never touches Vercel before Git reconciliation is safe | ✅ `_confirm_git` runs before the promote/reconcile block |
| New operations cannot supersede a reconciliation-required publication | ⚠️ M2 bypass |
| Same-operation resume remains possible | ❌ **H2** |
| `NOT_CONFIGURED` passes through `GIT_CONFIRMED` with zero Git activity | ✅ implemented + tested |
| No second Git publication path | ✅ `publish_project_branch` / `_publish_live_source` / `_record_source_sync*` all gone |

## Order of work

1. H1 (one-liner + 2 unit tests)
2. H3 (5 edits + invert 2 tests)
3. M1 (`_remote_head` split + real `ls-remote` garbage test)
4. M2 (one call + one test)
5. H2 (`_post_promote` re-entry + fault-injection test)
6. Decide M3, then fix or amend the plan
7. Add the M4 test using the existing `_DropStageStore`
8. LOW items L1-L3 (test-quality), L4-L8 as time allows

## Validation (must all pass before commit)

```
scripts/run_tests.sh website-builder/tests/test_r2_release_identity.py -q
scripts/run_tests.sh website-builder/tests/test_r2_publication_reconciliation.py -q
scripts/run_tests.sh website-builder/tests/test_r1_live_publish_finishing.py website-builder/tests/test_promote.py website-builder/tests/test_promote_parity.py website-builder/tests/test_publish_dedup.py website-builder/tests/test_r1_smoke_failure_recovery.py -q
scripts/run_tests.sh website-builder/tests/ -q
```

No test baseline was captured in this session — the sandbox blocked `python -m pytest`.
H1 in particular means the suite cannot currently be trusted to be green.

## Out of scope

Hydration, design-stack changes, no commit/push/deploy.
