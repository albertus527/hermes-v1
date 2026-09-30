# p17 — Canonical production URL: class-aware precedence over proven Vercel evidence

## 0. Status

Investigation complete; no source changed. Two open decisions were resolved with the user:

- **Premise first**: add an operator-only **read-only probe** and read the real Vercel payloads before/alongside the patch.
- **Remediation out of scope**: this plan fixes the resolver for future promotions. It does **not** rewrite an already-committed release URL (p17 stays as recorded).

## 1. Findings (investigation report)

### 1.1 Vercel's three URL classes, as actually represented

| Class | Host shape (p17) | Provider representation | Endpoint |
|---|---|---|---|
| 1. immutable deployment URL | `mopsypeyshop-g3bo8i3m6-albert-a121.vercel.app` (`<project>-<hash>-<scope>`) | deployment `url` | `GET /v13/deployments/{id}` |
| 2. deployment-bound production alias | `mopsypeyshop-albert-a121.vercel.app` (`<project>-<scope>`) | deployment `alias: string[]` | `GET /v13/deployments/{id}` |
| 3. project production domain | e.g. `mopsypeyshop.vercel.app` | domain record `{name, apexName, projectId, verified, redirect, gitBranch, customEnvironmentId}` | `GET /v9/projects/{nameOrId}/domains?production=true` |
| 2b. stable production alias | — | `alias[]` entries `{domain, target:'PRODUCTION', environment, deployment{id,...}, redirect}` and `targets.production.{id, alias[]}` | `GET /v9/projects/{name}` |

Documented but **unused** today: `GET /v2/deployments/{id}/aliases` (`{alias, uid, created, redirect}`), `GET /v1/projects/{id}/promote/aliases` (`{alias, id, status}`), deployment `readySubstate` (`PROMOTED` = has served production traffic).

Vercel docs (`/docs/deployments/generated-urls`, last updated 2026-09-08) state the auto **production URL** is `<project-name>-<scope-slug>.vercel.app`; there is no documented auto-assignment of a bare `<project>.vercel.app`. `*.vercel.app` is allocated first-come-first-served and cannot be reserved. **Consequence: `mopsypeyshop.vercel.app` may belong to another team.** The probe (Task 1) settles this.

### 1.2 Current selection algorithm — `website-builder/app/deploy/adapters.py:1463`

1. `_project_valid(project, app_id, expected_name)` ownership gate (`adapters.py:1483`).
2. Validate the project-name regex (`:1486`).
3. Tier (a) `GET /v13/deployments/{expected_deployment_id}` → append every string in `alias` passing `_safe_origin` (`:1493-1506`).
4. Tier (b) **only if (a) produced nothing**: `GET /v9/projects/{name}` → `alias[]` entries with `target=='PRODUCTION'` or `environment=='production'`, no `redirect`, and `deployment.id == expected_deployment_id`; then, still empty, `targets.production.alias[]` gated on `targets.production.id == expected_deployment_id` (`:1509-1543`).
5. Tier (c) **only if (a) and (b) produced nothing**: `GET /v9/projects/{name}/domains?production=true` → `verified is True`, `projectId` matches, no `redirect`, no `gitBranch` (`:1546-1568`).
6. `selected_host = sorted(set(candidates))[0]`; returns `canonical_source='VERCEL_PRODUCTION_ALIAS'` **regardless of which tier won** (`:1573-1581`).
7. Empty → `_fail('CANONICAL_PRODUCTION_URL_UNRESOLVED')`.

Consumer: `promote.py::_resolve_canonical` (`:1599`) passes `expected_deployment_id=intended_identity["deployment_id"]`, validates `https://<label>.vercel.app`, and `_post_promote` persists `canonical_production_url/canonical_source/canonical_host/canonical_deployment_id` into `promotion_intent` (`promote.py:1364-1416`).

### 1.3 Root cause — precedence, plus three secondary defects

