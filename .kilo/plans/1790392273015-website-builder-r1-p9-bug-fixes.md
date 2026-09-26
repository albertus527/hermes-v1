# Website Builder R1 — p9 R1 bug fixes (BUG 1–6)

Scope: `website-builder/` only. Branch `feature/website`. No commit / push / deploy / real Vercel or Telegram mutation.

## Context: root causes (all verified in the working tree)

| Bug | Root cause (file:line) |
|---|---|
| 1 | `IntakeProcessor._readiness_for()` returns `NEEDS_CLARIFICATION` for `MIXED/OUT_OF_SCOPE/UNCLEAR` (`app/core/intake.py:185`) **even when NAME+WHAT+WHY are complete**, but `_fallback_clarification()` only knows the three per-field questions (`intake.py:400-407`) and returns `None` when all three are present. `process()` therefore emits `readiness=NEEDS_CLARIFICATION, clarification_question=None` (`intake.py:280-285`); `apply_to_project` still writes `WAITING_INPUT` (`intake.py:506-508`) and `_handle_intake` sends nothing (`app/runtime.py:1909-1910`) → the p9 "hung" look. Contributing cause: FAST answered `clarification_needed=false` (brief complete) while still returning scope `MIXED`; the skill `.hermes/skills/website-builder-product-scope/SKILL.md` lists carts/payment/auth as unsupported, so a storefront request legitimately reads as MIXED. |
| 2 | `preview.py` puts `preview_url` in three outbound messages: photo caption `preview.py:627`, preview text `preview.py:663`, follow-up text `preview.py:831`. The URL is already durably persisted (`preview_intent.preview_url`, `latest_shown_preview.preview_url`, `promote.approve` → `approval.preview_url`), so the fix is outbound-only. |
| 3 | `_handle_approve` (`runtime.py:1997-2024`) only renders failures; on success it returns with no send. |
| 4 | `ensure_bootstrap` POSTs the bootstrap with `meta={'wbOwner': marker(app_id), 'wbBootstrap': sha256(namespace+'\0bootstrap\0'+app_id)}` (`adapters.py:848`) but **no local marker is persisted** (`preview.py:437-440` discards the result). `find_production_deployment` requires `wbOperation/wbRevision/wbArtifact` and otherwise fails closed `INCOMPLETE_LOOKUP` (`adapters.py:1154-1165`) — which a bootstrap can never carry. So the first real publish has an unclassifiable previous production. |
| 5 | `app/projects/promote.py` has no `logging` import at all; the only success-path operator signal is a single Telegram `🚀 Live:` message. |
| 6 | `ERROR_MESSAGES` (`runtime.py:942-984`) has no entry for `INCOMPLETE_LOOKUP`, `PROMOTION_RECONCILIATION_REQUIRED`, `PROMOTE_FAILED`, `PROMOTE_NOT_APPLIED`, `PROMOTE_VERIFICATION_FAILED`, or the rollback codes → all render as the generic fallback. **`WORKER_BUSY` already has specific copy** (`runtime.py:973`); that part of the report is stale. |

## Decisions (user-confirmed)

1. **KNOWN_BOOTSTRAP proof = provider marker + v13 meta re-read.** The deterministic remote marker is sufficient proof; when the `/v6` list response lacks usable `meta`, re-read `/v13/deployments/{id}` (read-only, same validation as `reconcile_production_deployment`). No new local durable marker, no migration.
2. **BUG 1 loop handling = record + re-ask variants, no auto-clear.** The scope gate is never auto-cleared; MIXED/OUT_OF_SCOPE keeps failing closed.
3. **BUG 2 = deliver BOTH desktop and mobile screenshots now.** Per-screenshot durable outcome keys; the one-photo-per-delivery assertions listed in Task 6 are updated as a sanctioned contract change.

## Invariants that must not regress

Exact approved artifact identity · durable `promotion_intent` before any remote promote · same-operation resume · remote reconciliation · rollback safety · production smoke before LIVE · per-message at-most-once Telegram delivery with PENDING = ambiguous = never resend · PREVIEW/APPROVE/PUBLISH distinction · no repair-budget increase · no raw preview URL to the user · no auto-publish.

---

## Task 1 — BUG 1: scope-aware clarification + WAITING_INPUT invariant

`app/core/intake.py`

