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

## B pass: intake/dispatch hardening + one-claim build admission (this task)

### History verified against `git log` / `git show` (evidence, not claims)

Confirmed by direct inspection of the referenced commits before starting:

- `ebf5cd2b0` — fixed a oneshot positional-argument bug passing the FRONTEND
  prompt to the Hermes CLI boundary.
- `573dff3c6` — bounded FRONTEND design discovery and extended the build
  subprocess timeout to 900s.
- `3697e6d33` — added recovery for a completed-but-truncated FRONTEND build
  when the Hermes subprocess hit its timeout artifact-side (`_has_complete_frontend_artifacts`).
- `48c24c42f` — added the Hermes adapter + tests; role selection (`_role_selection`)
  landed before the runtime composition root existed.
- `94949cc6d` / `e2236dc75` — added the Milestone A runtime composition root
  (`app/runtime.py`), `TelegramReceiveLoop`, and the intent-classification
  routing, wiring the dispatcher as a stand-alone process entrypoint.

The earliest FRONTEND-role wiring in this history relied on the _website_
Hermes profile's own `config.yaml`/`.env` defaults (there was no
runtime-composed `HERMES_HOME` override at that point yet); the A-reviewer's
credential-scope-overlay fix (see the "A gate closed" note below) is what
made per-profile secret scoping durable across the whole in-process
resolution span. I did not find artifacts describing a concrete
successful _live_ model/provider mapping actually exercised end-to-end
against a real Telegram/Vercel/Hermes deployment in this environment;
this remains UNKNOWN (see "Unknowns" below) — I did not invent one.

### A gate status (as reported, independently unverified live)

The task states the A gate is closed after an independent review pass:
credential scope overlay, VISION shortcut matching, empty-credential
rejection, auth diagnostic log filtering, and malformed-YAML
prevalidation fixes, verified by a fresh full-suite run (686 passed,
16 skipped, 0 retries). I did not re-derive or re-verify those specific
fixes' correctness myself in this pass — I read `app/hermes/adapter.py`
as it exists in the working tree (which already reflects those fixes:
`_hermes_home_scope`'s ExitStack-scoped secret overlay,
`_role_vision_support`'s exact-shortcut-match guard,
`_require_runtime_credentials`'s empty-key rejection, and the
`PreflightDiagnosticFilter` covering `hermes_cli.auth`) and treated it as
a stable, already-reviewed dependency boundary for the B work below. I
did not modify `app/hermes/adapter.py` in this pass.

### What changed in this pass (B scope)

**`app/core/intake.py`** — three real defects in accumulated-brief
handling, fixed:

1. `_persisted_brief` previously caught _any_ exception from
   `self.store.load(project_id)` and silently returned `{}`. This
   collapsed a genuine I/O/corruption failure into "no brief yet",
   which would then let a later turn re-derive (and potentially
   overwrite) NAME/WHAT/WHY from scratch on top of an unreadable state
   file. Fixed: only a missing/falsy `project_id` returns `{}`; a real
   `store.load` failure now propagates to the caller (the dispatcher's
   claim-then-effect boundary and the runtime's per-update try/except
   both already fail closed on an unexpected exception without
   persisting on top of unreadable state).
2. `_merge_brief`'s "single-segment turn shifts into the next missing
   field" heuristic was applied unconditionally, including to FAST's
   real semantic output. This meant a turn where FAST correctly
   extracted `only name=...` (e.g., a corrected spelling on a later
   turn) got silently redirected into filling `what`/`why` instead of
   being treated as a genuine NAME correction — clobbering FAST's own
   semantic authority with a heuristic that only makes sense for the
   _deterministic_ fallback extractor (which has no real understanding
   and always maps a lone segment to "name"). Fixed: the heuristic is
   now gated on `used_fallback=True`, so FAST's field assignments are
   applied as-is, and only the bounded fallback path still needs the
   shift-into-next-missing-field behavior.
3. `process()` always regenerated the clarification question from the
   generic per-field fallback prompt (`_fallback_clarification`), even
   when FAST had flagged a specific material ambiguity/correction with
   its own `clarification_question`. This overwrote FAST's contextual
   question (e.g., "You said both barbershop and salon — which one?")
   with a generic "What is X?" prompt. Fixed: when FAST reports
   `clarification_needed=True` with a `clarification_question`, that
   question is preserved; the generic fallback question is only used
   when FAST didn't supply one (or the fallback path is active).

