# Website Builder R1 smoke recovery plan

## Goal

Finish the `feature/website` fix for the reported p7 smoke failure without weakening mandatory preview smoke or claiming an unproven cause:

1. Capture the exact blocked request with sanitized, durable diagnostics.
2. Prove the cause on the existing deployment, then fix only that cause.
3. Make failed previews terminal-but-recoverable: no failed link is shown/published, deterministic bytes are not retried forever, transient failures get bounded same-operation retry, and a Telegram revision creates a new tested operation/deployment.
4. Add real-import, temporary-state regression coverage for routed and legacy turns, restart/crash windows, and duplicate side effects.

## Current evidence and confidence

### Confirmed

- The p7 deployment exists and both desktop/mobile runs persisted only the generic triple `blocked request`, `asset/request failure`, `console error`.
- No raw p7 state, deployed HTML, Playwright trace, operator log, screenshot, or browser event exists in the local workspace/runtime roots. The original implementation collapsed all denied requests to one string.
- The current smoke rule in `app/deploy/adapters.py` aborts anything that is not an exact-origin `GET`/`HEAD` request to a globally resolved destination, and records a safe navigation redirect. This security rule must remain strict unless evidence shows a legitimate same-origin request is denied by that rule.
- An aborted request naturally produces the reported `asset/request failure` and a browser console error. The current instrumentation does not correlate them, so the causal relationship is not yet proven for p7.
- Current committed code already has useful pieces that must be preserved: same-origin root-slash redirects, Vercel deployment/bypass reconciliation by operation/project identity, at-most-once photo/text delivery, the mandatory self-contained gate, and Google Fonts localization in `app/core/selfcontained.py` plus p7-shaped tests.

### Unproven hypothesis

- `tests/test_self_contained.py` contains a synthetic p7-shaped Google Fonts reference (`fonts.googleapis.com/css2` and `fonts.gstatic.com` assets), and HEAD includes a normalizer that vendors those fonts. The scratch repro is synthetic and the fixture was created without the real deployment ID or captured browser request. It is therefore a strong artifact hypothesis, not the confirmed p7 blocked URL.
- A historical run of this deployment reached Vercel `/api/sso`/login, but the current failure has no auth-wall or navigation/redirect line. Do not attribute the current triple to that historical auth event.

### Preserve existing worktree work

The worktree contains uncommitted, directly relevant WIP in `app/deploy/adapters.py`, `app/runtime.py`, `tests/r1_harness.py`, and an untracked `tests/test_r1_smoke_failure_recovery.py`, plus a separate uncommitted inline-style scan in `app/core/selfcontained.py`. Build on the recovery test rather than replacing it, do not reset any WIP, and do not use the inline-style scanner or `_scratch_*` files as p7 evidence. Leave scratch artifacts untracked; turn the relevant scratch scenarios into maintained tests.

### Read-only verification of the concurrent DeepSeek WIP

Useful direction, but not yet acceptable:

- It keeps the strict smoke request rule, adds useful structured host/path/method/resource fields, keeps the existing root-slash redirect rule, exposes smoke classification to the harness, and attempts bounded routed/legacy fall-through.
- `test_r1_smoke_failure_recovery.py` and the inline-style test assert that inline `<style>` is the **exact p7 cause** without captured p7 evidence. Rename/reword them as a scanner-gap regression until VPS instrumentation proves that artifact.
- `logger.exception(..., url)` and `OperationResult.data["url"]` still expose/preserve the raw preview URL; sanitize both and add caplog/result/state leak tests.
- The current scheme/host check in `_sanitize_smoke_url` now rejects relative/`data:`/`blob:` values correctly; retain it and add caplog/state coverage for credentials embedded outside the query.
- Records are capped independently of classification and legacy strings are unbounded, so a late deterministic failure can be lost. HTTP/auth/console precedence and blocked/requestfailed/console events are not correlated; networkidle timeout is also recorded as `browser_exception`, producing the wrong aggregate.
- `stage="smoked"` is still written on failure, smoke diagnostics/raw URL are lost by `BuildResult`, and `smoke_blocked` is written by runtime only after a later reconcile. The initial failed build and a crash before that marker can repeat old work.
- The runtime retries transient/unclassified failures on three separate user turns with no backoff, can block an immediate `REVISE`, and treats hard project/bypass/deployment reconciliation errors as smoke failures. Keep those identity errors fail-closed.
- The new “restart” test replaces only the Telegram loop; the side-effect test checks project creation but not deployment/bootstrap/bypass/Telegram/claims; the two-turn test starts from a hand-seeded failed intent rather than an actual failed build. Rewrite these as full-component restart and exact counter tests.
- The post-delivery revision crash test ignores its result and leaves a real unapplied-reservation wedge. The later “new operation” test also encounters `RevisionOrchestrator._fail()` setting `FAILED`, where routing allows only intake.
- Friendly-slug retries can downgrade a resolver read error to the opaque name and can repeat project/bootstrap POSTs after ambiguous create responses when provider reads are temporarily stale. Persist the binding and use identity-aware reconciliation.
- A confirmed auth wall reuses the same stored bypass secret indefinitely; add the one bounded reprovision path or explicitly leave it as a reported liveness gap. Do not silently classify it as an artifact repair.

## Non-negotiable invariants

- Never log or persist query strings, credentials, request headers, raw console/page text, bypass secrets, or exception messages that may embed a full URL.
- Persist only a stable category/reason, HTTP method, Playwright resource type, status when safe, sanitized hostname/path, viewport, and correlation/primary-failure IDs.
- Keep the smoke same-origin, safe-method, public-destination policy. Never broadly allow external traffic and never skip mandatory smoke.
- A failed smoke operation cannot become `latest_shown_preview` or be approved/promoted.
- Retry transient/ambiguous smoke only with the same operation, deployment, source revision, and artifact hash. Artifact bytes get a new revision instead.
- Project/bypass/deployment/Telegram ambiguity is reconciled by persisted identity and fails closed; it is not reclassified as a smoke defect and never causes a blind second mutation.
- Routed and legacy turns use the same recovery state and status-delivery rules.

# Ordered implementation

## 1. Evidence-first smoke instrumentation

### 1.1 Add a bounded structured failure accumulator

In `app/deploy/adapters.py`:

- Keep the existing stable `OperationResult` contract, but normalize every smoke result into a small persisted/loggable diagnostic shape:
  - `success`
  - `failure_classification`: `artifact_defect`, `transient`, `security_policy`, or `ambiguous`
  - `failure_summary`
  - bounded `failure_records`
  - `primary_failure`
  - viewport coverage
- Each request-related record contains only: stable category, stable sanitized reason, `resource_type`, `method`, lower-case `host`, bounded `path`, optional safe `status`, viewport name plus desktop/mobile dimensions, and optional `caused_by`/`primary` relation. Attach project/operation/deployment/source/artifact identity when the preview owner builds the diagnostic payload; the browser collaborator itself need not log them.
- Strip userinfo, fragment, and query. Reject values without a valid `http`/`https` host rather than treating an arbitrary string as a path. Bound path length and normalize control characters. Do not return the raw smoke URL in diagnostic data.
- Bound both structured records and legacy strings. Maintain counts and the highest-priority primary classification incrementally so a decisive failure after the first 24 console/request events cannot be dropped.
- Replace full-URL `logger.exception` output with a safe target (`host/path`) and exception type. Do not emit traceback/message text for URL-capable browser exceptions.

### 1.2 Capture the exact browser evidence

