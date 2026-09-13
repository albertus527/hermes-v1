# Website Builder R1 — Phases 2–7 Implementation Report (Final Correction Pass)

## A. Phase-by-Phase Result

### Phase 2: Core State and Contracts

**Implemented:**

- `app/core/lifecycle.py` — ProjectLifecycle enum with 12 states, valid transition map, `LifecycleError` on invalid transitions.
- `app/core/state.py` — `ProjectState` dataclass with all required fields. `ProjectStateStore` with file-based JSON persistence, atomic writes via temp+rename, and one-writer-per-project locking via `O_CREAT|O_EXCL` lock files.
- `app/core/contracts.py` — `OperationResult`, `DomainCheckResult`, `PreviewResult`, `PublishResult` structured contracts.

**Tests:** `tests/test_core.py` — 10 tests.

### Phase 3: Telegram Intake

**Implemented:**

- `app/channels/telegram.py` — `TelegramNormalizer` converts Telegram Update payloads to `NormalizedMessage`.
- `app/core/buffer.py` — `MessageBuffer` with lightweight debounce.
- `app/core/intake.py` — `IntakeProcessor` with deterministic pause/resume detection, Hermes FAST integration, and application-owned state transitions.
- `app/hermes/adapter.py` — Thin adapter to existing Hermes runtime. FAST uses the programmatic `AIAgent` boundary with `enabled_toolsets=[]` to guarantee zero tool definitions.

**Tests:** `tests/test_intake.py` — 22 tests.

**External configuration still needed:** Live Telegram bot token and webhook configuration.

### Phase 4: Domain Discovery

**Implemented:**

- `app/domain/discovery.py` — `generate_candidates()` (bounded to 5), `generate_alternatives()` (bounded to 3), `DomainDiscovery` with provider-neutral `lookup_fn` injection. All failures return UNKNOWN. Never infers availability from DNS. Supports defer to `.vercel.app`.

**Tests:** `tests/test_domain.py` — 11 tests.

**Provider/configuration still needed:** A concrete read-only domain discovery provider must be selected and configured.

### Phase 5: Website Builder Skills

**Implemented:**

- 3 read-only Hermes skill directories, each with a `SKILL.md`: `website-builder-environment`, `website-builder-product-scope`, `website-builder-design-dna`. UI UX Pro Max is reused from the Website Hermes profile, not vendored.

**Tests:** `tests/test_skills.py` — skill structure and content tests.

### Phase 6: Isolated Project Runner

**Implemented:**

- `app/sandbox/runner.py` — `validate_project_id()`, `project_workspace_path()`, `ProcessTracker`, `PortAllocator`, `ProjectRunner` with MAX*WORKERS=1, credential stripping for generated-project processes (including `NINEROUTER*`), credential preservation for Hermes platform processes, and `is_source_repo_clean()`using`git status --porcelain`.

**Tests:** `tests/test_runner.py` — 21 tests.

### Phase 7: First Frontend Build

**Implemented:**

- `app/projects/build.py` — `FrontendBuilder` that copies the canonical fixed starter (`<repo>/templates/frontend-starter/`), uses Hermes FRONTEND role via `HermesAdapter.frontend_build()` to derive Design DNA and generate source, runs fixed cheap checks through `ProjectRunner.run_command()`, and stops before Phase 8.
- FRONTEND does NOT run npm cheap checks. Application code runs `npm ci`, `npm run build`, `npm run typecheck` exactly once each through ProjectRunner.
- Design DNA is produced by FRONTEND, not hardcoded. No fabricated business facts.
- Successful cheap checks leave the project in RUNNING state (not PREVIEW_READY — that requires Phase 8 QA).

**Tests:** `tests/test_build.py` — 15 tests.
**Tests:** `tests/test_hermes_adapter.py` — 9 tests.

## B. Files Changed

