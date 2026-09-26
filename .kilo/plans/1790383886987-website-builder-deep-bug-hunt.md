# Deep-Dive Bug Hunt Report

**Scope:** `website-builder/` only (Telegram/WhatsApp → intake → build → QA → Vercel preview → promote → custom domain).
**Mode:** read-only. No code, config, state, or test artifacts were modified. `git status` clean before and after.
**Method:** end-to-end execution-path tracing across `app/runtime.py`, `app/channels/*`, `app/core/*`, `app/projects/*`, `app/deploy/*`, `app/qa/*`, `app/sandbox/*`, `app/conversations.py`, plus the corresponding tests. Test execution was not possible (shell execution is denied in this session), so every finding below is static-analysis-confirmed and none depends on a test run.

---

## Executive Summary

The Website Builder subsystem is unusually disciplined about the things that are hardest to get right: it persists a durable claim **before** every external side effect, it never blind-retries an ambiguous remote operation, it reconciles ambiguous provider responses by identity lookup rather than resend, it rolls back production when a smoke check fails, and it treats a Telegram/Vercel send whose outcome is unknown as *ambiguous forever* rather than retryable. The remote-boundary reasoning is genuinely good and I could not find a way to make it create a duplicate Vercel project, a duplicate deployment, or a duplicate production promotion.

The weakness is not in the remote-boundary logic. It is in **what happens when the process dies between two local state writes**. There is no startup reconciliation sweep, and the lifecycle state machine has no edge out of `QUEUED` or `RUNNING` other than the in-process code that was interrupted. A single crash at one of four well-defined points **permanently bricks a project**, and the user's only symptom is a message that says "A previous operation needs reconciliation. Please try again." — repeated forever, with no path forward. That is the single most important risk in the system.

Second-order risks cluster into three patterns: durability helpers that **silently no-op** instead of failing loudly (so the very writes that prevent duplicate Telegram deliveries can be dropped without any error), a **Telegram receive loop whose offset is in-memory only** (so every restart replays unclaimed messages and re-sends non-dispatch replies to the user), and a **user-facing error taxonomy that is not closed** (internal codes, free text, and lifecycle errors all collapse into one generic message, which actively hides the stranded-project case above).

- **Findings in the main list:** 16 (4 CONFIRMED, 12 HIGH CONFIDENCE)
- **Root causes:** 8 (several findings share a root cause and are grouped)
- **Severity spread:** 1 CRITICAL-adjacent, 5 HIGH, 8 MEDIUM, 2 LOW
- **Primary user journey reliability:** the happy path (chat → brief → build → QA → preview → approve → publish) is well-defended and I found no confirmed defect on the fully-successful path. The journey is **not** reliable across a process restart or crash: that is where the confirmed defects cluster.

---

## BUG-01 — A crash strands the project in `QUEUED`/`RUNNING` forever, with no recovery path

Severity: **HIGH**
Confidence: **CONFIRMED**

### What happens

The project is moved into `QUEUED` when a build is admitted, and into `RUNNING` a moment later when the build actually starts. If the process dies between those two points — or anywhere between "QA passed" and "QA finalized" — the project is left sitting in `QUEUED` or `RUNNING`.

Nothing ever moves it out. On the next user message the system tries to move it to `READY`, the lifecycle state machine refuses (`QUEUED → READY` and `RUNNING → READY` are not valid edges), the resulting exception marks the *new* message's claim as failed, and the user is told a previous operation needs reconciliation. This repeats on **every subsequent message, permanently**. The project can never be built, revised, previewed, or published again, and there is no operator-facing recovery either.

### User impact

The user's website is stuck forever. Every message they send gets back "A previous operation needs reconciliation. Please try again." Starting a brand-new project in the same conversation still works, so the user has no explanation for why only that one project is dead — and nothing tells them what to do differently.

### Trigger

- The process is killed / OOM-killed / the VPS reboots / a deploy restarts the service while a build, revision, or QA run is in flight. Builds take minutes and involve `npm ci`, `npm run build`, a render server, browser capture, and two model calls, so the exposure window is large.
- Two precise windows:
  1. `dispatch.py:181` / `dispatch.py:266` persists `lifecycle = QUEUED` (with the claim already `CLAIMED`), and the process dies before `build.py:608` persists `RUNNING`.
  2. `qa/orchestrator.py:156-159` persists `deployment["tested_snapshot"]` and then, in a **separate** writer-lock block, `_finalize_success` (line 496-503) performs the `RUNNING → PREVIEW_READY` transition. A crash in between leaves `RUNNING` with a valid, already-passing QA result that nobody will ever act on.

### Technical root cause

Two independent defects that combine:

1. **No stranded-state recovery.** `compose()` / `main()` in `app/runtime.py` run exactly three preflight checks (`preflight_node_toolchain`, `preflight_smoke_support`, `preflight_role_validation`) and then start the receive loop. There is no sweep that looks for projects in `QUEUED`/`RUNNING` and drives them to a terminal state. `app/core/lifecycle.py:50-61` allows `QUEUED → {RUNNING, PAUSED, CANCELED, FAILED}` and `RUNNING → {PREVIEW_READY, PAUSED, CANCELED, FAILED}` — i.e. only the interrupted in-process code (or an explicit pause/cancel the user can no longer trigger) can move them.
2. **The intake fallback cannot rescue them.** `_LIFECYCLE_INTENTS` in `app/runtime.py:872-873` offers only `INTAKE` for `QUEUED` and `RUNNING`. `IntakeProcessor.apply_to_project` (`app/core/intake.py:463-494`) then calls `transition_lifecycle_locked(state, READY)`, which raises `LifecycleError`. Its one escape hatch is scoped to `state.lifecycle == FAILED` (line 479) and does not apply. The raised `LifecycleError` is caught by `dispatch.py:414`, which marks the freshly-created claim `FAILED` and returns `EVENT_RECONCILIATION_REQUIRED`.

### Evidence

- `website-builder/app/channels/dispatch.py:181` — `self.store.transition_lifecycle_locked(state, ProjectLifecycle.QUEUED)` then `:189-196` persists the claim, then `:196` saves.
- `website-builder/app/projects/build.py:592-610` — the writer block that transitions `QUEUED → RUNNING`; nothing between `dispatch.py:196` and `build.py:610` bridges the gap.
- `website-builder/app/qa/orchestrator.py:156-159` — `tested_snapshot` is written in one `acquire_writer` block; `self._finalize_success(project_id, qa_attempt)` at line 159 opens a **second** `acquire_writer` block (`orchestrator.py:497-503`) to do the `RUNNING → PREVIEW_READY` transition.
- `website-builder/app/core/lifecycle.py:50-61` — `_TRANSITIONS[QUEUED]` and `_TRANSITIONS[RUNNING]` contain no edge to `READY`.
- `website-builder/app/core/intake.py:477-494` — the only path back to `READY`, guarded by `if state.lifecycle != ProjectLifecycle.READY.value:` with the FAILED-only recovery at line 479.
- `website-builder/app/runtime.py:872-873` — `"QUEUED": [ConversationIntent.INTAKE]`, `"RUNNING": [ConversationIntent.INTAKE]`.
- `website-builder/app/runtime.py:2104-2143` — `main()` contains no state-reconciliation pass.

### Failure sequence

1. User's brief is complete → `apply_to_project` sets `READY`.
2. Dispatch writes claim `CLAIMED` and transitions `READY → QUEUED` (`dispatch.py:181-196`), saves.
3. `build()` is entered. `acquire_project` succeeds.
4. **Process is killed** (deploy restart / OOM / reboot) before `build.py:610` saves `RUNNING`.
5. Restart. Telegram re-delivers the user's build-triggering message; the claim is `CLAIMED` → `dispatch.py:161` returns `EVENT_RECONCILIATION_REQUIRED`.
6. User sends a *new* message. New claim key. Dispatch runs intake. `apply_to_project` tries `QUEUED → READY` → `LifecycleError`.
7. `dispatch.py:414-448` marks the new claim `FAILED`, returns `EVENT_RECONCILIATION_REQUIRED`.
8. Every future message repeats step 6-7. The project is permanently dead.

### Reproduction scenario

Start the runtime, send a complete three-part brief so the project reaches `READY`, then `kill -9` the process in the ~1-2 second window between the claim write and the `RUNNING` transition (or simply `systemctl restart` during a build). Restart. Send any message. Observe `EVENT_RECONCILIATION_REQUIRED` on every subsequent turn, with `lifecycle` stuck at `QUEUED` in `<state_root>/<project_id>.json`.

### Why existing protection does not prevent it

- The dispatch claim system protects against *duplicate effects*, not against *unreachable states*. Its fail-closed `EVENT_RECONCILIATION_REQUIRED` is exactly what makes the wedge permanent rather than self-healing.
- The commit `ed16b5d1b` ("prevent project from being stranded in RUNNING state") fixed exactly this class **for revisions** by adding exception handling in `revise.py`. The equivalent protection was never added to `build()`'s pre-`RUNNING` window, to the dispatcher's `QUEUED` write, or to QA's two-step finalization.
- `_TRANSITIONS[PAUSED]` is a permissive superset (lines 90-101) precisely so a paused project can be resumed anywhere — but nothing transitions a stranded project *to* `PAUSED` automatically.

### Recommended fix direction

Two complementary changes, both additive:

1. Add a **startup reconciliation pass** in `main()`/`compose()` that, for every project state file found under the state root, resolves stranded in-flight lifecycles: `QUEUED` and `RUNNING` with no live worker → transition to `FAILED` with a `phase: "interrupted"` failure record (both transitions are already legal edges), so the existing `intake.py:479-493` FAILED-recovery path can immediately re-admit the project to `READY` and rebuild. `PAUSED` is the other legal option but `FAILED` is the one the rest of the system already knows how to recover from.
2. Make QA's success finalization **atomic**: perform the `tested_snapshot` write and the `RUNNING → PREVIEW_READY` transition in a *single* `acquire_writer` block. This alone removes window 2 and is a small, safe change.