1. Add `_scope_clarification(scope, brief) -> Optional[str]`: deterministic Indonesian templates keyed by `Scope`, using only `brief["name"]`. `MIXED` uses the storefront-vs-transaction wording from the brief (catalog/storefront + CTA vs checkout/payment/login/database/backend). `OUT_OF_SCOPE` asks which part should become the website. `UNCLEAR` asks for the website's purpose. Returns `None` when the brief is incomplete (per-field question wins). No invented business facts.
2. Add `_variant_scope_clarification(scope, brief, attempt)` for the bounded re-ask (variant 0 = base, variant ≥1 = explicit numbered 1/2 choice). Bounded at 2 variants; no auto-clear.
3. In `process()`: after the existing `DISCOVERY_READY` / FAST-question / `_fallback_clarification` ladder, enforce the invariant — if `readiness == NEEDS_CLARIFICATION` and the question is empty, fall back to `_scope_clarification`; if *that* is also empty, use a bounded generic question so the invariant can never be violated. Record `clarification_reason` (`MISSING_FIELD` / `SCOPE` / `GENERIC`) and `clarification_attempt` on `IntakeResult`.
4. Extend `_context_messages()` to include the persisted outstanding question (one bounded line) so FAST can answer it on the next turn — this is the main lever against the loop.
5. In `apply_to_project()`: add `state.pending_clarification = {"question", "reason", "field", "scope", "asked_at", "attempt", "variant"}`; persist it before transitioning; transition to `WAITING_INPUT` **only** when a non-empty question exists (otherwise stay in `DISCOVERING` and log at WARNING). Clear it on `DISCOVERY_READY` and on pause.
6. `app/core/state.py`: add `pending_clarification: Dict[str, Any] = field(default_factory=dict)` to `ProjectState` (legacy rows load with `{}`; `from_dict` is unaffected).
7. `app/runtime.py::_handle_intake`: unchanged control flow (it already sends `clarification_question` from the dispatch result and already short-circuits `duplicate`), plus one INFO log naming the reason.

Tests: new `tests/test_intake_scope_clarification.py`

- complete NAME+WHAT+WHY + `scope=MIXED` + `clarification_needed=false` ⇒ `clarification_question` is a non-empty, actionable question mentioning the storefront-vs-transaction distinction; `state.lifecycle == WAITING_INPUT` **and** `state.pending_clarification["question"]` equals what was sent to Telegram (end-to-end through the real dispatcher + loop, mirroring `test_intake_clarification_regression.py`).
- same for `OUT_OF_SCOPE` and `UNCLEAR`.
- property-style invariant over a matrix of (scope × brief-completeness): `readiness == NEEDS_CLARIFICATION ⇒ bool(question)`, and no turn persists `WAITING_INPUT` with an empty question.
- second turn asking the same scope question produces variant 1 and does **not** change lifecycle; scope gate still blocks the build (`build.call_count == 0`).
- replay of the same Telegram update: no second clarification, no second send.
- a `NEEDS_CLARIFICATION` result constructed with `clarification_question=None` cannot write `WAITING_INPUT` (direct `apply_to_project` call).

## Task 2 — BUG 2: no preview URL outbound; both screenshots delivered

`app/deploy/preview.py`

1. Photo caption → no URL: desktop `"Preview sudah siap — tampilan desktop."`, mobile `"Preview sudah siap — tampilan mobile."`
2. Preview text (`preview.py:662-664`) → the desired UX, no URL:
   `Preview sudah siap.\nKalau ada yang mau diubah, kirim revisinya.\nKalau sudah oke, bilang "approve".`
3. Follow-up text (`preview.py:829-833`) → drop the URL line, keep name + the existing revisi/baru-question; drop the now-unused `preview_url` parameter from `_maybe_send_follow_up` and update the two call sites (`preview.py:294`, `preview.py:717`).
4. Deliver **both** screenshots in a fixed order (`desktop_screenshot`, then `mobile_screenshot`) using the existing PENDING→send→outcome discipline per shot. New durable keys on `preview_intent`:
   - `screenshot_attempted`: ordered list of shot keys
   - `screenshot_outcome`: `{shot_key: NOT_SENT|PENDING|SENT}`
   - `screenshot_message_id`: `{shot_key: int}`
   Legacy `photo_attempted` / `photo_outcome` / `photo_message_id` are still written as an **aggregate** (`photo_outcome == SENT` only when every shot is SENT; `PENDING` if any is PENDING) so existing readers/operator tooling keep working.
5. The "both deliveries confirmed before `latest_shown_preview`" gate must require **every** screenshot SENT plus the text SENT. `DELIVERY_RECONCILIATION_REQUIRED` / `DELIVERY_STATE_PERSIST_FAILED` paths unchanged.
6. `latest_shown_preview.preview_url` and `preview_intent.preview_url` stay exactly as they are (internal binding). No change to the approval binding.