| Path                                                            | Purpose                                   |
| --------------------------------------------------------------- | ----------------------------------------- |
| `website-builder/app/__init__.py`                               | Application package root                  |
| `website-builder/app/core/__init__.py`                          | Core package                              |
| `website-builder/app/core/lifecycle.py`                         | Lifecycle state machine                   |
| `website-builder/app/core/state.py`                             | Persistent state + locking                |
| `website-builder/app/core/contracts.py`                         | External operation contracts              |
| `website-builder/app/core/buffer.py`                            | Message debounce                          |
| `website-builder/app/core/intake.py`                            | Intake processing with Hermes FAST        |
| `website-builder/app/channels/__init__.py`                      | Channels package                          |
| `website-builder/app/channels/telegram.py`                      | Telegram normalization                    |
| `website-builder/app/domain/__init__.py`                        | Domain package                            |
| `website-builder/app/domain/discovery.py`                       | Domain discovery                          |
| `website-builder/app/sandbox/__init__.py`                       | Sandbox package                           |
| `website-builder/app/sandbox/runner.py`                         | Isolated project runner                   |
| `website-builder/app/projects/__init__.py`                      | Projects package                          |
| `website-builder/app/projects/build.py`                         | First frontend build with Hermes FRONTEND |
| `website-builder/app/hermes/__init__.py`                        | Hermes adapter package                    |
| `website-builder/app/hermes/adapter.py`                         | Thin Hermes adapter                       |
| `website-builder/skills/README.md`                              | Skill index                               |
| `website-builder/skills/website-builder-environment/SKILL.md`   | Environment conventions                   |
| `website-builder/skills/website-builder-product-scope/SKILL.md` | Product scope rules                       |
| `website-builder/skills/website-builder-design-dna/SKILL.md`    | Design DNA contract                       |
| `website-builder/config/default.yaml`                           | Default configuration                     |
| `website-builder/tests/__init__.py`                             | Tests package                             |
| `website-builder/tests/test_core.py`                            | Phase 2 tests                             |
| `website-builder/tests/test_intake.py`                          | Phase 3 tests                             |
| `website-builder/tests/test_domain.py`                          | Phase 4 tests                             |
| `website-builder/tests/test_skills.py`                          | Phase 5 tests                             |
| `website-builder/tests/test_runner.py`                          | Phase 6 tests                             |
| `website-builder/tests/test_build.py`                           | Phase 7 tests                             |
| `website-builder/tests/test_hermes_adapter.py`                  | Hermes adapter tests                      |
| `website-builder/docs/IMPLEMENTATION_REPORT.md`                 | This report                               |

**Removed:** `app/qa/`, `app/deployment/`, `app/workers/` (speculative empty packages).

## C. Dependencies Added

**NONE.** All implementation uses Python standard library only.

## D. Environment Configuration Still Required on VPS

1. **Telegram Bot Token**: Required for live Telegram intake.
2. **Domain Discovery Provider**: Read-only domain availability API credentials.
3. **Node.js 26 + npm**: Required for `npm ci` / `npm run build` / `npm run typecheck`.
4. **Website Hermes Profile**: `~/.hermes-website` with provider/model config for FAST/FRONTEND.
5. **Workspace Root**: `~/website-workspaces` must exist and be writable.

## E. Deferred Work

Explicitly confirmed unimplemented:

- Phase 8 QA + bounded repair
- Phase 9 Vercel preview
- Phase 10 revisions
- Phase 11 approval/production
- Phase 12+ features

## F. Architecture Check

Introduced:

- **New agent**: NONE — FAST/FRONTEND are logical roles using existing Hermes/9Router.
- **Queue**: NONE — `MessageBuffer` is lightweight in-memory debounce.
- **Database**: NONE — File-based JSON state.
- **Service**: NONE — No new long-running services.
- **MCP**: NONE.
- **Framework**: NONE — No workflow, orchestration, or DI frameworks.
- **Model-routing layer**: NONE — Uses existing Hermes provider resolution.

## G. Phase 8 Handoff

Concrete integration points now existing for future Phase 8:

1. **Project workspace**: `ProjectRunner.create_workspace()` returns isolated path with `.hermes/`, `.browser/`, `.runtime/`.
2. **Built source**: `FrontendBuilder.build()` produces workspace with `dist/` and `design-dna.json` after successful cheap checks.
3. **State revisions**: `source_revision` and `design_dna_version` persisted.
4. **Process tracking**: `ProcessTracker` can register browser/dev-server processes.
5. **Port allocation**: `PortAllocator` provides deterministic ports.
6. **Lifecycle**: Project is in `RUNNING` state after successful Phase 7, ready for Phase 8 to advance to `PREVIEW_READY` after QA.
7. **Hermes adapter**: `HermesAdapter` provides the seam for Phase 8 VISION invocation with zero tool access.

Phase 8 should consume these boundaries without redesigning them.