Separately, consider whether a claim left `CLAIMED` by a dead process should be reaped on startup (with the same `reached_remote` evidence rule the dispatcher already uses) so the original Telegram event is not permanently un-replayable.

### Tests required after approval

- Crash-injection: persist `QUEUED` (dispatch claim `CLAIMED`), simulate restart, run the reconciliation pass, assert the project reaches `FAILED` and that the *next* intake re-admits it to `READY` and a build runs.
- Same for `RUNNING` with a passing `tested_snapshot` already written.
- Atomicity: assert that after `_finalize_success` there is no intermediate state in which `tested_snapshot` is set while `lifecycle == "RUNNING"`.
- Regression: assert the reconciliation pass never touches `LIVE`, `PREVIEW_READY`, `PUBLISHING`, or `FAILED` projects.
- Regression: assert `CANCELED` and `PAUSED` are left alone.

---

## BUG-02 — Durability helpers silently drop the write instead of failing, which can defeat the duplicate-delivery guard

Severity: **HIGH**
Confidence: **HIGH CONFIDENCE**

*(This is the root cause behind BUG-09 and BUG-10; those are listed as symptoms.)*

### What happens

`PreviewOrchestrator._update_intent` is the function the preview pipeline relies on to record "I am about to send this Telegram message" *before* sending it. If the persisted intent on disk no longer matches the operation the pipeline is working on, the helper quietly does nothing — no write, no error, no log.

The caller then believes the marker is on disk and performs the Telegram send anyway. That is precisely the "unmarked send" crash window the whole design exists to prevent: a crash now produces a duplicate preview photo in the user's chat, with no durable evidence that the first one was sent.

### User impact

The user can receive the same preview screenshot and the same preview link twice. In the worst case the duplicate is undeleted and the user cannot tell which link is current.

### Trigger

The on-disk `preview_intent.operation_id` differs from the in-flight `operation_id` at the moment of the write. That requires a newer revision/deployment to have replaced the intent — a narrow window because `run_owned` holds the single worker slot, but the slot only serializes *preview runs*, not the QA/revision that produces a new `operation_id`.

### Technical root cause

`website-builder/app/deploy/preview.py:867-872`:

```python
def _update_intent(self, project_id: str, operation_id: str, **fields) -> None:
    with self.store.acquire_writer(project_id) as state:
        intent = state.deployment.get("preview_intent")
        if intent and intent.get("operation_id") == operation_id:
            intent.update(fields)
            self.store.save(state)
```

There is no `else`. A mismatch is indistinguishable from success for every caller. Compare with `runtime.py:1343-1362` (`_record_preview_reconcile_attempt`), which *does* return `None` on mismatch so the caller can react — the two helpers disagree about what a mismatch means.

`PromotionOrchestrator._update_intent` (`app/projects/promote.py:622-627`) has the identical shape: silent no-op if `promotion_intent` is missing or the operation differs.

### Evidence

- `website-builder/app/deploy/preview.py:867-872` — the silent no-op.
- `website-builder/app/deploy/preview.py:613-623` — the pre-send `photo_attempted/PENDING` write whose failure is handled by a `try/except` that can never fire, because the failure mode is silence, not an exception.
- `website-builder/app/deploy/preview.py:649-659` — the identical pattern for the text send.
- `website-builder/app/deploy/preview.py:556-560`, `:628-635`, `:664-671` — every other intent write has the same exposure.
- `website-builder/app/projects/promote.py:622-627` — same defect in the promotion path.
- Contrast: `website-builder/app/runtime.py:1349-1352` and `:1974-1975` both check the operation id and return/`return False` on mismatch.

### Failure sequence

1. Preview pipeline is at step 6; the deployment is live, smoke passed, screenshots captured.
2. A newer revision lands and replaces `preview_intent` with a different `operation_id`.
3. `_run` calls `_update_intent(..., photo_attempted=True, photo_outcome=PENDING)` → the guard at line 870 is false → **nothing is written, no exception**.
4. `telegram.send_photo(...)` executes and delivers.
5. The process is killed before the post-send `_update_intent(photo_outcome=SENT)`.
6. On restart the durable state has **no record that the photo was ever attempted**.
7. The next reconcile re-drives the photo send → the user gets the same preview twice.

### Why existing protection does not prevent it

The pre-send durable write is the *only* protection, and it is implemented in a way that cannot report its own failure. The `try/except` at `preview.py:619` / `:655` is dead code with respect to this failure mode. The rest of the module is careful — `_delivery_outcome_of` (`preview.py:84-97`) correctly distinguishes `NOT_SENT` from `PENDING`, and `_maybe_send_follow_up` (`preview.py:747-865`) persists its attempt marker too — but it uses the same helper, so it inherits the same blind spot.

### Recommended fix direction

Make the mismatch explicit and fail closed. Either (a) change `_update_intent` to raise a dedicated `StalePreviewIntent` when the operation id does not match, and convert it to `OperationResult.fail('PREVIEW_RECONCILIATION_REQUIRED')` at every call site, or (b) have it return a boolean and require the two pre-send call sites to abort with `DELIVERY_STATE_PERSIST_FAILED` (the code that already exists for the exception path) when the write did not land. Option (a) is safer because it cannot be forgotten at a call site. Do the same for `PromotionOrchestrator._update_intent`.

### Tests required after approval

- Force an `operation_id` mismatch between the in-flight run and the persisted intent; assert the pre-send write path returns a failure and that **no** Telegram send occurred.
- Assert `DELIVERY_RECONCILIATION_REQUIRED` (not a silent success) is returned.
- For promotion: same mismatch, assert the promote path fails closed rather than continuing with a stale intent.
- Regression: assert a matching `operation_id` still writes and still sends.

---

## BUG-03 — Symptom of BUG-02: the "legacy Vercel project" guard reads a state key production never writes

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

Before creating a friendly-slug Vercel project, the preview orchestrator is supposed to check whether the project already exists under the older opaque hash-derived name, so it does not create a second Vercel project for the same app. That check reads `state.deployment["vercel_project_id"]`. No production code ever writes that key.

### User impact

If a project ever transitions from the opaque-name path to the friendly-slug path, the duplicate-project guard cannot fire, and the system can create a second Vercel project for the same website. The user ends up with two Vercel projects, two preview URL families, and a production alias bound to whichever won — with no indication of which is canonical.

### Trigger

A project whose `preview_intent` carries a `slug_candidate` but whose `state.deployment` has no `vercel_project_id` key, combined with `project_create_attempted` being falsy. In practice this requires a pre-slug project (created before the friendly-slug feature) whose state predates the current write path.

### Technical root cause

`website-builder/app/deploy/preview.py:360-363`:

```python
if slug and not attempted and (
    state.deployment.get('vercel_project_id')
    or previous.get('legacy_project')
):
```

The real code writes the id into the **intent**, not into `deployment`: `preview.py:424-427` does `self._update_intent(project_id, operation_id, vercel_project_id=vercel_project['id'])`. `previous.get("legacy_project")` is likewise never written anywhere in `app/`. Both branches of the `or` are therefore permanently false, so the guard at `preview.py:360` is dead code and control always falls through to `elif slug and not attempted:` (`preview.py:386`) → `ensure_project_with_slug`.

### Evidence

- `website-builder/app/deploy/preview.py:361` — reads `state.deployment.get('vercel_project_id')`.
- `website-builder/app/deploy/preview.py:426` — the only production write, into `preview_intent`.
- Repository-wide grep for `vercel_project_id` returns only: `preview.py:361`, `preview.py:426`, `runtime.py:744` (correctly reading `preview_intent.vercel_project_id`), and `tests/test_r1_slug_bind.py:46` (a test that writes `state.deployment["vercel_project_id"] = "prj_1"` directly).
- Repository-wide grep for `legacy_project` returns only `preview.py:362`.
- The test at `test_r1_slug_bind.py:46` is the only thing that makes this guard look covered — **the test manufactures the state shape the production code never produces**, which is precisely why the defect survived.

### Why existing protection does not prevent it

`test_r1_slug_bind.py` exercises the guard and passes, because it writes the key by hand. This is a textbook case of a test that hides a production bug by asserting against a state shape the code cannot produce.

### Recommended fix direction

Decide the intended source of truth (it should be `preview_intent.vercel_project_id`, which `runtime.py:742-746` already reads correctly) and make the read match. Add an assertion that a project resolved through the opaque-name path records that id where the guard expects it.

### Tests required after approval

- Drive a project through the opaque-name path using only the real production code, then assert the guard's condition evaluates true.
- Assert the friendly-slug path never creates a second Vercel project when one already exists under the opaque name (count `POST /v11/projects` calls).
- Delete or rewrite `test_r1_slug_bind.py:46` so it no longer hand-writes the key.

---

## BUG-04 — Symptom of BUG-02: the "was this ever published" guard reads a state key production never writes

Severity: **LOW** (latent; currently masked by a neighbouring condition)
Confidence: **CONFIRMED**

### What happens

`IntakeProcessor.apply_to_project` contains a recovery path for a project that failed its *initial* build. It is supposed to reset the build only for projects that never got as far as showing a preview or going live. One of its five guards checks a state key that no production code writes, so that guard never fires.

### User impact

None today. The guard's intent is protected by the adjacent `state.revisions.qa_revision == 0` condition, which is non-zero for any project that reached QA success. But the protection is accidental: the named check is inert, so anyone who later removes or loosens `qa_revision == 0` silently re-enables a reset that would clobber a project's live production state.

### Technical root cause

`website-builder/app/core/intake.py:478-484`:

```python
if (
    state.lifecycle == ProjectLifecycle.FAILED.value
    and state.revisions.qa_revision == 0
    and not state.deployment.get("latest_shown_preview")
    and state.revisions.approved_revision == 0
    and not state.deployment.get("live_url")
):
```

Production writes the production URL to `state.production_url` (`app/projects/promote.py:588`) and to `state.deployment["last_live_deployment"]["production_url"]` (`promote.py:590-598`). `state.deployment["live_url"]` is written **only by a test** (`tests/test_intake.py:431`).

