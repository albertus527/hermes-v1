# WEBSITE_BUILDER_R1_IMPLEMENTATION_PLAN.md

Status: IMPLEMENTATION PLAN
Depends on: WEBSITE_BUILDER_R1_CANONICAL_SPEC.md

## Goal

Reach a real vertical slice quickly, then complete R1 without reopening architecture.

First milestone:

> Telegram natural-language request -> NAME + WHAT + WHY -> domain discovery -> Hermes FRONTEND build -> isolated sandbox -> desktop/mobile QA -> Vercel preview -> screenshot/link back to Telegram -> one natural revision -> QA again -> artifact-specific approval -> production.

WhatsApp and remaining R1 paths are added after this works.

# Phase 0 — Fork, Repo, Environment

1. Fork Hermes into your GitHub account.
2. Clone the fork to the Linux VPS/dev machine.
3. Add upstream remote and create `website-builder-r1` branch.
4. Keep Hermes core untouched initially.
5. Create a Website Builder application layer with modules for channels, core, projects, domain, workers, Hermes adapter, sandbox, QA, deployment, skills, tests, config, and docs.
6. Commit the canonical spec and this implementation plan.
7. Pin Hermes commit/version, Node, package manager, Playwright/Chromium, frontend starter, and skill versions.

Conceptual layout:

```text
website-builder/
├── app/
│   ├── channels/
│   ├── core/
│   ├── projects/
│   ├── domain/
│   ├── workers/
│   ├── hermes/
│   ├── sandbox/
│   ├── qa/
│   └── deployment/
├── skills/
├── frontend-starter/
├── tests/
├── config/
└── docs/
```

# Phase 1 — Compatibility Smoke Tests

Do these before full implementation.

1. Hermes basic run: task -> tool -> edit file -> run command.
2. Hermes -> 9Router: prove FAST, FRONTEND, VISION and intended fallback.
3. Prove VISION can receive a screenshot/image.
4. Choose one fixed frontend starter and prove install/dev/build/check.
5. Launch Chromium/Playwright and capture desktop 1440x900 + mobile 390x844.
6. Test Vercel with a disposable project: preview, external access, explicit production behavior, deployment ID, smoke test, rollback/last-known-good.
7. Select one read-only domain discovery source and test advertised TLD availability/pricing/error behavior.
8. Telegram smoke test: inbound text/image/file and outbound text/image/link plus event IDs.

STOP if any fundamental integration is impossible. Fix the bounded integration, not the architecture.

# Phase 2 — Core State and Contracts

1. Implement Project persistent state.
2. Implement lifecycle:
   DISCOVERING -> WAITING_INPUT -> READY -> QUEUED -> RUNNING -> PREVIEW_READY -> REVISION_REQUESTED -> PUBLISHING -> LIVE, plus FAILED/PAUSED/CANCELED.
3. Enforce valid transitions in code.
4. Implement one-writer-per-project atomic lock.
5. Implement requirements_version, design_dna_version, source_revision, qa_revision, preview_revision, approved_revision.
6. Implement processed-event deduplication.
7. Define structured external-operation results/errors.

# Phase 3 — Telegram Intake

1. Telegram payload -> normalized internal message.
2. Lightweight message debounce/buffering.
3. Explicit pause/resume handling (`eh bentar`, `tunggu dulu`, etc.).
4. FAST interprets scope; code enforces scope.
5. FAST extracts NAME + WHAT + WHY.
6. Ask only the smallest blocking clarification.
7. Store concise interpreted brief.
8. Do not require confirmation when no material ambiguity remains.

Acceptance examples:

```text
"Bikin Northcut."
-> ask WHAT + WHY

"Northcut, barbershop."
-> ask WHY

"Northcut, barbershop, biar orang booking WA."
-> DISCOVERY_READY
```

# Phase 4 — Domain Discovery

1. Generate a small bounded domain candidate set from NAME.
2. Query the selected source.
3. Persist status, price/currency/term/renewal if available, source, checked_at.
4. If preferred domain is unavailable, suggest only a few sensible alternatives.
5. Always support “decide later; continue with .vercel.app”.
6. Provider failure -> UNKNOWN; never infer availability from DNS.
7. Do not implement domain purchase.