1. **Precedence is inverted vs. contract B.** Tier (a) runs first and short-circuits (b) and (c) via `if not candidates`, so a verified project production domain can *never* win. This is the p17 shape.
2. **`canonical_source` is hardcoded** (`:1578`) for every tier, so p17's durable `VERCEL_PRODUCTION_ALIAS` does not identify the evidence class that actually won.
3. **Cross-class lexicographic tie-break** (`:1573-1574`). `'-'` (0x2D) < `'.'` (0x2E), so within one tier `mopsypeyshop-albert-a121.vercel.app` sorts ahead of `mopsypeyshop.vercel.app`. Tie-break must be intra-class only.
4. **Contract D unmet in tier (c):** a verified domain is accepted with no proof it targets the promoted deployment. Domain records carry no deployment id; the proof must be composed with `targets.production.id == expected_deployment_id`. Tier (c) also reads `target`/`environment`, fields the documented domain schema does not have.

Not defects — must be preserved:

- **Contract A/F already hold.** All three `_post_promote` call sites (`promote.py:987`, `:1036`, `:1089`) pass the **promoted** identity (`promoted_identity` / `resume_identity` / `production_identity` from `_promoted_production_binding`, `promote.py:206`), so `canonical_deployment_id` is deployment **B**. **The p16 deployment-binding fix (`promote.py:206-249`, `test_promoted_recovery.py`) must not be weakened.**
- `release.py:1111` rewrites `intent["canonical_production_url"]` from the committed record at LIVE — consistent, leave alone.
- Dead code: `adapters.py:1584-1595` is the orphaned tail of the removed `_default_domain_verified`, unreachable after the `except` return at `:1583`, referencing stale `status`/`body`. Remove.

## 2. Target algorithm (contract B, verified candidates only)

Single private collector returns three independently-populated, independently-sorted lists; the public resolver picks the first non-empty in contract order and labels the source with the winning class.

```
1  ownership gate (_project_valid)                                   [unchanged]
2  require expected_deployment_id; absent -> CANONICAL_PRODUCTION_URL_UNRESOLVED   (contract D)
3  GET /v9/projects/{name}  -> fresh
   prod_binding_id = fresh.targets.production.id
   binding_proven  = (prod_binding_id == expected_deployment_id)
4  class PROJECT_DOMAIN   (preferred)
     only when binding_proven
     GET /v9/projects/{name}/domains?production=true
     keep: verified is True, projectId == project['id'], no redirect,
           gitBranch is None, customEnvironmentId is None,
           _safe_origin('https://'+name+'/')
     explicit non-production target/environment (if present) disqualifies; absent is fine
   class PRODUCTION_ALIAS
     fresh.alias[] : target=='PRODUCTION' or environment=='production',
                     no redirect, deployment.id == expected_deployment_id
     fresh.targets.production.alias[] : only when binding_proven
   class DEPLOYMENT_ALIAS
     GET /v13/deployments/{expected_deployment_id} (id + projectId + teamId revalidated)
     body.alias[] minus body.url's hostname                               (contract: never the immutable deployment URL)
5  for hosts, source in ((PROJECT_DOMAIN,'VERCEL_PROJECT_DOMAIN'),
                         (PRODUCTION_ALIAS,'VERCEL_PRODUCTION_ALIAS'),
                         (DEPLOYMENT_ALIAS,'VERCEL_DEPLOYMENT_ALIAS')):
        if hosts: host = sorted(set(hosts))[0]  # intra-class tie-break ONLY
                  return ok({'canonical_production_url': f'https://{host}/',
                             'canonical_source': source,
                             'project_name': name, 'canonical_host': host})
6  _fail('CANONICAL_PRODUCTION_URL_UNRESOLVED')
```

Invariants to hold in the rewritten body:

- **C** — no hostname is ever built from the project name; every candidate string comes from a provider field.
- **D** — class 1 requires `binding_proven`; class 2 requires an explicit `deployment.id` match; class 3 is proven by the deployment read itself. With no `expected_deployment_id`, nothing qualifies.
- **E** — unprovable pretty domain ⇒ fall through to a verified alias; only when all three are empty does it fail closed.
- Deterministic but *arbitrary* intra-class tie-break. Do **not** add a "prefer the bare name" heuristic — that is name-matching by another route.

## 3. Tasks

### Task 1 — Read-only operator probe — **DONE** (evidence step still open)

Delivered:

- `VercelAdapter.inspect_canonical_evidence()` (`app/deploy/adapters.py`) + the `_evidence_*` projection helpers. Allowlisted per the table in step 4, primitive-coerced, capped at 20 records / 128 chars. It re-uses the real `canonical_production_url` only to *report* what the resolver returns today, under a `resolver` key.
- `--inspect-canonical-url <project_id>` (`app/runtime.py`), with `_inspect_promoted_deployment_id` picking `promotion_intent.promoted_deployment_id` then `deployment.last_live_release.deployment_id`. No `--as`, no mutation, no Telegram, no receive loop. `RuntimeComposition` gained read-only `vercel` / `registry_store` accessors so the probe uses the same adapters as the mutating paths.
- Deleted the unreachable `_default_domain_verified` tail that was stranded between the resolver's `except` return and `_valid_hostname_for_api` (previously `adapters.py:1584-1595`).
- Tests: 7 adapter tests (allowlist, no-leak of `meta`/`env`/`protectionBypass`/pagination/verification, caps, coercion, resolver agreement, binding mismatch, foreign project) and 8 CLI tests. Full `website-builder` suite: `2133 passed, 23 skipped`, one pre-existing unrelated flake (`test_frontend_watchdog.py::test_timeout_leaves_no_orphan_tree`, passes in isolation, timing-sensitive under full-suite load).

Remaining from the original step list: step 1 (route the probe through a shared `_canonical_candidates` helper) is deliberately **deferred to Task 3**, so the resolver stays byte-for-byte unchanged until the p17 evidence is in. Step 4 is implemented.

### Task 2 — Run the probe against p17 and record the outcome — **BLOCKED on the operator**

Cannot be executed from the implementation machine: there is no `.website-builder` state root here, no Vercel credentials configured, and the p17 state lives on the operator host (`/home/albertus527/.website-builder/state`). Requires running, on the operator host:

```
python -m app --inspect-canonical-url tg-6329821361-p17
```

Then answer in the PR description:

- Does `mopsypeyshop.vercel.app` appear in `GET /v9/projects/mopsypeyshop/domains` (with which `project_id`/`verified`/`redirect`/`git_branch`/`custom_environment_id`), in `targets.production_aliases`, or in the promoted deployment's `alias`?
- If present with `project_id` = this project and `verified: true` and `production_binding_matches: true` → the patch changes p17's resolution path (the URL is re-resolved on the next promote; the already-committed value stays).
- If absent → p17's recorded URL is the provider-correct production host and the user's expectation is unsatisfiable without claiming a foreign domain. **Report that; do not synthesise.**

Do not apply Tasks 3-5 before this output exists.

---

<details><summary>Original Task 1 wording (superseded by the DONE section above)</summary>

1. Extract the collectors from `canonical_production_url` into `VercelAdapter._canonical_candidates(app_id, project, *, expected_name, expected_deployment_id) -> (classed_hosts, evidence)`. `canonical_production_url` becomes a thin selector over it — one code path, no duplicated filtering.
2. Add `VercelAdapter.inspect_canonical_evidence(...) -> OperationResult` returning `{expected_deployment_id, binding_proven, classes: {source: [hosts]}, evidence: <ALLOWLISTED projection>, selected: {...} | None}`. It must **not** feed the resolver; it is a report only.
3. Add `--inspect-canonical-url <project_id>` in `app/runtime.py`, next to `--reconcile-publish` (`runtime.py:2723`). Read-only: no `--as`, no mutation, no Telegram. Resolve `app_id` and the bound slug exactly as promotion does (`_bound_slug_for`, `runtime.py:812`); prefer `promotion_intent.promoted_deployment_id`, else `deployment.last_live_release.deployment_id`.
4. **Bounded, allowlisted output.** The probe returns and logs only the provider fields needed to prove canonical identity, projected field-by-field in the adapter — never a raw payload, never a `json.dumps` of a provider body:

   | record | allowed fields |
   |---|---|
   | project | `id`, `name`, `accountId`, `targets.production.{id, alias[]}` |
   | domain | `name`, `projectId`, `verified`, `redirect`, `gitBranch`, `customEnvironmentId` |
   | deployment | `id`, `projectId`, `teamId`, `url`, `alias[]` |

   Every value is coerced to a primitive (`str`/`bool`/`None`) at projection time; non-conforming values are reported as `None` rather than passed through. Anything not in this table — `env`, `meta`, `lastAliasRequest`, `protectionBypass`, `targets` other than `production`, pagination, error bodies — is never returned or logged. Output is additionally length-capped: at most 20 records per class and 128 chars per string (capped values end in `...`; a non-conforming entry is dropped or `None`, and the caps are visible in the code as `_EVIDENCE_MAX_RECORDS` / `_EVIDENCE_MAX_STRING`). No token, no environment variables, no filesystem paths.