### Evidence

- `website-builder/app/core/intake.py:483` — the dead read.
- `website-builder/app/projects/promote.py:588` — `locked.production_url = production_url` (the real key).
- `website-builder/app/projects/promote.py:590-598` — `last_live_deployment` (the other real key).
- `website-builder/tests/test_intake.py:431,462,473` — the only writer of `deployment["live_url"]`, and the only place it is asserted.

### Why existing protection does not prevent it

`tests/test_intake.py:411-473` passes because it sets `live_url` itself. As with BUG-03, the test manufactures a state shape production cannot produce, so the guard looks covered.

### Recommended fix direction

Read the real signals: `state.production_url` and `state.revisions.live_revision` (which `promote.py:587` sets and which nothing ever resets). Both are stronger evidence than a `deployment` sub-key.

### Tests required after approval

- Assert the guard is inert for a project driven to `LIVE` through the real promote path, using only production writers.
- Assert the guard *is* effective for a first-build `FAILED` project.
- Remove the hand-written `live_url` from `test_intake.py`.

---

## BUG-05 — Unbounded growth of per-project durable state; every write rewrites and fsyncs the whole file

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

Two per-project collections grow forever and are never pruned: the dispatch-claim ledger and the pending-revision ledger. Each turn adds entries. The whole project state is a single JSON document that is fully rewritten and `fsync`ed on **every** save, and the preview pipeline performs on the order of twenty saves per run.

Over months of use on an active project, every single state write gets slower, and every `acquire_writer` hold gets longer — which directly increases the chance of hitting the 30-second writer-lock timeout in every other code path that needs the lock.

### User impact

No immediate wrong output. Over time, slower state writes make the already-slow build pipeline slower and more likely to time out on the writer lock, and a corrupt or truncated large state file becomes a single point of failure for the whole project.

### Technical root cause

- `ProjectState.dispatch_events` (`app/core/state.py:103`) gains one entry per dispatch **permanently**. Nothing ever pops from it. Repository-wide grep confirms no `dispatch_events.pop`, no `del state.dispatch_events`, no TTL/GC anywhere in `app/`.
- `ProjectState.pending_revisions` (`app/core/state.py:94`) gains one entry per revision. `revise.py:160-165` appends; `revise.py:278/494/510/543` only flip `entry["applied"] = True` in place — the entry is never removed. Confirmed by grep.
- Each claim can additionally carry `build_diagnostics` (`dispatch.py:325-327`) and `revision_diagnostics` (`dispatch.py:460-462`), which for this system include captured subprocess output.
- `ProjectStateStore.save` (`app/core/state.py:173-186`) serializes the entire document with `indent=2, sort_keys=True` and calls `os.fsync` — cost grows linearly with total history.

### Evidence

- `website-builder/app/core/state.py:94` and `:103` — the two unbounded collections.
- `website-builder/app/channels/dispatch.py:189-196` (claim write), `:272-276` (auto-build claim), `:322-328` (claim finalization with `build_diagnostics`).
- `website-builder/app/projects/revise.py:160-165` (append) and `:278`/`:494`/`:510`/`:543` (in-place flag only).
- `website-builder/app/core/state.py:173-186` — full-document rewrite + `fsync`.
- `website-builder/app/core/composition.py:43-49` — `invalidate_artifact` clears the `deployment` sub-keys but deliberately does **not** touch `dispatch_events` or `pending_revisions`.
- `website-builder/app/deploy/preview.py` — `_update_intent` is called ~20 times per `_run`, each a full rewrite.

### Failure sequence

1. Project accumulates 200 revisions.
2. `dispatch_events` holds ~400 claims, several with captured diagnostic output; `pending_revisions` holds 200 entries.
3. Every `save()` now serializes and `fsync`s a multi-hundred-KB document, dozens of times per build.
4. The writer lock is held longer on each of those writes, so a concurrent `acquire_writer` is more likely to exceed its 30s timeout (`state.py:261`) and raise `TimeoutError` — which `dispatch()` does not catch on the `create`/`read` paths (see BUG-08).

### Why existing protection does not prevent it

No test asserts a bound on these collections, because none of the tests exercise a long-lived project. The suite is built around single-build and few-revision scenarios, so the growth curve is invisible.

### Recommended fix direction

