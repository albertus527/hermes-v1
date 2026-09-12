# Frontend Starter (Website Builder R1)

The single fixed frontend starter for autonomous website builds, per
[`WEBSITE_BUILDER_R1_CANONICAL_SPEC.md`](../../docs/WEBSITE_BUILDER_R1_CANONICAL_SPEC.md)
§13 (_Fixed Frontend Runtime_) and
[`WEBSITE_BUILDER_R1_IMPLEMENTATION_PLAN.md`](../../docs/WEBSITE_BUILDER_R1_IMPLEMENTATION_PLAN.md)
Phase 1 step 4.

Every project uses this stack. Projects do not invent their own.

## Ownership

This directory is **platform-owned source material**.

An autonomous website build **copies** this starter into an isolated project
directory under `WEBSITE_WORKSPACE_ROOT`, then works only inside that copy.

> Generated projects must NEVER modify `templates/frontend-starter/`.

The project runner that performs the copy is **not** implemented yet.

## Stack

| Concern    | Choice                                                        |
| ---------- | ------------------------------------------------------------- |
| UI library | React 19                                                      |
| Build tool | Vite 8                                                        |
| Language   | TypeScript 6                                                  |
| Styling    | Tailwind CSS 4                                                |
| Runtime    | Node.js 26 (`.nvmrc` → `26.5.0`, `engines.node` → `>=26 <27`) |

Nothing else. No Next.js, no component library, no animation library, no
state-management framework, no backend, no database, no auth, no CMS.

## Fixed commands

```bash
nvm use                # Node 26.5.0 (see .nvmrc)

npm ci                 # reproducible install from package-lock.json
npm run dev            # local development server
npm run build          # tsc -b && vite build  ->  dist/
npm run preview        # serve the production build
npm run typecheck      # tsc -b only
```

`npm ci` is the install command for every copied project — the committed
`package-lock.json` is what makes builds reproducible. Use `npm install` only
when deliberately changing dependencies of the starter itself.

## Layout

```text
templates/frontend-starter/
├── .npmrc               # engine-strict: enforce Node >=26 <27
├── .nvmrc               # 26.5.0
├── index.html           # single HTML entry point
├── package.json         # fixed dependencies + fixed scripts
├── package-lock.json    # committed; required by `npm ci`
├── tsconfig.json        # solution file
├── tsconfig.app.json    # src/ (DOM, react-jsx, strict)
├── tsconfig.node.json   # vite.config.ts
├── vite.config.ts       # react + tailwind plugins
├── public/
│   └── favicon.svg      # neutral placeholder icon
└── src/
    ├── App.tsx          # neutral placeholder page
    ├── index.css        # Tailwind import + minimal global baseline
    ├── main.tsx         # React root
    └── vite-env.d.ts
```

## Baseline included

- **Responsive**: mobile-first, fluid container, no fixed-width layout, media
  capped at `max-width: 100%`.
- **Semantic HTML**: `header` / `nav` / `main` / `section` / `footer`
  landmarks, one `h1`, labelled sections.
- **Minimal global CSS**: Tailwind Preflight plus a small baseline suited to
  marketing/informational sites — readable line height, balanced headings,
  visible `:focus-visible` ring, skip link, `prefers-reduced-motion`, and
  light/dark `color-scheme`.
- **Neutral placeholder `App`**: proves the toolchain renders. It carries no
  business content and is replaced wholesale by a generated project.

## Conventions for generated projects

- Keep the fixed scripts (`dev`, `build`, `preview`, `typecheck`) working — QA
  and deployment rely on them.
- Build output is `dist/` (static). Do not change `outDir`.
- The dev/preview servers bind `host: true` with non-strict ports so the
  project runner can assign a free port (`npm run dev -- --port <n>`).
- Do not commit secrets. `.env*` is git-ignored inside a project copy.