Tests: new `tests/test_preview_no_url_outbound.py`

- After a successful preview: `preview_url` still present in `preview_intent`, `latest_shown_preview` and `approval`; **no** outbound text, caption or follow-up contains the URL or its host; exactly 2 photo sends, desktop before mobile, each with a PNG.
- per-shot crash matrix: force the SENT persistence write to fail for (a) desktop, (b) mobile, (c) text ⇒ the re-drive fails closed with `DELIVERY_RECONCILIATION_REQUIRED` and sends **nothing** further (`screenshot_outcome` shows the failed shot PENDING).
- pre-send persist failure for the mobile shot ⇒ desktop sent, mobile never attempted, no text sent.
- ordering probe: at each `send_photo`/`send_text` call, that message's own durable outcome is already PENDING.

## Task 3 — BUG 3: approval acknowledgement

`app/runtime.py`

1. New `_send_approval_ack_once(project_id, chat_id)`: keyed on the **persisted** `state.deployment["approval"]` identity (`operation_id`, `deployment_id`, `source_revision`) read inside the writer lock; durable `state.deployment["approval_ack"] = {identity…, "outcome": "PENDING"|"SENT", "message_id"}`.
   Pre-send PENDING write → `send_text` → post-send SENT write; on `TELEGRAM_REJECTED` (definitely not sent) reset to `NOT_SENT` so a later distinct approve may re-drive; on an ambiguous result leave PENDING and never resend. Mirrors `_send_preview_status_once`.
2. Text: `✅ Preview approved.\nKalau sudah siap ditayangkan, bilang "publish".`
3. `_handle_approve`: unchanged failure path; on success, if `result.data.get("duplicate")` return without sending, else call the ack sender. No promote call is added — approve still never publishes.
4. INFO log on ack sent.

Tests: new `tests/test_approve_acknowledgement.py` (real dispatcher + loop + real store)

- successful approve ⇒ exactly one ack with the expected text; `approved_revision` bound; no production_url; lifecycle still `PREVIEW_READY`.
- replay of the same update ⇒ no second ack.
- a **different** update approving the same identity ⇒ no second ack (identity-keyed).
- a different identity (new preview) ⇒ a new ack is allowed.
- failed approve (`NO_SHOWN_PREVIEW` / `STALE_APPROVAL`) ⇒ existing error reply, no ack, no ack marker written.
- ambiguous send (exception) ⇒ outcome stays PENDING, a later approve does not resend.
- publish dispatch does not emit an approve ack (guards against double messaging).

## Task 4 — BUG 4: previous-production classification

`app/deploy/adapters.py` — `find_production_deployment`

1. If the `/v6` item's `meta` is absent or fails the identity check, re-read `GET /v13/deployments/{id}` and validate exactly as `reconcile_production_deployment` does (`id`, `projectId`, `teamId`) before trusting its `meta`. Any failure ⇒ `INCOMPLETE_LOOKUP` (unchanged fail-closed).
2. Emit an additive, positive-only discriminator **only** when the bootstrap is proven:
   `data['bootstrap_proof'] = {'deployment_id': identifier, 'bootstrap_operation_id': <deterministic id>}`
   requires: `meta['wbOwner'] == self._marker(app_id)`, `meta['wbBootstrap'] == self._bootstrap_operation_id(app_id)`, no `wbOperation`, and `self._current_production_id(project) == identifier`. Complete `wbOperation/wbRevision/wbArtifact` ⇒ REAL_PRODUCTION (byte-identical result shape to today, no binding cross-check added).

`app/projects/promote.py`

3. Replace `_previous_identity_from_result` with `_classify_previous_production(result) -> (kind, identity)` where kind ∈ `NO_PRODUCTION | KNOWN_BOOTSTRAP | REAL_PRODUCTION | UNKNOWN_OR_INCOMPLETE_PRODUCTION`. Backward compatible with existing fakes: `{'deployment_id': None}` ⇒ NO_PRODUCTION; a complete identity dict ⇒ REAL_PRODUCTION; no discriminator ⇒ UNKNOWN.
4. `UNKNOWN_OR_INCOMPLETE_PRODUCTION` keeps today's `INCOMPLETE_LOOKUP` return (no promote POST). `KNOWN_BOOTSTRAP` ⇒ `previous_production = None` **and** the new durable `promotion_intent["previous_production_class"]`, so bootstrap and no-production stay distinguishable on resume.
5. Same-operation resume is untouched: an intent with `previous_production is None` and a persisted class still reconciles/re-promotes as before; the class is informational on resume and is never used to re-derive identity.
6. `_rollback_and_fail` receives `None` for bootstrap ⇒ `NO_PREVIOUS_PRODUCTION`, no rollback to the placeholder.

