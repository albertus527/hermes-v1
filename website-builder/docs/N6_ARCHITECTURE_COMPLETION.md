# N6 — Architecture Completion Report

Status: N6-A → N6-E implemented on `feature/website-r2-redesign`.
Worktree: `/home/albertus527/hermes-website-r2`
Baseline: N5 at `c2c36849f` (last N1–N5 commit).

N6 completes the roadmap's intended architecture. N1–N5 delivered real, wired
modules, but the chain was not fully operational: there was no **pre-build design
document**, the Decision Engine's Design job never ran, and the QA "guidance
digest" was write-only. N6 closes those gaps.

## What N6 adds

| Checkpoint | Deliverable | Commit |
|---|---|---|
| N6-A | `decision_engine` config: independent `intake_mode`/`design_mode` (disabled/shadow/active), safe-fallback parser, runtime field | `196525f03` |
| N6-B | Intake modes: disabled / shadow / **active** (routing + reference only; FAST authoritative) | `2a4fd3694` |
| N6-C | Pre-build `DesignGuidance.md`: contract kind, grounded compiler, store, pre-FRONTEND build wiring | `5b3e8f84e` |
| N6-D | FRONTEND receives PRD + DesignGuidance; D2/D3a design context pack activated (gated) | `3a971ebf6` |
| N6-E | Per-invocation guidance snapshots; QA/VISION parity fail-closed; design-compliance vs spec-parity | (this commit) |

## Data flow (as implemented)

```
USER → Decision Engine Intake (disabled|shadow|active; routing only when active)
     → FAST (AUTHORITATIVE: scope, facts, requirements, readiness)
     → Requirement Creator: PRD compiler → PRD.md
     → Decision Engine Design (disabled|shadow|active)
     → Requirement Creator: Design Guidance compiler → DesignGuidance.md   [pre-build]
     → D2/D3a resource selector + OpenViking retrieval → design context pack
     → FRONTEND  (receives PRD + DesignGuidance + pack; records a guidance SNAPSHOT)
     → design-dna.json → Design.md                                          [post-build]
     → deterministic checks → VISION (spec parity + design compliance) + Impeccable
     → QA acceptance verifies the RECORDED snapshot (fail closed on mismatch)
     → Preview → User Approval → existing GitHub/Vercel lifecycle
```

## The three documents are semantically distinct

| Document | When | Source | Version field |
|---|---|---|---|
| `PRD.md` | pre-build | FAST brief | `requirements_version` |
| `DesignGuidance.md` | **pre-build** | brief + Decision Engine decision + D2 selector | `design_guidance_version` |
| `Design.md` | post-build | the Design DNA FRONTEND produced | `design_dna_version` |

Guidance is an INPUT the application decided; Design DNA / Design.md are OUTPUTS
FRONTEND decided. They are separate kinds, separate files, separate version
fields, and the guidance kind is never readable as the design kind (mutation-proven).

## Key guarantees (each mutation-proven)

- **No configuration error can enable the engine.** Unknown/malformed mode →
  `disabled`; `max_candidates` clamped and never widened.
- **Disabled = byte-identical.** Both modes default disabled; a disabled
  deployment constructs no engine, writes no guidance, builds no pack.
- **Shadow changes nothing.** Intake result is identical to disabled; no prompt,
  published bytes, or approval decision is affected.
- **Active intake cannot exceed routing.** Parity tests prove `scope`, `brief`,
  `readiness`, and clarification are unchanged versus disabled.
- **Grounded, never fabricated.** A guidance section with no evidence carries an
  explicit `UNRESOLVED` marker; a candidate without provenance never becomes an
  approved reference.
- **Abstention/outage degrades safely.** Decision Engine outage → brief-only
  honest guidance; OpenViking outage → empty candidates → abstain; neither
  fabricates a resource, and neither breaks a build.
- **Post-build Design.md is never retroactive input.** The invocation snapshot
  records Design.md as absent at invocation time.
- **Parity is recorded, not inferred.** QA/VISION verify the RECORDED snapshot;
  a used document that changed or vanished fails closed with
  `GUIDANCE_VERSION_MISMATCH`. A document that was absent and later appeared
  (the post-build Design.md) is correctly *not* a mismatch.
- **Bounded history, current-release evidence retained.** Snapshots cap at
  `MAX_SNAPSHOT_HISTORY`; the newest record is always kept.
- **Spec parity ≠ design compliance.** VISION is handed the pre-build guidance
  and asked to report the two judgements separately.

## Acceptance evidence

| Check | Result |
|---|---|
| N6-A tests | 17 passed |
| N6-B tests | 16 passed; mutation 5/5 killed |
| N6-C tests | 17 + 5 build-path; mutation 6/6 killed |
| N6-D tests | 13 passed; mutation 6/6 killed |
| N6-E tests | 17 + 2 QA-orchestrator parity; mutation 6/6 killed |
| Full suite | compared to the 20-failure baseline fingerprint (see ledger) |

## Residual gaps / limitations (honest)

- **Active mode is NOT enabled on production.** All new behaviour is
  default-disabled. Active mode requires a separate live E2E qualification.
- **Mocked tests are not production readiness.** The FRONTEND/VISION/OpenViking
  boundaries are exercised with stubs and fakes; no live model, browser, npm, or
  OpenViking server was used in this batch.
- **The design context pack is an enhancement.** When it cannot be built (e.g. no
  `hermes_home`), the build proceeds without it; that path is tested but not
  exercised live.
- **20 pre-existing baseline failures remain** (18 hydration, 1 design_resources,
  1 suite_hygiene), documented with a fingerprint and proven on a pristine HEAD.
  N6 adds tests and no new failures.

## Proposed controlled live E2E plan (separate, NOT executed here)

1. Enable `requirement_creator.enabled` + `decision_engine.design_mode: shadow`
   on a staging project; confirm documents are written and the build is otherwise
   unchanged (shadow must not alter output).
2. Enable `design_mode: active` with OpenViking OFF; confirm DesignGuidance.md is
   grounded in the brief only and FRONTEND still builds.
3. Enable OpenViking + `design_mode: active`; confirm candidates carry real
   provenance and the pack is bounded.
4. Run one full new-website + one revision lifecycle; confirm the recorded
   snapshot matches at QA and the release reaches LIVE only after approval.
5. Inject a document change between build and QA; confirm
   `GUIDANCE_VERSION_MISMATCH` fails closed.
6. Enable `intake_mode: active`; confirm routing-only behaviour and FAST parity.
