# Production Canonical-URL Resolution & Smoke Target Plan

## 1. Root Cause Analysis

### 1.1 The Exact Failure Mechanism
In `website-builder/app/deploy/adapters.py` (`canonical_production_url` at line 1463):
1. The adapter attempts to verify whether `<project_name>.vercel.app` is an active verified domain using `_default_domain_verified(name, project)` (lines 1498-1502, 1530-1543).
2. `_default_domain_verified` queries `GET /v9/projects/{name}/domains` and searches strictly for `entry.get('name') == name + '.vercel.app'`.
3. In Vercel, project names are unique per team/account, but `*.vercel.app` subdomains are globally unique across all of Vercel. When a project named `testbakery` is created in an account and `testbakery.vercel.app` is already claimed elsewhere, Vercel auto-assigns an available suffixed default domain such as `testbakery-eight.vercel.app`.
4. Vercel registers `testbakery-eight.vercel.app` in `GET /v9/projects/{name}/domains` (and in project/deployment aliases) with `verified: true`.
5. Because `entry.get('name')` (`testbakery-eight.vercel.app`) does not equal `testbakery.vercel.app`, `_default_domain_verified` returned `False` (`proven = False`).
6. Line 1502:
   ```python
   canonical_source = 'VERCEL_PROJECT_DOMAIN' if proven else 'VERIFIED_PROJECT_NAME'
   ```
   Because `proven` was `False`, it fell back to `source=VERIFIED_PROJECT_NAME` and synthesized `url = f'https://{name}.vercel.app/'` (`https://testbakery.vercel.app/`).
7. Line 1459 in `website-builder/app/projects/promote.py` logged:
   `Canonical production URL resolved project=... source=VERIFIED_PROJECT_NAME url=https://testbakery.vercel.app/ host=testbakery.vercel.app`
8. Production smoke testing then executed against `testbakery.vercel.app` (which was either non-existent or pointing to an unrelated site), failing with `empty_or_broken_image`.

---

## 2. Core Decisions & Principles

1. **Single Durable Authority for Canonical Production Identity**:
   - `promotion_intent` is the ONLY authoritative record for canonical production identity, tied directly to `operation_id` and `deployment_id`.
   - `pending_publication` is not an independent source of truth for canonical production identity.

2. **Single Error Code End-to-End**:
   - Use `CANONICAL_PRODUCTION_URL_UNRESOLVED` consistently across `adapters.py`, `promote.py`, `runtime.py`, and all test fixtures.
   - Eliminate `CANONICAL_URL_UNRESOLVED` to prevent code split and ambiguous classifications.

3. **Strict, Tiered Alias Selection (No Speculative Sorting)**:
   - Authority order:
     a. Alias explicitly attached to the exact promoted `deployment_id` (`GET /v13/deployments/{deployment_id}` -> `alias` list).
     b. Project production alias explicitly proven to target that same deployment and project (`GET /v9/projects/{name}` -> `alias` entry with `target == 'PRODUCTION'` and `deployment.id == deployment_id`).
     c. Verified project domain only when Vercel provider data explicitly proves it is the production target.
   - Never pick among unrelated verified `.vercel.app` domains by lexicographic sort.
   - Deterministic sorting (lexicographical) is ONLY permitted to break ties among already-proven equivalent production aliases targeting this exact deployment/project.
   - If binding to the intended deployment cannot be proven, fail closed with `CANONICAL_PRODUCTION_URL_UNRESOLVED`.

4. **Strict Interface Without Compatibility Fallback**:
   - No fallback in `_resolve_canonical` that retries without `expected_deployment_id`.
   - No catching `TypeError` to tolerate outdated test doubles, avoiding masking real `TypeError`s inside provider logic.
   - Update every test double/fake to the strict signature `canonical_production_url(app_id, project, *, expected_name=None, expected_deployment_id=None)`.

5. **Accurate User-Facing Failure Copy**:
   - Add `PRODUCTION_SMOKE_FAILED` to `ERROR_MESSAGES` in `runtime.py` with truthful semantics:
     `"Website sudah berhasil dipromosikan ke production, tapi pemeriksaan akhir di alamat production belum lolos. Preview kamu tetap aman. Status production belum akan ditandai selesai sampai pemeriksaan ini berhasil."`
   - Does not claim background human/team monitoring.
   - Does not claim rollback happened or assert unverified safety properties.

6. **Precise Recovery Semantics**:
   - Reconcile exact deployment -> resolve verified actual production host -> run smoke against that host -> only if smoke passes, transition LIVE.
   - Never claim production smoke "will pass" without execution.

---

## 3. Exact Files Changed

1. `website-builder/app/deploy/adapters.py`:
   - Update `VercelAdapter.canonical_production_url` signature to strictly accept `expected_deployment_id=None`.
   - Query Vercel provider truth for the exact promoted `deployment_id` (deployment aliases and project aliases where `target == 'PRODUCTION'` and `deployment.id == deployment_id`).
   - Filter candidate aliases to valid `*.vercel.app` hostnames (preserving R1 decision 3: default production domain, no custom domain system).
   - If multiple proven equivalent aliases exist for the deployment, break ties deterministically (lexicographically).
   - Replace `CANONICAL_URL_UNRESOLVED` with `CANONICAL_PRODUCTION_URL_UNRESOLVED`.
   - Remove the `VERIFIED_PROJECT_NAME` synthesis fallback entirely. Fail closed when no verified production alias is proven.

