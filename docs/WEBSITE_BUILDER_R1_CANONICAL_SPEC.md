# WEBSITE_BUILDER_R1_CANONICAL_SPEC.md

Status: FROZEN FOR IMPLEMENTATION

## 1. Objective

Build an autonomous AI Website Studio on top of Hermes for non-technical users.

A user should be able to describe a website in ordinary conversation through Telegram or WhatsApp and receive a polished, tested, deployable website quickly.

Product aspiration:

> Chat in the morning. Usable website by noon/afternoon.

R1 optimizes for simplicity, reliability, frontend/UI quality, deterministic project execution, and fast shipping.

## 2. Product North Star

Minimum user input:

1. NAME — website/business name.
2. WHAT — what the website/business is.
3. WHY — what the visitor should primarily understand or do.
4. Design references/assets — optional.

Core rule:

> Infer implementation details. Never invent business intent.

The system may infer responsive behavior, sensible typography, accessibility basics, navigation conventions, and performance practices. It must not invent material business facts such as services, pricing, addresses, contact details, testimonials, claims, or conversion goals.

## 3. R1 Scope

Supported:
- landing pages, company profiles, portfolios
- restaurant/cafe, barbershop/service-business, event/wedding, personal-brand sites
- product/SaaS marketing sites
- basic multi-page informational sites
- galleries
- outbound WhatsApp/social/contact links
- one supported contact-form integration with a verified destination
- responsive design, basic animation, SEO, accessibility, performance
- reference-driven redesign

Not supported:
- native mobile/desktop apps
- authentication/user accounts
- custom CMS/admin panels
- shopping carts/payment processing
- custom booking engines
- complex databases/application dashboards
- ERP/full CRM/trading bots/games
- unrelated coding/debugging

External services may be linked. R1 does not build the external operational platform itself.

## 4. Channels

R1 supports Telegram and WhatsApp. Implementation sequence is Telegram first, WhatsApp second.

```text
Telegram Adapter ─┐
                  ├─> Normalized Message -> Website Builder Core
WhatsApp Adapter ─┘
```

Normalized messages include event ID, authenticated user identity, conversation ID, project routing context, text, attachments, reply context, and timestamp. Events are deduplicated before mutation.

## 5. Scope Gate

Interpret as WEBSITE, WEBSITE_RELATED, MIXED, OUT_OF_SCOPE, or UNCLEAR.

AI interprets; application code enforces authorization and capability boundaries.

## 6. Requirement Gate

Before design/build establish NAME + WHAT + WHY.

Ask only questions that materially change the website. Do not implement confidence as a separate subsystem; derive readiness from missing or contradictory fields.

## 7. Discovery vs Publication Readiness

DISCOVERY_READY means enough information exists to design/build a meaningful preview.

PUBLICATION_READY additionally requires launch-critical facts/actions to be verified: working primary CTA, required contact details, loaded assets, no invented claims, no unresolved material decision, and an approved QA-passed revision.

Clearly labeled placeholders may exist in preview, never silently in production.

## 8. Pre-Build Domain Discovery

Before expensive design/build:

```text
Website name
 -> candidate domains
 -> availability
 -> pricing
 -> alternatives
 -> user decision OR explicit defer
```

Brand and domain are separate.

Use one read-only R1 domain discovery source. Never infer availability from DNS absence.

Persist domain, status, price, currency, term, renewal price when known, source, and checked_at.

Statuses:
- UNKNOWN
- AVAILABLE_UNRESERVED
- UNAVAILABLE
- OWNED_UNVERIFIED
- OWNED_VERIFIED

Lookup failure becomes UNKNOWN and must allow “decide later; use .vercel.app”.

R1 supports search, pricing when available, alternatives, preferred-domain storage, defer, `.vercel.app`, and guided connection of an existing user-owned domain.

Automated domain purchase/payment/registrar commerce/renewal are deferred.

## 9. Design Input