- For route blocks, persist the precise rule: `off_origin_or_method`, `unsafe_redirect`, `redirect_depth_exceeded`, `non_public_destination`, or `policy_evaluation_error`.
- Map `request.failure` to a stable code such as blocked/aborted, timeout, connection reset/refused, DNS, or unknown. Do not persist `str(failure)` because it may contain a full URL.
- Correlate a blocked request with the matching `requestfailed` and console error using sanitized `(viewport, method, host, path, resource_type)` plus the Playwright request identity available in the fake/live event. Mark those events secondary; do not classify the console line as a second independent defect.
- Give a 401/403/login navigation wall precedence over its generic HTTP/navigation secondary events and classify it as provider security policy, not artifact defect.
- Split HTTP status handling: deterministic 404/410 for render-critical resources is an artifact defect; 429/5xx is transient.
- Split navigation outcomes: the existing same-origin `/` to `/` redirect remains valid; query-changing, path-changing, cross-origin, or excessive redirects fail.
- Return structured page-health data (`empty_page`, bounded broken-image host/path list) instead of one boolean. A blocked external image must be secondary to the original blocked-request cause.
- On `page.goto` timeout, record `networkidle_timeout` without also manufacturing an unrelated outer `browser_exception`. Reserve `browser_exception` for launch/context/driver failures not already classified.
- Normalize missing/invalid screenshots into `missing_screenshot` (or an explicit screenshot-error category) rather than losing them after the browser returns.

### 1.3 Persist and propagate diagnostics

- In `app/deploy/preview.py`, attach a sanitized `smoke_diagnostics` payload to failed `OperationResult`s; do not duplicate the full preview URL or absolute screenshot paths inside the smoke payload. Resolve screenshots from the already persisted smoke directory/relative filename when needed.
- In `app/projects/build.py`, add an optional structured `diagnostics` field to `BuildResult` and populate it on preview failure with project/operation/deployment/source/artifact identity, classification, primary failure, viewport, bounded records, and relative screenshot evidence only.
- In `app/channels/dispatch.py`, carry the structured build diagnostics in the action result so Telegram can render the correct terminal status without reclassifying from the error string. Persist `reached_remote` at the actual first Vercel mutation boundary, not once before the entire preview orchestrator: snapshot/slug/output/local-state failures before that boundary remain retryable build failures, while post-boundary failures reconcile by persisted remote identity.
- Log once from the operation owner with `project_id`, `operation_id`, `deployment_id`, viewport, category/rule, resource type, method, host, and path. Never log the preview URL with a query.

### 1.4 Prove p7 before applying a cause claim

Run the new smoke against the existing p7 deployment on the VPS/runtime host, using the existing project/deployment identity and stored bypass secret. This must be read-only: lookup/reconcile and smoke only; no project create/patch, deployment create, bootstrap mutation, Telegram send, or publish.

Acceptance format, with query omitted:

```text
category=<blocked_request or another captured category>
rule=<exact policy rule>
method=<captured method>
resource_type=<captured type>
host=<captured host>
path=<captured path>
viewport=desktop|mobile
project_id=tg-6329821361-p7
deployment_id=dpl_Fy9T5eMvWRY2pAGZcfvTRGpCEMwe
```

Only after this capture may the report call the root cause confirmed. If the capture instead shows a legitimate same-origin denial, branch as described below; do not force the Google Fonts explanation.

## 2. Fix only the proven cause

### Branch A: generated external runtime asset

If the captured request is external and render-critical:

- Add a regression using the captured host/path/method/resource type, without reproducing query values or credentials.
- If it is the already-supported Google Fonts case, use the existing `selfcontained.py` normalizer and verify the final `dist` has local font assets and no Google stylesheet/font runtime references.
- If it is another exact asset, extend generation/normalization only for that proven allowlisted asset and add the corresponding local-output test. Do not add a general external-request allow.
- Reject the old bytes before QA/preview. A new revision must rebuild and record a new source/artifact identity.

If build-time acquisition of the proven asset fails, do not run FRONTEND/QA repair for a timeout, DNS failure, 429, or 5xx. Mark the build as infrastructure failure and preserve enough identity for a same-build retry; unsupported/unsafe content remains an artifact failure.

### Branch B: legitimate same-origin request denied by smoke