### Task 3 — Rewrite the resolver (`VercelAdapter.canonical_production_url`)

Implement §2, and route `inspect_canonical_evidence` through the same `_canonical_candidates` helper (Task 1 step 1, deferred) so the probe and the resolver cannot drift. Keep the method name, signature, `CANONICAL_PRODUCTION_URL_UNRESOLVED`, and the ownership gate. Update the docstring to state the three classes, the ordering, and that the tie-break is intra-class only. The stranded dead tail has already been deleted in Task 1.

### Task 4 — Label + version provenance (`website-builder/app/projects/promote.py`)

The old resolver hardcoded `VERCEL_PRODUCTION_URL`'s `canonical_source` to `VERCEL_PRODUCTION_ALIAS` **regardless of the winning evidence class**, so a legacy record carrying that string has no trustworthy class provenance. A recorded URL is therefore reusable only when its class provenance is both *recognized* and *produced by the class-aware resolver*.

1. Add `canonical_resolution_version = 2` to the intent written by `_post_promote` alongside `canonical_production_url` / `canonical_source` / `canonical_host` / `canonical_deployment_id` (`:1405-1411`). The resolver's own class-aware selection is version 2; the pre-class-aware hardcoded-label behavior is version 1 (implicit, absent).
2. `:1364-1372` — reuse a recorded canonical URL **only when all hold**: `canonical_deployment_id == intended_identity["deployment_id"]`; `canonical_resolution_version == 2`; and `canonical_source` is one of the three recognized class labels (`VERCEL_PROJECT_DOMAIN`, `VERCEL_PRODUCTION_ALIAS`, `VERCEL_DEPLOYMENT_ALIAS`). Otherwise **re-resolve** and overwrite the intent through the normal path. A legacy record (no version, or a version-1 record with the hardcoded label) is treated as having *no* usable provenance — never as a class-1 win.
3. Re-resolution must not weaken the p16/p17 recovery contract: it happens inside `_post_promote` after the production binding is already confirmed, against `intended_identity["deployment_id"]` (the promoted deployment B), so contract A/F still hold and the fail-closed `CANONICAL_PRODUCTION_URL_UNRESOLVED` path is unchanged.
4. `_resolve_canonical` (`:1599`) — unchanged contract (strict call with `expected_deployment_id`, `https` + `*.vercel.app` validation, `None` on any doubt). Additionally require `canonical_source` to be one of the three recognized labels, so an unrecognized label cannot be persisted.
5. **Committed LIVE releases are never rewritten.** `release.py:1111` keeps projecting the committed `production_url` into the intent as it does today; version/label are historical record, not a migration target. p17 remediation stays out of scope.

### Task 5 — Test doubles

Align `canonical_source` with the class each fake models: `tests/r1_harness.py:437`, `tests/test_promote.py:206`, `tests/test_promote_parity.py:485`, `tests/test_promote_bootstrap_classification.py:395`, `tests/test_r1_live_publish_finishing.py:271`. `tests/test_promoted_recovery.py:76-84` keeps returning `VERCEL_PROJECT_DOMAIN` (its fake resolves a project domain) — unchanged.

### Task 6 — Tests

Adapter level (`tests/test_promote.py`, reuse `_FakeTransport` / `_make_adapter` / `_valid_project`):