2. `website-builder/app/projects/promote.py`:
   - In `_resolve_canonical`: strictly call `helper(app_id, vercel_project, expected_name=expected_name, expected_deployment_id=deployment_id)`. No fallback retry without `expected_deployment_id`. No masking `TypeError`.
   - In `_post_promote`: persist resolved canonical URL identity (`canonical_production_url`, `canonical_source`, `canonical_host`, `canonical_deployment_id`) into `promotion_intent` via `self._update_intent(...)`.
   - On re-drive / resume at `STAGE_PRODUCTION_CONFIRMED`: check if `promotion_intent` already contains `canonical_production_url` for the matching `deployment_id`. If so, reuse that verified target.
   - When production smoke fails, return `error_code="PRODUCTION_SMOKE_FAILED"` so caller and user-facing layer receive the distinct production failure code.

3. `website-builder/app/runtime.py`:
   - In `ERROR_MESSAGES`: add `PRODUCTION_SMOKE_FAILED` with truthful copy:
     `"Website sudah berhasil dipromosikan ke production, tapi pemeriksaan akhir di alamat production belum lolos. Preview kamu tetap aman. Status production belum akan ditandai selesai sampai pemeriksaan ini berhasil."`
   - In `_handle_approve` and `_handle_publish`: handle `PRODUCTION_SMOKE_FAILED` using the new copy instead of the misleading preview smoke copy (`"Preview-nya belum lolos pemeriksaan..."`).

4. Test Files:
   - `website-builder/tests/test_promote.py`: update fakes to strict signature and add regression tests A, B, C, D, E.
   - `website-builder/tests/r1_harness.py`: update `FakeVercel.canonical_production_url` to accept `expected_deployment_id=None` and standardize on `CANONICAL_PRODUCTION_URL_UNRESOLVED`.
   - `website-builder/tests/test_promote_parity.py`: update fakes to match signature and error code.
   - `website-builder/tests/test_r1_live_publish_finishing.py`: update fakes to return `CANONICAL_PRODUCTION_URL_UNRESOLVED` and match signature.
   - `website-builder/tests/test_publish_error_copy.py`: verify copy rendering for `PRODUCTION_SMOKE_FAILED`.

---

## 4. Detailed Specification of Changes

### 4.1 `website-builder/app/deploy/adapters.py`
In `canonical_production_url(self, app_id, project, *, expected_name=None, expected_deployment_id=None)`:
1. `_project_valid(project, app_id, expected_name=expected_name)`: must pass, else `_fail('PROJECT_IDENTITY_MISMATCH')`.
2. Fetch fresh project representation from `/v9/projects/{project['name']}`.
3. Candidate alias collection:
   - **Step A**: If `expected_deployment_id` is provided, fetch deployment body via `/v13/deployments/{expected_deployment_id}` (or inspect `_validated_deployment`). Extract `alias` list if `aliasAssigned` is True (or deployment is READY).
   - **Step B**: Inspect `fresh.get('alias', [])` where `entry.get('environment') == 'production'` or `entry.get('target') == 'PRODUCTION'`. Check if `entry.get('deployment', {}).get('id') == expected_deployment_id`. Collect `entry.get('domain')`.
   - **Step C**: Inspect project domains `/v9/projects/{project['name']}/domains`. Only consider domains where `verified is True` AND provider data explicitly confirms it targets the production branch / environment.
4. Filtering & Validation:
   - Discard domains that do not end in `.vercel.app` (custom domains belong to R2).
   - Discard redirects (`redirect` truthy).
   - Discard any alias not proven to belong to `expected_deployment_id` or this project.
5. Selection:
   - If proven candidates exist: sort lexicographically and select the first.
   - Return:
     ```python
     OperationResult.ok({
         'canonical_production_url': f'https://{selected_host}/',
         'canonical_source': 'VERCEL_PRODUCTION_ALIAS',
         'project_name': name,
         'canonical_host': selected_host,
     })
     ```
6. Fail Closed:
   - If no candidate can be proven to target this deployment/project:
     `return _fail('CANONICAL_PRODUCTION_URL_UNRESOLVED')`

### 4.2 `website-builder/app/projects/promote.py`
In `_resolve_canonical(self, app_id, vercel_project, expected_name, deployment_id=None)`:
- Strictly call:
  ```python
  result = helper(
      app_id, vercel_project,
      expected_name=expected_name,
      expected_deployment_id=deployment_id,
  )
  ```
  No fallback retry without `expected_deployment_id`.
- Validate returned URL has safe origin and valid `.vercel.app` host.