- Reproduce the exact method/resource/redirect with the real `PreviewSmokeTester` fake-browser seam.
- Change only the specific redirect/method/origin helper proven wrong. Keep global-address, safe-method, redirect-depth, and cross-origin checks intact.
- Add negative tests proving unrelated off-origin/private/non-GET requests remain blocked.

### Branch C: no exact evidence

Do not claim the external-font cause or broadly relax smoke. Complete observability/recovery/tests and report that the exact p7 request still needs VPS evidence.

## 3. Persisted recovery state machine

### 3.1 Make `PreviewOrchestrator` the owner of terminal smoke state

In `app/deploy/preview.py`, evolve `preview_intent` with backward-compatible fields:

- `stage`: use `smoke_running`, `smoke_retry_wait`, `smoke_failed`, and existing `smoked` only for an actual pass; delivery stages remain separate.
- `smoke`: attempt count, max attempts, classification, primary failure, bounded records, last-attempt time, and next-retry time.
- `failure_status_delivery`: operation ID and `PENDING|SENT|NOT_SENT`/message ID using the same fail-closed semantics as photo/text delivery.

Derive terminal/blocked behavior from the current stage/classification; do not rely on a separately mutable `last_smoke_classification`, `pre_turn_reconcile_attempts`, or `smoke_blocked` boolean that can become stale. Read the WIP fields only for migration compatibility. Never mint a placeholder `preview_intent` with no operation ID: a later real operation replaces it and resets the attempt budget, so missing identity must fail closed instead.

Within the preview owner:

- Persist operation/deployment identity and `smoke_running` before each browser attempt.
- Retry only `transient`/`ambiguous` smoke/provider failures: three total attempts with injected sleeper/backoff (1s then 2s by default).
- An artifact/security failure of generated bytes is terminal immediately.
- On a retry, resume from smoke/readiness for the same operation. Do not repeat output commit, project create/patch, bootstrap mutation, deployment create, or bypass generation.
- A direct repeat of a terminal same-operation artifact failure returns the persisted failure without browser/remote work. Only a new revision changes operation identity.
- On smoke pass, clear old failure fields before delivery. Never reuse a previous artifact classification for a later screenshot/delivery error.

### 3.2 Separate hard reconciliation failures from smoke failures

In `app/runtime.py` and `app/channels/dispatch.py`:

- Only an actual structured smoke result enters the smoke retry/recovery path. Error-code guessing is insufficient.
- `PROJECT_IDENTITY_MISMATCH`, `PROJECT_RECONCILIATION_REQUIRED`, `PREVIEW_RECONCILIATION_REQUIRED`, `INCOMPLETE_LOOKUP`, bypass reconciliation ambiguity, and dispatch claim failures remain fail-closed and require identity reconciliation. They must not consume smoke attempts or unblock revision after three turns.
- Represent a remote call with a durable intent/outcome identity rather than only a pre-call boolean. After restart or an ambiguous response, look up by project/slug/app ID and operation ID through the provider consistency window. Only a definitive not-found result may authorize one controlled retry of that same operation; never issue an immediate second POST merely because the first read-after-write is briefly stale. Persist the retry count/outcome.
- A confirmed Vercel auth wall first reconciles the bypass by project identity. If the stored secret is confirmed unusable, permit at most one persisted, explicit bypass reprovision and retry smoke on the same deployment. An ambiguous reprovision fails closed; it never creates another project/deployment.
- Fix the first-bypass provider contract before trusting recovery: `BypassProvisioner` currently requires `reconcile_protection_bypass`, while the production `VercelAdapter` exposes only `read_protection_bypass`; an empty authoritative read can therefore block a fresh project before smoke. Align the interface and add a real `VercelAdapter` + empty-store + explicit-absence provisioning test so the offline fakes do not mask this production failure.
- Both routed and legacy entry points consult the same persisted preview state before mandatory reconciliation.

### 3.3 Preserve the two-turn repair path