Covered by new tests in `test_intake_dispatch_integration.py`
(`test_fast_ambiguity_question_preserved_over_generic_fallback`,
`test_fast_correction_across_turns_overrides_persisted_name`) and by
the three-turn integration test's context-message assertion.

**`app/channels/dispatch.py`** — single-claim build admission:

Previously, `TelegramReceiveLoop._handle_intake` (in `app/runtime.py`)
issued a _second_ top-level `dispatcher.dispatch(update, project_id,
"build", ...)` call after a successful intake dispatch, when the
persisted state showed `lifecycle == "READY"`. This second call derived
its claim key from the identical `(principal, conversation_id,
event_id)` tuple as the intake claim already recorded moments earlier
(the claim key is `sha256([principal, conversation_id, event_id])`,
scoped only by that tuple — not by action). On the very same Telegram
event this collided with the intake claim already present under that
key and returned `EVENT_ACTION_MISMATCH` instead of ever building; on
replay it would additionally re-derive the same colliding key again.
Fixed: `TelegramDispatcher.dispatch()` now admits the follow-on build as
a **separate sub-claim**, keyed off `sha256(intake_key + ":auto_build")`,
persisted (`CLAIMED` -> effect -> `DONE`/`FAILED`) under its own writer
acquisition _inside_ the same `action == "intake"` branch, before any
Telegram-visible response is returned. The runtime's
`_handle_intake` no longer issues a second top-level dispatch call at
all — it only inspects `result.data["build_triggered"] /
["build_success"] / ["build_error"]` that the dispatcher now reports.
This preserves "claim persisted before effect run" for the build,
adds no new external effect ordering, and makes replay of the same
update return `{"duplicate": True}` for the intake claim (build already
ran and does not re-run) rather than intermittently succeeding or
failing depending on the second call's own dedup race.

Covered by:
`test_intake_dispatch_integration.py::test_three_turns_two_questions_then_exactly_one_build`,
`::test_duplicate_turn_replay_no_additional_effects`,
`::test_restart_replay_no_additional_effects` (new dispatcher/store
instances over the same persisted root, simulating a process restart),
`::test_build_dispatch_failure_recorded_and_not_silently_retried`,
`::test_persistence_failure_before_effects_blocks_effect`,
`::test_unauthorized_principal_cannot_trigger_intake`; and updated
`test_runtime.py::TestDispatcherInvocation::test_build_triggered_when_ready`.

**`app/runtime.py`** — `_handle_intake` simplified to consume the
dispatcher-reported build outcome instead of issuing the second
colliding dispatch call described above.

**`tests/test_core.py`** —
`TestProjectStateStore.test_one_writer_lock` previously synchronized the
two competing writer threads with a fixed `time.sleep(0.05)` between
starting the first thread and starting the second, asserting the first
thread would still be holding the lock by then. This is a wall-clock
timing assumption, exactly the kind of flake risk flagged in this
project's testing rules (loose ties to CPU scheduling can intermittently
invert the race under load). Fixed: replaced with a
`threading.Event` (`first_holds_lock`) that the first writer sets only
_after_ actually entering the `acquire_writer` context (i.e., after
provably holding the lock), which the second writer waits on before
attempting its own time-bounded acquisition. This removes the timing
assumption entirely while preserving the original assertion (exactly one
writer acquires the lock).

**New file `tests/test_intake_dispatch_integration.py`** — real
end-to-end integration tests exercising the actual `TelegramDispatcher`

- `IntakeProcessor` + `ProjectStateStore` collaborators together (no
  mocking of internal application logic), modeled on the canonical
  Northcut/barbershop/WhatsApp-booking scenario: three conversational
  turns, two clarifying questions, then exactly one build. Only the
  Hermes adapter (FAST) and the frontend builder (external/expensive
  boundaries: LLM calls, npm/workspace builds) are mocked. Covers:
  the 3-turn happy path with context-message assertion, duplicate-update
  idempotency, restart-replay idempotency (fresh store/dispatcher
  instances over the same persisted root), build-failure recording
  without silent retry, a claim-persistence failure blocking all
  downstream effects, unauthorized-principal rejection, and the two FAST
  semantic-authority fixes above.

### Before/after test counts

