# Vercel Promotion Parity + p9 Recovery — Website Builder R1

Scope: `website-builder/` only. No redesign of the deployment architecture. No runtime dependency on the Vercel CLI (CLI was used only as the behavioral oracle). No commit / push / deploy.

## 1. The parity gap (verified, not guessed)

Sources: `vercel/vercel@main` `packages/cli/src/commands/promote/request-promote.ts` and `.../promote/status.ts`; Vercel REST reference `rest-api/projects/point-production-traffic-to-a-given-deployment`, `rest-api/deployments/create-a-new-deployment`, `rest-api/projects/find-a-project-by-id-or-name`.

### Provider contract

| Fact | Source |
|---|---|
| `POST /v10/projects/{projectId}/promote/{deploymentId}` is an **alias remap**, "does NOT rebuild the deployment" | REST ref |
| Its only success statuses are **201 and 202**. 400/401/403/409/410/422 are errors. No 200, no 204, no body | REST ref |
| **202 = queued**, not complete ("Promotion has been queued and will begin when the active rolling release completes") | CLI source |
| `POST /v13/deployments` with `deploymentId` + `target` is a **redeploy**: "The redeployment gets a new ID, URL, and build" | REST ref |
| `meta` on a redeploy is **caller-supplied** (the CLI sends `{action:'promote'}` / `{action:'redeploy'}`); it is not documented as inherited from the source deployment | CLI source + REST ref |
| Promotion is **asynchronous**. Progress lives on `GET /v9/projects/{idOrName}` → `lastAliasRequest {fromDeploymentId, fromRollingReleaseId, jobStatus, requestedAt, toDeploymentId, type}` with `jobStatus ∈ {pending, in-progress, succeeded, failed, skipped}` | CLI `status.ts` + REST ref |
| A CLI promote timeout **does not stop** the remote promotion: "it does not affect the actual promotion which will continue to proceed" | `docs/cli/promote` |
| CLI routes on the deployment's own `target`: `target == 'production'` → alias remap; anything else (preview) → **promote-by-creation**, new deployment id, returns immediately | CLI `request-promote.ts` |
| The CLI **never** asserts `deployment.target == 'production'` after promoting. Its success proof is `lastAliasRequest.jobStatus === 'succeeded'` | CLI `status.ts` |
| Authoritative production binding is the project's `targets.production.id` (not the deployment's `target` field). Deployment objects separately carry `aliasAssigned`, `aliasFinal`, `readySubstate` | REST ref |

### Four defects in Hermes

- **D1 — wrong mechanism.** `VercelAdapter.promote_deployment` (`app/deploy/adapters.py:932`) unconditionally POSTs the alias-remap endpoint. Website Builder's content deployments are preview-target (`deploy_static_files` omits `target`; `test_preview_adapters.py:94` asserts this), and the alias-remap path is exactly the one the provider does not serve for previews → deterministic 4xx → `PROMOTE_RECONCILIATION_REQUIRED`.
- **D2 — wrong accepted status set.** `adapters.py:958` accepts `{200, 201, 204}`. The provider documents `{201, 202, …}`. A **202 (queued)** is read as a failure. This also breaks the rollback path, which is a genuine alias remap.
- **D3 — success condition asserts an unguaranteed field.** `adapters.py:966` (`promote_deployment`) and `adapters.py:1112` (`check_domain_production`) require `deployment.target == 'production'`. That field is not part of the promotion contract; alias remap binds `targets.production` without rewriting the deployment's own `target`.
- **D4 — no bounded confirmation, no job awareness.** `reconcile_production_deployment` (`adapters.py:1279`) performs exactly **one** binding read and returns a conclusive `NOT_PROMOTED` whenever the binding is not the intended id. `promote.py:620-626` then converts that into a terminal `PROMOTE_NOT_APPLIED`. `lastAliasRequest` is referenced **nowhere** in `website-builder/` (grep: 0 hits). D1+D2+D4 together turn an accepted, still-in-flight promotion into a definitive "not promoted".

### Net effect on p9

