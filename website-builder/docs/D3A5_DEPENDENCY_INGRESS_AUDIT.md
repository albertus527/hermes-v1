# D3a.5 — Dependency Ingress Audit (Part A)

Status: internal engineering record for the D3a.5 qualification repair.
Scope: every path by which a design resource or registry component can cause a
package.json mutation, a lockfile mutation, or a package-manager invocation.

This audit was produced **before** changing behaviour. It answers the ten
questions in the task and records, for each, whether the pre-repair code was a
real defect or already safe.

## The ingress map

| # | Ingress path | Entry point | Can it reach argv / mutate manifests? |
|---|---|---|---|
| 1 | npm dependency install (D2 selection) | `design_install.DesignDependencyInstaller.install_dependency` | Yes — but only via the closed `DEPENDENCY_PACKAGES` / `DEPENDENCY_PACKAGE_PINS` tables. A raw package string is unrepresentable. |
| 2 | shadcn **builtin** component install | `install_components` → `build_registry_argv` → pinned shadcn `add <component>` | Yes. Component name is bounded by `ALLOWED_SHADCN_COMPONENTS`, **but the CLI writes npm packages (`cn`, `radix-ui`) into package.json itself.** |
| 3 | shadcn **external** registry component install | `design_registry.build_registry_request` → `build_registry_argv` → pinned shadcn `add <locator>` | Yes. Locator is application-owned; the CLI still writes upstream-declared packages into package.json. |
| 4 | 21st.dev / React Bits **catalog fetch** | `design_catalog_fetch.fetch_catalog_payload` | No argv. Returns catalog entities only. |
| 5 | 21st.dev / React Bits **catalog normalization** | `design_catalog.normalize_catalog` | No argv. Maps declared names through the registry resolver. |
| 6 | transitions.dev recipe materialization | `design_transitions.build_add_argv` → pinned `transitions-dev add <slug>` | Yes, but only for a catalog-listed slug; writes Markdown only (verified). |
| 7 | Impeccable critic scan | `design_critic.run_critic_scan` → `node <engine> detect --json --quiet` | Executes the skill's own Node entrypoint; fixed argv; no install. |
| 8 | pinned one-off CLI runners | `design_install.build_pinned_cli_prefix` | Yes, but only from the closed `PINNED_CLIS` table. |
| 9 | capability / activation resolution | `design_activation.activate_resource` | No. Offline, no subprocess, no socket. |
| 10 | manifest load | `design_resources.load_design_resource_manifest` | No execution. Validates adapter names against a closed table. |

## Answers to the ten audit questions

**1. Can any raw upstream package string become package-manager argv?**
Pre-repair: **No** for the npm path — `resolve_package` maps a dependency *id*
through a frozen table and returns `None` for anything else. The registry path
passed only an application-owned *locator*, never a package string.
Post-repair: still **No**, and now also **No** for the version constraint —
`design_npm_spec.parse_npm_package_spec` refuses every hostile npm spec and the
parsed name is only ever looked up, never forwarded.

**2. Can `shadcn add <registry>` install dependencies Hermes did not
independently review?** Pre-repair: **YES — real defect.** The CLI is a black
box that writes packages straight into package.json. Live proof: adding
`https://reactbits.dev/r/SplitText-TS-TW` writes `gsap@^3.15.0` and
`@gsap/react@^2.1.2`; adding the builtin `button` writes `cn@^0.4.0` and
`radix-ui@^1.7.0`. None of those were reviewed by Hermes, and all were left as
floating ranges.

**3. Can an upstream registry mutate package.json/lockfiles with packages
outside `DEPENDENCY_PACKAGES`?** Pre-repair: **YES — same defect.** The
post-install verifier only checked component *files*; it never inspected the
manifest delta. Post-repair: the manifest delta is snapshotted and verified
against an application-owned reviewed set before the install is accepted.

**4. Does Hermes verify the package manifest delta after registry
installation?** Pre-repair: **No.** Post-repair: **Yes** —
`DesignDependencyInstaller.install_components` snapshots direct dependencies
before the CLI runs, computes the delta after, and refuses any package outside
the reviewed set.

**5. Does Hermes verify lockfile/package versions, or only component files?**
Pre-repair: **only component files.** Post-repair: the **direct-dependency**
delta in package.json is verified and normalized to exact application-owned
pins. The full transitive lockfile is deliberately **out of scope** (npm
territory); the guard is for new DIRECT project-level declarations.