1. `test_canonical_prefers_verified_project_domain_over_deployment_alias` — classes 1 and 3 populated → domain wins, `canonical_source == 'VERCEL_PROJECT_DOMAIN'`.
2. `test_canonical_project_domain_requires_binding_to_promoted_deployment` — verified domain present but `targets.production.id` is another deployment → domain rejected, falls through to the deployment alias (contract D + E).
3. `test_canonical_stable_production_alias_preferred_over_deployment_alias` — class 2 wins over class 3.
4. `test_canonical_deployment_alias_is_last_resort` — only `body.alias` → `'VERCEL_DEPLOYMENT_ALIAS'`.
5. `test_canonical_never_returns_the_immutable_deployment_url` — `body.url` present in `alias`-adjacent fields; assert selection is the alias.
6. `test_canonical_rejects_domain_bound_to_another_project` — `projectId` mismatch → skipped, falls through.
7. `test_canonical_requires_expected_deployment_id` — omitted → `CANONICAL_PRODUCTION_URL_UNRESOLVED`.
8. `test_canonical_source_label_is_distinct_per_class` — one assertion per class.
9. `test_canonical_intra_class_tiebreak_is_deterministic` — two verified production domains → same winner on repeated calls, and no cross-class mixing.
10. Keep existing `test_regression_adapter_case_c_no_verified_alias_fails_closed` and strengthen it to assert `<project_name>.vercel.app` is never produced (contract C).
11. `test_canonical_rejects_malformed_domain_payload` — non-bool `verified`, missing `name`, `gitBranch` set → no class-1 candidate.

Orchestrator level:

12. `test_p17_project_domain_wins_end_to_end` — fake returns the project-domain host; smoke targets it; `state.production_url` matches; `promotion_intent.canonical_source == 'VERCEL_PROJECT_DOMAIN'`; `promotion_intent.canonical_resolution_version == 2`; **`canonical_deployment_id == promoted B`**.
13. `test_promoted_recovery_resolves_canonical_against_promoted_deployment` — in `tests/test_promoted_recovery.py`, assert `canonical_calls == [B]` so contract A/F is pinned by the p16 regression suite.
14. `test_reuse_requires_recognized_source_and_version_2` — parametrized over the reuse gate: (a) version 2 + recognized label ⇒ reused without re-resolving; (b) version 2 + unrecognized label ⇒ re-resolved; (c) legacy record with the hardcoded `VERCEL_PRODUCTION_ALIAS` and no version ⇒ re-resolved (the version-1 string is not proof of class provenance); (d) version 2 but `canonical_deployment_id != promoted B` ⇒ re-resolved.
15. `test_reuse_never_rewrites_a_committed_release` — an intent at stage `live` / a committed release keeps its recorded `production_url` even when the recorded source is legacy.
16. Probe tests: `inspect_canonical_evidence` returns the same three class lists the resolver selects from; its `selected` agrees with `canonical_production_url` for a given fixture; and the projection is **allowlisted** — a fixture payload carrying extra fields (`env`, `meta`, `lastAliasRequest`, `protectionBypass`, pagination, error body) must yield none of them in the result, and oversized record lists/strings must be capped with `truncated` set.

Run: `scripts/run_tests.sh website-builder/tests/test_promote.py website-builder/tests/test_promoted_recovery.py website-builder/tests/test_promote_parity.py -q`, then the full `website-builder/tests/` suite (CI-parity wrapper, not bare `pytest`).

## 4. Risks / non-goals

- **p17's recorded URL will not change** — remediation is explicitly out of scope; `_post_promote` reuse is correct. Say so in the PR. Task 4's version gate means a legacy in-flight record *re-resolves* rather than reusing, but the same fail-closed and promoted-identity guarantees apply.
- The probe is a diagnostic, so it reads the same endpoints the resolver reads. It exposes only allowlisted identity fields (Task 1 step 4) and is not a general Vercel payload viewer.
- Extra read: the resolver now always reads `/v9/projects/{name}` (once, shared by classes 1 and 2). Tier (c) is no longer a last-resort call — it is evaluated first, and is skipped when `binding_proven` is false.
- Do **not** touch `_promoted_production_binding`, the promote-by-creation path, `reconcile_external_promotion`, or `check_domain_production`.
- Do not add a "prefer bare name" tie-break, and do not add `<name>.vercel.app` synthesis anywhere.
- Do not consume `/v2/deployments/{id}/aliases` or `/v1/projects/{id}/promote/aliases` in this change; the probe reports them as evidence only.

## 5. Open items after Task 2

- If the probe shows `mopsypeyshop.vercel.app` present and proven, Task 3's ordering change is sufficient.
- If it shows absent, the correct outcome is "no code change to the selected host" — the patch then exists to make the *contract* enforceable and the evidence legible, and the operator conversation becomes "this is your production URL".
- If two verified production domains exist for one project, intra-class tie-break stays arbitrary; surface it to the operator instead of encoding a preference.