`POST` → 4xx-or-202 → single immediate read → binding still the bootstrap → `NOT_PROMOTED` → `PROMOTE_NOT_APPLIED` at `promote.py:623`. Exactly the observed log sequence. The manual `vercel promote dpl_5mGK9GNBbqoPtbGqCEDcfiJb5yNT --yes` took the **promote-by-creation** branch (the deployment is preview-target), so the current remote production is very likely a **different deployment id** carrying only `meta: {action:'promote'}` and no `wbOperation/wbRevision/wbArtifact`. See §6.

## 2. Code changes

### 2.1 `app/deploy/adapters.py` — `VercelAdapter`

Add:
- `_promotion_state(project)` → one `GET /v9/projects/{name}` returning `(binding_id, last_alias_request, project_body)`. Raises on any malformed shape.
- `_classify_alias_job(record, expected_to_id, now, recent_window)` → `IN_FLIGHT | SUCCEEDED | FAILED | ABSENT | UNKNOWN`, mirroring the CLI algorithm (needs `jobStatus`, `requestedAt`, `toDeploymentId`; `type == 'promote'` for the failure branch).
- `confirm_production_promotion(app_id, project, expected_identity, *, polls=12, interval=2.0, sleep_fn=time.sleep, now=time.time, expected_name=None)` — **bounded read-only** loop, 60 s budget (12 polls × 2 s, per decision). Per poll: project GET → binding id.
  - binding == `expected_identity['deployment_id']` → GET that deployment, revalidate `id`, `projectId`, `teamId`, and the full `wbOwner/wbOperation/wbRevision/wbArtifact` meta, plus `readyState == 'READY'` → `ok({'status':'PROMOTED','deployment_id','production_url'})`.
  - else classify the job: `FAILED` **with `toDeploymentId == expected id`** → `ok({'status':'PROMOTED_FAILED'})`. `IN_FLIGHT`/`ABSENT`/`UNKNOWN` → sleep, continue.
  - exhausted, or any malformed/ambiguous shape → `fail('PROMOTION_RECONCILIATION_REQUIRED')`.
  - **Never** returns `NOT_PROMOTED`. A stale binding is not evidence of anything.
- `reconcile_external_promotion(app_id, project, intended_identity, *, expected_name=None)` — read-only, for the p9 recovery branch. `ok({'status':'PROMOTED','production_url', 'promoted_deployment_id'})` when either:
  - binding == `intended deployment_id` and its wb* meta matches exactly; **or**
  - binding != `intended deployment_id` **and** `lastAliasRequest.type == 'promote'` **and** `fromDeploymentId == intended deployment_id` **and** `toDeploymentId == binding id`, and that deployment is `READY` and `aliasAssigned` — provider-attested lineage.
  `ok({'status':'PROMOTED_UNPROVEN'})` when production exists but neither proof holds. `fail('PROMOTION_RECONCILIATION_REQUIRED')` when truth cannot be read. Zero mutations.

Change `promote_deployment` into a CLI-parity router, branching on the deployment's own `target` (read via the existing pre-promote identity GET, `adapters.py:950`):
- `target == 'production'` → `_promote_by_alias_remap`: existing endpoint, **accept `{201, 202}`** (keep 200/204 tolerated), then `confirm_production_promotion`.
- `target != 'production'` → `_promote_by_creation`: `POST /v13/deployments` with `{'name': project['name'], 'project': project['id'], 'deploymentId': deployment_id, 'target': 'production', 'meta': {'action':'promote', **meta}}`. **Do not send `files`/`builds`** — those are inherited from the source deployment and re-sending them risks a divergent artifact. Accept `{200, 201}`; require a well-formed new `id`; boundedly poll the child to `readyState == 'READY'`; then `confirm_production_promotion` against the **child's** identity.
  - Deterministic 4xx/401/403/422 on the create → `fail('PROMOTED_REJECTED')` (terminal, conclusive).
  - `409` → reconcile read-only, never a second create.
  - Returns the promoted identity so the orchestrator can persist it.