In `_post_promote`:
1. Check `promotion_intent`:
   - If `intent.get("canonical_production_url")` is already recorded and `intent.get("canonical_deployment_id") == intended_identity["deployment_id"]`:
     Reuse the recorded canonical URL data.
   - Else:
     Resolve canonical via `self._resolve_canonical(..., deployment_id=intended_identity["deployment_id"])`.
     If resolution fails:
     `return self._fail_with_rollback_outcome(..., "CANONICAL_PRODUCTION_URL_UNRESOLVED", ...)`
     Persist resolved canonical identity in `promotion_intent`:
     ```python
     self._update_intent(
         project_id,
         intended_identity["operation_id"],
         canonical_production_url=canonical["canonical_production_url"],
         canonical_source=canonical["canonical_source"],
         canonical_host=host,
         canonical_deployment_id=intended_identity["deployment_id"],
     )
     ```
2. Production smoke:
   - Target the resolved `canonical_production_url`.
   - If smoke fails:
     Return `OperationResult.fail("PRODUCTION_SMOKE_FAILED", error_code="PRODUCTION_SMOKE_FAILED", data={...})`.

### 4.3 `website-builder/app/runtime.py`
1. Add to `ERROR_MESSAGES`:
   ```python
   "PRODUCTION_SMOKE_FAILED": (
       "Website sudah berhasil dipromosikan ke production, tapi pemeriksaan "
       "akhir di alamat production belum lolos. Preview kamu tetap aman. "
       "Status production belum akan ditandai selesai sampai pemeriksaan ini berhasil."
   ),
   ```
2. In `_handle_approve` and `_handle_publish`:
   When `result.error_code == "PRODUCTION_SMOKE_FAILED"` or `result.data.get("production_smoke_failed")`:
   Send error reply with `PRODUCTION_SMOKE_FAILED`.

---

## 5. Regression Test Suite

- **Test A (Disjoint Project Name and Actual Production Host)**:
  `project_name = "testbakery"`, Vercel production alias = `"testbakery-eight.vercel.app"`.
  Verify canonical production URL resolves to `https://testbakery-eight.vercel.app/` and production smoke targets `testbakery-eight.vercel.app`, never `testbakery.vercel.app`.

- **Test B (Matching Name and Production Host)**:
  `project_name = "mysite"`, Vercel production alias = `"mysite.vercel.app"`.
  Verify existing behavior remains valid: resolves to `https://mysite.vercel.app/`.

- **Test C (No Verified Production Alias / Fail Closed)**:
  Provider returns no verified production alias for the deployment.
  Verify resolver fails closed with `CANONICAL_PRODUCTION_URL_UNRESOLVED`.
  Verify it never guesses `<project_name>.vercel.app`.

- **Test D (Alias Belonging to Another Project/Deployment Rejected)**:
  Vercel returns an alias pointing to a different `deployment_id` or another project ID.
  Verify resolver rejects it and fails closed with `CANONICAL_PRODUCTION_URL_UNRESOLVED`.

- **Test E (Crash / Re-drive from PRODUCTION_CONFIRMED Reuses Verified Target)**:
  Simulate crash after `STAGE_PRODUCTION_CONFIRMED` and canonical resolution.
  Re-drive via `resume_publish` / `_promote`. Verify it reuses the persisted verified host in `promotion_intent` rather than re-synthesizing from project name.

- **Test F (User Copy Verification)**:
  Verify `render_error_message("PRODUCTION_SMOKE_FAILED")` renders the truthful copy:
  `"Website sudah berhasil dipromosikan ke production, tapi pemeriksaan akhir di alamat production belum lolos. Preview kamu tetap aman. Status production belum akan ditandai selesai sampai pemeriksaan ini berhasil."`
  and never leaks internal codes or makes unverified claims.

---

## 6. Analysis of `tg-6329821361-p16` Re-drive Safety

### Safe Recovery Semantics
Recovery must strictly follow:
1. Reconcile exact deployment: verify from durable state and Vercel provider data that `dpl_AsiNzqieqgw1tVXxmdRSgNWGAFhS` is still the intended production deployment.
2. Resolve verified actual production host: obtain `testbakery-eight.vercel.app` from Vercel provider data.
3. Run smoke against `https://testbakery-eight.vercel.app/`.
4. Only if smoke passes, transition project to `LIVE`.

### Feasibility of Re-drive Without Rebuild/Re-publish
- Git commit `dc901eb103bb0a699f50bc3e407c2d0cf72813e7` is already published to branch `testbakery`.
- Vercel deployment `dpl_AsiNzqieqgw1tVXxmdRSgNWGAFhS` was built and promoted.
- If operator reconciliation confirms the remote Vercel state and local `promotion_intent` match this deployment, it is reasonable to expect no rebuild/re-push is required. However, the system must not assume smoke will pass beforehand—smoke execution on `testbakery-eight.vercel.app` remains the mandatory gate before transitioning `LIVE`.

---

## 7. Verification Plan

1. **Focused tests**:
   - `pytest website-builder/tests/test_promote.py -k "canonical or smoke"`
   - `pytest website-builder/tests/test_publish_error_copy.py`
   - `pytest website-builder/tests/test_promote_parity.py`
   - `pytest website-builder/tests/test_r1_live_publish_finishing.py`

2. **Full test suite**:
   - `pytest website-builder/tests/`