With references: URLs, screenshots, images, logos, brand assets. Different references may contribute UX, palette, layout, or motion. Extract characteristics and synthesize an original design; do not copy source/assets/pixel-perfect layouts.

Without references: use WHAT + WHY. Offer lightweight design directions when useful. If the user delegates design authority (“yang bagus aja”), FRONTEND may choose after business intent is clear.

## 10. Design DNA

Design DNA is the design source of truth and should capture:
- brand personality
- palette/tokens
- typography
- spacing/density
- page inventory
- layout/navigation/motion rules
- primary CTA
- assets
- verified content
- unresolved facts

Revisions update Design DNA where relevant rather than accumulating disconnected prompts.

## 11. Design Resources

- UI UX Pro Max: primary design guidance
- User references: primary user-specific visual preference
- Godly/curated inspiration: optional, never a build dependency
- 21st.dev: optional implementation building blocks, never a build dependency; respect licenses

## 12. Omarchy Principle

Use a conventional headless Linux VPS/server, preferably stable Ubuntu/Debian style. Omarchy itself is NOT an R1 dependency.

Borrow the Omarchy principle: give Hermes explicit read-only environment/domain skills covering workspace conventions, frontend starter, build/run commands, browser/screenshots, QA, Git/Vercel workflow, and rollback expectations.

## 13. Fixed Frontend Runtime

Choose one frontend starter/toolchain for R1 and fixed install/dev/build/check commands. Do not let each project invent a different stack.

## 14. Frontend-First Model Policy

Priority:
1. website generation
2. frontend engineering
3. UI/UX adherence/taste
4. React/TypeScript/CSS
5. responsive implementation
6. vision/screenshot reasoning
7. agentic tool use
8. general coding
9. backend coding

Do not select a backend-oriented model as primary merely because of general SWE benchmarks.

## 15. Model Roles

FAST:
- scope/requirements/revision parsing/routine conversation
- no final permission/publication authority

FRONTEND:
- design discovery
- Design DNA
- frontend planning/implementation
- responsive/motion
- debugging/repair

VISION:
- reference and screenshot inspection
- visual findings/design adherence
- does not independently edit

FRONTEND and VISION may share an underlying model but use separate calls/contexts.

9Router maps FAST/FRONTEND/VISION to approved role-aware combinations/fallbacks. If no qualified fallback exists, pause rather than silently downgrade.

## 16. Initial Model Evaluation

Do not block implementation on a large benchmark. Test primary FRONTEND + intended fallback on 2–3 representative briefs including mobile and one revision task. Expand after the vertical slice works.

## 17. Project Isolation

Rule:

> Parallel across projects. Sequential within each project.

Each active project gets isolated Hermes session/config, memory, workspace, browser context, runtime/process resources, and revision state.

Shared skills are read-only.

Generated code cannot access other projects or raw platform credentials. Prefer controlled platform operations such as check_domain(), create_preview(), and publish_revision().

Treat external references/components as untrusted input and block access to private infrastructure.

## 18. Project Lifecycle

Keep lifecycle small:

```text
DISCOVERING
WAITING_INPUT
READY
QUEUED
RUNNING
PREVIEW_READY
REVISION_REQUESTED
PUBLISHING
LIVE

FAILED
PAUSED
CANCELED
```

Transitions are enforced in code. One project has one active writer with atomic acquisition, timeout/cancel, and recovery behavior.

## 19. Conversation Buffering

Use lightweight debounce/turn collection so fragmented Telegram/WhatsApp messages do not spawn independent expensive jobs.

Explicit pause language such as “eh bentar” pauses expensive progression until resumed.

Pending revisions preserve order. Later instructions override only conflicting earlier instructions.

## 20. Revision and Approval Contract

Track at minimum:
- requirements_version
- design_dna_version
- source_revision
- qa_revision
- preview_revision
- approved_revision
- deployment_id

Approval is bound to a specific tested preview/source revision. If a newer unresolved revision exists, an older preview cannot be silently published.

## 21. Build and QA

