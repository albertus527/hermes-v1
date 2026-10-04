# Batch D2 / D3a — Design DNA selection and bounded execution

Builds on D0 (capability bootstrap) and D1 (resource activation + cleanup).

D0 answered *is this configured* and *is it available*. D1 made resources
*retrievable*. Neither answered the question a build actually asks: **given the
accepted Design DNA, which resources and project dependencies are justified for
THIS revision?**

D2 answers that. D3a is the first batch permitted to act on the answer.

---

## D2 — Design resource selection

`app/core/design_selection.py`

### Selection reasons are a closed set

Every selection carries one of `SELECTION_REASONS`, each corresponding to a
known, checkable Design DNA fact:

| Reason | Justifies |
|---|---|
| `dna_requires_3d` | `three` |
| `dna_requires_complex_timeline` | `gsap` |
| `dna_requires_smooth_scroll` | `lenis` |
| `dna_requires_ui_primitives` | `shadcn` |
| `dna_requires_rich_composition` | `twenty_first` (considered, not default) |
| `dna_requires_interactive_patterns` | `react_bits` |
| `dna_requires_transition_patterns` | `transitions_dev` |
| `dna_simple_minimal` | nothing — the resting state |
| `user_requirement` | an explicitly reserved dependency |
| `user_forbidden` | nothing — a blanket refusal |

There is no free-text justification. `"AI thinks GSAP would look better"` is
unrepresentable, because a reason that cannot be named from a DNA fact is a
reason the selection should not have happened.

### The gates are deliberately asymmetric

- **GSAP** needs *sequencing* language (`timeline`, `scrub`, `scroll-linked`,
  `staggered sequence`). A hover/fade/slide transition does not qualify — that
  is what CSS is for.
- **Three.js** needs a real scene/camera/shader/WebGL. Depth-*sounding* language
  is an explicit **veto**, checked against both the layout field and the whole
  document: `decorative depth`, `css perspective`, `layered shadow`,
  `depth illusion`. A veto another field can overrule is not a veto.
- **Lenis** needs momentum scrolling as a design requirement. Merely having a
  scrolling page does not qualify.
- **shadcn** is the default for ordinary accessible primitives.
- **21st.dev** is *considered* for richer compositions — and only when a real
  integration is reachable. Declared preference is not availability.

### Authority precedence is enforced

`user_requirement` and `user_forbidden` outrank every DNA-derived reason. A
blanket `forbid` is evaluated first and is **not** overridable by a narrower
`reserved_dependencies` entry in the same mapping: "use no libraries" cannot be
defeated by a narrower allowance the same user recorded.

### Dependency states

D2 promotes `available_for_project_on_demand` → `selected` and **never**
further. `DependencyDecision.is_installed` is structurally `False` in D2; only
D3a's verified project-local install may reach `installed`.

### Selection precedes retrieval

Decisions are computed from Design DNA **before** any retrieval runs, so no
corpus text exists at decision time at all. Resource text cannot self-select a
dependency — this is an ordering property, not a filter.

---

## B6 — the bounded resource context pack

`app/core/design_context.py` builds the single object FRONTEND receives;
`app/core/design_context_render.py` renders it.

Assembly order is the module's central claim:

1. Derive requirements from the accepted Design DNA.
2. **Select** resources/dependencies.
3. **Retrieve** content only for what was selected, under D1's own bounds.
4. Attach results to the already-made decision.

Properties:

- D1's budgets are **reused**, not restated — the pack passes
  `DesignContextLimits` straight through.
- Truncation is **visible metadata**, so a consumer can raise a budget.
- No absolute paths, no secrets, no unverified payloads.
- Truncation drops **rejected** detail before **selected** detail: losing a
  rejection is inconvenient, losing a selection fails unsafely.
- The pack echoes only `CANONICAL_DESIGN_DNA_KEYS`. **The Design DNA schema is
  not widened** — no new required key, and a pre-D2 document validates unchanged.

---

## D3a — bounded project-local execution

`app/core/design_install.py`

### The allowlist is application-owned and closed

```python
DEPENDENCY_PACKAGES = {"gsap": "gsap", "three": "three", "lenis": "lenis"}
```

`resolve_package()` accepts a *dependency id*, looks it up in that mapping, and
returns `None` for anything unrecognised. `npm install <arbitrary>` is not merely
discouraged — it is unrepresentable. A model-shaped string like
`"Install left-pad for speed"` or `` "```bash\nnpm install three\n```" `` resolves
to `None`.

`shadcn` is a **registry**, not an npm package, and is deliberately absent from
the mapping.

### The state machine