**6. Are `registryDependencies` recursively bounded?** Pre-repair: not parsed
at all. Post-repair: a component's `registryDependencies` must equal the
reviewed contract's expected set (empty for SplitText); a non-empty unexpected
set fails closed.

**7. Are peer/dev/optional dependency fields handled?** Pre-repair: **No.** The
snapshot/verify now covers `dependencies`, `devDependencies`,
`optionalDependencies` and `peerDependencies` so a package cannot be smuggled
into a different section.

**8. Can npm ranges, tags, aliases, git URLs, file URLs, workspace specs, or
scoped-package specs bypass the resolver?** Pre-repair: the resolver mapped the
whole string and simply failed to match — so a *versioned* spec like
`gsap@^3.13.0` was silently dropped (a **defect**: requirements were lost), and
a hostile spec would also be dropped but without an explicit refusal. Post-repair:
`parse_npm_package_spec` explicitly refuses aliases (`npm:`), git/ssh/http
URLs, `file:`/`link:`/`workspace:`, dist-tags (`latest`/`next`), wildcards, and
malformed scoped specs, and the raw string is never echoed.

**9. Are comments/docs claiming dependencies are allowlisted when executable
policy disagrees?** Pre-repair: `design_registry`'s docstring claimed the
component's dependencies were "both in the closed dependency allowlist" while
the resolver could not parse `gsap@^3.13.0` and the CLI installed the
packages itself. Post-repair: docs match the code; the reviewed contract is the
executable authority.

**10. Can an approved component change dependency requirements upstream without
Hermes noticing?** Pre-repair: **YES.** A name allowlist cannot detect an added
package. Post-repair: the reviewed contract's *expected set* must equal the
live declared set, so an added dependency makes the component non-installable
until reviewed.

## The trusted registry boundary is a TYPE (consistent with D3a)

D3a expresses a boundary as a **frozen dataclass whose `__post_init__` makes the
invariant a property of construction** (`RegistryInstallRequest`,
`ReviewedComponentContract`) — not a convention every call site must remember.
The trust boundary follows the SAME idiom: `TrustedRegistryBoundary`
(`app.core.design_registry`), assembled by `trusted_registry_boundary()`.

- It holds every input the registry boundary **trusts** (sources, hosts, locator
  templates, approved identities, reviewed contracts, the builtin allowlist and
  its reviewed dep/import/nested rows, the exact registry-introduced pins, the
  pinned CLI spec) and validates them on construction. An incoherent boundary
  raises `ValueError` at construction — it cannot be assembled at all.
- What it deliberately does **NOT** hold: the registry response (the materialized
  file, the declared dependency fields, the ranges, the emitted imports). Those
  are only ever **compared against** the boundary.
- Validated incoherences (each refused): empty/duplicate sources; a host for an
  unknown source; a non-builtin source with no host; a locator template without a
  host; a builtin row missing from the allowlist; a nested builtin outside the
  allowlist; a non-exact introduced pin; a builtin-introduced package with no
  pin; a floating pinned CLI.

**It is load-bearing, not decorative.** `trusted_registry_boundary()` is
CONSULTED on BOTH install paths — the external path in `build_registry_request`
(which builds the only installable request) and the builtin path in
`install_components` (which never goes through the request builder). An
incoherent boundary refuses the request / runs **no command**
(`REASON_BOUNDARY_INCOHERENT`), so a drifted trust model cannot drive a registry
command. "A guard that no execution path depends on is not a guard" is
satisfied by construction: both paths call it.

Pinned by `tests/test_design_registry_contract.py`
(`test_the_live_trusted_boundary_is_coherent`,
`test_an_incoherent_trusted_boundary_cannot_be_constructed`,
`test_the_boundary_serializes_without_untrusted_data`) and the `partbc` guards
*"the trusted boundary type validates on construction"* and *"a non-builtin
registry source must have a host"*.

## The trusted registry boundary (one structural property)

**TRUSTED** — application-owned, in code, never supplied at runtime:

- registry **sources** (`REGISTRY_SOURCES`, closed) and **hosts**
  (`REGISTRY_HOSTS`, closed); **locator templates** (`_LOCATOR_TEMPLATES`)
- **approved identities** (`_APPROVED_COMPONENTS`, derived from contracts) and
  **reviewed contracts** (`_REVIEWED_COMPONENT_CONTRACTS`)