Internal QA runs in the isolated sandbox before external preview:

```text
BUILD
 -> cheap build/runtime/route checks
 -> render
 -> desktop screenshot
 -> mobile screenshot
 -> visual QA
 -> functional QA
 -> bounded repair
 -> retest
 -> final verification
```

Use one shared repair budget across visual and functional QA; initial default may be two repair passes.

Publication gate includes working build/runtime, primary CTA, navigation/routes, mobile usability, loaded assets, no critical console/runtime errors, basic keyboard accessibility, and no unresolved launch-critical placeholder.

If critical failures remain, return blocked/failed; never call it ready to publish.

## 22. Preview and Production

Do not deploy every QA iteration to Vercel.

```text
sandbox build
 -> local/browser QA
 -> PASS
 -> external preview
 -> screenshot + preview URL
 -> revision/approval
 -> validate approved revision
 -> publish
 -> production smoke test
```

Preview and production authorization are separate. Retain last-known-good deployment.

## 23. Deployment

R1 uses GitHub + Vercel. Default public result is project-name.vercel.app. Existing user-owned custom domains may be connected through guided configuration. Automated registrar purchase is deferred.

## 24. Persistent Project State

Persist project/owner/channel identity, lifecycle, requirements/version, domain state, Design DNA/version, assets, source/QA/preview/approved revisions, pending revisions, pause state, repository/deployment metadata, production URL, and failure/recovery metadata.

## 25. External Failure Rules

9Router, domain discovery, Telegram, WhatsApp, GitHub, and Vercel integrations return structured success/error results. Reconcile retries safely. Webhook replay must never duplicate publication.

## 26. Target Experience

Target first preview:
- simple site: ~30–45 minutes aspiration
- normal R1 business site: ~60–90 minutes aspiration
- more involved R1 site: same-day aspiration

User waiting time is separate from agent processing time.

## 27. Simplified Architecture

```text
Telegram / WhatsApp
        |
Channel Adapters
        |
Single Core
- dedupe / route / buffer / pause
- scope + requirement rules
- lifecycle + authorization
        |
        +--> Domain Discovery
        |
Persistent Project State + Pending Work
        |
Exclusive Project Worker
        |
Hermes
- environment skills
- UI UX Pro Max
- reference analysis
- Design DNA
- frontend build
- QA/repair
        |
        +--> 9Router: FAST / FRONTEND / VISION
        |
Isolated Project Sandbox
- files / runtime / browser
        |
GitHub + Vercel Preview
        |
Telegram / WhatsApp
        |
Revision / Artifact-specific Approval
        |
Production Promotion
        |
LIVE
```

The core, scheduler, and deployment functions may live in one application. R1 does not require a separate distributed queue service if persistent state and atomic project ownership are safe.

## 28. Anti-Frankenstein Rule

Before adding an agent/framework/service/database/queue/MCP/model role/infrastructure layer ask:

> Does this materially reduce user effort or improve website quality/reliability?

If not, reject or defer it.

Prefer deterministic code/state machines for permissions, lifecycle, revision ownership, publication, budgets, and side effects.

## 29. R1 Freeze

Frozen unless implementation reveals a concrete blocker:
- website-only scope
- Zero Prompt Engineering
- NAME + WHAT + WHY
- discovery vs publication readiness
- pre-build domain discovery with defer path
- Design DNA
- optional multi-reference composition
- no-reference design discovery
- UI UX Pro Max
- optional Godly/21st resources
- conventional Linux VPS; Omarchy philosophy only
- frontend-first models
- FAST / FRONTEND / VISION
- 9Router
- Telegram + WhatsApp R1; Telegram first in implementation
- project isolation
- parallel projects / sequential within project
- one writer per project
- revision-specific approval
- bounded shared QA repair budget
- local sandbox QA before external preview
- GitHub + Vercel
- `.vercel.app` default
- existing-domain support
- automated domain purchase deferred

No further open-ended architecture review is required.

Proceed to implementation.
