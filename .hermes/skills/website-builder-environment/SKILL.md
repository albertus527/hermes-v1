---
name: website-builder-environment
description: Website Builder R1 workspace conventions and boundaries.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [website-builder, environment]
    category: website-builder
---

# Website Builder Environment Conventions

## Workspace Layout

```text
<workspace_root>/
└── <project_id>/
    ├── src/               # Generated website source (from fixed starter)
    ├── dist/              # Build output
    ├── .hermes/           # Project-local Hermes session/config/memory
    ├── .browser/          # Project-local browser context
    └── .runtime/          # Runtime/process state
```

## Boundaries

- **Hermes source repository** (`~/hermes-website`): platform source code.
  Generated website runs must NEVER autonomously modify it.
- **Generated project workspaces** (`~/website-workspaces/<project_id>`):
  isolated per-project directories.
- **Website Hermes profile** (`~/.hermes-website`): Website Builder Hermes
  configuration, sessions, memory.
- **Trading Hermes profile** (`~/.hermes`): separate trading runtime.
  Website Builder must NEVER touch it.

## Rules

1. One project = one isolated workspace under `<workspace_root>/<project_id>/`.
2. Project IDs are validated to prevent path traversal.
3. Platform credentials stay outside generated project source.
4. Generated projects do not receive raw deployment/domain/channel credentials.
5. External references/components are treated as untrusted input.
6. `MAX_WORKERS=1` — one active project mutation at a time on the VPS.