- the builtin **allowlist** (`ALLOWED_SHADCN_COMPONENTS`, 16 names), the
  per-component reviewed **deps/imports/nested** tables
- the **pinned CLI** (`PINNED_CLIS["shadcn"] == shadcn@4.21.0`) and every
  **exact pin** (`REGISTRY_INTRODUCED_PACKAGE_PINS`, `DEPENDENCY_PACKAGE_PINS`)

**UNTRUSTED** — the registry response:

- the component **file** it materializes
- its declared `dependencies` / `registryDependencies` / dev / peer fields
- the **ranges** it writes into `package.json`
- the emitted source's **imports**

**The boundary:** every untrusted value is compared against a trusted table, and
nothing untrusted reaches argv or the manifest without that check —
declared deps must **equal** the contract; a locator must be the **canonical**
one; the manifest delta must be the **reviewed set** at **exact pins**; emitted
imports must be **reviewed**; and **no removal** may occur. `unapproved =>
nothing runs` is structural: a refused request produces **no argv**.

Pinned by `tests/test_design_registry_contract.py`
(`test_no_untrusted_identity_can_reach_argv`,
`test_a_trusted_identity_with_a_hostile_declaration_is_refused`,
`test_a_trusted_identity_with_an_unexpected_nested_dep_is_refused`,
`test_the_trusted_tables_are_the_only_source_of_truth`) and the
`partbc` mutation guard *"the trusted host table is closed"*.

## Boundary statement

Per the task, Hermes does **not** attempt to pin the universe of transitive npm
dependencies. The guarded boundary is the set of dependencies introduced
**directly** by a design registry or resource installation operation.

### The shadcn builtin registry path

`SOURCE_SHADCN_BUILTIN` is a **local, named** registry path, deliberately disjoint
from the external URL path:

- It has **no host** (absent from `REGISTRY_HOSTS`) and **no locator template**
  (absent from `_LOCATOR_TEMPLATES`); `resolve_registry_locator` returns `None`
  for it, and `_APPROVED_COMPONENTS[shadcn_builtin]` is **empty**.
- A builtin is requested by **name** against the 16-name allowlist; its argv is
  `shadcn add <name> --yes --overwrite` — **no URL ever reaches argv**.
- A builtin `RegistryInstallRequest` must carry **no locator** and **no
  dependency ids**: its direct packages are reviewed per component in
  `REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES` (plus emitted-source imports in
  `REVIEWED_BUILTIN_COMPONENT_IMPORTS`). A directly-constructed request supplying
  ids — allowlisted or not — is refused, so a caller cannot assert a dependency
  contract the reviewed table never declared.
- `install_external_component` refuses a builtin request outright (no command),
  so the two paths cannot be crossed in either direction.

### Four separate application-owned sets

The dependency tables are deliberately **separate**, so a registry helper can
never become D2-selectable and a D2 dependency can never be treated as a registry
helper:

| set | table | contents | role |
|---|---|---|---|
| **A** D2-selectable | `DEPENDENCY_PACKAGES` / `DEPENDENCY_PACKAGE_PINS` | `gsap`, `@gsap/react`, `three`, `lenis` (+ `@types/three` companion) | a design decision may pick these |
| **B** builtin registry writes | `REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES` | `cn`, `radix-ui` | what the pinned shadcn CLI writes |
| **C** builtin registry imports | `REVIEWED_BUILTIN_COMPONENT_IMPORTS` (+ `REVIEWED_BUILTIN_COMPONENT_NESTED`) | `lucide-react` | what the CLI does NOT install; Hermes does |
| **D** external contract | `_REVIEWED_COMPONENT_CONTRACTS` | `gsap`, `@gsap/react` | a reviewed external component's direct packages |

The **pinned official shadcn builtin registry dependencies** (B ∪ C) are their own
application-owned set, exactly `REGISTRY_INTRODUCED_PACKAGE_PINS` — **not** set A.
No package NAME is shared between A and B ∪ C, and no registry helper is a D2 id.
Proven by `test_the_builtin_registry_packages_are_a_separate_application_owned_set`.

### The external registry component path

An external component's direct-package allowlist is its **reviewed contract**
(`_REVIEWED_COMPONENT_CONTRACTS`), keyed by `(source, component_id)` and today
exactly one entry: `(react_bits, SplitText)` → packages `{gsap, @gsap/react}`,
no nested `registryDependencies`. `install_external_component` derives its
accepted set from that contract — never from a field on the passed object — and
enforces it in BOTH directions:

- **EXTRA package** (an unreviewed addition) → `REASON_REGISTRY_DEPENDENCY_DRIFT`.
- **MISSING contract package** (a reviewed dependency the registry dropped) →
  `REASON_REGISTRY_PACKAGE_NOT_EXACT`. SplitText's source imports both `gsap`
  and `@gsap/react`, so a run that lands only `gsap` cannot build and must fail.
- Both packages are installed at their exact application pins (`gsap 3.15.0`,
  `@gsap/react 2.1.2`); an emitted-source import outside the contract is refused
  (`REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED`).
- A component with no reviewed contract is non-installable (`REASON_CONTRACT_MISSING`).

### Decision: lucide-react is REGISTRY-INTRODUCED, not pre-provisioned (Option 2)

`lucide-react` is a package the pinned shadcn CLI's emitted sources import but the
CLI does **not** install (5 of the 16 builtins: accordion, checkbox, dialog,
select, sheet). Two designs were possible; the chosen one:

**`lucide-react` remains a reviewed, application-owned, exact-pinned package that
Hermes installs only when a reviewed builtin's emitted source requires it.**

- It is **not** added to the shared starter template
  (`templates/frontend-starter/`), and it is **not** a D2-selectable dependency
  (`DEPENDENCY_PACKAGES`). It is a registry-introduced pin
  (`REGISTRY_INTRODUCED_PACKAGE_PINS["lucide-react"] == "1.52.0"`).
- Rationale:
  1. **Boundary.** `templates/frontend-starter/` is shared infra **outside**
     `website-builder/`; pre-provisioning there would widen this batch beyond its
     website-only boundary and change a template other paths copy verbatim.
  2. **Only 5 of 16 builtins need it.** Pre-provisioning would add an unused
     dependency to *every* generated project, including the 11 that never import
     it — a scope increase, not a narrowing.
  3. **The starter's own source imports no lucide** (only `components.json`
     declares `"iconLibrary": "lucide"`), so there is nothing to satisfy at
     build time until a component that needs it is installed.
  4. **It is already fail-closed.** Whether a project ships lucide or not, the
     install path verifies/installs the exact pin; a project already at the pin
     runs **no** install, and a missing one installs exactly once at `1.52.0`.
- Proven by `tests/test_design_registry_mutation.py`:
  `test_lucide_react_is_registry_introduced_not_d2_selectable`,
  `test_a_project_without_lucide_gets_the_exact_pin_installed`,
  `test_a_project_already_shipping_lucide_at_the_pin_installs_nothing`.

### Intended policy, stated explicitly (Option 1)

**`package.json` direct-dependency declarations are authoritative. The
transitive tree is out of scope.**

- **A lockfile that grows is OBSERVED, not policed.** Every real install grows
  `package-lock.json` / `pnpm-lock.yaml` / `yarn.lock` with transitive packages.
  That is the package manager's territory; a lockfile change alone must **not**
  fail an install. Hermes reads the lockfile only to *detect the package manager*
  (`LOCKFILES`) — never as a dependency-policy authority.
- **A new DIRECT dependency declaration is REFUSED** unless it is in the
  application-owned reviewed set — regardless of what the lockfile says.
- **No full lockfile solver.** Hermes does not resolve, diff, or verify the
  transitive graph, and does not attempt to freeze the transitive tree. The
  guard is the set of DIRECT project-level declarations a design install causes.
- Rationale: the VPS-proven defects were all direct-declaration defects (an
  unreviewed package added to `package.json`, an upstream range left un-pinned,
  a source import with no direct dependency, a removed direct dependency).
  Policing the transitive tree would add a solver this batch explicitly refuses
  while proving nothing about the boundary that actually matters.

Proven by `tests/test_design_registry_mutation.py`:
`test_a_lockfile_that_only_grows_is_accepted` and
`test_a_new_direct_dependency_is_refused_even_if_the_lockfile_also_grows`.

## Second-pass audit (same class, one layer down)

After the first repair, the whole design-resource ingress was re-swept for the
**same class** of bug — a dependency set that a caller/registry could widen:

- **Execution surface.** Only two design modules execute anything:
  `design_critic` (fixed argv, `shell=False`) and `design_install` (five
  `_run` sites, every one fed a builder-produced argv). All other `run_command`
  call sites in the app use hardcoded argv (`npm ci`, `npm run build`, …). No
  design string reaches a shell.