Other adapter changes:
- `find_production_deployment` (`adapters.py:1184`): make the project's `targets.production.id` the **primary** read instead of `GET /v6/deployments?target=production&limit=1`, so a deployment promoted while its own `target` stayed preview is still found. Keep the `_authoritative_deployment_meta` re-read and `_is_proven_bootstrap` proof unchanged.
- `reconcile_production_deployment` (`adapters.py:1279`): keep signature and result shape for existing callers, but `NOT_PROMOTED` now requires **positive** evidence of a conclusive negative (`lastAliasRequest.jobStatus == 'failed'` for our id). Absent/pending job, unreadable project, or incomplete meta → `fail('PROMOTION_RECONCILIATION_REQUIRED')`.
- `check_domain_production` (`adapters.py:1096`): **drop** the `body.get('target') != 'production'` term (`:1112`). Keep id, `projectId`, `teamId`, `name`, `readyState`, and full meta.

### 2.2 `app/projects/promote.py` — `PromotionOrchestrator`

- **Widen the resume gate** (`promote.py:344-347`): `is_resume` is also true when `lifecycle == FAILED` **and** `promotion_intent.operation_id == approval.operation_id` **and** `"previous_production" in promotion_intent` **and** `failure.phase == 'promotion'`. Under the writer lock, transition `FAILED → PUBLISHING` and reuse the persisted `previous_production` verbatim (never recompute the rollback target). Requires the `FAILED → PUBLISHING` edge in `app/core/lifecycle.py` — **check the transition table; add the edge + a table test in `tests/test_core.py` if it is currently illegal.**
- **New public `resume_publish(project_id, workspace, principal_id=None, reference_token=None)`** — the operator entry point. Validates the resume preconditions, requires **owner** auth via `require_owner_role`, acquires the `ProjectRunner` slot, then delegates to `_promote_authorized`. Returns a distinct `RESUME_NOT_APPLICABLE` when preconditions do not hold, so a first publish can never be confused with a recovery.
- **Recovery ordering in `_promote_authorized`** (before the existing `is_same_operation` reconcile at `:546`): when `is_resume`, first call `reconcile_external_promotion`:
  - `PROMOTED` → `_update_intent(stage='promoted', promoted_deployment_id=…, production_url=…)` → `_post_promote(..., reconciled=True)` (production smoke, then LIVE). **Zero promote POSTs.**
  - `PROMOTED_UNPROVEN` → `_fail(project_id, 'PROMOTE_FAILED', 'PROMOTION_IDENTITY_UNPROVEN')`; `promotion_intent` left intact. No mutation of remote state.
  - `fail(...)` → existing `_fail_reconciliation_required(...)` (non-terminal, still resumable).
  - Only a conclusive "not promoted at this binding" falls through to the promote POST.
- **Persist the promoted identity**: when promote-by-creation is used, add `promoted_deployment_id` + `promoted_identity` (the same four fields) to `promotion_intent` and use it as the post-promote `intended_identity`. `deployment_id` keeps the approved preview id for provenance and rollback.
- **Post-promote classification** (replacing `promote.py:592-640`):
  - `PROMOTED_REJECTED` → `_fail('PROMOTE_FAILED','PROMOTED_REJECTED')`, terminal.
  - `PROMOTED_FAILED` (conclusive provider job failure only) → `_fail('PROMOTE_FAILED','PROMOTE_NOT_APPLIED')`, terminal.
  - `PROMOTION_RECONCILIATION_REQUIRED` → `_fail_reconciliation_required(...)`, **never** `PROMOTE_NOT_APPLIED`. Lifecycle stays `PUBLISHING`, intent intact, resumable (the remote job is still running).
- **Rollback** (`_rollback_and_fail`, `:908`) is unchanged in policy: exactly one attempt, re-promoting the previous deployment with **its own** identity. It now routes through the alias-remap branch (previous production is `target == 'production'`) and accepts 201/202.
- **Production smoke URL is unchanged**: keep `_production_url_for` (`https://<deployment_id>.vercel.app`) as the smoke target. Binding equality already proves alias routing, and this keeps every existing test and the rollback contract intact. Reading the canonical production alias host from `project.alias[]` is a documented follow-up, explicitly out of scope here.