- On initial build/revision smoke failure, persist the terminal state before returning to the dispatcher. The next distinct Telegram event must not spend another turn retrying the same artifact before handling `REVISE`.
- If the user explicitly sends `REVISE` while a transient/ambiguous smoke retry is pending, supersede that same-operation retry and start the new revision; do not make the user exhaust three old-operation turns first. No old project/deployment mutation is repeated.
- A `PREVIEW_READY` project with a terminal failed intent and no shown preview should offer intake/revision recovery, not approval/publish; `PromotionOrchestrator` must still reject it through the existing no-shown-preview guard.
- A new revision clears only the failed current intent, increments source revision, builds new bytes, reruns self-contained validation and QA, computes a new operation/deployment, and delivers only after smoke passes.
- If a revision itself reaches QA and then fails preview smoke, do not send it through generic `RevisionOrchestrator._fail()` into `FAILED`. Finalize the source/QA revision identity, keep the project `PREVIEW_READY` with the same recoverable failed-preview state, and allow the next normal revision reservation. This fixes the concrete `FAILED -> READY` with `source_revision>0`/`qa_revision>0` wedge.
- In `app/core/intake.py`, add a fail-closed guard so a FAILED post-QA project is never moved to READY unless the canonical initial-build reset conditions all hold.

### 3.4 One meaningful Telegram status

- Add one runtime helper that atomically claims the failure-status delivery for `(project_id, operation_id, terminal_classification)` before sending.
- Use it for initial build failure, revision failure, and restart recovery; suppress the generic `SMOKE_FAILED` reply from `_handle_intake` when this structured status owns the response.
- `PENDING` or confirmed-sent outcomes are not resent. A confirmed provider failure is logged with its stable code.
- Artifact status says the preview was withheld and a revision will build a new local/tested snapshot. Transient status reports bounded attempt `n/3`; terminal infrastructure status says no link was shared and reconciliation is required. Do not expose host/path/provider payloads to Telegram.

### 3.5 Legacy p7 migration and restart

Existing p7 has `stage="smoked"` despite failures and no structured records. On first load:

- If `smoke.failures` is non-empty, atomically normalize to `stage="smoke_failed"`, classification `ambiguous_legacy`, and a terminal same-operation marker. Do not infer Google Fonts, do not auto-retry the old bytes, and do not send a historical duplicate status automatically.
- If there are no smoke failures, treat `smoked` as a smoke pass and reconcile only the remaining delivery identity.
- A new revision replaces the whole intent with the new operation and clears all legacy attempt/classification fields.
- Add process-restart tests before and after each durable write: intent creation, deployment identity, smoke-running, retry-wait, smoke-failed, failure-status PENDING/SENT, and delivery PENDING/SENT.

## 4. Sibling audit boundaries

Apply only reproductions proven by tests:

- **Slug/project/bypass binding:** downstream ownership checks already propagate the trusted expected slug/name. Concrete liveness/duplicate bugs remain: (1) a transient `slug_for` read failure is swallowed and downgraded to the opaque legacy name after `project_create_attempted=True`; (2) a legacy project with no `vercel_slug` is assigned a friendly candidate before first proving whether the existing opaque project already exists, which can create a second Vercel project; (3) runtime's local bypass-existence check uses an opaque name while `BypassProvisioner` stores/reads by remote project ID. Persist one trusted binding before the first project side effect; for migration, reconcile the existing opaque identity before deriving a friendly slug; use the same remote project ID for bypass storage/lookup; after a slug-bound project is attempted, retry the persisted read-only binding rather than changing identity. Test a one-turn resolver read failure, a legacy owned opaque project, and a friendly remote project through shown-preview redrive.
- **Dispatch claims/replay:** preserve action-claim ownership; add the smoke-status subclaim and verify a replay of the same update cannot send or mutate twice.
- **Read-after-write:** retain operation/deployment lookup before retry and complete identity checks before promotion. For an ambiguous project-create or bootstrap result, perform bounded read-after-write checks by the persisted app/slug/operation identity before any second POST; a briefly stale 404 must not trigger a duplicate mutation. Ambiguous create/deploy/bypass outcomes remain lookup-only.
- **Failed revision:** fix the concrete post-QA preview-failure state as above; do not broaden lifecycle transitions. Also close the post-delivery/pre-`revision_seq` crash window idempotently: if the current reservation's tested preview is already shown, re-entry finalizes that reservation instead of returning `REVISION_NOT_RESERVED` or sending again.
- **Custom-domain smoke composition and the inline-style scanner gap:** record as unrelated findings unless a changed test ties them to this incident; do not include speculative fixes in this task.