- **Non-execution modules** (`design_retrieval`, `design_context`,
  `design_selection`, `design_policies`, `design_capabilities`, `design_dna`,
  `design_catalog`, `design_resources`) only READ files — no writes, subprocess,
  or network.
- **Two layer-2 gaps found and closed:**
  1. `RegistryInstallRequest.__post_init__` only checked that dependency ids
     were *allowlisted*, not that they matched the **reviewed contract**. The
     type is public, so a directly-constructed request could carry a **superset**
     of allowlisted ids. Now the constructor binds the set to the contract
     (external path) and the allowlist check still guards the builtin path.
  2. `install_external_component` derived its accepted package set from a field
     on the passed object. A duck-typed stand-in could widen it. It now resolves
     the **reviewed contract** itself and derives the accepted set from that, so
     the argument cannot expand the boundary.
- **Coherence guard added:** `ALLOWED_SHADCN_COMPONENTS` and
  `REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES` must have identical key sets, and
  every reviewed builtin package must be exact-pinned.

## Third-pass audit (built-in shadcn — the emitted-source import closure)

The built-in path was audited for the **same class** as the SplitText gap: a
component reported `installed` whose build cannot resolve its imports. Live
against `shadcn@4.21.0` in a copy of the real starter, `shadcn add <builtin>`
writes only the registry-declared packages, but the emitted `.tsx` imports more:

| builtin | CLI writes | emitted source imports (not provided) |
|---|---|---|
| accordion, checkbox, select, sheet | `cn`, `radix-ui` | **`lucide-react`** |
| **dialog** | `cn`, `radix-ui` | **`lucide-react`**, **`@/components/ui/button`** |

`lucide-react` is neither declared in the registry item nor installed, and the
starter declares it in no section; `dialog.tsx` also imports a sibling `button`
the item does not list in `registryDependencies`, so `shadcn add dialog` creates
only `dialog.tsx`. `npx tsc -b` on the result fails with `TS2307` for both. This
is the **same class** as the external-component boundary, applied one layer down.

- **Fix:** `REVIEWED_BUILTIN_COMPONENT_IMPORTS` records each builtin's reviewed
  source imports; `REVIEWED_BUILTIN_COMPONENT_NESTED` records the reviewed nested
  closure (`dialog -> button`). `install_components` expands the requested set by
  the **application-owned** closure (never upstream metadata), installs any
  missing required import through the same exact-pin mechanism, and verifies
  every requested **and nested** component materialized. A required import with
  no reviewed pin, or a failed exact-pin install, fails closed.
- **`lucide-react` is exact-pinned at `1.52.0`** (`REGISTRY_INTRODUCED_PACKAGE_PINS`),
  the version the starter's own `components.json` (`"iconLibrary": "lucide"`)
  already intends. Verified: the real starter now typechecks clean (`tsc -b`
  exit 0) after the builtin install + the exact-pin install of `lucide-react`.
- **Coherence guards added:** the import table's key set equals
  `ALLOWED_SHADCN_COMPONENTS`; every imported package has an exact pin.

## Fourth-pass audit (the emitted SOURCE is untrusted, not just the identity)

The component **identity** is reviewed (allowlisted); the **installation** is
not. The registry chooses which file to materialize, and that file may import a
package the registry never declared — so the `package.json` delta is clean and
the earlier guards pass, yet the build cannot resolve. Confirmed live: an
approved `button` whose emitted source added `import { Widget } from
"brand-new-unreviewed-pkg"` was reported `installed` with `brand-new-unreviewed-pkg`
absent from the manifest.

- **Fix:** after materialization, the emitted source of every installed component
  (builtin closure and external component) is parsed for its **bare package
  specifiers** and each must be in the application-owned reviewed set —
  `react`, `react-dom`, `cn`, `radix-ui`, `lucide-react`, plus, for an external
  component, that component's reviewed contract packages. Anything else fails
  closed (`REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED`).
- **Bounded and total:** `bare_package_of` ignores relative paths and the `@/`
  project alias, strips subpaths (`gsap/ScrollTrigger` → `gsap`) and preserves
  scopes (`@gsap/react`); reads are capped at `_MAX_COMPONENT_SOURCE_CHARS`.
- **The identity is trusted; the installation is not** — this is the boundary
  the four passes converge on: allowlisted identity + reviewed contract +
  manifest-delta guard + emitted-source import guard.

## Fifth-pass audit (mutations: removals, not just additions)