### 2.3 `app/runtime.py`
- `ERROR_MESSAGES`: add `PROMOTED_REJECTED` and `PROMOTION_IDENTITY_UNPROVEN` with deliberate Indonesian copy in the same register as `PROMOTE_NOT_APPLIED` — never claim success, never leak the code. `PROMOTION_RECONCILIATION_REQUIRED` and `PROMOTE_NOT_APPLIED` entries stay.
- `main()`: add `--reconcile-publish <project_id> --as <principal_id>`. Placed after `compose()` and after `reconcile_stranded_projects` (unchanged), **before** the receive loop. Resolves the workspace via the same `workspace_for` the dispatcher uses, calls `composition.promote.resume_publish(...)`, logs the structured outcome, returns 0/1, and exits without starting the Telegram loop. `--as` must equal `state.owner_id`; mismatch fails closed. No Telegram routing change (`_LIFECYCLE_INTENTS` untouched) — the recovery is operator-only, so no new user-reachable state machine.

## 3. Tests

New file `website-builder/tests/test_promote_parity.py` (fake `Transport` + fake deps, no network, no credentials, `sleep_fn` injected so no real time passes):

1. **Official promote-success shapes/statuses** — alias remap 201 and 202 both accepted; create 200/201 accepted; 202 is *queued → confirm*, never a failure.
2. **Binding moves while `deployment.target` stays preview** → `PROMOTED`. Proves D3 removal.
3. **Eventual-consistency confirmation** — stale binding on polls 1..N, intended binding at N+1 → `PROMOTED`.
4. **Old binding during early polls is not a final failure** — assert `PROMOTE_NOT_APPLIED` never appears and that more than one poll was made.
5. **Timeout stays fail-closed** — 12 stale polls → `PROMOTION_RECONCILIATION_REQUIRED`; lifecycle and `promotion_intent` byte-unchanged.
6. **Exact p9 scenario** — seed the state file with the user's literal values: `lifecycle=FAILED`, `failure.error_code=PROMOTE_NOT_APPLIED`, `promotion_intent.stage=publishing`, `operation_id=a4e8c7867fb0a14c831815d167fb8a5079efb1e7251b5aec8e153d4dc5c6cf50`, `deployment_id=dpl_5mGK9GNLbqoPtbGqCEDcfiJb5yNT`, `previous_production_class=KNOWN_BOOTSTRAP`, `production_url=null`; remote already promoted → reconcile → production smoke → `LIVE`, with **zero POST calls recorded on the transport** (both alias-remap branch and lineage branch).
7. **Wrong remote deployment → no adoption** — binding is an unrelated id → `PROMOTION_IDENTITY_UNPROVEN`, lifecycle still `FAILED`, zero POSTs.
8. **Incomplete metadata → fail closed** — child deployment with `meta={'action':'promote'}` and no `lastAliasRequest` lineage → `PROMOTION_IDENTITY_UNPROVEN`, zero POSTs.
9. **Duplicate/retry issues no second promote** — call `resume_publish` twice; total promote POSTs across both runs == 0; second run is the existing LIVE idempotent no-op (`promote.py:383-394`).

Updates to existing suites:
- `tests/test_promote.py` (~798, ~861): `PROMOTE_NOT_APPLIED` now only for a conclusive provider job failure; add cases for `PROMOTED_REJECTED` and for "ambiguous → `PROMOTION_RECONCILIATION_REQUIRED`, state untouched".
- `tests/test_preview_adapters.py` reconcile matrix + `test_deployment_target_production_*`: updated for the dropped `target` requirement and the binding-primary `find_production_deployment`.
- `tests/test_publish_error_copy.py` + `tests/test_crash_recovery.py`: add the two new codes to the `ERROR_MESSAGES` coverage contract.
- `tests/test_promote_operator_logging.py`: pin the new boundary logs (rejected / job-failed / confirm-timeout / resume-adopted) and assert no token, `Authorization`, or protected preview URL leaks.
- `tests/r1_harness.py`: fake `promote_deployment` / `reconcile_external_promotion` updated to the new result shapes.

## 4. Validation

Memory constraint: the full suite must run serially (`full_suite_memory_risk`, `test_runner_workers`).