|                             | Before this pass (per task description)                                                                                       | After this pass                                                                                                                                                                   |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| website-builder full suite  | 617 baseline + 47 runtime = ~664 passed, 23 pre-existing failures (missing Hermes repo root on sys.path, Windows), 16 skipped | **694 passed, 0 failed, 16 skipped** (29 files)                                                                                                                                   |
| New tests added             | —                                                                                                                             | 8 (`test_intake_dispatch_integration.py`)                                                                                                                                         |
| Modified pre-existing tests | —                                                                                                                             | 2 (`test_core.py::test_one_writer_lock` desynchronized from wall-clock; `test_runtime.py::test_build_triggered_when_ready` updated for new single-claim build admission contract) |

The previously-reported 23 Windows `sys.path` failures in
`test_frontend_role_cli.py`, `test_hermes_adapter.py`, `test_qa.py` were
NOT present in this pass's run — I did not change anything relevant to
that import path and did not investigate further since it was out of
this task's scope; this is recorded as an observed discrepancy, not a
claimed fix.

### Runner command used (this pass)

```bash
scripts/run_tests.sh website-builder/tests -j 1
```

Executed via Git Bash on Windows, per repository policy
(`scripts/run_tests.sh`, not raw `pytest`, for hermetic CI parity —
env var isolation, TZ=UTC, LANG=C.UTF-8, per-file subprocess isolation).
If a bare `python -m pytest website-builder/tests -q` is run directly
instead, it will NOT have the same isolation (real `HERMES_HOME`/env
credential vars if set in the calling shell, local timezone/locale, and
no per-file subprocess isolation) — it is not considered an equivalent
hermetic run by this project's own testing rules, and I did not use it
as the basis for any pass/fail claim above.

### Role/config schema (no invented models)

`app/hermes/adapter.py` and `app/runtime.py` both express the schema
exactly as: `website_builder.models.<ROLE>.model` (non-empty string) and
`website_builder.models.<ROLE>.provider` (non-empty string), for
`<ROLE>` in `FAST, FRONTEND, VISION`, under the Website Builder Hermes
profile's own `$HERMES_HOME/config.yaml`. I did not add, infer, or hardcode
any concrete model/provider name anywhere in this pass — this remains
fully operator-configured, consistent with `ROLE_CONFIG_SCHEMA_HINT` in
`app/runtime.py`.

### Safe, VPS-appropriate, READ-ONLY preflight check (no live calls)

To confirm role configuration resolves WITHOUT starting the receive
loop or making any live network/model call, from the deployed
`website-builder` checkout on the VPS:

```bash
cd website-builder
python -c "
from pathlib import Path
from app.hermes.adapter import HermesAdapter
report = HermesAdapter(store=None, hermes_home=Path.home() / '.hermes-website').validate_role_configuration()
print(report['ok'], report['errors'], report['config_path'])
"
```

This calls the exact same `_validate_role_configuration()` path
`preflight_role_validation()` uses at real startup (`app/runtime.py`),
resolves FAST/FRONTEND/VISION against the real profile config and
provider seam (credential presence, provider enablement, custom-provider
identity, VISION image-capability gate), and NEVER performs a live model
call or starts `TelegramReceiveLoop`/`getUpdates` polling. It prints
`True {} <path>` on success or the per-role error dict otherwise. Nothing
here mutates state, contacts Telegram/Vercel, or spends model credits.

### Unknowns (explicitly not fabricated)

- **Concrete live model/provider mapping**: UNKNOWN. No artifact in this
  repository/history records a real end-to-end run against live
  Telegram/Vercel/Hermes credentials; the runtime remains
  `LIVE_META_BLOCKED_BY_CREDENTIALS`-equivalent for Telegram/Vercel too
  (see "External integration status" above, unchanged by this pass).
- **Original "19 categories" coverage matrix**: UNKNOWN / unavailable.
  I searched this task's referenced history, this document's prior
  revisions, and locally available artifacts for a matrix enumerating 19
  original test/behavior categories and found none in this repository;
  I am not fabricating one. The coverage delta I CAN state precisely is
  the before/after test-count table above, which is a real, re-run
  measurement, not a reconstruction of an unavailable historical
  document.
- **A-reviewer fixes' correctness**: taken as given per the task
  description and the working tree's current `app/hermes/adapter.py`
  content; not independently re-derived in this pass (see "A gate
  status" above).

This pass did not touch `app/hermes/adapter.py`, VISION policy, trading
code, canonical spec documents, or any commit/push/default-profile
operation, per this task's constraints.
