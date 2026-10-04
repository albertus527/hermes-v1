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

`installed` is reachable **only** after `project_declares_dependency()` observes
the package in the project's own `package.json`. A selection flag, a
zero-exit command, or each individually is insufficient — a command that exits 0
having written nothing useful reports `install_failed` with
`reason = "the install command succeeded but the package is absent from the
project manifest; the state is not upgraded to installed"`.

Failure never silently substitutes a different package or changes a user
requirement.

### Commands are built from constants, never concatenated

Commands are argv **lists** (no shell), built from allowlisted constants:

```
npm install gsap --save-exact --no-audit --no-fund
npm dlx shadcn@2.1.6 add button card --yes --overwrite
```

- The package manager is **detected** from the project's own lockfile, so a
  pnpm/yarn project is never driven with npm (which would rewrite the other
  manager's lockfile).
- `--save-exact` — an unpinned dependency can float on a later build.
- shadcn is **pinned** at `2.1.6`; production never resolves a floating `latest`.
- Only **explicitly requested, allowlisted** components reach the CLI. No default
  bundle. `--yes` keeps the invocation non-interactive inside a supervised run.
- Bounded timeouts (300 s), and every command carries a bounded `CommandReceipt`.

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
| `tests/test_design_install.py` | 43 passed |
| `tests/test_design_resources.py` (D0) | 70 passed, 1 skipped |
| `tools/mutation_check_d0.py` | all 9 guards killed |
| `tools/mutation_check_d1.py` | all 33 guards killed |
| `tools/mutation_check_d2_d3a.py` | all 21 guards killed |
| full `pytest tests/` | **2547 passed, 26 skipped, 1 failed** |

The single full-suite failure is
`tests/test_frontend_watchdog.py::test_shutdown_cancellation_leaves_no_orphan_tree`
— the known Windows watchdog/orphan-tree baseline. Verified pre-existing: it fails
identically on a pristine `HEAD` worktree with zero D-batch files present, and
`app/hermes/watchdog.py` and `test_frontend_watchdog.py` are untouched by this
batch. Watchdog and process-management code was not modified to make the suite
green.

Two genuine bugs were found and fixed by the new tests:

1. A blanket user `forbid` was overridden by a narrower reservation.
2. A 3D counter-signal veto could be re-triggered by another field.

---

## VPS-only validation still required

- Real `ui_ux_pro_max` retrieval after the lexical-token cleanup.
- Actual project-local `npm install` behaviour in a **disposable isolated
  workspace** (never a live project).
- Actual shadcn CLI/registry behaviour when selected.
- Linux path containment and symlink checks.
- Node 26.5 runtime; `agent-browser` sandbox env remains valid.