Every prior guard compared **additions** (`dependency_delta` reports what
`after` CONTAINS). A package the install silently **REMOVED** was invisible:
confirmed live, `install_components(["button"])` was reported `installed` while a
pre-existing `react` dependency was dropped from `package.json`.

- **Fix:** `removed_direct_dependencies(before, after)` reports every package
  present in `before` and absent from `after`; the delta guard treats any removal
  as offending (`REASON_REGISTRY_DEPENDENCY_DRIFT`). A whole-operation
  `_removal_after_install(before)` check runs last on both the builtin and
  external paths, catching a removal performed by a NORMALIZATION install (which
  runs after the delta guard and so is not covered by it).
- **Rule:** a registry install may ADD its reviewed packages; it must never
  DELETE a pre-existing project dependency.

## Final dependency policy (post-repair)

- **Package identity is closed and application-owned.** `DEPENDENCY_PACKAGES`
  maps a dependency *id* to a package name; nothing else can produce a package.
- **Every install is an EXACT pin.** `DEPENDENCY_PACKAGE_PINS` holds
  `gsap 3.15.0`, `gsap_react (@gsap/react) 2.1.2`, `three 0.186.1`,
  `lenis 1.3.26`; `three`'s companion `@types/three 0.186.0` lands in
  `devDependencies`.
- **Registry-introduced helpers are exact-pinned too.** `cn 0.4.0` and
  `radix-ui 1.7.0` (`REGISTRY_INTRODUCED_PACKAGE_PINS`), verified live against
  shadcn@4.21.0.
- **The registry cannot bypass the boundary.** After `shadcn add`, the DIRECT
  dependency delta (all four sections) is compared to the reviewed set; any
  unreviewed package, or a reviewed package in the wrong section, fails the
  install. Surviving ranges are normalized to the exact pins.
- **Upstream version constraints are checked, never installed.**
  `parse_npm_package_spec` + `constraint_is_satisfied` prove the app pin is
  inside the declared range (`gsap@^3.13.0`, `@gsap/react@^2.1.2`); the range is
  never forwarded.
- **A component is installable only if it matches its reviewed contract.**
  `_REVIEWED_COMPONENT_CONTRACTS[(react_bits, SplitText)]` expects
  `{gsap, gsap_react}` and no nested registry dependencies; a missing, extra, or
  unexpected entry fails closed.

## Impeccable parser runtime (Part H)

- The skill-v4.1.0 artifact ships **no** `package.json`; `detect.mjs` needs four
  npm modules for the full static HTML/CSS engine. Without them it prints
  `DEGRADED - HTML parser modules unavailable` and regex-falls-back (an
  **undercount**).
- Exact pins (from the upstream `package.json`): `htmlparser2 12.0.0`,
  `css-select 7.0.0`, `css-tree 3.2.1`, `domutils 4.0.2`.
- They are provisioned **only** during explicit Hermes design-profile
  setup/provisioning (`IMPECCABLE_PARSER_PACKAGE_PINS`), into the skill's own
  `node_modules`. A website build never installs them.
- Capability reporting distinguishes **critic callable** (`critic_available`)
  from **full-quality** (`critic_degraded=False`, `degraded=False`). A degraded
  scan is `critic_available=True, critic_degraded=True, authoritative=False` —
  a clean result there is never authoritative.

## Manual VPS smoke commands

```bash
# 1. SplitText resolves to the reviewed contract.
python -c "from app.core.design_registry import build_registry_request as b; \
  o=b('react_bits','SplitText',declared_dependencies=['gsap@^3.13.0','@gsap/react@^2.1.2']); \
  print(o.ok, o.request.required_dependency_ids)"

# 2. React Bits catalog discovery (live).
python -c "from app.core.design_catalog_fetch import discover_catalog as d; \
  r=d('react_bits'); print(r.ok, 'SplitText' in r.installable_ids())"

# 3. 21st is credential-gated (no free discovery).
python -c "from app.core.design_activation import activate_resource as a; \
  from app.core.design_resources import load_design_resource_manifest as m; \
  c=a(__import__('pathlib').Path('/tmp'), m().get('twenty_first'), system='Linux', machine='x86_64'); \
  print(c.discovery_available, c.authentication_required, c.degraded)"

# 4. Impeccable engine quality (full / degraded / missing).
python -c "from app.core.design_activation import engine_quality as q; \
  from pathlib import Path; print(q(Path.home()/'.hermes'/'skills'/'impeccable')))"
```