Tests: new `tests/test_promote_bootstrap_classification.py` + adapter tests in `tests/test_preview_adapters.py`

- first publish with a proven bootstrap ⇒ success, `previous_production is None`, `previous_production_class == "KNOWN_BOOTSTRAP"`, exactly one promote POST, production smoke ran, LIVE reached.
- bootstrap + production smoke failure ⇒ no rollback POST, `failure.rollback == "NO_PREVIOUS_PRODUCTION"`, error code `SMOKE_FAILED`.
- real prior production with a complete identity ⇒ `REAL_PRODUCTION`, `previous_production` persisted, rollback re-promotes the previous deployment's own identity (existing `(a)` test must keep passing unchanged).
- unknown/incomplete prior production (missing `meta`, malformed, wrong `wbOwner`, `wbBootstrap` mismatch, production binding pointing elsewhere) ⇒ `INCOMPLETE_LOOKUP`, **zero** promote POSTs.
- `/v6` without `meta` + authoritative `/v13` proving bootstrap ⇒ `KNOWN_BOOTSTRAP`; `/v13` re-read failing/mismatched ⇒ `INCOMPLETE_LOOKUP`.
- same-operation retry after a KNOWN_BOOTSTRAP intent ⇒ unchanged semantics (reconcile, no second classify-driven identity, no duplicate POST).
- adapter unit tests for the discriminator using the existing `Transport`/`project()` helpers.

## Task 5 — BUG 5: operator INFO logging

- `app/projects/promote.py`: add `logger = logging.getLogger(__name__)` and the boundary logs: `Approval accepted project=… revision=… deployment=…`, `Publish starting project=… revision=…`, `Previous production classified=<kind> project=… deployment=…`, `Promotion intent persisted project=… operation=…`, `Promotion confirmed deployment=…`, `Production smoke passed project=…`, `Project LIVE production_url=…`. No preview URL, no token, no brief/user payload.
- `app/runtime.py`: INFO on the approval acknowledgement and on a clarification being asked (`reason=` only).

Tests: `tests/test_promote_operator_logging.py` — attach a handler to the `app.projects.promote` / `app.runtime` loggers (pattern from `test_audit_regressions.py:277-293`) and assert the seven promote lines appear on the happy path, that the classification line carries the right kind, and that no record contains a bypass secret, a token, or a preview URL.

## Task 6 — BUG 6: user-facing publish-failure copy

`app/runtime.py` — add specific `ERROR_MESSAGES` entries (Indonesian, matching the existing `SMOKE_FAILED` style) for the publish path, keeping every internal code stable:

`INCOMPLETE_LOOKUP` (the p9 example, verbatim from the brief), `PROMOTION_RECONCILIATION_REQUIRED`, `PROMOTE_FAILED`, `PROMOTE_NOT_APPLIED`, `PROMOTE_VERIFICATION_FAILED`, `INCOMPLETE_PREVIEW_IDENTITY`, `ROLLBACK_FAILED`, `ROLLBACK_RECONCILIATION_REQUIRED`, `ROLLBACK_TARGET_IDENTITY_INCOMPLETE`, `PROJECT_RECONCILIATION_REQUIRED`, `PROJECT_IDENTITY_MISMATCH`, `DEPLOYMENT_IDENTITY_MISMATCH`, `INVALID_DEPLOYMENT_ID`, `INVALID_PRODUCTION_URL`.

No copy may claim success when state is ambiguous.

Tests: extend the existing coverage list in `tests/test_crash_recovery.py` (the parametrized "has specific copy and does not render generically" test) with the new codes, and add a test asserting `render_error_message("INCOMPLETE_LOOKUP")` equals the exact p9 copy and never contains the raw code.

## Task 7 — Sanctioned updates to existing tests (BUG 2 contract change)

Per-delivery photo counts become 2. Update the counts/comments only — no assertion is weakened, deleted, skipped or loosened:

- `tests/test_r1_full_story.py:83` `==1→==2`, `:94` `==2→==4`, `:127` `==2→==4`
- `tests/test_r1_phase_g_full_story.py:125` `==1→==2`, `:131` `==2→==4`, `:155` `==1→==2`, `:160` `==2→==4` (`:199`, `:211`, `:259` are already relative/zero-based → unchanged)
- `tests/test_r1_preview_create_matrix.py:100` `==1→==2` (`:59`, `:80` assert `[]` → unchanged)
- `tests/test_r1_revision_crash.py:75` `==1→==2`, `:92` `+1→+2`, `:108` `+1→+2`
- `tests/test_r1_simulation_baseline.py:65` `==1→==2`
- `tests/test_r1_smoke_failure_recovery.py:476`, `:517`, `:598` `==1→==2` (and the `:598` message text)
- `tests/test_r1_slug_bind.py:163` `==1→==2`
- `tests/test_self_contained.py:1031` `+1→+2`
- `tests/test_preview_orchestrator.py:482` `['photo','text']→['photo','photo','text']`, `:498` `photo_calls == 1 → == 2`; `:448` stays `['photo']` (the desktop SENT-write failure aborts before mobile) — verify against the implementation and adjust only if the aggregate write timing requires it
- `tests/test_conversations.py:569` `assert "https://tested.vercel.app" in follow` → `not in` (product contract changed; `:566` `len(text_sends) == 2` unchanged)

## Validation

```bash
# focused
scripts/run_tests.sh website-builder/tests/test_intake_scope_clarification.py -q --file-retries=0
scripts/run_tests.sh website-builder/tests/test_preview_no_url_outbound.py -q --file-retries=0
scripts/run_tests.sh website-builder/tests/test_approve_acknowledgement.py -q --file-retries=0
scripts/run_tests.sh website-builder/tests/test_promote_bootstrap_classification.py -q --file-retries=0
scripts/run_tests.sh website-builder/tests/test_promote_operator_logging.py -q --file-retries=0
scripts/run_tests.sh website-builder/tests/test_promote.py website-builder/tests/test_preview_orchestrator.py -q --file-retries=0

# full
bash scripts/run_tests.sh website-builder/tests -q --file-retries=0
```

Run per-file (the runner is file-granular). All external boundaries stay mocked/faked — no real Vercel, Telegram, or promotion calls. Baseline before starting: record the full-suite result; the pre-existing Windows `sys.path` failures documented in `docs/LOCAL_HANDOFF.md` (`test_frontend_role_cli.py`, `test_hermes_adapter.py`, `test_qa.py`) are not regressions. Then `git diff` review for unrelated changes.

## Risks / notes

- BUG 2 changes the Telegram message count per preview (2 photos + 1 text + 1 follow-up). Task 7 is the sanctioned fallout; anything else that breaks is a real regression.
- BUG 1 does not make MIXED self-clearing. If FAST keeps returning MIXED after the user answers, the user sees the variant-1 numbered question and the project stays in `WAITING_INPUT` — fail-closed, operator-visible, and explicitly an R2 item (a semantic/operator decision, not a code shortcut).
- BUG 4's KNOWN_BOOTSTRAP proof depends on `VERCEL_OWNERSHIP_NAMESPACE` being unchanged since the bootstrap was created; a namespace change makes the marker unprovable and correctly falls back to `INCOMPLETE_LOOKUP`.
- `whatsapp.send_preview_url` is dormant (no orchestrator caller, only its own unit test) and is deliberately left untouched; if WhatsApp is ever wired up it must obey the same no-URL rule.

## p9: retry or restart?

p9's `INCOMPLETE_LOOKUP` is raised at `promote.py:351-354` **before** the writer-lock block, so nothing was mutated: lifecycle stayed `PREVIEW_READY`, `promotion_intent` is `null`, `failure` is `null`, and the approval is bound to the exact `latest_shown_preview` identity. That is a clean, consistent retry state.

**p9 is safe to retry from its current persisted state** after redeploying the fix — send `publish` again in the same conversation. `approve()` rebinds the same identity, `promote()` re-runs, and with the classification fix the bootstrap production yields `previous_production = None` ⇒ promote ⇒ production smoke ⇒ LIVE. A fresh p10 is needed only if the bootstrap proof fails (see below). Before retrying, run the read-only verification: `GET /v13/deployments/dpl_DkHYjGgneY5pos4bUrqLdHLHbGfu` and confirm `meta.wbOwner == marker(app_id)` and `meta.wbBootstrap == sha256(namespace + '\0bootstrap\0' + app_id)` and `targets.production.id == dpl_DkHYjGgneY5pos4bUrqLdHLHbGfu`. If any of those do not hold, p9 will fail closed again and a fresh p10 is required.

Note: p9's already-delivered preview messages still contain the old URL (nothing re-sends an already-shown preview), and its "approve" produced no acknowledgement — if the user sends `approve` again after the fix, the new ack will fire (none was ever sent, so it is not a duplicate).