# Phase 5 — Omarchy-Style Website Builder Skills

Create explicit read-only skills/instructions for Hermes:

1. Environment/workspace conventions.
2. R1 product scope and no-business-invention rules.
3. UI UX Pro Max concrete installation/use.
4. Minimal Design DNA contract.
5. Browser/screenshot procedure.
6. QA rules and severity.
7. Git/Vercel workflow.
8. Recovery/rollback expectations.

Use normal headless Linux VPS; do not install Omarchy as an R1 dependency.

# Phase 6 — Isolated Project Runner

1. Create `/workspaces/<project_id>/`.
2. Isolate Hermes session/config/memory per project.
3. Isolate browser context per project.
4. Isolate runtime/process resources and prevent port collision.
5. Track and safely clean stale processes.
6. Keep platform secrets outside generated code.
7. Expose controlled platform operations rather than raw deployment/domain/channel credentials.
8. Treat reference URLs/components as untrusted input.

# Phase 7 — First FRONTEND Build

Start with no-reference delegated design.

Test brief:

```text
Name: Northcut
What: premium barbershop
Why: show services and drive WhatsApp booking
Design: agent decides
```

Steps:

1. FRONTEND derives design direction.
2. FRONTEND creates concrete Design DNA.
3. Use verified test content and valid CTA destination.
4. Build from the fixed frontend starter.
5. Run fixed cheap checks.
6. Do not let FAST own Design DNA.

# Phase 8 — QA and Repair

1. Cheap checks: build/runtime/routes/obvious console errors.
2. Render site locally in sandbox.
3. Capture desktop/mobile screenshots.
4. VISION receives DNA version, source revision, viewport, screenshots, and references when relevant.
5. Run functional QA: primary CTA, routes/navigation, assets, mobile usability, keyboard basics.
6. Use one shared repair budget; initial `MAX_REPAIR_PASSES = 2`.
7. FRONTEND repairs.
8. Retest affected checks.
9. If critical failures remain, mark blocked/failed and do not call it publication-ready.

# Phase 9 — External Preview

Only after internal QA is preview-eligible:

1. Commit tested source revision.
2. Create Vercel preview.
3. Record immutable source/deployment identity.
4. Open preview as an ordinary external user and smoke-test.
5. Send Telegram screenshot + preview URL + natural revision invitation.

Do not deploy every internal QA loop to Vercel.

# Phase 10 — Natural Revision

Example: `hero kegedean, button lebih warm`.

1. FAST parses ordered revision intent.
2. Update Design DNA where relevant.
3. FRONTEND applies changes.
4. Create new source revision.
5. Invalidate affected QA.
6. Run bounded QA again.
7. Create/send a new approval-eligible preview.

Preserve revision order. Later instructions override only conflicts, not compatible earlier changes.

# Phase 11 — Approval and Production

1. FAST interprets `oke live` / `publish` / similar.
2. Code verifies authorization.
3. Require:
   - target == latest shown tested preview
   - no unresolved newer revision
   - PUBLICATION_READY == true
4. If stale/ambiguous, wait or clarify.
5. Publish exactly the approved tested artifact.
6. Production smoke-test.
7. Store last-known-good deployment.
8. Send final production URL to Telegram.

This completes Milestone A.

# Milestone A — First Complete Vertical Slice

Do not add more scope until this works end-to-end:

```text
Telegram
 -> natural brief
 -> NAME + WHAT + WHY
 -> domain discovery
 -> FRONTEND design/build
 -> isolated sandbox
 -> desktop/mobile QA
 -> bounded repair
 -> Vercel preview
 -> screenshot/link to Telegram
 -> natural revision
 -> new QA/preview
 -> artifact-specific go-live
 -> production
 -> smoke test
 -> live URL to Telegram
```

Measure:
- first-preview latency
- build failure rate
- repair passes
- token/model cost
- revision success rate

# Phase 12 — Reference Inputs

After Milestone A:

1. User logos/images.
2. Screenshot references via VISION.
3. Controlled URL references.
4. Multi-reference composition: A=UX, B=color, C=layout, D=motion.
5. FRONTEND synthesizes one original Design DNA.
6. Do not copy source code/assets/pixel-perfect sites.