```bash
HERMES_TEST_WORKERS=1 bash scripts/run_tests.sh \
  website-builder/tests/test_promote_parity.py \
  website-builder/tests/test_promote.py \
  website-builder/tests/test_promote_bootstrap_classification.py \
  website-builder/tests/test_preview_adapters.py \
  website-builder/tests/test_crash_recovery.py \
  website-builder/tests/test_publish_error_copy.py \
  website-builder/tests/test_promote_operator_logging.py \
  -q --file-retries=0

HERMES_TEST_WORKERS=1 bash scripts/run_tests.sh website-builder/tests -q --file-retries=0
```

No real Vercel or Telegram mutation: every test drives the existing fake `Transport` (`test_preview_adapters.py:22`) and fake deps. No live credentials anywhere in the suite.

## 5. p9 recovery procedure

**Step 1 — read-only preflight (no mutation).** Determine which §6 branch p9 is actually in before touching anything: read the project record, then read `GET /v9/projects/{name}` and `GET /v13/deployments/{current production id}`. Record: production binding id, that deployment's `target`, `readyState`, `aliasAssigned`, and `meta`, plus `lastAliasRequest`. Log only ids/states — never the token or the protected preview URL.

**Step 2 — reconcile, zero promote POSTs.**

```bash
python -m app --reconcile-publish p9 --as <owner_id_from_state>
```

- Branch 1 or 2 → expect `LIVE`, `production_url` set, `last_live_deployment` written, production smoke screenshots under `qa/production_smoke/`, and **0 promote POSTs**.
- Branch 3 → the command fails closed with `PROMOTION_IDENTITY_UNPROVEN` and leaves `FAILED` + intent intact. Nothing remote is touched. Report this to the user; do not work around it.

**Step 3 — verify.** `lifecycle=LIVE`, `promotion_intent.stage=live`, `production_url` is a `*.vercel.app` origin, `failure is None`, and the alias host serves the approved artifact.

## 6. Expected p9 outcome — say this up front

Because the manual `vercel promote <preview-id> --yes` took the **promote-by-creation** branch, the remote production is most likely a **new deployment id** whose meta is only `{action: 'promote'}` (the CLI sends exactly that, and the REST reference does not document meta as inherited). If Vercel also recorded no `lastAliasRequest` lineage for it, p9 lands in **branch 3** and **cannot** be safely adopted: there is no provider truth tying that deployment to the approved artifact identity, and the user's own rule is "adopt only if the exact trusted identity matches". In that case the honest answer is that p9 is not reconcilable, and the remedy is a fresh **p10** publish using the now-correct Hermes-native promote-by-creation, which stamps `wbOwner/wbOperation/wbRevision/wbArtifact` onto the new production deployment so it is independently identity-verifiable and re-promotable for rollback.

**p10 is recommended for fresh end-to-end validation regardless** — it is the only way to exercise the new promote path, the bounded confirmation, and production smoke against a real provider.

## 7. Risks

- Promote-by-creation changes what goes LIVE: `last_live_deployment.deployment_id` becomes the child id, not the approved preview id. The preview id is retained in `promotion_intent.deployment_id` for provenance and rollback. Audit every reader of `last_live_deployment` for an assumption that it equals the preview id.
- Two promote mechanisms now exist (alias remap for `target == 'production'`, promote-by-creation otherwise), both funnelling into one `confirm_production_promotion`. Keep identity validation in exactly one place.
- `check_domain_production` is used by the custom-domain flow; relaxing `target` there widens what counts as production. It is correct (the field was never the binding) but must keep the full meta revalidation.
- Dropping `target == 'production'` from `promote_deployment` means a deployment whose binding never moved can no longer be reported as "promote attempted but not applied" by field inspection alone. The binding equality + bounded loop replaces that proof, which is strictly stronger.

## 8. Out of scope

- Vercel CLI as a runtime dependency (oracle only).
- Canonical production-alias-host smoke target (`project.alias[]`) — deliberate, see §2.2.
- Telegram-facing "retry publish" for `FAILED` projects.
- Rolling-release-gated promotion handling (the CLI's `rollingRelease` branch) — this project does not enable rolling releases; if `project.rollingRelease` is ever truthy, fail closed rather than guess.
