# R2 Bootstrap Evidence — what proves the next deployment is preview-only

Status: implemented (mock evidence chain only). Real-provider empirical
validation is a separate, explicitly unauthorized decision.

## The problem

`ensure_bootstrap` spends Vercel's one-time first-deployment auto-promotion
on content-free bytes so that real user content is never deployment #1. A
successful bootstrap response alone is NOT proof that the *next* real-content
deployment is preview-only. The provider's documented "omitted target ->
preview" rule is observed to be NOT honoured for deployment #1
(2026-10-25, dorma-atelier: `prj_C2lFWS2fBPlAI7GycXPVG5GS2ebb`), and a
successful 201 on the bootstrap does not by itself bind production.

The fail-closed guard `BOOTSTRAP_REQUIRED_BEFORE_DEPLOY` (adapters.py,
restored at `473e4692b`) enforces the ordering at the adapter boundary:
while the project has no production binding, no real-content POST is issued.
After bootstrap, the production binding exists and the guard is inert for
normal preview deployments.

## Evidence chain (A ∧ B ∧ C)

A claim that "the next omitted-`target` deployment is preview-only" is
supported only when ALL of the following hold, each verified independently
against provider reads (never inferred from the response of the call that
created the state):

### Evidence A — bootstrap confirmed (required)
`ensure_bootstrap` reached its terminal confirmed state:
`project.targets.production.id == bootstrap_deployment_id` AND that
deployment's `readyState == READY`. A 201 on the bootstrap POST is
insufficient: the binding must be observed on a subsequent project GET.

### Evidence B — next preview-shaped deployment did not move the binding
(required) The next deployment (target omitted or explicitly `preview`)
response satisfies:
- `body.target` is `None` or `"preview"` (never `"production"`), AND
- a subsequent project GET still shows
  `targets.production.id == bootstrap_deployment_id` (unchanged by the new
  deployment).

If `body.target == "production"` or the binding now points at the new
deployment, the classification is `DEPLOYMENT_PROMOTED_TO_PRODUCTION`
(detection, not prevention) and the evidence chain is broken.

### Evidence C — the new deployment is not publicly reachable (required,
behavioral) The new deployment's URL is unreachable from an anonymous
(no-bypass-credential) browser, i.e. Deployment Protection is still active
on the preview domain and the deployment is not bound to a public production
alias.

Important: a protected URL returning 401 is NOT sufficient proof by
itself. 401 only shows the protection layer answered; it does not show the
deployment is not simultaneously bound to a public alias. Evidence C
requires the alias/binding state (from a project GET) to confirm the new
deployment is not the canonical production target.

## Conclusion rule

- A ∧ B ∧ C all present → the next omitted-`target` deployment MAY be
  asserted preview-only.
- ANY missing → "cannot assert". The fail-closed guard
  (`BOOTSTRAP_REQUIRED_BEFORE_DEPLOY` while the binding is absent, and the
  `DEPLOYMENT_PROMOTED_TO_PRODUCTION` terminal classification when the
  binding is present but points at the new deployment) remains in force.

### Production-shaped rejection (finalization P0)

A deployment response whose own payload says `target == "production"` is
**never** accepted as a preview, whether or not the project binding has
caught up with it. Two terminal outcomes are kept distinct:

| Outcome | Meaning | Persisted as |
|---|---|---|
| `DEPLOYMENT_PROMOTED_TO_PRODUCTION` | binding GET confirms the new deployment IS the production target | confirmed promotion |
| `DEPLOYMENT_PRODUCTION_TARGET_REJECTED` | provider labelled the deployment `production`, but the binding is absent/ambiguous/unreadable — alias may still be propagating | production-shaped, **not** confirmed |

Both are terminal: no retry, no second POST, no lookup on restart. The
unconfirmed variant is never upgraded to the confirmed one on restart; a
legacy persisted state that has `promoted_terminal=True` but no surviving
`promoted_error_code` fails closed as the production-shaped outcome. The
rejection is persisted in `external_promotion` with
`confirmed_promotion=False` and `evidence=PRODUCTION_TARGET_LABEL`, so the
audit trail states exactly what was proven.

### Bootstrap crash-safe idempotency (finalization P1)

Vercel has no server-side idempotency key: a repeated bootstrap POST creates
a second deployment. `ensure_bootstrap` therefore accepts an optional
caller-owned durable `bootstrap_state` mapping. When wired:

1. **Before the POST** the adapter records a deterministic marker in that
   mapping (`wbBootstrapMeta` = the payload digest).
2. On every **re-entry** — timeout, 5xx, reset, process restart — a present
   marker means the POST may already have happened. The adapter takes the
   **GET-only** path: relocate the deployment by deterministic metadata and
   poll its readiness. It never re-POSTs.
3. A missing/legacy marker cannot prove the POST never happened, so an
   unknown prior attempt fails closed (`BOOTSTRAP_RECONCILIATION_REQUIRED`)
   rather than authorizing a second create. Only a project with no marker
   AND no visible deployment may POST.
4. The marker is **not** a provider-enforced idempotency claim; it is an
   application-side crash/timeout record that converts "unknown" into
   "reconcile, never create".

The production runtime persists this mapping in the project's own deployment
state (`deployment["bootstrap_marker"]`), so a process restart reloads the
same marker. Orchestrators without the store keep the historical adapter-only
relocation path.

## Mock test surface

`tests/test_bootstrap_preview_evidence.py` drives `ensure_bootstrap` +
`deploy_static_files` over a canned transport through the A/B/C
combinations and asserts:
- A true but B `target=production` → classified `PROMOTED` (detection).
- C fails (new deployment anonymously reachable / binding moved) → result
  marked "preview-only unproven" and the terminal outcome stands.

## Real-provider empirical validation (separate decision, NOT done)

A one-time end-to-end proof requires a disposable Vercel project and two
bootstrap-level deployments (zero-content POST + observe response + project
GET). That is a real provider mutation and requires separate explicit
approval; it is out of scope for this implementation.
