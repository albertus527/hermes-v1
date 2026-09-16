# Website Builder — Current Local Handoff (Phase 15 closeout + Milestone A runtime wiring)

This file reflects the current repository state after the Phase 15 runtime-readiness closeout and the R1 Milestone A production runtime composition/bootstrap.

## Phase 15 status

Phase 15 is currently:

- IMPLEMENTED_DORMANT
- LOCAL_CONTRACT_VERIFIED
- LIVE_META_BLOCKED_BY_CREDENTIALS

This means the WhatsApp webhook/dispatch seam, config validation, secret redaction, and multi-message handling are implemented and exercised through local contract tests, but they are not live-runtime validated against Meta because no real WhatsApp credential set or webhook endpoint is available in this environment.

## Milestone A runtime wiring status

The production runtime composition root is now implemented:

- `app/runtime.py` — composition root constructing all existing collaborators
- `app/__main__.py` — canonical executable entrypoint (`python -m app`)
- `app/runtime.py:TelegramReceiveLoop` — bounded getUpdates long-polling loop with natural-conversation intent routing

Canonical invocation:

```bash
cd website-builder
python -m app
```

Required environment variables:

- `TELEGRAM_BOT_TOKEN` — Telegram Bot API token
- `VERCEL_TOKEN` — Vercel API token
- `VERCEL_TEAM_ID` — Vercel team ID
- `HERMES_HOME` — Hermes profile home (default: `~/.hermes-website`)

Optional:

- `WEB3FORMS_ACCESS_KEY` — Web3Forms contact form key
- `WEBSITE_BUILDER_WORKSPACE_ROOT` — project workspaces root
- `WEBSITE_BUILDER_STATE_ROOT` — persistent state root
- `WEBSITE_BUILDER_OUTPUT_REPO` — output Git repository path
- `VERCEL_OWNERSHIP_NAMESPACE` — Vercel project ownership namespace

The runtime wires:

- `ProjectStateStore` -> `ProjectRunner` -> `HermesAdapter` -> `IntakeProcessor`
- `ReferenceIntake` / `DirectionsOrchestrator` / `FrontendBuilder` / `RevisionOrchestrator`
- `PreviewOrchestrator` (Vercel + Telegram + smoke + output Git)
- `PromotionOrchestrator` / `CustomDomainOrchestrator`
- `TelegramDispatcher` as the single mutation authority
- `TelegramReceiveLoop` feeding normalized Updates through `AuthenticatedTelegramContext` into the dispatcher

Natural-conversation routing:

- `TelegramReceiveLoop._classify_intent()` uses the existing FAST Hermes role
  (zero-tool programmatic boundary) to classify each natural-language turn into
  a bounded intent: INTAKE, REVISE, APPROVE, or PUBLISH.
- Intent vocabulary is state-aware: only lifecycle-appropriate intents are
  offered to FAST (e.g. PREVIEW_READY offers REVISE/APPROVE/PUBLISH;
  DISCOVERING offers only INTAKE).
- Fail-safe: FAST failure, malformed output, unsupported intent, or ambiguity
  always falls back to INTAKE — never a destructive action.
- REVISE derives `seq` from persisted `queued_revision_seq + 1` and dispatches
  through the existing `TelegramDispatcher` revise action.
- PUBLISH dispatches approve then publish (canonical two-step artifact-specific
  approval contract), both through the dispatcher's existing dedup/authz/
  lifecycle gates.
- The dispatcher's `dispatch_events` dedup ensures replay of the same Telegram
  update is idempotent for both revision and publication.

WhatsApp remains IMPLEMENTED_DORMANT. No WhatsApp credentials are required for startup.

## Current verification

Verified locally via the repository test wrapper:

```bash
scripts/run_tests.sh website-builder/tests -j 1
```

Result as of this pass:

- 26 files
- 617 tests passed (baseline) + 47 runtime tests passed (26 bootstrap + 21 routing)
- 23 pre-existing failures (Windows environment: missing Hermes repo root on sys.path)
- 16 skipped

The 23 pre-existing failures are in `test_frontend_role_cli.py`, `test_hermes_adapter.py`, and `test_qa.py` — they require the Hermes repo root on `sys.path` and fail on Windows before this change. They are not regressions.

## Local workflow and commit status

Canonical source workflow is:

local Windows edit/test/commit/push
->
VPS git pull/build/test/run

No source editing or committing is intended from the VPS. This repository remains the local source of truth; the VPS is used to pull, build, and run the already-reviewed tree.

## External integration status

The following remains honestly labeled as contract/mock verified only and not real runtime verified:

- Meta WhatsApp webhook verification and payload handling
- outbound WhatsApp Graph API calls via injected transport stubs
- Telegram dispatcher compatibility path
- any live Meta endpoint behavior requiring a real account and webhook registration
- Telegram Bot API getUpdates (runtime loop is implemented but not yet live-network validated)
- Vercel preview/production deployment (adapters are implemented but not yet live-network validated)

Do not claim R1 SHIPPABLE yet. The live Meta runtime path is blocked by credentials and real webhook setup. The live Telegram/Vercel runtime path is blocked by credentials and real network access.

## Scope and cleanup

This closeout purposefully did not reopen the earlier Phase 9–16 architecture or broaden the Website Builder scope. It only fixed the remaining Phase 15 runtime-readiness gaps, updated stale Phase 15 documentation, and added the Milestone A production runtime composition/bootstrap.

The stale `website-builder/PHASE9-16.patch` artifact was not kept as an active runtime asset; it was removed once it was confirmed to be obsolete duplicate state.

## VISION capability gate note

I did not change the current website-builder VISION capability gate while addressing the WhatsApp closeout. I did not find a concrete VISION regression/test contradiction in this task that required a code change, so no VISION policy change was introduced here.