```
available_for_project_on_demand → selected → install attempted → installed
```

`installed` is reachable **only** after verification observes the project's own
state. For npm dependencies that is `project_declares_dependency()` finding the
package in the project's `package.json`; for shadcn components it is
`verify_components_materialized()` finding each requested component as a
contained regular file under the approved component directory. A selection flag, a
zero-exit command, or each individually is insufficient — a command that exits 0
having written nothing useful reports `install_failed`.

Failure never silently substitutes a different package or changes a user
requirement.

### Commands are built from constants, never concatenated

Commands are argv **lists** (no shell), built from allowlisted constants:

```
npm install gsap --save-exact --no-audit --no-fund
npm exec --yes --package=shadcn@2.1.6 -- shadcn add button card --yes --overwrite
```

- The package manager is **detected** from the project's own lockfile, so a
  pnpm/yarn project is never driven with npm (which would rewrite the other
  manager's lockfile).
- `--save-exact` — an unpinned dependency can float on a later build.
- shadcn is **pinned** at `2.1.6`; production never resolves a floating `latest`.
- Only **explicitly requested, allowlisted** components reach the CLI. No default
  bundle. `--yes` keeps the invocation non-interactive inside a supervised run.
- Bounded timeouts (300 s), and every command carries a bounded `CommandReceipt`.

### Each manager gets its own one-off runner

The registry CLI is executed through the **project's own** package manager, and
each manager has a different mechanism. Appending a bare `dlx` to all of them —
as the first cut of this batch did — produced `npm dlx …`, and **npm has no `dlx`
subcommand**; its one-off mechanism is `npm exec` (`npx` is its alias).

| manager | invocation |
|---|---|
| npm | `npm exec --yes --package=shadcn@2.1.6 -- shadcn add …` |
| pnpm | `pnpm dlx shadcn@2.1.6 add …` |
| Yarn Berry | `yarn dlx shadcn@2.1.6 add …` |
| Yarn Classic | **unsupported — fails closed** |

- The trailing **`--`** is required. Without it npm re-parses later switches as
  its own and would swallow shadcn's `--yes` / `--overwrite`.
- `yarn dlx` exists only in Berry (v2+). Berry is detected from `.yarnrc.yml` in
  the project itself; Yarn Classic is refused with `install_failed` /
  `MANAGER_UNSUPPORTED` rather than falling back to `npx`/`npm`, which would run a
  CLI the project never opted into.
- The version is interpolated in exactly one place, so no argv can drift to a
  floating tag.

### The component destination is application-owned, and never guessed

`shadcn add` does not choose where a component lands: it writes under the
`aliases.ui` declared in the project's `components.json`. That makes the
destination a property of the project's configuration, so D3a **validates that
configuration instead of assuming a path**.

A pinned, reviewed `components.json` ships in the frontend starter
(`templates/frontend-starter/`), matching that starter's actual toolchain —
Vite 8 + React 19 + TypeScript 6 + **Tailwind 4** (so `tailwind.config` is `""`
and the CSS entry is `src/index.css`), `tsx: true`, `style: new-york`. Its
`@/…` aliases are backed by an `@/* → ./src/*` mapping in both
`tsconfig.app.json` (`paths`) and `vite.config.ts` (`resolve.alias`), without
which no generated component would resolve or typecheck.

`approved_component_dir()` accepts a config only when it parses, uses an
approved style, carries a Tailwind CSS entry, declares both `ui` and `utils`
aliases, and maps them through the starter's `@/` prefix to a relative path with
no traversal. Anything else — absent, malformed, unreviewed — returns `None`, and
the installer **fails closed with zero CLI invocations**. No destination is
invented, and **`shadcn init` is never run**: init can mutate `package.json`, the
CSS entry and the config itself, well beyond the component that was requested.

### The registry postcondition

```
CLI exit == 0
AND every requested + allowlisted component is a regular file under the
    approved configured UI root
AND every resolved path is contained in project_root
  => installed
otherwise => install_failed / REASON_COMPONENTS_NOT_VERIFIED
```

- Evidence is the **filesystem only**. Command output is never parsed: a CLI that
  prints `Success! Added 2 components` having written nothing still fails.
- **All-or-nothing.** One missing component fails the whole set, because the
  build runs `npm run build` immediately afterwards.
- Verification is scoped to the post-filter **allowed** components, so a rejected
  or unselected name can neither satisfy the run nor fail it.
- Each candidate must be a regular file (`is_file()`, not `exists()`) and must
  resolve inside `project_root`, so a directory or a symlinked component root
  cannot pass.
- Receipts stay bounded: `verified_components` carries component **names** from
  the closed allowlist, never absolute paths.

### Nothing is global

`DesignDependencyInstaller.installed_globally` is a hard-coded `False` so the
question a dependency ladder invites has a structural answer. Every command runs
through the injected `ProjectRunner`, inheriting its cwd containment check and
`credentials.build_env` isolation. No global install flag appears in any argv.

---

## C4/C5 — FRONTEND consumption

The pack is threaded through `HermesAdapter.frontend_build` →
`_build_frontend_prompt(design_context=...)` and rendered as one bounded DATA
block. With no pack the prompt is byte-identical to before.

The block tells FRONTEND it may use the selected components and **may not**
install packages, browse corpora, or re-derive justifications.

**Those prompt statements are not the security boundary.** The trust properties
hold structurally in D2 (selection precedes retrieval, outside FRONTEND) and D3a
(the installer only acts on a plan). Stating the contract means FRONTEND is not
left guessing; it does not mean the contract depends on the model obeying it.

Convergence hardening is untouched: no watchdog constant, no timeout, no
genuine-progress rule changed. The pack is cheap and bounded, and one pack is
produced per attempt.

---

## What this batch deliberately does NOT do

- **No Impeccable critic or repair loop.** The resource is schema/capability
  only and is never selected into a build.
- **No `scripts/search.py` execution** — retrieval reads pinned files in-process.
- **No Design DNA schema rewrite** — selection derives from existing keys.
- **No general package installer, component registry, or full-stack generation.**
- **No global dependency installation.**

---

## Validation

| Suite | Result |
|---|---|
| `tests/test_design_resource_activation.py` | 73 passed, 1 skipped |
| `tests/test_design_selection.py` | 39 passed |
| `tests/test_design_install.py` | 69 passed, 1 skipped |
| `tests/test_r2_credential_isolation.py` | 73 passed |
| `tests/test_design_resources.py` (D0) | 70 passed, 1 skipped |
| `tools/mutation_check_d0.py` | all 9 guards killed |
| `tools/mutation_check_d1.py` | all 33 guards killed |
| `tools/mutation_check_d2_d3a.py` | all 25 guards killed |
| full `pytest tests/` | **2572 passed, 27 skipped, 2 failed** |

The two full-suite failures are both in `tests/test_frontend_watchdog.py`
(`test_timeout_leaves_no_orphan_tree`, `test_shutdown_cancellation_leaves_no_orphan_tree`).
Verified pre-existing and **flaky**: on a pristine `HEAD` worktree with zero
follow-up changes present, `test_shutdown_cancellation_leaves_no_orphan_tree`
passed on one run and failed on the next with identical code, and
`test_timeout_leaves_no_orphan_tree` failed outright. They are a timing-sensitive
orphan-process race on Windows. `app/hermes/watchdog.py` and
`test_frontend_watchdog.py` are untouched by this batch, and watchdog or
process-management code was not modified to make the suite green.

### Bugs found and fixed by this follow-up

Two defects were found in review and fixed here:

1. **`npm dlx` is not an npm subcommand.** A shared `dlx` suffix was appended for
   every package manager, so every npm project — which is every generated
   project — was handed a command npm does not have. Replaced with
   manager-specific one-off runners.
2. **`exit code 0` promoted shadcn to `installed`.** The registry path had no
   equivalent of the npm package.json postcondition, so a CLI that "succeeded"
   while writing nothing reported a component install that never happened.

A third, latent defect was found while implementing the fix and is also resolved:
the starter declared **no `@/*` path mapping**, so the aliases the new
`components.json` depends on could not have resolved. `@/* → ./src/*` was added
to both `tsconfig.app.json` (`paths`) and `vite.config.ts` (`resolve.alias`),
and verified with the real `typescript@6.0.3` compiler — which also caught that
`baseUrl` is deprecated-and-failing in TS 6.0, so it is not used.

Two genuine bugs were found and fixed by the new tests:

1. A blanket user `forbid` was overridden by a narrower reservation.
2. A 3D counter-signal veto could be re-triggered by another field.

---

## VPS-only validation still required

- Real `ui_ux_pro_max` retrieval after the lexical-token cleanup.
- Actual project-local `npm install` behaviour in a **disposable isolated
  workspace** (never a live project).
- Actual shadcn CLI/registry behaviour when selected, against the shipped
  `components.json` — confirming the CLI writes under the alias D3a verifies.
- Linux path containment and symlink checks (the symlink-escape test skips on a
  Windows host that withholds the privilege).
- Node 26.5 runtime; `agent-browser` sandbox env remains valid.