# Failure matrix to encode in tests

| Capture | Stable category examples | Action class | Recovery |
|---|---|---|---|
| External render-critical request | `blocked_request/off_origin_or_method` | artifact defect | no same-byte retry; new revision/local asset |
| Same-origin `/` redirect | none | pass | existing rule |
| Unsafe/path/query/cross-origin redirect | `navigation_redirect/unsafe_redirect` | artifact/security | new revision |
| HTTP 404/410 render-critical asset | `http_error` | artifact defect | new revision |
| HTTP 429/5xx | `http_error` | transient | bounded same-operation retry |
| Console/page error correlated to blocked request | secondary record | inherit primary | no extra repair attempt |
| Uncorrelated console/page error | `console_error`/`page_error` | artifact or ambiguous | evidence-driven; retry only ambiguous |
| Broken/empty rendered page | `broken_image`/`empty_page` | artifact unless secondary to block | new revision |
| Networkidle timeout without a blocked cause | `networkidle_timeout` | transient | bounded same-operation retry |
| Browser launch/driver exception | `browser_exception` | transient/ambiguous | bounded same-operation retry |
| Vercel 401/403/login wall | `auth_wall` | provider security policy | reconcile/reprovision bypass once; no deployment duplicate |
| Screenshot exception/absent PNG | `missing_screenshot` | transient | bounded retry; never show |
| Project/deployment/bypass identity unknown | reconciliation error | ambiguous remote | fail closed; lookup by identity only |
| Telegram outcome unknown | delivery PENDING | ambiguous delivery | no resend |

# Regression tests

## Smoke unit/integration tests

Update/add behavior tests in `tests/test_preview_adapters.py`, `tests/test_smoke_bypass.py`, and a focused `tests/test_r1_smoke_failure_matrix.py`:

1. Exact p7 captured category once evidence exists: category/rule/method/resource type/host/path/viewport, no query.
2. The blocked request causes correlated request-failed/console records rather than three independent primary defects.
3. Secrets in query, userinfo, headers, exception text, path edge cases, and long console content never appear in records, state, result data, or caplog.
4. Same-origin `/` redirect passes; unsafe redirects and private/off-origin/non-GET requests fail.
5. 404 asset, 429/5xx, broken image, empty body, uncorrelated console/page error, networkidle timeout, browser launch exception, auth wall, and missing screenshot each produce the matrix classification.
6. More than 24 secondary failures cannot hide a later deterministic primary.
7. Existing bypass leakage and trusted-origin tests remain green.

## Build/self-contained tests

Update `tests/test_self_contained.py`, `tests/test_build_self_contained.py`, and relevant build tests:

- The exact captured external asset is rejected before QA and, when supported, emitted locally with no runtime external reference.
- A vendor timeout/DNS/429/5xx does not invoke FRONTEND/QA repair; a deterministic unsupported dependency does.
- `BuildResult.diagnostics` survives preview failure without full URLs/secrets.

## Orchestrator/state tests

Update `tests/test_preview_orchestrator.py`, `tests/test_r1_phase_g_full_story.py`, and revision crash tests:

- Artifact smoke failure: one deployment, no photo/text preview, terminal failed intent, structured BuildResult/RevisionResult, and same-op direct retry is a no-op.
- A preview failure before any Vercel call leaves `reached_remote=False` and the dispatch claim retryable; a post-boundary failure is reconciled by persisted operation identity.
- Transient smoke failures then pass: same operation/deployment, three-attempt bound, injected backoff, no duplicate Vercel mutation.
- Exhausted transient retry: terminal status once, no endless pre-turn retry, failed preview cannot be approved/promoted.
- Auth wall: identity lookup, at most one bypass reprovision, same deployment, ambiguous reprovision fails closed.
- Fresh real `VercelAdapter` + empty `BypassSecretStore` provisions the first bypass exactly once when the authoritative read explicitly reports no secret; friendly projects use the remote project ID as the local key.
- Hard identity/claim errors stay fail-closed and never become smoke retry counters.
- New revision after artifact failure: new source/artifact/operation/deployment and successful smoke.
- Revision preview smoke failure remains `PREVIEW_READY`/recoverable and a following revision succeeds.
- Friendly-slug binding survives a one-turn resolver read failure without an opaque-name lookup or ownership ambiguity, and shown-preview redrive finds the bypass secret by the same remote project ID without a second reconciliation mutation.
- A legacy opaque project is found before any friendly project POST; an ambiguous create/bootstrap response plus a temporarily stale 404 performs read-after-write reconciliation, not a second mutation.
- A crash after successful preview delivery but before revision finalization is re-driven successfully, with no second deployment/send.
- Process restart with each persisted intent stage and with legacy p7 `stage="smoked"` + generic failures. Reconstruct the state store, registry, preview/deploy collaborators, dispatcher, and receive loop from the same temp files; replacing only the Telegram loop is not a process-restart test.

## Two consecutive Telegram turns

Complete the untracked `tests/test_r1_smoke_failure_recovery.py` (using `_scratch_loop2.py` only as a reference), parameterized over routed and legacy entry points:

1. Turn 1: a new project build reaches deterministic preview smoke failure; assert exact deployment/bootstrap/bypass/smoke counts, one failure status, and no preview photo/text.
2. Restart all components from the persisted temp state, not just the Telegram loop.
3. Turn 2: a real `REVISE` intent builds revised content and passes smoke; assert one new operation/deployment, no old-op smoke call, exactly one final photo/text delivery, and unchanged remote-project/bypass identity.
4. Replay either update and assert only the existing claim/state result is returned.
5. Assert every action result (`BuildResult`, `RevisionResult`, and revision re-drive) explicitly; the current post-delivery crash test can pass while leaving the reservation unapplied.

# Validation commands

Run serially because this repository's full Python suite is memory-heavy on Windows:

```bash
bash scripts/run_tests.sh website-builder/tests/test_preview_adapters.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_smoke_bypass.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_build_self_contained.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_self_contained.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_preview_orchestrator.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_r1_smoke_failure_matrix.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_r1_smoke_failure_recovery.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_r1_failure_matrix.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_r1_revision_crash.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_r1_slug_bind.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_revise.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_r1_phase_g_full_story.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_intake_dispatch_integration.py -j 1
bash scripts/run_tests.sh website-builder/tests/test_bypass_leakage.py -j 1
bash scripts/run_tests.sh website-builder/tests -j 1
python -m ruff check <changed-python-files>
git diff --check
```

## VPS acceptance and final report

After local tests:

1. Deploy the candidate runtime without changing p7.
2. Re-smoke deployment `dpl_Fy9T5eMvWRY2pAGZcfvTRGpCEMwe` read-only and capture the sanitized exact request from both viewports.
3. Apply/use the proven cause fix through one explicit p7 revision (no publish).
4. Verify a new tested snapshot/operation/deployment, local self-contained output, successful smoke, and exactly one final preview delivery.
5. Report separately:
   - confirmed root causes;
   - rejected hypotheses (old asyncio, current auth wall, and any synthetic resource not observed);
   - exact changed files/behavior;
   - tests and outcomes;
   - any case still lacking VPS evidence.

Do not state that every smoke error is fixed. The minimum completion bar is the exact p7 blocked request proven, its narrow cause fixed, two consecutive Telegram turns recovering, and no duplicate remote/delivery side effects.