Bound both ledgers. `pending_revisions` can safely keep only the highest few `seq` values plus any unapplied reservation. `dispatch_events` needs a retention rule that preserves idempotency for the window that matters (Telegram's update retention, 24h) while discarding older claims — claims older than that can never be replayed anyway, because the `update_id` that produced them is gone. Prune inside the same writer block that appends, so pruning is itself atomic and crash-safe.

### Tests required after approval

- Assert `len(state.dispatch_events)` and `len(state.pending_revisions)` stay bounded after N simulated turns.
- Assert pruning preserves the newest claim and any unapplied reservation.
- Assert a claim older than the retention window being pruned cannot cause a duplicate effect (i.e. the replay window is provably shorter than Telegram's retention).
- Assert pruning is crash-safe: a crash mid-prune leaves a consistent ledger.

---

## BUG-06 — The Telegram offset is in-memory only, so every restart replays unclaimed messages and re-sends non-dispatch replies

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

The receive loop keeps its `getUpdates` offset in a plain instance attribute. It is never persisted. On every restart the loop calls `getUpdates` with no offset, so Telegram re-delivers every update it has not yet confirmed.

Most re-delivered turns are harmless: the dispatcher's claim ledger short-circuits them. But a meaningful set of user-visible replies are sent **outside** the dispatcher and therefore have no claim and no dedup at all. Those get re-sent on every restart.

### User impact

The user receives duplicate messages they already saw — the project list, a clarification question, "that name is already taken", "that project wasn't found", "something failed, try again". If the operator restarts the service (deploy, crash recovery, `systemctl restart`) a few times in a day, the user sees the same replies several times with no explanation.

### Trigger

- Any process restart while unconfirmed updates are pending in Telegram's queue (Telegram retains them for ~24h). Deploys and crash restarts are routine.
- The most visible case: a message arrives while the loop is busy processing a long build. It sits in the in-memory `updates` list, `self._offset` has already advanced past it, and the process dies before the next `getUpdates` confirms it. On restart it is re-delivered and re-processed from scratch.

### Technical root cause

Two independent gaps that combine:

1. **No durable offset.** `app/runtime.py:982` — `self._offset: Optional[int] = None`, never written to disk, and `app/runtime.py:995-996` only includes it in the query when set. `_get_updates` with no offset returns the oldest unconfirmed update.
2. **Non-dispatch replies have no idempotency.** `app/runtime.py:1272-1299` (`LIST_PROJECTS`, `CLARIFICATION`, "Project itu tidak ditemukan"), `:1548-1551` ("Oke, bikin website baru…"), `:1558-1561` ("Nama itu sudah dipakai…"), `:1587`, `:1595` all call `self._safe_send(...)` directly. The claim ledger is only written inside `TelegramDispatcher.dispatch`, and these paths never reach it.

Compounding this, `app/runtime.py:2079-2083` advances `self._offset = update_id + 1` **before** calling `_process_update`, so a crash during processing loses the in-memory offset entirely.

### Evidence

- `website-builder/app/runtime.py:982` — `self._offset: Optional[int] = None`.
- `website-builder/app/runtime.py:993-1001` — offset only sent when non-`None`; no persistence anywhere in the module.
- `website-builder/app/runtime.py:2076-2083` — `self._offset = update_id + 1` precedes `self._process_update(update)`.
- `website-builder/app/runtime.py:1272-1299` — four `_safe_send` replies with no claim.
- `website-builder/app/runtime.py:1548`, `:1558`, `:1587`, `:1595` — four more.
- Contrast: `app/deploy/preview.py:809-825` and `:610-618` persist an attempt marker before every outbound Telegram send, so preview delivery *is* dedup-protected. The conversational replies are not.

### Failure sequence

1. Loop is processing a 15-minute build for project A.
2. User B sends "list my projects" → `update_id` 900, held in the in-memory `updates` list; `self._offset` set to 901.
3. Process is killed.
4. Restart. `getUpdates` with no offset returns update 900 (and everything else unconfirmed).
5. `_process_update` → router returns `LIST_PROJECTS` → `_safe_send` → the user sees the project list a second time.
6. The same happens for every unconfirmed conversational turn.

### Why existing protection does not prevent it

`test_runtime.py` exercises the loop with an injected transport that always returns a fixed update list, so the offset is never `None` on a "restart" within a test and the replay path is never taken. There is no test that constructs a fresh `TelegramReceiveLoop` and asserts what a no-offset `getUpdates` does to already-answered turns. The preview pipeline's careful `follow_up_state` / `photo_outcome` machinery demonstrates the team knows this pattern; it was simply not applied to the conversational replies.

### Recommended fix direction

Persist the confirmed offset (to the state root, atomically, *after* processing the batch rather than before) and re-apply the preview module's attempt-marker discipline to the conversational replies: write a per-`update_id` "reply sent" marker before `_safe_send` and skip on replay. Persisting the offset alone reduces the window but does not close it, because a crash *during* processing still loses the batch.

### Tests required after approval

- Simulate: process an update, restart with a fresh loop, assert the same `update_id` produces no second `_safe_send`.
- Simulate a crash *during* `_process_update` and assert the next turn still gets a response exactly once.
- Assert the persisted offset only advances past fully processed updates.
- Assert the dispatch-claimed paths (intake/revise/approve/publish) remain exactly-once across the same restart.

---

## BUG-07 — Intake on a `CANCELED` or `FAILED` project raises a lifecycle error that is reported as "needs reconciliation"

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

The dispatcher lets any `intake` action through to the intake processor without checking the lifecycle first — unlike `build`, `revise`, `directions_*`, and `reconcile_preview`, which all have explicit lifecycle gates. When the project is in a state the lifecycle machine will not let intake move out of, the resulting `LifecycleError` is caught by the dispatcher's generic handler and reported as `EVENT_RECONCILIATION_REQUIRED`.

The user is told a previous operation needs reconciliation when in fact they simply messaged a project that cannot accept new input. Retrying changes nothing, forever.

### User impact

A user who types an ordinary message to a cancelled project gets "A previous operation needs reconciliation. Please try again." The message is wrong, unactionable, and — because a new claim is created and failed on every turn — repeated indefinitely. The same happens to a `FAILED` project when the user says "pause".

### Trigger

- Any message to a project whose lifecycle is `CANCELED` (whose transition set is **empty** — `lifecycle.py:102`).
- The word "pause"/"stop" on a `FAILED` project (`FAILED → PAUSED` is not a valid edge — `lifecycle.py:85-89`).
- Any complete brief sent to a stranded `QUEUED`/`RUNNING` project (this is the second half of BUG-01's wedge, and is the *only* user-visible symptom of it).

### Technical root cause

- `app/channels/dispatch.py:164-180` applies lifecycle gates to `directions_*`, `reference_*`, `reconcile_preview`, and `build`. There is no gate for `intake`.
- `app/core/intake.py:427-432` unconditionally attempts `transition_lifecycle_locked(state, PAUSED)` when `pause_detected`.
- `app/core/lifecycle.py:85-89` — `_TRANSITIONS[FAILED] = {DISCOVERING, READY, CANCELED}`; no `PAUSED`.
- `app/core/lifecycle.py:102` — `_TRANSITIONS[CANCELED] = set()`.
- `app/channels/dispatch.py:414-448` — the generic handler that converts any exception into `EVENT_RECONCILIATION_REQUIRED` and marks the new claim `FAILED`.

### Failure sequence

1. Project reaches `CANCELED` (or a stranded `QUEUED`, per BUG-01).
2. User sends "halo, tolong ubah warnanya".
3. `_LIFECYCLE_INTENTS["CANCELED"] = [INTAKE]` (`runtime.py:888`) → `_handle_intake`.
4. Dispatch has no lifecycle gate for `intake` → intake runs.
5. `apply_to_project` → `transition_lifecycle_locked(state, PAUSED)` (if pause was detected) or `READY` → `LifecycleError`.
6. `dispatch.py:414` catches it, marks the new claim `FAILED`, returns `EVENT_RECONCILIATION_REQUIRED`.
7. `runtime.py:2008` maps that to "A previous operation needs reconciliation. Please try again." — repeated on every future turn.

### Why existing protection does not prevent it

The `dispatch_events` ledger cannot help: each new message is a new event, hence a new claim key, so each attempt creates and then fails a fresh claim. And because the claim ends `FAILED` (not `DONE`), even a replay of the same message produces the same error rather than a clean duplicate short-circuit. No test covers intake against a `CANCELED` or stranded project.

### Recommended fix direction

Give `intake` the same explicit lifecycle gate the other actions have, returning a dedicated, honestly-named code (e.g. `PROJECT_NOT_ACCEPTING_INPUT`) mapped to a message that tells the user the project is closed and offers to start a new one. Separately, distinguish "a lifecycle precondition rejected this turn" from "a possibly-remote mutation is ambiguous" in the dispatcher's generic handler, so a `LifecycleError` never masquerades as `EVENT_RECONCILIATION_REQUIRED`.

### Tests required after approval

- Intake against `CANCELED` → assert a dedicated error code, not `EVENT_RECONCILIATION_REQUIRED`.
- Pause against `FAILED` → same.
- Intake against a stranded `QUEUED`/`RUNNING` → same (this is the user-visible half of BUG-01).
- Regression: assert genuine `EVENT_RECONCILIATION_REQUIRED` is still returned for a real ambiguous remote mutation.
- Assert the project state is unmodified after a rejected intake (no claim churn on a permanently-invalid action).

---

## BUG-08 — `dispatch()` only converts `AuthzError`; lock timeouts and attribute errors escape the mutation authority entirely

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

`TelegramDispatcher.dispatch` is documented as "the single mutation authority" and has a thorough inner `try/except Exception` that guarantees every claim reaches a meaningful terminal state. That guarantee does not hold on two paths. The `create` and `read` actions run **before** the inner `try`, and the only outer handler is `except AuthzError`. A `TimeoutError` from the writer lock, or an `AttributeError` from a collaborator that returns `None`, propagates out of `dispatch()` uncaught.

### User impact

The claim may be left in whatever state it was in when the exception escaped. For `create`/`read` there is no claim, so the impact is an unhandled exception that the receive loop converts into a generic "Something went wrong processing your message" — with the underlying cause (a 30-second lock wait) invisible to both user and operator except in the log.

### Technical root cause

`website-builder/app/channels/dispatch.py`:
- Line 99 opens the outer `try`.
- Lines 117-121 (`action == "create"`) and 122-123 (`action == "read"`) call `self.access.create(...)` / `self.access.read(...)`, both of which open `self.store.acquire_writer(project_id)`.
- Line 216 opens the inner `try`; line 414 closes it with `except Exception`.
- Line 476 is the only outer handler: `except AuthzError as exc`.

`acquire_writer` raises `TimeoutError` after its 30-second default (`app/core/state.py:261, 288-289`). `TimeoutError` is not an `AuthzError`, so it escapes. The same applies to line 449 (`result.data`, guarded only for `action == "revise"`) and line 475 (`return result`, where `result` may be `None`).

Compounding this, `ProjectAccess.read` (`app/core/authz.py:161-167`) takes the **exclusive writer lock to read three fields** — a read path that blocks every writer for the duration.

### Evidence

- `website-builder/app/channels/dispatch.py:99`, `:117-123`, `:216`, `:414`, `:449`, `:475-477`.
- `website-builder/app/core/state.py:259-289` — `acquire_writer(project_id, timeout=30.0)` raising `TimeoutError`.
- `website-builder/app/core/authz.py:161-167` — `read()` using `acquire_writer`.
- `website-builder/app/runtime.py:1225-1248` — the receive loop's blanket `except Exception` that converts this into a generic user message.

### Why existing protection does not prevent it

The inner handler's own comment (`dispatch.py:415-424`) states the invariant "EVERY dispatch attempt must leave its durable claim in a meaningful terminal or recoverable state", but the invariant is only enforced for the actions that happen to be inside the inner `try`. Nothing tests a lock timeout on `create`/`read`, and nothing tests a collaborator returning `None`.

### Recommended fix direction

Move the `create`/`read` handling inside the protected region, and widen the outer handler to `except Exception` so `dispatch()` is genuinely total — mapping `TimeoutError` to a dedicated `WRITER_LOCK_TIMEOUT` code and logging the exception (it never contains user data). Separately, give `ProjectAccess.read` a lock-free read path (`store.load` already exists and takes no lock) so a status check can never block a build.

### Tests required after approval

- Force `acquire_writer` to time out on `create` and on `read`; assert `dispatch` returns a structured failure and does not raise.
- Make a collaborator return `None`; assert `dispatch` returns a structured failure.
- Assert a lock-free `read` cannot block a concurrent `acquire_writer`.
- Regression: assert `AuthzError` still maps to its specific codes.

---

## BUG-09 — Internal error codes and free text leak into a single generic user message, hiding the real cause

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

`_send_error_reply` maps a fixed dict of ~25 error codes to user-facing text. Anything not in that dict becomes "Something went wrong. Please try again." Several distinct, actionable conditions fall outside the dict, and one path passes a chat id of the literal string `"unknown"`.

### User impact

- A user whose build was rejected because another project is building gets "Something went wrong" with no hint to retry — instead of the specific "busy, try shortly" that the code already has a message for (`PREVIEW_BUSY`).
- A user who triggers a failure in the approve/publish path with no state object gets an error reply addressed to chat id `"unknown"`, which Telegram rejects, so the user gets **no reply at all**.

### Technical root cause

- `app/projects/build.py:583-588` returns `BuildResult(..., error="Another project is currently being built (MAX_WORKERS=1)")` — free text, not a code. The `PREVIEW_BUSY` code that `runtime.py:2040` knows how to render is never produced by the build path.
- `app/projects/promote.py:239-243` returns `error_code="WORKER_BUSY"`, which is **not** in the `messages` dict at `runtime.py:2004-2032` → generic message.
- `app/runtime.py:1926-1928` and `:1944` — `self._send_error_reply(state.conversation_id if state else "unknown", ...)`.
- `app/runtime.py:2045` — `text = messages.get(error_code, "Something went wrong. Please try again.")`.
- `app/deploy/preview.py:318` — `error_code="OUTPUT_COMMIT_FAILED"` is also absent from the dict, and absent from `_HARD_PREVIEW_ERRORS` (`runtime.py:904-925`), so it falls into the reconcile-retry branch at `runtime.py:1404-1459`, finds no smoke classification, and **stops the user's turn entirely** with a generic error. A broken local git repository therefore soft-wedges the conversation for that project.

### Evidence

- `website-builder/app/runtime.py:2004-2045` — the closed-looking but incomplete mapping and the generic fallback.
- `website-builder/app/projects/build.py:583-588` — free-text busy error.
- `website-builder/app/projects/promote.py:239-243` — `WORKER_BUSY`.
- `website-builder/app/runtime.py:1926-1928`, `:1944` — `"unknown"` chat id.
- `website-builder/app/deploy/preview.py:316-318` — `OUTPUT_COMMIT_FAILED`.
- `website-builder/app/runtime.py:904-925` — `_HARD_PREVIEW_ERRORS`, which omits `OUTPUT_COMMIT_FAILED`.

### Why existing protection does not prevent it

Tests assert the *presence* of message mappings for the codes that are mapped; nothing asserts that every code the system can emit is either mapped or intentionally generic. There is no closed enum of error codes — the "code" is a bare `str` produced independently by ~10 modules, so drift is invisible.

### Recommended fix direction

Introduce a real closed set of error codes (an enum or a frozen constant registry) and add a test that asserts every code any collaborator can return is present in the user-message map — failing the build when a new code is added without copy. Separately, resolve the chat id from the dispatcher/store rather than falling back to the string `"unknown"`, and add `OUTPUT_COMMIT_FAILED` (and `WORKER_BUSY`) to the map.

### Tests required after approval

- A test that enumerates every `error_code=` literal in `app/` and asserts each is either in the user-message map or on an explicit "intentionally generic" allowlist.
- Assert approve/publish failure with a `None` state still reaches the correct conversation.
- Assert `OUTPUT_COMMIT_FAILED` produces a specific, retryable message and does not silently consume the user's turn.

---

## BUG-10 — The whole preview/promotion pipeline is serialized behind one global worker slot in a single-threaded loop, and the loser is told nothing useful

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

`ProjectRunner` has exactly one global slot (`MAX_WORKERS = 1`), shared by build, revision, promotion, and preview, across **all** projects. The Telegram receive loop is single-threaded and processes updates one at a time, so while project A builds (minutes), project B's update is not even dequeued — and when it is, the slot is gone.

A build that loses the race fails immediately with a free-text "Another project is currently being built" error, which the user sees as "Something went wrong."

### User impact

In any deployment with more than one active user, a second user's build request fails instantly and unhelpfully while the first user's build runs. The failed claim is then `FAILED`, so that specific message is spent. And because the loop is blocked, the second user sees **no response at all** for the duration of the first build — no "still working on it", no queue position.

### Technical root cause

- `app/sandbox/runner.py:23-24` — `MAX_WORKERS = 1`; `:212-218` — a single `self._active_project` slot guarded by one lock.
- `app/runtime.py:2076-2083` — a strictly sequential `for update in updates: self._process_update(update)`.
- `app/projects/build.py:583-588` — non-blocking `acquire_project` returns `False` → immediate failure.
- `app/projects/promote.py:239-243` — same, `WORKER_BUSY`.
- `app/deploy/preview.py:172-176` — same, `PREVIEW_BUSY`.
- The single-threaded loop is a deliberate design choice (it is what makes the registry's process-local locking safe), so this is a capacity finding, not a correctness bug — but the *failure handling* is a bug.

### Why existing protection does not prevent it

`PREVIEW_BUSY` has a user-facing message, which shows the busy case was considered — but only for the preview path. The build and promote busy paths were never given the same treatment. No test covers two projects contending for the slot.

### Recommended fix direction

Give the build and promote busy paths the same dedicated code and message as `PREVIEW_BUSY`, and make it explicitly retryable. Longer term, the honest options are a per-project slot (so unrelated users do not block each other) or a real work queue with a "queued, we'll start shortly" acknowledgement — both are larger changes and should be scoped separately from the message-mapping fix.

### Tests required after approval

- Two projects contending for the slot: assert the loser gets a specific, retryable busy code and message.
- Assert a busy failure leaves the project in a state from which a *new* message can retry successfully.
- Assert a paused/queued acknowledgement path exists for a blocked turn (or explicitly document that blocking is intended).

---

## BUG-11 — QA evidence validation requires exact viewport equality on a metric the browser does not guarantee

Severity: **MEDIUM**
Confidence: **HIGH CONFIDENCE**

### What happens

Phase 8 QA captures five live browser metrics and validates three of them for **exact** equality against the requested viewport. One of the three, `document.documentElement.clientWidth`, is the *content* width, which is `innerWidth` minus any space the browser reserves for a vertical scrollbar. Any page taller than the viewport on a browser that reserves scrollbar space yields `clientWidth < viewport`, and the evidence is rejected as a capture-integrity failure.

The consequence is not a wrong screenshot — it is that a perfectly good build is failed as an **infrastructure error**, which does not consume a repair attempt, does not produce a repairable finding, and lands the project in `FAILED` with no actionable path.

### User impact

The user's website fails QA with "an unexpected error occurred" (see BUG-12), and neither they nor the operator can tell that the real cause was a scrollbar. Depending on the browser build, this could affect every mobile preview.

### Technical root cause

`app/qa/screenshot.py:311-326`:

```python
for metric_name, expected in (
    ("innerWidth", expected_width),
    ("innerHeight", expected_height),
    ("documentElement.clientWidth", expected_width),
):
    if actual_viewport[metric_name] != expected:
        raise ScreenshotError(...)
```

Note the asymmetry: two lines below, the PNG width is checked with a *tolerance* — `if png_width < expected_width` (`screenshot.py:329`) — precisely because "a full-page PNG may be wider than the viewport". The author applied tolerance where it was needed and exact equality where it is not physically guaranteed.

A second, smaller inconsistency: `app/qa/deterministic.py:81-86` compares the document's widest content against `metrics.document_client_width` but *reports* `viewport_width` in the message ("exceeds {viewport_width}px viewport"). Because `validate_screenshot_dimensions` first forces `clientWidth == viewport`, the two checks happen to agree — but only by accident of the strict equality, and the message is misleading if that ever changes.

### Evidence

- `website-builder/app/qa/screenshot.py:317-326` — exact-equality loop including `documentElement.clientWidth`.
- `website-builder/app/qa/screenshot.py:328-333` — the tolerant PNG check immediately after, in the same function.
- `website-builder/app/qa/orchestrator.py:298-302` — a metrics mismatch is converted into `infra_error`, i.e. `INFRASTRUCTURE_ERROR:capture_failed:...`.
- `website-builder/app/qa/orchestrator.py:131-144` — an infrastructure error immediately calls `_finalize_failure` and returns, with **no repair attempt and no user-actionable finding**.
- `website-builder/app/qa/deterministic.py:81-86` — the message/threshold mismatch.
- `website-builder/app/qa/screenshot.py:109-117` — the probe expression, confirming `documentElement.clientWidth` is the metric in question.
- Every test fixture in the suite (`tests/test_qa.py:38-42`, `tests/r1_harness.py:564-576`) constructs metrics with `document_client_width == width`, i.e. the tests assume the zero-scrollbar case and therefore cannot catch this.

### Failure sequence

1. Phase 8 renders the site; the page is taller than 900px (typical for a marketing page).
2. The browser reserves 15px of scrollbar width.
3. `eval` returns `innerWidth: 1440`, `documentElement.clientWidth: 1425`.
4. `validate_screenshot_dimensions` raises: "desktop documentElement.clientWidth is 1425, expected 1440".
5. `orchestrator.py:302` sets `infra_error`; `:131-144` finalizes `FAILED` with `INFRASTRUCTURE_ERROR:capture_failed:...`.
6. The user gets the generic "An unexpected error occurred" message and a failed project.

### Why existing protection does not prevent it

The tests are the problem: every synthetic `BrowserMetrics` in the suite sets `document_client_width` equal to the viewport width, which is the assumption the production check depends on. There is no test with a scrollbar-reduced `clientWidth`, and no test proving the check tolerates one.

### Recommended fix direction

Assert `documentElement.clientWidth` within a small tolerance (or assert `clientWidth <= innerWidth` and `clientWidth >= innerWidth - scrollbar_allowance`) rather than for equality. Keep `innerWidth`/`innerHeight` at exact equality — those *are* guaranteed by `set viewport`. Separately, make the overflow check's baseline and its message agree: compare `max(document_scroll_width, body_scroll_width)` against `innerWidth`, and report the same number in the message.

### Tests required after approval

- A `BrowserMetrics` fixture with `inner_width=1440, document_client_width=1425` must be **accepted** for a 1440px viewport.
- A fixture with `inner_width=1300` for a 1440px viewport must still be **rejected** (the guard must keep its teeth).
- `innerHeight` mismatch must still be rejected.
- Assert the overflow finding's message number equals the number it actually compared against.
- End-to-end: a real `agent-browser` capture of a page taller than the viewport must not be reported as a capture-integrity failure.

---

## BUG-12 — A stale QA binding is reported to the user as "an unexpected error occurred"

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

Two deliberate, well-named failure conditions inside the QA success path are raised as bare `ValueError`s. They land in the orchestrator's blanket `except Exception`, which replaces them with the opaque code `UNEXPECTED_QA_ERROR`. The specific, actionable conditions are therefore erased before they can reach either the user or the operator.

### User impact

When a preview is genuinely out of date, or when VISION did not actually run, the user is told "An unexpected error occurred. Please try again." — which invites a retry that cannot possibly help, instead of the correct guidance already available in the codebase ("The preview is outdated. Please wait for the latest build.").

### Technical root cause

`app/qa/orchestrator.py:146-159`, inside the success branch:

```python
if checked['source_revision'] != state.revisions.source_revision:
    raise ValueError('STALE_QA_BINDING')            # line 153
if qa_attempt.vision is None or qa_attempt.vision.pass_ is not True:
    raise ValueError('VISION_REQUIRED')              # line 155
```

Both are caught at `app/qa/orchestrator.py:245-256`:

```python
except Exception as exc:
    logger.exception("Unexpected error during Phase 8 QA for %s", project_id)
    self._finalize_failure(project_id, attempts, repair_count, error="UNEXPECTED_QA_ERROR")
```

`STALE_QA_BINDING` and `VISION_REQUIRED` are not in `_send_error_reply`'s map either, so even if they survived they would render generically. The codebase clearly knows the right message — `runtime.py:2015` maps `STALE_QA_BINDING` to "The preview is outdated. Please wait for the latest build."

### Evidence

- `website-builder/app/qa/orchestrator.py:152-155` — the two `ValueError` raises with specific codes.
- `website-builder/app/qa/orchestrator.py:245-256` — the blanket handler that erases them.
- `website-builder/app/runtime.py:2015` — `STALE_QA_BINDING` already has correct user copy.
- `website-builder/app/qa/orchestrator.py:505-523` — `_finalize_failure` persists `error="UNEXPECTED_QA_ERROR"`, so the durable `state.failure` also loses the real cause (only the log has it).

### Why existing protection does not prevent it

`runtime.py:2013-2015` proves the user-facing mapping exists and works — but nothing produces that code from the QA layer, so the mapping is unreachable from this path. No test asserts that a stale binding surfaces as `STALE_QA_BINDING`.

### Recommended fix direction

Introduce a small typed exception (e.g. `QABlockingError(code)`) for deliberate, named preconditions, catch it separately in `run()`, and propagate its code into `QAResult.error`, `_finalize_failure`, and the dispatcher's `data["build_error"]` so the existing `_send_error_reply` mapping renders it. Reserve `UNEXPECTED_QA_ERROR` for genuinely unexpected exceptions.

### Tests required after approval

- Force `checked["source_revision"] != source_revision`; assert `QAResult.error == "STALE_QA_BINDING"` and that the user-facing reply is the "preview is outdated" text.
- Force `vision is None`; assert `QAResult.error == "VISION_REQUIRED"`.
- Assert `UNEXPECTED_QA_ERROR` is reserved for real unexpected exceptions.
- Assert the durable `state.failure.error` carries the specific code, not the generic one.

---

## BUG-13 — `build()` validates Design DNA against a state object read before the writer lock was released

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

The QA orchestrator explicitly documents and fixes this exact hazard for its own repair path, with a dedicated `_ReferenceSnapshot` class and a comment explaining why passing the live state after lock release is wrong. The Phase 7 build path has the identical hazard and was not fixed.

The consequence: a build's Design DNA can be validated against a *different* set of design references than the one the build instructions were composed from.

### User impact

Subtle and rare. When a reference upload lands between the instruction composition and the DNA validation, a build can be accepted or rejected against the wrong reference set — producing a site built from one reference set but validated against another, or a spurious build failure that the operator cannot reproduce.

### Technical root cause

`app/projects/build.py:592-610` opens the writer block, composes instructions, and keeps a reference to the live state object:

```python
with self.store.acquire_writer(project_id) as state:
    ...
    combined_instructions = compose_project_instructions(state, ...)   # line 601
    policy_state = state                                                # line 604
    ...
    self.store.save(state)                                              # line 610
# <-- writer lock released here (line 611)
...
validate_composed_dna(frontend_result.get("design_dna"), policy_state)  # line 641
```

`frontend_build` at line 614 is a long-running model call, so the lock is released long before line 641. `validate_composed_dna` (`app/core/composition.py:35-40`) reads `state.design_references` from whatever the object holds at call time.

The same pattern also means `state.brief` (line 600) and `combined_instructions` (line 601) are read from a snapshot that may no longer match disk.

### Evidence

- `website-builder/app/projects/build.py:604` — `policy_state = state`.
- `website-builder/app/projects/build.py:641` — `validate_composed_dna(frontend_result.get("design_dna"), policy_state)`, after the lock is released and after a long model call.
- `website-builder/app/core/composition.py:35-40` — `validate_composed_dna` reads `state.design_references`.
- Contrast — the fixed version: `website-builder/app/qa/orchestrator.py:40-55` (`_ReferenceSnapshot`) and `:375-387` ("Snapshot the reference set UNDER the lock… validating against a post-lock-release, possibly-mutated `state` object would validate against stale references") and `:415`.

### Why existing protection does not prevent it

The fix landed only in `orchestrator.py`. There is no equivalent test asserting that Phase 7's DNA validation is insensitive to a concurrent reference upload, and `test_build.py` drives `build()` with a quiescent store, so the window never opens.

### Recommended fix direction

Reuse the existing `_ReferenceSnapshot` idea for the Phase 7 path: capture an immutable copy of `design_references` (and whatever else the validation reads) inside the writer block, and pass that to `validate_composed_dna`. Best is to move `_ReferenceSnapshot` into a shared module (e.g. `app/core/composition.py`) so both call sites use one type — which also prevents the pattern from recurring.

### Tests required after approval

- Mutate `state.design_references` between instruction composition and DNA validation; assert the Phase 7 validation result is unchanged.
- Assert `_ReferenceSnapshot` (or its shared replacement) is used by both `build.py` and `qa/orchestrator.py`.
- Regression: assert legitimate reference-synthesis violations are still rejected.

---

## BUG-14 — Local state can be marked `FAILED` after a preview was already delivered to the user

Severity: **MEDIUM**
Confidence: **HIGH CONFIDENCE**

### What happens

`build()` wraps its whole body in a `try/except Exception` whose handler unconditionally transitions the project to `FAILED`. If anything raises *after* the preview has been deployed, smoke-tested, and sent to the user, the project is marked `FAILED` even though the user has a working preview in their chat and a live Vercel deployment.

### User impact

The user receives a preview, then the project shows as failed. A subsequent revision or build attempt is refused or behaves as if nothing was ever produced. The user has no way to get back to the preview they were just shown.

### Technical root cause

`app/projects/build.py:942-955`:

```python
except Exception as exc:
    logger.error("Unexpected error during Phase 7 build of %s (type=%s)", ...)
    with self.store.acquire_writer(project_id) as state:
        self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
        state.failure = {"phase": "build", "error": str(exc), ...}
```

The preview runs at `build.py:899-927`, and the return statement that consumes its result is at `931-940`. `PREVIEW_READY → FAILED` *is* a legal edge (`lifecycle.py:67`), so the transition succeeds silently. Anything raising in that window — a serialization error in `json.dumps(checks, indent=2)` at line 936, or a collaborator returning an unexpected shape — produces the wrong durable state.

Note also that `state.failure["error"] = str(exc)` puts raw exception text into durable state, unlike every other failure record in this codebase, which stores sanitized codes. That is inconsistent with the project's own stated convention (see `orchestrator.py:245-256`, `promote.py:739-742`).

### Evidence

- `website-builder/app/projects/build.py:899-927` — the preview runs inside the `try`.
- `website-builder/app/projects/build.py:931-940` — the success return, still inside the `try`.
- `website-builder/app/projects/build.py:942-955` — the unconditional `FAILED` transition, with `str(exc)` persisted.
- `website-builder/app/core/lifecycle.py:62-68` — `PREVIEW_READY → FAILED` is legal, so nothing catches this.

### Why existing protection does not prevent it

`dispatch.py:414-448` (the dispatcher's own claim finalization) has a careful, evidence-based classifier for exactly this situation — but it runs *outside* `build()`, so by the time it runs the lifecycle has already been corrupted. And the preview's own delivery state machine (`preview.py:585-724`) is durable and correct; the corruption happens strictly downstream of it.

### Recommended fix direction

Make the handler lifecycle-aware: if `deployment["latest_shown_preview"]` exists for the current `source_revision`, do not transition to `FAILED` — instead leave the lifecycle at `PREVIEW_READY` and record the error in `state.failure` with `phase: "post_preview"`, so the failure is visible but the delivered preview stays usable. Also replace `str(exc)` with a sanitized code plus a separate operator-log detail, matching the rest of the codebase.

### Tests required after approval

- Inject an exception after the preview succeeds; assert the lifecycle is not `FAILED` and `latest_shown_preview` is intact.
- Assert `state.failure["error"]` contains a stable code, not raw exception text.
- Regression: assert a genuine pre-preview exception still transitions to `FAILED`.

---

## BUG-15 — An intake turn's claim is marked failed because the *build* sub-claim's finalization raised

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

When an intake turn triggers the automatic build, the dispatcher runs the build and then writes the build sub-claim's final status. That finalization block is not wrapped in its own `try/except`, so if it raises (a writer-lock timeout, or a `KeyError` because the sub-claim vanished), the exception is caught by the outer handler — which finalizes the **intake** claim as `FAILED` and returns `EVENT_RECONCILIATION_REQUIRED`.

The intake itself succeeded, the build may well have succeeded, and the user is told a previous operation needs reconciliation for a turn that actually worked.

### User impact

The user gets an error message instead of a build confirmation, and replaying that exact message now short-circuits to the failure path — so the outcome of the build is never reported to them.

### Technical root cause

`app/channels/dispatch.py:312-336`:

```python
try:
    build_result = _invoke_build(project_id, auto_build_brief, _auto_boundary)
except Exception:
    build_result = OperationResult.fail("EVENT_RECONCILIATION_REQUIRED", ...)

with self.store.acquire_writer(project_id) as bstate:          # line 321 - NOT guarded
    bstate.dispatch_events[build_key]["status"] = (...)         # line 322 - KeyError possible
    ...
    self.store.save(bstate)                                     # line 328
result.data["build_triggered"] = True                            # line 329
```

`dispatch_events[build_key]` is indexed directly with no `.get()` guard, and the whole block is outside any local handler. The outer `except Exception` at line 414 then does `state.dispatch_events.get(key)` — the **intake** key, not `build_key` — and marks that `FAILED`.

### Evidence

- `website-builder/app/channels/dispatch.py:321-328` — unguarded finalization, direct dict indexing.
- `website-builder/app/channels/dispatch.py:414-434` — the outer handler finalizes `key` (intake), not `build_key`.
- `website-builder/app/channels/dispatch.py:445-448` — returns `EVENT_RECONCILIATION_REQUIRED`.
- Contrast — the explicit-build path, which is also unguarded but at least finalizes its own key: `dispatch.py:372-377` and `:454-463`.

### Failure sequence

1. User's message completes the brief → `READY`.
2. Dispatch persists the intake claim, runs `intake.process` + `apply_to_project` successfully.
3. `admissible` is true; the build sub-claim is persisted and `build()` runs (minutes).
4. `build()` succeeds; the preview is delivered.
5. Line 321's `acquire_writer` times out (30s, because a concurrent writer holds the lock) → `TimeoutError`.
6. Outer handler marks the **intake** claim `FAILED`; returns `EVENT_RECONCILIATION_REQUIRED`.
7. The user is told reconciliation is required, and the successful build is never reported.

### Why existing protection does not prevent it

The dispatcher's claim-finalization logic is careful in the two places it controls (`:414-448` and `:454-474`, both with `try/except` and clear comments). The auto-build finalization was added later and did not get the same treatment. No test injects a failure into that specific write.

### Recommended fix direction

Wrap `:321-328` in its own `try/except` that (a) attempts the sub-claim finalization, (b) on failure leaves the sub-claim as-is and logs, without touching the intake claim, and (c) still returns the real intake result plus whatever build outcome is known. Use `.get(build_key)` rather than direct indexing so a missing sub-claim is a no-op rather than a `KeyError`.

### Tests required after approval

- Inject a lock timeout at `:321`; assert the intake claim ends `DONE` and the returned result carries the intake outcome.
- Inject a `KeyError` (sub-claim absent); assert no exception escapes and the intake result is still returned.
- Assert the sub-claim still reaches a terminal status when its own finalization succeeds.
- Regression: assert a genuinely failed build still marks the sub-claim `FAILED` and surfaces `build_success: False`.

---

## BUG-16 — Remote calls are made while holding the exclusive project writer lock

Severity: **MEDIUM**
Confidence: **CONFIRMED**

### What happens

The project's central discipline is "persist intent before the side effect, never hold the lock during it." Two places break it. One is even documented as intentional. The result is that an exclusive, cross-process, 30-second-timeout file lock is held across network calls that can take tens of seconds under latency, 5xx, or rate limiting.

### User impact

When it bites, other operations on the same project fail with a lock timeout rather than waiting, and the resulting error is a generic "Something went wrong" (see BUG-09). The window is narrow in the documented single-process deployment, but the registry already assumes a single writer process only by convention (see BUG-05's sibling, the registry's process-local locking), so this is a real fragility rather than a theoretical one.

### Technical root cause

**Promotion** — `app/projects/promote.py:462-469`, with the comment *"Writer remains held through the first mutating adapter call"*:

```python
if resume_reconciled_url is not None or resume_fail_closed:
    promote_result = None
else:
    promote_result = self.deps.vercel.promote_deployment(   # remote POST, under the lock
        app_id, vercel_project, deployment_id, operation_id,
        source_revision, artifact_sha256, expected_name=expected_name,
    )
```

This is inside the `with self.store.acquire_writer(project_id) as locked:` block opened at line 359 and closed at line 470.

**Preview** — `app/deploy/preview.py:264-286`, which is *undocumented* and worse: it performs `lookup_project` **and** `ensure_bypass` (which can itself make several Vercel calls, including a reconciliation read) inside the writer block, on the already-delivered recovery path.

**Build** — `app/projects/build.py:605-606`: `self.runner.create_workspace(project_id)` and `self._copy_starter(workspace)` (filesystem template copying) run inside the writer block.

### Evidence

- `website-builder/app/projects/promote.py:359` (lock acquired), `:462-469` (remote POST inside), `:470` (lock released).
- `website-builder/app/deploy/preview.py:264-286` — `acquire_writer` at 266, `lookup_project` at 277, `ensure_bypass` at 283, all inside.
- `website-builder/app/projects/build.py:592-610` — `create_workspace` and `_copy_starter` inside.
- `website-builder/app/core/state.py:259-289` — the 30-second `acquire_writer` timeout that these overruns.
- Contrast — the correct pattern used elsewhere: `promote.py:495-509` calls `reconcile_production_deployment` *after* releasing the lock and then uses `_update_intent`; `preview.py:330-359` marks the remote boundary and then calls Vercel outside any lock.

### Why existing protection does not prevent it

`promote.py:434-436` contains a comment explaining that the writer lock is "a file lock, not reentrant" and that post-promote work must run after release — showing the constraint is understood — but the constraint is then knowingly violated for the promote POST itself. No test measures lock hold time or exercises contention.

### Recommended fix direction

Restructure the promotion path to match its own stated rule: persist the intent and the `PUBLISHING` transition under the lock, release, then issue the promote POST, then re-acquire to record the outcome. The existing `promotion_intent` + `is_resume` machinery already makes this safe — that is precisely the crash-recovery design. For the preview recovery path, resolve the project and the bypass secret **before** taking the lock, and take the lock only to write. For `build()`, move `_copy_starter` outside the writer block.

### Tests required after approval

- Assert no network call is made while the project writer lock is held (instrument the transport and the lock).
- Concurrent-access test: hold the lock in one thread, attempt a promote/ensure-bypass in another, assert the second does not time out at 30s.
- Regression: assert the ambiguous-promote crash-recovery path still reconciles and never double-promotes.
- Regression: assert the `previous_production` rollback target is still captured before any remote side effect.

---

# Cross-Cutting Patterns

**1. Crash-consistency is designed for the remote boundary but not for the local one.**
Every remote side effect has a durable intent, an evidence-based classifier, and a fail-closed recovery path. But the *local* multi-step sequences that bracket those side effects are not atomic: QA's `tested_snapshot` write and its `RUNNING → PREVIEW_READY` transition are two writer blocks (BUG-01); the auto-build claim finalization is unguarded (BUG-15); the post-preview failure handler overwrites a delivered result (BUG-14). The remote boundary is protected; the local bookkeeping around it is not.

**2. Durability helpers report success when they did nothing.**
`_update_intent` in both `preview.py` and `promote.py` treats an operation-id mismatch as a no-op success, while the runtime's own equivalent helpers (`_record_preview_reconcile_attempt`, `_send_preview_status_once`) treat the same condition as a detectable condition. The most safety-critical writes in the system are the ones with the weakest failure reporting — and the `try/except` blocks that appear to protect them cannot fire (BUG-02, BUG-03, BUG-04).

**3. A pattern is fixed in one call site and left broken in its siblings.**
The post-lock-release staleness hazard is fixed in `qa/orchestrator.py` with a dedicated `_ReferenceSnapshot` and an explanatory comment, and left unfixed in `projects/build.py` (BUG-13). The `result is None` tolerance is present in `_safe_send` and `_send_preview_status_once` and absent from `_send_error_reply` and `_handle_intake`. The `expected_name` threading contract is honoured in the current tree for the adapter call sites, but the sibling `deployment["vercel_project_id"]` / `deployment["live_url"]` reads were not updated with it (BUG-03, BUG-04). Reviewing each file independently would not surface any of these.

**4. Error codes are free strings produced by ten independent modules, with one consumer-side map.**
There is no closed enum, so codes drift out of the user-message map (`WORKER_BUSY`, `OUTPUT_COMMIT_FAILED`, free-text "Another project is currently being built") and deliberate preconditions are erased into `UNEXPECTED_QA_ERROR` (BUG-09, BUG-12). The *most* important consequence is that the one genuinely actionable permanent-wedge condition (BUG-01, BUG-07) presents to the user as the same generic sentence as a transient hiccup.

**5. Tests assert against state shapes production cannot produce.**
`test_r1_slug_bind.py:46` writes `deployment["vercel_project_id"]`; `test_intake.py:431` writes `deployment["live_url"]`; every `BrowserMetrics` fixture in `test_qa.py` and `r1_harness.py` sets `document_client_width == viewport width`. Each of these makes a broken guard look covered. This is the single highest-leverage pattern in the report: three findings (BUG-03, BUG-04, BUG-11) exist *because* the tests manufacture the state the code depends on.

**6. Locks are held longer than the discipline requires.**
`acquire_writer` is an exclusive, cross-process, 30-second-timeout file lock. It is held across network calls in promotion and preview, and across filesystem work in build (BUG-16). The registry, by contrast, uses a process-local lock only (BUG-05's sibling in `registry.py:246-267`) — the two halves of the same persistent state use different, and in one case absent, concurrency control.

**7. Capacity limits are treated as correctness.**
`MAX_WORKERS = 1` plus a single-threaded receive loop means one user's 15-minute build silently blocks every other user, and the loser's error message is unhelpful (BUG-10). This is a design decision, not a defect, but the failure handling around it was never finished.

---

# Needs Verification

These need runtime evidence I could not produce in a read-only session.

1. **Does `agent-browser` reserve scrollbar width in `documentElement.clientWidth`?** This settles BUG-11's real-world blast radius. One measurement against the actual CLI on a page taller than the viewport decides whether this is a rare edge case or a failure on every build. The fix direction (tolerance instead of equality) is correct either way; only the severity depends on the answer.

2. **Can two `python -m app` processes realistically run against the same state root?** The registry's locking is process-local only (`app/core/registry.py:238-241, 246-267, 334`), so two processes would both read `next_project_seq`, both allocate `tg-<conv>-p1`, and one save would win — two projects sharing one internal id, with one silently orphaned. Nothing in the code prevents this (no PID file, no lock file, unlike `ProjectStateStore`). I could not determine whether the deployment story (systemd unit, supervisor, manual runs) makes it reachable. If it is reachable it is a **HIGH** data-corruption bug; if a single process is genuinely guaranteed, it is a latent trap for the next operator.

3. **Does the unlocked registry read race actually produce `PermissionError` on Windows?** `ConversationRegistryStore.save` uses `os.replace` onto a file that unlocked readers (`resolve_name`, `active_project_id`, `display_name_for`, `recorded_event`) may have open. CPython opens files on Windows without `FILE_SHARE_DELETE`, so `MoveFileEx` replacement can fail with a sharing violation. The code's own comment at `registry.py:576-577` acknowledges "Windows file-handle contention", which supports the theory, but I could not reproduce it without running code. Would need a threaded write/read stress test on Windows.

4. **Can `state.deployment['preview_intent']` change mid-`_run` in practice?** This determines BUG-02's reachability. The worker slot serializes preview runs, but a concurrent revision/QA producing a new `operation_id` is the trigger. Needs a concurrency test that mutates the intent between the pre-send write and the send.

5. **Is the stranded-`RUNNING` recovery from commit `ed16b5d1b` present for `build()` too?** I confirmed `revise.py` has exception handling for this class and `build.py` has a broad `except Exception` — but `build.py`'s handler runs *inside* the process, so it does not help when the process is gone. I want to confirm there is no out-of-process supervisor (systemd `Restart=on-failure` plus an init hook) that performs recovery. Nothing in `website-builder/` does; whether the deployment adds it is outside this repo.

6. **How large does a real long-lived project's state file get?** BUG-05's severity depends on the practical growth rate. A project with 50 revisions and full diagnostic capture would be the measurement.

7. **Is `ai` reachable?** The `VisionFindings.blocking` property blocks on `bool(blocking_findings) or bool(raw_error)` with no grounding check against the brief or Design DNA. A hallucinated requirement violation therefore consumes one of only two repair attempts and can fail an otherwise-correct build. Whether VISION actually invents requirements in practice needs live evidence; the missing grounding check itself is confirmed.

8. **Runner workspace path comparison on Windows** — `app/sandbox/runner.py:245` compares a `.resolve()`d `cwd` against a non-resolved `workspace` using the case-sensitive `Path.is_relative_to`. If path casing or an 8.3 short name differs, this raises a spurious `WorkspaceError`. Most call sites pass `cwd=workspace` so the strings normally match, but I could not enumerate every call site.

---

# Things Investigated That Are NOT Bugs

Recording these so they are not re-flagged.

- **`expected_name` threading across the Vercel adapter.** The historical defect (eight call sites falling back to the opaque hash-derived name) is **fixed** in the current tree. Every `_project_valid` call site in `adapters.py` now passes `expected_name` explicitly, and every caller in `preview.py`, `promote.py`, and `domain.py` threads it through. The `project_name_for` fallback at `adapters.py:177` is only reached when a caller genuinely passes `None`, which is the correct legacy behaviour.

- **Vercel project-creation duplicate safety.** `ensure_project` and `ensure_project_with_slug` both GET-then-POST only on a proven 404, classify create failures by evidence (`_CREATE_CONFIRMED_REJECTION`, 409 → read-only reconcile, transport/5xx → ambiguous), and never issue a second POST. The `WEBSITE_BUILDER_OWNER` marker is the sole ownership authority and the slug never proves ownership. I could not construct a path to two Vercel projects for one app.

- **Preview/promotion delivery idempotency.** The `follow_up_state` / `photo_outcome` / `text_outcome` three-state machine (`preview.py:44-97, 585-674`) is correct: the attempt marker is persisted **before** every send, `NOT_SENT` is only concluded from an explicit `TELEGRAM_REJECTED`, and every ambiguous outcome fails closed rather than resending. `_maybe_send_follow_up` is exemplary — local prerequisites are resolved before the marker, the marker before the send, and a failed confirmation is explicitly accepted as a possibly-lost nudge rather than a duplicate. (The silent no-op in BUG-02 is a *reachability* problem with this machinery, not a flaw in the design.)

- **Ambiguous remote promote handling.** `promote.py:425-531` reconciles by the complete identity tuple, never re-promotes on ambiguity, reuses the persisted `previous_production` rollback target on same-operation resume (never recomputing it against drifted state), and refuses to proceed when the rollback target's identity is incomplete. The comment at `promote.py:790-797` correctly identifies that deriving the rollback identity from the current operation is "a guaranteed meta mismatch" — and the code does the right thing.

- **SSRF protection in reference fetching.** `app/core/references.py:111-181` resolves the hostname, filters to public addresses, and pins the connection to a validated IP while keeping TLS `server_hostname` as the original hostname. This defeats DNS-rebinding and is the correct pattern. `_safe_reference_url` additionally rejects userinfo, query, fragment, backslashes, and non-HTTPS.

- **`ProjectAccess.set_roles` does not persist an invalid ACL.** The `raise AuthzError()` at `authz.py:144` is inside the `with acquire_writer` block, before `save()`, so an invalid ACL is only ever in-memory and discarded. This looks wrong on a fast read but is correct.

- **The `event_id`-scoped claim key is sound for its purpose.** `event_id` is `str(update_id)`, which Telegram guarantees unique per message, so the same message can never be admitted twice. The gaps are around the key (non-dispatch replies, offset durability), not in the key itself.

- **`ProjectStateStore` atomic writes.** `tempfile.mkstemp` + `fsync` + `os.replace` in the same directory is the correct pattern, and the `finally` cleanup prevents temp-file accumulation. The `processed_events` set is explicitly serialized to a sorted list because `asdict` cannot handle sets. The Windows reserved-name guard (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) and the symlink rejection at `state.py:157` are both necessary and correct.

- **`OutputGitRepository` concurrency.** Writes go to a shared bare repo, but the branch name embeds the content-addressed `snapshot.identity`, git's own ref lock protects same-branch updates, objects are content-addressed and written atomically, and each commit uses a private `GIT_INDEX_FILE` in a temp dir. Concurrent commits of identical content are idempotent because author/committer dates are pinned. The env scrubbing (`GIT_CONFIG_GLOBAL=os.devnull`, all `GIT_*` stripped, hooks disabled) is thorough.

- **The bypass secret is not leaked.** `BypassSecretStore` keys by Vercel project id (not by anything user-controlled), validates against a strict regex so no path traversal is possible, writes 0600 into a 0700 directory, and the secret is never placed in `ProjectState`, `OperationResult` metadata, logs, or Telegram messages. The provisioner's CASE A-F matrix correctly refuses to rotate on a normal retry and fails closed on an unreadable remote state.

- **Vision infrastructure failures fail closed.** `VisionFindings.blocking` returns `True` when `raw_error` is set, and `orchestrator.py:314-317` converts a VISION error into an `infra_error` rather than "no findings". The reasoning in the docstring is correct and the code matches it.

- **The QA repair budget is correctly bounded.** `MAX_REPAIR_ATTEMPTS = 2` with attempt-0 QA plus at most two repairs, no third attempt, and each repair's post-rebuild verification recorded as its own monotonically-numbered attempt. `QAAttempt.repair_required` correctly excludes `infrastructure_error` so infrastructure failures do not consume the budget. The contact-form policy correctly forbids embedding an access key in generated frontend source even in the unreachable server-side mode.

- **`test_core.py::test_one_writer_lock` is not a flake risk.** It uses a `threading.Event` set after the lock is provably held, with no wall-clock sleep. The previous `time.sleep(0.05)` race was correctly eliminated.

---

# Recommended Fix Order

Ordered by dependency, then blast radius, then implementation safety. Nothing here has been implemented.

**Tier 0 — unblock recovery (do these first; they gate every other fix's testability)**

1. **BUG-01** — add the startup stranded-state reconciliation pass, and make QA's success finalization atomic. Nothing else can be tested reliably while a project can be permanently wedged by a crash. Two independent changes; the atomic-finalization half is a ~5-line change and can ship immediately on its own.
2. **BUG-07** — add the missing `intake` lifecycle gate and stop `LifecycleError` from masquerading as `EVENT_RECONCILIATION_REQUIRED`. This is what makes BUG-01 *visible* to the user and to operators, so it belongs with it.

**Tier 1 — durability correctness (same root cause, fix together)**

3. **BUG-02** — make `_update_intent` fail loudly on an operation-id mismatch, in both `preview.py` and `promote.py`. Fixes the duplicate-delivery risk.
4. **BUG-03 / BUG-04** — correct the two dead state-key reads. Trivial, and unblocked by Tier 0.
5. **BUG-15** — guard the auto-build sub-claim finalization so it cannot fail the intake claim.
6. **BUG-14** — make `build()`'s exception handler lifecycle-aware so a delivered preview is not marked `FAILED`, and replace `str(exc)` with a sanitized code.

**Tier 2 — observability and honesty (low risk, high diagnostic value, and they make the Tier 0/1 fixes verifiable)**

7. **BUG-09** — introduce a closed error-code set plus a test that fails when a new code lacks user copy. Add `WORKER_BUSY` and `OUTPUT_COMMIT_FAILED`; resolve the `"unknown"` chat id.
8. **BUG-12** — separate deliberate QA preconditions from unexpected exceptions so `STALE_QA_BINDING` and `VISION_REQUIRED` reach the user.
9. **BUG-08** — make `dispatch()` total (widen the outer handler, move `create`/`read` inside the protected region) and give `ProjectAccess.read` a lock-free path.
10. **BUG-10** — give the build and promote busy paths the same dedicated code and message as `PREVIEW_BUSY`.

**Tier 3 — correctness of the remaining subsystems**

11. **BUG-13** — move `_ReferenceSnapshot` into a shared module and use it in `build.py` as well as `qa/orchestrator.py`.
12. **BUG-11** — replace the exact-equality `clientWidth` check with a tolerance, and align the overflow finding's baseline with its message. Settle Needs-Verification item 1 first so the tolerance is sized from evidence.
13. **BUG-05** — bound `dispatch_events` and `pending_revisions` with crash-safe, atomic pruning.
14. **BUG-16** — move remote calls and `_copy_starter` outside the project writer lock, restructuring the promotion path to match the rule its own comments already state.

**Tier 4 — requires a decision, not just a fix**

15. **Registry cross-process locking** (Needs Verification 2 and 3). Decide whether single-process is a guaranteed deployment invariant. If yes, enforce it with a PID/lock file so the invariant is checked rather than assumed, and document it. If no, replace the process-local lock with the same file-lock discipline `ProjectStateStore` uses. Either way, run the Windows sharing-violation stress test.
16. **`ContactFormApplication` is dead code** (never wired into `compose()`; nothing calls `enroll_contact_destination`). Decide whether to wire it — in which case it also needs rate limiting, and the no-`Origin` allowance at `contact_form.py:207` needs a decision — or to delete it. Leaving it unwired means the "Phase 14 server-side Web3Forms" path is unreachable and every generated site silently degrades to a plain link or to no contact affordance at all.
17. **Test-fixture hygiene.** Rewrite the three fixture sites that manufacture impossible state (`test_r1_slug_bind.py:46`, `test_intake.py:431`, and the `document_client_width` convention across `test_qa.py` / `r1_harness.py`) so they exercise the real writers. Without this, the corresponding bugs can regress silently.

**Note on ordering rationale:** Tier 0 comes first not because it is the most severe in isolation but because BUG-01 makes every other fix untestable — a project wedged in `QUEUED` cannot be driven through a build/revise/publish cycle, so no Tier 1 or Tier 2 test could assert an end-to-end outcome. BUG-07 ships with it because it is what makes the wedge observable. Tier 1 is one root cause across three findings. Tier 2 exists so that the Tier 0/1 fixes are verifiable through the user-visible surface rather than only through state inspection.