# Phase 13 — Lightweight Design Directions

For no-reference users who do not delegate design authority:

1. FRONTEND creates 2–3 lightweight directions.
2. Send concise visual cards/previews.
3. User chooses.
4. Build one website.

Never build three full websites just to offer a choice.

# Phase 14 — Contact Form

Choose one supported submission integration.

Requirements:
- verified destination
- clear success/failure
- no fake submission behavior

If unavailable, offer a working WhatsApp/email/contact link instead.

# Phase 15 — WhatsApp Adapter

1. Verify the actual provider/account rules for inbound text/media, webhook identity, delayed outbound notifications, links/images, and message-window/template requirements.
2. Normalize WhatsApp into the same internal envelope.
3. Reuse the exact same core/project/build/QA/revision/approval flow.
4. Define safe cross-channel identity linking; never infer ownership from similar names.

WhatsApp remains R1; it is simply implemented after Telegram proves the vertical slice.

# Phase 16 — Existing Custom Domain

User-owned domains only:

```text
domain supplied
 -> ownership/config verification
 -> required DNS
 -> guided manual DNS
 -> verify
 -> attach to approved Vercel project
 -> HTTPS smoke test
```

No automated domain purchase in R1.

# Phase 17 — Concurrency and Recovery

Test:

1. Two different projects simultaneously -> isolated memory/files/browser/runtime.
2. Same project two mutations -> only one writer.
3. Multiple revisions during build -> ordered application.
4. Pause during work -> safe stop/checkpoint.
5. Kill worker -> recover to known state.
6. Replay webhook -> no duplicate action.
7. Simulate Vercel timeout after acceptance -> reconcile before retrying; no duplicate publication.

# Phase 18 — Small Frontend Model Evaluation

Now evaluate real rendered output.

Use 2–3 representative briefs and compare primary FRONTEND + intended fallback on:
- visual quality
- brief adherence
- UI/UX
- mobile responsiveness
- correctness
- revision quality
- speed
- cost

Do not promote a backend-oriented model because of generic coding benchmarks.

Expand the benchmark only when evidence says it is useful.

# Phase 19 — R1 Acceptance

R1 is shippable when these work:

Conversation:
- Telegram + WhatsApp
- buffering
- pause/resume
- NAME + WHAT + WHY
- scope rejection

Domain:
- availability/pricing/alternatives
- UNKNOWN failure
- defer to `.vercel.app`

Design:
- delegated design
- lightweight direction choice
- image/screenshot reference
- multi-reference composition
- Design DNA revisions

Build:
- fixed frontend stack
- isolated project session/workspace/browser/runtime
- no secret leakage

QA:
- desktop/mobile
- CTA/routes/assets
- keyboard basics
- bounded repair
- critical failure block

Preview:
- immutable tested preview
- screenshot + public URL

Revision:
- natural language
- ordered revisions
- stale approval protection

Production:
- artifact-specific approval
- publication readiness
- deployment + smoke test
- last-known-good

Custom domain:
- existing-domain guided connection

Concurrency:
- multiple projects
- one writer/project
- recovery/deduplication

# Phase 20 — Stop Condition

When acceptance passes:

```text
R1 = SHIPPABLE
```

Do NOT immediately add automatic domain commerce, CMS, auth, ecommerce, extra agents/model roles, more deployment platforms, or distributed queue infrastructure.

Run real projects and use measured failures to define R1.1/R2.

# Immediate First Session Checklist

```text
1. Fork Hermes
2. Clone fork
3. Create website-builder-r1 branch
4. Add canonical spec + implementation plan
5. Pin environment
6. Hermes basic smoke test
7. Hermes -> 9Router FAST/FRONTEND/VISION smoke test
8. Fixed frontend starter smoke test
9. Browser screenshot smoke test
10. Vercel preview/production lifecycle smoke test
11. One domain lookup smoke test
12. Telegram in/out smoke test
13. Project state + one-writer lock
14. NAME + WHAT + WHY
15. Build Northcut end-to-end
```

Do not ask Hermes to implement all R1 in one giant task. The first goal is a real vertical slice.
