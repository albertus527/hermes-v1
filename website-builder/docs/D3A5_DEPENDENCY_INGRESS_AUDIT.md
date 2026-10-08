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
| 4 | 21st.dev / React Bits **catalog fetch** | `design_catalog_fetch.fetch_catalog_payload` / `build_discovery_url` | No argv. Returns catalog entities only. The URL is a module constant plus an encoded search term; no URL parameter exists. |
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
(which builds the only installable request) AND in the executing
`install_external_component`, and the builtin path in `install_components` (which
never goes through the request builder). An incoherent boundary refuses the
request / runs **no command** (`REASON_BOUNDARY_INCOHERENT`), so a drifted trust
model cannot drive a registry command.

**A defect found and fixed here.** The claim above was only *half* true before this
pass: `build_registry_request` consulted the boundary, but `install_external_component`
— the PUBLIC executing function — did **not**, and a duck-typed stand-in bypasses
the request builder entirely. Verified: with an incoherent boundary (a builtin dep
row outside the allowlist), `install_external_component` STILL ran
`shadcn add https://reactbits.dev/r/SplitText-TS-TW`. This is the SAME class as the
locator bypass already fixed in that function ("`install_external_component` is
public and a duck-typed stand-in bypasses the constructor"). Fix: consult the
boundary in `install_external_component` itself, mirroring `install_components`, so
"no registry command from a drifted trust model" is a property of the EXECUTING
function, not of the caller having gone through the request builder.

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

### 21st.dev discovery: the false positive is REPLACED, not merely muted

Part G removed a false-positive parser that turned 21st's ROUTE/CATEGORY links
(`/community/components/popular`, `/components/s/hero`) into fabricated catalog
identities. That left 21st with a *dead* adapter: it still fetched `llms.txt`,
which publishes NO component-identity schema, and so produced ZERO identities
even with a credential.

The replacement is the REAL machine surface, verified live (2026-10) from
`https://21st.dev/openapi.json`:

```
GET https://21st.dev/api/v1/components/search?q=<term>&scope=public&limit=24
  -> HTTP 401 without `Authorization: Bearer <21st_sk_...>`
  -> 200 {"query","scope","results":[{slug,name,install_ref,...}]}
```

- **Credential-gated, honestly.** `CREDENTIAL_REQUIRED_FOR_DISCOVERY` names 21st;
  without a key NO request is attempted (a 401 round-trip is not a useful
  degradation). The state is `REASON_CREDENTIAL_REQUIRED`, and the capability
  layer already reports `discovery_available=False` / `degraded=True`.
- **The request cannot be widened.** `build_discovery_url` builds it from module
  constants plus a BOUNDED (`MAX_QUERY_CHARS`), PERCENT-ENCODED search term --
  there is no URL/host/locator parameter. A hostile term
  (`x&scope=team&evil=https://evil.example`) is inert data.
- **Results are proposals, not identities.** The JSON flows through the unchanged
  `normalize_catalog`; nothing reviewed for 21st means nothing installable.
- **No fabrication.** A malformed or empty search response is a degraded empty
  result, never an invented entry.

### The sections the guard reasons over: `dependencies` is the FLOOR

The instruction names `dependencies` as the MINIMUM the guard must reason over.
It is the first member of the reviewed surface and is snapshotted
UNCONDITIONALLY -- even with an empty allowed set, an addition to or a removal
from `dependencies` is refused, because the section is part of the snapshot, not
opted into by a caller.

Every dimension of `dependencies` is covered:

| dimension | handled by |
|---|---|
| reviewed addition | membership + section check (`verify_direct_dependency_delta`) |
| unreviewed addition | membership check -> refused |
| version change (either direction) | `dependency_delta` -> refused |
| removal | `removed_direct_dependencies` -> refused |
| wrong section (a runtime pkg moved to dev/optional/peer) | section check -> refused |
| non-exact range | the SEPARATE exactness dimension (`--save-exact`, `project_satisfies_spec`, `reviewed_registry_package_pins`) |

The four reviewed sections are a MINIMUM, not a maximum: everything OUTSIDE them
is frozen by `changed_manifest_sections`, so a mutation to any other top-level
section (``overrides``, ``resolutions``, ``pnpm``, ``packageManager``,
``scripts``, ``engines``, ``workspaces``, ``bundle(d)Dependencies``,
``peerDependenciesMeta``, ``publishConfig``, ``browser``, ``exports``,
``imports``, ``files``, ``type``, ``private``) is refused too.

A test pins `dependencies` as a REQUIRED section and a mutation guard proves that
dropping it from ``SNAPSHOT_SECTIONS`` fails the suite.

### `devDependencies`: reasoned over, with the mapping form for its one legitimate shape

`devDependencies` is part of the reviewed surface and is snapshotted
unconditionally. It differs from `dependencies` in one way: a reviewed package is
LEGITIMATELY added there -- ``@types/three`` is the companion of ``three`` -- so
the allowed SECTION for that package is ``devDependencies``, not the runtime
default. That is exactly what the verifier's MAPPING form
(``package -> the ONE section it may be added to``) is for; the SEQUENCE form
(the registry shape, where every reviewed helper is runtime) maps everything to
``dependencies``, so a registry reviewed package placed in ``devDependencies`` is
refused.

| dimension of `devDependencies` | handled by |
|---|---|
| reviewed addition (``@types/three``) | mapping form -> accepted |
| unreviewed addition | membership -> refused |
| reviewed pkg via the runtime-only sequence form | section check -> refused |
| version change | the SEPARATE exactness dimension (see below) |
| removal | ``removed_direct_dependencies`` -> refused |

**The membership / exactness split.** ``verify_direct_dependency_delta`` checks
WHICH packages and their SECTION; a VERSION change of an already-allowed package
passes membership by design. That dimension is the SEPARATE exactness check:
``--save-exact`` on the argv, ``project_satisfies_spec`` /
``project_satisfies_dependency`` as the postcondition, and
``reviewed_registry_package_pins`` on the registry path. Verified for
``devDependencies`` specifically: the postcondition refuses ``@types/three`` at a
non-exact pin (``0.100.0`` and ``^0.186.0`` both fail; ``0.186.0`` passes), so no
version change to a reviewed devDependency survives an ``installed`` claim.

A test pins ``devDependencies`` as a reviewed section, the mapping-form addition,
the sequence-form refusal, the removal, and the exactness backstop. A mutation
guard proves that dropping it from ``SNAPSHOT_SECTIONS`` fails the suite (it would
become a FROZEN section, so the legitimate ``three`` install -- which adds
``@types/three`` to ``devDependencies`` -- would be refused).

### `optionalDependencies`: reasoned over, and double-covered by the freeze layer

`optionalDependencies` is part of the reviewed surface and is snapshotted
unconditionally. Unlike ``dependencies`` and ``devDependencies``, **no
application-owned spec targets it today** -- the four D2 specs write only
``dependencies`` (gsap, @gsap/react, three, lenis) and ``devDependencies``
(@types/three). It is therefore included **defensively**: an unreviewed package
smuggled into ``optionalDependencies`` must still be refused.

It is caught by TWO independent layers:

1. **the delta/membership check** -- because it is in ``SNAPSHOT_SECTIONS``, an
   addition there is compared against the reviewed set and the allowed section;
2. **the section-freeze check** -- as a backstop, ``changed_manifest_sections``
   catches ANY change outside the reviewed surface, so even if
   ``optionalDependencies`` were removed from ``SNAPSHOT_SECTIONS`` an
   unreviewed addition would still be refused (verified on a mutated tree: the
   verdict flips from ``added=('optionalDependencies', 'evil')`` to
   ``sections_changed=('optionalDependencies',)`` -- refused either way).

| dimension of `optionalDependencies` | handled by |
|---|---|
| unreviewed addition | delta/membership -> refused (and the freeze as a backstop) |
| reviewed addition via the mapping | accepted |
| reviewed pkg via the runtime-only sequence form | section check -> refused |
| removal | ``removed_direct_dependencies`` -> refused |
| version change | the separate exactness dimension |

Because no spec targets it, dropping it from the reviewed surface is caught by the
delta classification changing (the section-specific test fails) rather than by a
legitimate install breaking -- noted honestly so the guard is not over-claimed.

### `peerDependencies`: reasoned over, and `peerDependenciesMeta` frozen beside it

`peerDependencies` is the last of the four reviewed sections and is snapshotted
unconditionally. Like ``optionalDependencies``, no application-owned spec targets
it today, so it is included DEFENSIVELY and is caught by the same TWO layers: the
delta/membership check (it is in ``SNAPSHOT_SECTIONS``) and the section-freeze
backstop (verified on a mutated tree: dropping it flips the verdict from
``added=('peerDependencies', 'evil')`` to
``sections_changed=('peerDependencies',)`` -- refused either way).

Its sibling ``peerDependenciesMeta`` -- which marks a peer optional/required -- is
a **separate top-level key OUTSIDE the surface**, so any change there is frozen
by ``changed_manifest_sections`` and refused. Verified.

| dimension of `peerDependencies` | handled by |
|---|---|
| unreviewed addition | delta/membership -> refused (freeze as a backstop) |
| reviewed addition via the mapping | accepted |
| reviewed pkg via the runtime-only sequence form | section check -> refused |
| removal | ``removed_direct_dependencies`` -> refused |
| version change | the separate exactness dimension |
| ``peerDependenciesMeta`` change | outside the surface -> frozen -> refused |

A test pins the reviewed surface as EXACTLY the four sections, so no section can
be silently added or lost. A mutation guard proves that dropping
``peerDependencies`` fails the suite (the delta classification changes).

### The comparison identifies a package MOVED between sections

A relocated package (``dependencies`` -> ``devDependencies``) was previously
reported only as a name appearing in BOTH ``added`` and ``removed`` -- the move was
**inferable but not stated**. The instruction requires it be identified, so the
verdict now carries ``moved`` as a first-class finding, a
``(from_section, to_section, package)`` triple.

Crucially, a relocated package was never genuinely ADDED (it existed before) and
never genuinely REMOVED (it exists after), so keeping it in ``added``/``removed``
was the same defect as conflating a version change with an addition. The move is
now reported in ``moved`` ONLY, and the five buckets **partition** the delta:

```
moved:   [["dependencies", "devDependencies", "cn"]]
added:   [["dependencies", "evil"]]        <- an independent addition
removed: [["dependencies", "react"]]       <- an independent removal
```

| case | identified as |
|---|---|
| a move between any two reviewed sections | ``moved=(from, to, pkg)`` |
| a move of a REVIEWED package | ``moved`` -> refused (allowed to add into a section is not a licence to move) |
| a move + an independent addition + removal | each in its own bucket; no name in two buckets |
| a move that ALSO re-pins | ``moved`` (the wrong section is the more actionable finding; the raw ``dependency_delta`` still carries the version) |

### The comparison identifies a REMOVED direct package

The before/after comparison identifies a removed direct package via
``removed_direct_dependencies`` -- necessary because ``dependency_delta`` only
reports what ``after`` CONTAINS, so a silent deletion is invisible to it. Verified
across every dimension:

| case | identified as |
|---|---|
| package removed, section remains | ``removed=('dependencies', 'cn')`` |
| package removed, its reviewed section disappears | ``removed=(...)`` (a non-empty section always names its packages) |
| a package MOVED between sections | BOTH ``removed`` (original section) and ``added`` (new section) |
| a removal of a REVIEWED package | ``removed`` -- allowed to ADD a reviewed package is not allowed to REMOVE one |
| a version change | ``changed``, NOT ``removed`` |
| a correctly-placed reviewed addition | neither bucket |

**Honest boundary (out of scope, not a defect):** an *empty* reviewed section
(``devDependencies: {}``) disappearing is not reported -- it has **no package to
name**, so it is not a dependency mutation. No package can be hidden this way: a
section WITH packages always names them, and a move to another section is caught
by both buckets.

### The comparison identifies an ADDED package as an addition, not a re-pin

The before/after comparison must identify **an added direct package**. It did, but
the field was named ``added`` while holding BOTH a genuinely new package AND a
version change of an existing one -- ``dependency_delta`` reports a package when
``before[name] != after[name]``, so the two landed in the same bucket and a
consumer could not tell "a new package appeared" from "an existing package was
re-pinned". That is the same class as the rest of this batch: a name narrower than
the property it reports.

Fix: the verdict now carries ``added`` (genuinely NEW -- absent from ``before``)
and ``changed`` (already present, version moved) as SEPARATE fields, and
``to_dict`` emits both. Both are still offending, so ``ok`` is unchanged; the split
is about IDENTITY, not policy. The ``ok``/payload binding covers ``changed`` too.

```
added:   [["dependencies", "evil"]]    <- a new package
changed: [["dependencies", "react"]]   <- 19.3.0 -> 19.4.0
```

### The verifier is WIRED into every operation allowed to mutate the manifest

Auditing which operations may mutate a generated project's `package.json` found
**three** that run a package manager, but only **two** verified a delta:

| operation | runs a package manager | verified a delta |
|---|---|---|
| `install_components` (shadcn builtin) | yes | yes |
| `install_external_component` (registry) | yes | yes |
| **`install_dependency` (D2 npm)** | yes | **NO** |

`install_dependency` ran `npm install <pkg>@<ver> --save-exact` (which runs the
package's own **lifecycle scripts** -- untrusted upstream code) and then verified
only that the REQUIRED spec was PRESENT. That postcondition is blind to an EXTRA
dependency, a removal, or a section change. Reproduced end-to-end: a runner that
adds an extra direct dependency alongside the requested one still reported
`installed`.

**This is a real dependency mutation**, so the guardrail's condition is met and the
fix belongs here.

Fix: `install_dependency` now captures `snapshot_direct_dependency_state` BEFORE
any command and calls `verify_direct_dependency_delta` AFTER the postcondition
passes, with the allowed set derived from the application-owned spec table
(`package -> the ONE section each belongs in`, so `three` -> `dependencies` and
`@types/three` -> `devDependencies` are both accepted while a wrong placement is
refused). A non-ok verdict returns `INSTALL_FAILED` with the verdict's reason.

Verified: an extra dependency, a removed project dependency, and an unexpected
section are all refused; a clean `gsap` install and a two-section `three` install
still report `installed` (no false positive).

### A reusable bounded direct-dependency snapshot/diff verifier

New: one reusable check an operation can wrap around ANY mutation, instead of each
site re-deriving the delta logic:

```
snapshot = snapshot_direct_dependency_state(project_root)   # before
...operation...
verdict  = verify_direct_dependency_delta(snapshot, snapshot_direct_dependency_state(project_root),
                                          allowed_packages=reviewed_set)
```

`DirectDependencySnapshot` pairs BOTH views in one capture -- the four reviewed
dependency sections AND every top-level `package.json` key -- so an operation does
not have to remember which snapshots to take.

`DirectDependencyDeltaVerdict` binds `ok` to its payload AT CONSTRUCTION: `ok=True`
iff no offending delta, and a refusal must name at least one offending item plus a
reason. The verdict names only NAMES (sections, packages) -- never a manifest value
-- so it is safe to log verbatim.

The verifier composes the existing primitives rather than re-implementing them:
`dependency_delta` (filtered by the reviewed set + the runtime section),
`removed_direct_dependencies` (a delta is blind to a silent deletion), and
`changed_manifest_sections` (a section outside the reviewed surface can redirect a
package version or SOURCE). An EMPTY `allowed_packages` accepts no addition -- the
conservative default.

Bounded: the DECISION is computed from the full delta; only the REPORTED names are
capped (`_MAX_DELTA_NAMES`), and `truncated` records when the cap bites.

Verified: agreement with `registry_dependency_delta_is_acceptable` (the primitive
the install paths call), the full decision matrix, the construction-time
invariants, the bound, and names-only serialization. Pinned by 4 mutation guards.

### The D3a.5 mutation drivers: a KILL is a FAILED test, not an exit code

While pinning the reduced-motion guard, a defect in the D3a.5 drivers themselves
surfaced. Each driver mutates a guard and checks the focused tests go RED, then
reports `[KILLED]`. But "red" has two causes:

```
1 failed, 38 passed in 0.74s   -> a real FAILURE   -> the guard is load-bearing
1 error in 0.24s               -> collection error -> the tests NEVER RAN
```

The drivers matched only the PLURAL `" errors in "`, while pytest prints
`"1 error in"` (SINGULAR) for one collection error. So a mutation that merely
broke collection -- tests never executed -- was reported `[KILLED]`. That is a
**false kill**, and it would weaken every "N/N guards killed" claim.

Fix: a KILL is keyed on pytest's summary token **`"failed"`**. Anything else
(a collection error, singular or plural, or "no tests ran") is `[INVALID]` and
recorded as unproven. Applied to all five D3a.5 drivers; a regression test
(`tests/test_mutation_driver_classification.py`) pins the rule.

Scope note: the older D0/D1/D2 drivers are separate work and were NOT touched.
(That scope decision is deliberate and unchanged.)

### The guard string: `@media (prefers-reduced-motion: reduce)` — scope pinned

The detector handles the canonical selector and every realistic spelling of it
(no space after `@media`, extra/absent whitespace, uppercase, a newline before
the brace). Its scope is pinned as the STANDALONE block:

| input | result |
|---|---|
| `@media (prefers-reduced-motion: reduce) {` | **True** |
| `@media(prefers-reduced-motion: reduce){` / case/whitespace variants | **True** |
| `@media (prefers-reduced-motion: no-preference) {` | False |
| `@media (min-width: 40em) {` | False |
| bare phrase / no brace | False |
| **combined** `@media (prefers-reduced-motion: reduce) and (min-width: 40em) {` | **False (deliberate)** |
| **comma list** `@media screen, (prefers-reduced-motion: reduce) {` | **False (deliberate)** |

The combined/comma forms are a deliberate false NEGATIVE: upstream ships only the
standalone block (32/32), and widening the pattern to reach them would re-match
the PROSE mention and reintroduce the false positive. `True` asserts that
accessibility code is present, so the safe error is to under-report, not to claim
a guard a file may lack.

### The real recipe: embedded CSS + an `@media` reduced-motion guard

The real `card-resize.md` (1658 bytes) contains: HTML usage, tunable `:root`
variables, the CSS **embedded** in fenced ```css blocks (under `## CSS`), and a
`@media (prefers-reduced-motion: reduce) { ... }` guard. "JavaScript
orchestration: None -- pure CSS." That embedded CSS is WHY no
`transitions/<slug>.css` file exists.

Two defects found while verifying this, both fixed:

1. **The guard detector over-matched.** `_REDUCED_MOTION_RE` matched the bare
   phrase `prefers-reduced-motion: reduce`, and every real recipe ALSO explains
   the guard in prose ("The `@media (prefers-reduced-motion: reduce)` guard ...
   is required"). So removing the CSS block while leaving the prose still
   reported the guard PRESENT -- a false positive on the one property being
   reported. The pattern now requires the actual block
   (`@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{`), which changes
   the answer for NONE of the 32 real recipes and rejects the prose-only case.

2. **The flag's docstring overstated.** `Recipe.has_reduced_motion_guard` was
   documented as "reports what the recipe FILE contains", but it is built from
   `free-manifest.json`, which carries only `slug`/`name`/`tier` -- so it is
   ALWAYS `False` while every real recipe DOES have the guard. A consumer reading
   `False` would conclude "no guard" -- the opposite of the truth. The docstring
   now states that `False` means NOT ESTABLISHED, not "absent", and that the
   value is never taken from untrusted manifest metadata.

### The artifact contract: `required: transitions/<slug>.md`, `optional: (none)`

The optional half is EMPTY, and that is the verified upstream truth:

* `RECIPE_OPTIONAL_SUFFIXES = ()`; `RECIPE_SUFFIXES = (".md",)`.
* The real `transitions-dev@0.3.0` tarball ships only `free/<slug>.md`; `add`
  writes exactly one file. So no companion is part of the artifact.
* A stray `.css` beside the Markdown is IGNORED by the default verifier, not
  counted (verified: `.md` + `.css` -> `('card-resize.md',)`).
* The `optional_suffixes` parameter is reachable ONLY by explicit injection, and
  exists so a future genuinely-shipped companion can be added WITHOUT
  reintroducing a requirement. It is not dead: passing `(".css",)` accepts a
  present companion and is exercised by tests.

The empty set is load-bearing, not incidental: making it `(".css",)` fails 2
tests, and a mutation guard pins that.

### Transitions materialization is exactly `transitions/<slug>.md`

Re-verified against the REAL `transitions-dev@0.3.0` package (downloaded from the
npm registry, source read directly), not against documentation:

```
bin/transitions-dev.mjs
  const OUT_DIR = flags.dir || "transitions";
  cmdAdd(slug):  writeFileSync(join(OUT_DIR, slug + ".md"), md)   # ONE file
```

The tarball ships only `free/<slug>.md` (33 recipes) -- no `.css` companions. The
app passes no `--dir`, so the destination is the CLI's default `transitions/`,
and `verify_recipe_materialized` requires `transitions/<slug>.md`. No
`package.json` or dependency code exists in `design_transitions.py` at all, so
the transitions path cannot mutate dependencies.

**Doc drift found + fixed.** `verify_recipe_materialized`'s docstring claimed a
`.css` companion is "accepted *if and only if* upstream emitted one" -- which
reads as "a present `.css` IS accepted". It is not: `RECIPE_OPTIONAL_SUFFIXES`
is empty, so the default IGNORES a stray `.css` beside the Markdown. The
docstring now states the current truth, and a doc-coherence test + guard pin it.

### The bulk selectors are refused at the VOCABULARY, not by the fixture

`build_add_argv`'s docstring named `all` as dangerous ("a request for every
recipe at once") and a test asserted `build_add_argv(..., "all", catalog) is
None`. But the guard was **catalog membership**, so that held only while the
fixture catalog omitted `all`. **Reproduced:** a manifest that LISTS
`{"slug": "all", "tier": "free"}` made the catalog contain `all` and
`build_add_argv` return `… transitions-dev add all` -- the exact open-ended
action the module refuses. The CLI treats the `--`-prefixed forms
(`--free`/`--all`/`--pro`) as bulk installs (`bin/transitions-dev.mjs`:
`if (flags.free || flags.all || flags.pro) return cmdAddAll(...)`), so a bare
`all` forwarded to `add` is one argv away from "install every recipe".

**Fix:** `RESERVED_RECIPE_SLUGS = {"all", "free", "pro"}` +
`recipe_slug_is_reserved`, and `recipe_slug_is_well_formed` refuses them at the
vocabulary -- mirroring `design_registry.RESERVED_COMPONENT_IDS`. Now "no argv
requests every recipe at once" is a property of the module, not of whichever
manifest was supplied. **No false positive:** none of the 32 real
`transitions-dev@0.3.0` free slugs collides with the set (verified).

### Item 2 evidence: a REGRESSION that pins capability == execution

Item 2 ("capability truth must match actual execution requirements") is evidenced
by a regression suite in `tests/test_design_resource_coherence.py` -- the file
whose stated purpose is catching DRIFT between layers. The assertions:

* `authentication_required` equals the adapter's own `credential_requirement`
  for every catalog source (no second opinion);
* with no credential, exactly the gated axes are closed;
* with a credential, exactly the gated axes open;
* install is gated by REVIEW, never by a credential (a key does not confer
  install for a source with an empty reviewed allowlist);
* an unmet required credential degrades the capability;
* every installable on-demand resource has the mechanism its axis claims.

These run in the partc driver (`RUN_TESTS`), so the `21st is credential-gated`
mutation now kills 8 tests rather than 3 -- the drift cannot return without the
coherence regression going red.

### A configured credential must actually REACH the request

"If real discovery requires an API key": then a key that IS configured must be
used. It was not.

`fetch_catalog_payload` accepted a `credential_provider` but had **no default**,
and no production caller supplied one. The capability layer reported
`discovery_available=True` on the strength of a configured key, while execution
refused with `REASON_CREDENTIAL_REQUIRED`:

```
API_KEY_21ST=21st_sk_...           # configured
_catalog_capability(21st)  -> discovery_available=True   # capability says yes
discover_catalog(21st)     -> ok=False, "this catalog needs a credential that
                               is not configured"         # execution says no
```

A configured credential never reached the request. Fix: the adapter now has a
default provider, `_environment_credential`, which reads the credential from the
**same** names the capability layer checks. The names live in ONE table
(`design_resources.CREDENTIAL_ENV_NAMES`), imported by both layers, so presence
and use cannot drift.

```
configured key  -> Authorization: Bearer <key>   (attempted, 401 named if bad)
no key          -> no request at all, bounded credential-required state
```

Property asserted: for every branch, `capability.discovery_available` agrees with
what `discover_catalog` actually does.

### Capability truth is DERIVED from the adapter, never asserted

Item 2: capability truth must match ACTUAL execution requirements.

The capability layer reported the credential requirement from **parameters**
(`metadata_without_auth`, `retrieval_requires_auth`) that a call site supplied.
Nothing bound them to the adapter's own tables, so they could drift -- and did
so observably:

```
_catalog_capability("twenty_first", metadata_without_auth=True, ...)
  -> discovery_available=True        # capability claims free discovery
discover_catalog("twenty_first")
  -> ok=False, "this catalog needs a credential that is not configured"
```

The capability asserted one thing while execution did another. The values
happened to be correct at the two call sites, but the TYPE allowed a lie.

Fix: the credential requirement is now DERIVED. `design_catalog_fetch` owns the
closed tables (`CREDENTIAL_REQUIRED_FOR_DISCOVERY`, `CREDENTIAL_REQUIRED_FOR_RETRIEVAL`)
and a `credential_requirement(source)` accessor; `_catalog_capability` consumes
that -- the SAME fact execution consults. The two booleans are gone from its
signature, so no call site can disagree with the adapter.

Verified live (2026-10), which is why the two tables differ per source:

| source | discovery | retrieval |
|---|---|---|
| 21st | `GET /api/v1/components/search` -> **401** | `https://21st.dev/r/<u>/<slug>` -> **403 Authentication required** |
| React Bits | `llms.txt` open -> **200** | `https://reactbits.dev/r/<Component>-<LANG>-<STYLE>` -> **200** |

Property asserted: for every catalog source,
`authentication_required == (discovery_requires_auth or retrieval_requires_auth)`
from the adapter's tables, and a credential-gated axis is closed when no
credential is present.

### `discovery_available` is backed by a BUILDABLE request, not a key

The live-adapter probe (`_has_live_discovery_adapter`) decides whether the
capability layer may report `discovery_available=True`. It tested **KEY
MEMBERSHIP** in the endpoint tables:

```
CATALOG_ENDPOINTS = {"twenty_first": None}   # a dead key
_has_live_discovery_adapter("twenty_first") -> True    # WRONG
build_discovery_url("twenty_first")         -> None    # nothing to fetch
```

So a source present as a key with a `None` value -- or one whose endpoint fails
its own host allowlist -- was reported as having a live adapter: **discovery
appeared available with no request behind it.**

Fix: the probe now delegates to the adapter's own `build_discovery_url(source)`,
the single function that decides whether a request can be built (host allowlist
included), and reports available iff it returns a URL. The property asserted is
now `_has_live_discovery_adapter(s) <=> build_discovery_url(s) is not None`, for
every source.

### The interface we call is the AUTHORITATIVE one (21st says so)

21st publishes several machine-readable surfaces. Which one is authoritative is
not a guess -- 21st's own ARD manifest says so:

```
/.well-known/ard.json
  entries:
    identifier  urn:air:21st.dev:api:rest-v1
    type        application/vnd.oai.openapi+json;version=3.0
    url         https://21st.dev/openapi.json
```

Exactly **one** API entry exists (`urn:air:21st.dev:api:rest-v1`) -- there is no
v2 or rival API. Its RFC 9727 catalog (`/.well-known/api-catalog`) anchors that
service at `https://21st.dev/api/v1`, and our search endpoint
(`.../api/v1/components/search`) sits under that base. So the surface our
adapter calls **is** the authoritative one, by 21st's own designation.

The same manifests pin the auth method. `auth.md` (the ARD-designated agent auth
doc) says:

> "Send an API key as a Bearer token on every request; that header works on both
>  MCP endpoints and on REST v1... REST v1 reads `Authorization: Bearer ...`
>  only and answers `401` to `x-api-key`."

Our adapter sends `Authorization: Bearer <key>` and never `x-api-key` -- the one
the REST surface reads. Both facts are now pinned by tests + guards.

### The official machine-readable interface is still the one we call

Re-verified that our adapter uses 21st's OWN documented machine-readable
discovery surface, and that it is still live:

- `GET https://21st.dev/api/v1/components/search` -> **401** without a key (live).
- 21st's RFC 9727 API catalog (`/.well-known/api-catalog`) names exactly this:
  `anchor: https://21st.dev/api/v1` -> `service-desc: https://21st.dev/openapi.json`.
- `openapi.json` is byte-identical to the copy the adapter was written against
  (sha256 `ea4a86354995ba2a`), so no drift.

While re-reading 21st's OWN agent skill
(`/.well-known/skills/21st-cli-use/SKILL.md`), a real mismatch surfaced: it
documents the credential env vars as **`TWENTYFIRST_TOKEN` / `API_KEY_21ST`**,
while our `CREDENTIAL_ENV_NAMES["twenty_first"]` recognised only
`TWENTY_FIRST_API_KEY` / `TWENTYFIRST_API_KEY` -- **no overlap**. A user who
followed upstream's docs got a FALSE NEGATIVE (reported unconfigured). Fixed by
adding the two official names (keeping the old ones for compatibility).

### The free-tier claim is SURFACE-SCOPED, not absolute

Removing the "free metadata search" claim over-corrected: the code then said
"There is no free tier", which is its own inaccuracy. 21st's own pricing section
says otherwise:

```
### Free access
- Browse all public components, themes, and templates
- 2 component code retrievals, copies, or installs per day across Web, MCP, CLI
- Free marketplace search across Web, CLI, and MCP
```

So 21st DOES advertise a free allowance -- but for the **Web/CLI/MCP** surfaces,
which this application never calls. The surface this application calls is the
**REST API**, whose global `ApiKeyAuth` gates both search and retrieval (401
live). The gate is a **credential**, not a payment: the API documents 401 and
never 402.

Fix: the module docstring, the inline comment, and the manifest comment now
state the claim **scoped to the surface** ("on the surface this application
calls..."), instead of the absolute "no free tier". A test asserts the absolute
overstatement is absent, and a guard proves that assertion is load-bearing.

### The "free metadata search tier" claim is gone from EVERY surface

The earlier review's finding (21st's metadata search is NOT free) had been
corrected in the module docstring and two tests, but a full sweep found it still
live in two more places:

- `config/design_resources.yaml` (the resource manifest): "metadata SEARCH is
  real and works with no credential (the free tier)". This is the CONTRACT the
  manifest asserts, and it contradicted the verified REST surface.
- `tools/mutation_check_d3a5_partc.py` header: "free-search-gated -> 21st
  reports NO discovery while its free metadata search demonstrably works".

Both were corrected to the verified truth (21st has NO free tier; both axes are
credential-gated). A test now asserts the manifest comment does not claim a free
21st tier, and a guard proves that assertion is load-bearing.

### Bearer credential: handling verified, and a leak vector closed

Re-verified the credential end to end:

- **Presence, never the value.** `CREDENTIAL_ENV_NAMES['twenty_first']` names two
  env vars; `credential_present` returns a bool and treats whitespace-only as
  absent. The value is never stored on an object (`CatalogFetchResult` has no
  credential field), never in `to_dict()`, the capability report, a reason
  string, or captured logs.
- **Header construction.** Exactly `Authorization: Bearer <value>`, and only
  when a credential is present.
- **Scoping.** No `url`/`host`/`endpoint` parameter exists, so the header can
  only go to the module-owned URL. `_NoRedirect` refuses every 3xx, so an
  approved host cannot bounce the header to another host.
- **Leak vector closed.** The module promises "never raises for an upstream
  problem", but urllib raises `http.client.HTTPException` subclasses
  (`BadStatusLine`, `IncompleteRead`, `LineTooLong`, `UnknownProtocol`) that are
  NOT `OSError` and slipped past `except (OSError, ValueError)`. Confirmed by
  repro: they PROPAGATED. An escaping traceback can carry the request headers,
  i.e. the Bearer value. Fixed with a catch-all that returns the bounded
  `REASON_UNEXPECTED_ERROR` and echoes nothing from the exception.

### The REST/OpenAPI surface: no endpoint is unauthenticated

The whole OpenAPI surface was enumerated (33 operations). The spec declares
``security: [{"ApiKeyAuth": []}]`` GLOBALLY and no operation overrides it, so
**every** endpoint requires a Bearer key -- including ``GET /components/search``,
whose 401 was confirmed live. There is no unauthenticated surface.

Re-verified the doc/code coherence for that fact (audit question 9, applied to
the 21st surface): the module docstring had claimed "21st.dev's metadata search
may work with no credential while authenticated component retrieval requires
one", and two tests repeated "free search". All were corrected to the verified
truth -- 21st is credential-gated on BOTH axes. A guard now fails if the false
claim returns to the module.

### The failure vocabulary matches the DOCUMENTED contract (200 / 401 / 429)

Re-verified the 21st discovery/search contract against a FRESH fetch of
``https://21st.dev/openapi.json`` (byte-identical to the earlier copy, so no
drift):

```
server   : https://21st.dev/api/v1
endpoint : GET /components/search   (summary: "Search components")
params   : q (required), scope in {team,mine,public} default team, limit 1..50 default 25
200 body : {"query","scope","results":[ComponentSearchResult]}
codes    : 200, 401, 429
auth     : ApiKeyAuth, bearer 21st_sk_...
```

Live probes: unauthenticated -> 401; bogus bearer -> 401 ``invalid_api_key``.

The contract documents **401** and **429** distinctly, but the adapter collapsed
every non-200 into one generic `REASON_BAD_STATUS` -- so a bad/expired key and a
rate limit were indistinguishable, and neither was named. Fix: `_reason_for_status`
maps 3xx -> redirect, **401 -> `REASON_AUTH_REJECTED`**, **429 ->
`REASON_RATE_LIMITED`**, anything else -> the generic reason. Both new reasons are
members of the closed set; a status code still never becomes a reason string.

Verified end-to-end against the documented codes: 200 -> ok; 401 -> auth
rejected; 429 -> rate limited; 403/404/500 -> generic. The adapter matches the
contract field for field (server URL, required `q`, `scope=public` within the
enum, `limit=24` within 1..50, container key `results`, bearer auth).

### A success flag is bound to its payload at CONSTRUCTION

The `ok` fix was one instance of a general defect: a "success" flag whose
implementation does not require the thing it asserts. Four such types were
representable:

| type | flag | could be constructed as |
|---|---|---|
| `CatalogFetchResult` | `ok` (`reason is None`) | `ok=True, payload=None` |
| `RegistryRequestOutcome` | `ok` | `ok=True, request=None` |
| `CatalogResult` | `ok` (`not warnings`) | `ok=True, entries=()` |
| `InstallOutcome` | `installed` (`state=="installed"`) | installed, `verified_*=False/()` |

The fetch case was the sharpest: its invariant was a **comment plus an `assert`**
(`# implied by ok`), and `python -O` STRIPS asserts -- verified live: under `-O` a
payload-less success silently normalized to an empty degraded result instead of
failing.

Fix: each flag is now bound by `__post_init__`, so the invariant is a property of
the TYPE, not of a caller remembering (or of asserts being enabled):

* `CatalogFetchResult`: `ok` requires a payload.
* `RegistryRequestOutcome`: `ok` iff a request is present (both directions).
* `CatalogResult`: `ok` requires at least one entry.
* `InstallOutcome`: `installed` requires `verified_in_manifest` or
  `verified_components`.

All four are construction-time `ValueError`s; production already satisfied them,
so nothing legitimate is refused.

### `ok` recognises only the source's OWN verified container shape

`CatalogResult.ok` is `not warnings`, and warnings cover only EMPTY/MALFORMED --
so `ok=True` reads as "the source's catalog was read". But the container keys it
accepted were SOURCE-AGNOSTIC: `components`, `results`, `items`, `data`,
`entries`. 21st's verified REST search response nests components under
**`results`** only (OpenAPI `{"query","scope","results"}`), so a payload keyed
`components` was reported `ok=True` -- a 21st catalog read that never happened.

Fix: `CATALOG_CONTAINER_KEYS` is per-source and closed -- 21st maps to
`"results"`, React Bits to `None` (its surface is a BARE LIST). A key a source
never emits now yields `ok=False` with `WARNING_CATALOG_MALFORMED`, so `ok`
means "this source's own shape was recognized".

Verified: 21st keyed `components`/`items`/`data`/`entries` -> `ok=False`; 21st
keyed `results` -> `ok=True`; React Bits bare list -> `ok=True`, a mapping ->
`ok=False`; and a bare list for 21st -> `ok=False`. Production only ever passes
the verified shapes, so nothing legitimate is refused.

### Root cause of the 21st route false positive: a scrape over `/components/<slug>`

The old pattern matched the **generic tail** `/components/<slug>` and dropped the
`/@<author>/` anchor. A 21st component page is
`/@<author>/components/<slug>`; a category route (`/community/components/s/<tag>`)
and a highlight route (`/community/components/popular`) share the SAME
`/components/<tail>` ending but have NO author. So every route tail became an
identity -- on the real `llms.txt` the generic pattern yields **exactly**
`featured`, `newest`, `popular`, `s`, `week`.

```
generic  /components/([a-z0-9-]+)              -> 5 fabricated identities
anchored /@<author>/components/([a-z0-9-]+)    -> (nothing)
```

The first fix kept the anchored pattern. That is not enough: an anchored regex
**still scrapes a path for which this application has no verified schema**, and
its only consumer was a test -- so the extraction is **removed outright**. The
module now keeps **no** regex over 21st's `/components/<slug>` paths, anchored or
not; `parse_markdown_catalog` returns `[]` for 21st unconditionally.

```
kept alive only by tests  ->  removed
property now asserted     ->  NO 21st path text becomes an identity, ever
```

The rule for any future schema is recorded in the module: it must be extracted
with an `/@<author>/`-anchored pattern, never the generic `/components/<slug>`
tail. The reserved-segment refusal (`RESERVED_COMPONENT_IDS`) remains as defense
in depth.

### The import boundary sees EVERY module-loading form

`declared_imports` is the generic scanner over UNTRUSTED emitted source, and its
invariant is "every bare package the source imports is reviewed". It only knew
three forms -- `import ... from`, `export ... from`, `import "x"` -- so two
reachable forms were missed:

```
const e = require("evil-pkg")          -> install reported `installed`
const e = await import("evil-pkg")     -> install reported `installed`
```

An unreviewed package reached either way slipped through the boundary.

Fix: added `_REQUIRE_RE` and `_DYNAMIC_IMPORT_RE`, and `declared_imports` now
scans all four patterns. The set is exhaustive for JS/TS module syntax: a
template-literal specifier (`import x from \`pkg\``) is a SYNTAX ERROR in
TypeScript, so no emitted source can contain one and it is not a gap.

Verified against all 16 real builtins emitted by the pinned CLI: 0
false-refusals (every import is `cn`/`lucide-react`/`radix-ui`/`react`/
`class-variance-authority`). Residual over-matching (a `from "x"` inside a
comment or string literal) is fail-CLOSED, and no reviewed source contains one.

**The "always provided" set must EQUAL the starter's runtime dependencies.**
This claim was FALSE until the typecheck pass: `class-variance-authority` --
which the starter ships as a direct dependency and which `alert`/`badge`/
`button`/`tabs` (and `dialog`, via its nested `button`) import in their emitted
source -- was missing from `_ALWAYS_PROVIDED_PACKAGES` (then only
`react`/`react-dom`). Live against the pinned `shadcn@4.21.0`, **5 of the 16
reviewed builtins were REFUSED** for importing a package the project already had
(`a component's emitted source imports a package outside the application-owned
reviewed set`). Fixed by making the set exactly the starter's `dependencies`;
a test pins the two together so they cannot drift.

### The FILE boundary is project-wide, not component-dir-only

The file-delta guard (step 4) snapshots only the APPROVED COMPONENT DIRECTORY,
but its stated invariant is "an install writes its reviewed artifacts and
NOTHING else". Confirmed by repro: a CLI that wrote `.env`, `src/evil.ts`,
`scripts/evil.mjs`, or `.vscode/settings.json` -- all OUTSIDE the component dir
-- was reported `installed`.

Fix: `snapshot_project_files` (prunes `node_modules`/`.git`/caches rather than
walking them) + `unreviewed_project_files`, wired as step 4d on BOTH paths. A
new file anywhere in the project that is neither a reviewed component file (under
the approved dir) nor a reviewed manifest file fails with
`REASON_UNREVIEWED_PROJECT_FILE`. Verified the real pinned CLI writes only
`package-lock.json` and `src/components/ui/<name>.tsx`, so a legitimate install
is not flagged; a growing lockfile (transitive deps) is still accepted.

### The reserved-route rule lives at the INSTALLABILITY authority

The catalog vocabulary refused the five reserved route segments, but
installability is decided by `design_registry` -- a SEPARATE table
(`_REVIEWED_COMPONENT_CONTRACTS`). Nothing tied the two, so a mistaken reviewed
contract made a reserved word installable. Confirmed by repro: adding a contract
for `popular` yielded

```
resolve_registry_locator('twenty_first', 'popular') -> https://21st.dev/r/popular
build_registry_request('twenty_first', 'popular').ok -> True
```

The catalog refused it; the registry did not.

Fix: the single source of truth `RESERVED_COMPONENT_IDS` (and
`component_id_is_reserved`) moved INTO `design_registry` -- the module that
decides installability -- and `design_catalog` imports it (it already imports the
registry, so the dependency direction is correct). Enforcement is now threefold:

1. `ReviewedComponentContract.__post_init__` raises `ValueError` for a reserved
   segment, so no contract (hence no approved identity) can exist for one.
2. `resolve_registry_locator` -- the ONLY function that can produce an
   installable locator -- re-checks, so even a directly-widened approved table
   yields `None`.
3. The catalog vocabulary refuses the same set.

Verified: a mistaken contract for `popular` now raises at construction; a
directly-injected approved entry still resolves to no locator.

### Reserved 21st route segments are refused at the identity vocabulary

21st's public index publishes CATEGORY pages (``/community/components/s/<tag>``)
and HIGHLIGHT pages (``/community/components/popular``, ``/newest``,
``/featured``, ``/week``). The single-letter category prefix and the bare
highlight slugs are page ROUTES, not components -- the five segments
``s``, ``popular``, ``newest``, ``featured``, ``week``.

Removing the false-positive parser stopped them being EXTRACTED, but they were
still slug-shaped, so ``component_id_is_valid('twenty_first', 'popular')``
returned True and a JSON payload claiming ``{"id": "popular"}`` built an entry.
The documented invariant ("routes are not identities") was a property of one
parser, not of the vocabulary.

Fix: a closed, application-owned table ``_RESERVED_COMPONENT_IDS`` refuses them
inside ``component_id_is_valid`` itself, so no path -- parser, JSON payload, or
direct call -- can turn one into a component id. Verified: all five are refused
by the vocabulary, produce no entry, resolve to no locator, and cannot build a
request; ordinary slug-shaped ids (``hero``, ``card``, ``pricing-section``) are
unaffected.

### `installable` means REVIEWED, not merely listed

`CatalogEntry.installable` (and `installable_ids()`) derived installability from
**declared dependencies alone** -- it never checked whether the application has
an **approved canonical locator**. Confirmed live: `discover_catalog('react_bits')`
reported **64 installable** components while exactly **1** (`SplitText`) was
reviewed; `resolve_registry_locator` returned `None` for the other 63 and
`build_registry_request` refused them. The module docstring claimed "nothing here
can make an unreviewed component installable" -- the code contradicted it.

Fix: `installable` now requires BOTH `dependencies_in_policy` AND
`has_approved_locator` (resolved by the registry, the same table the install
path uses). `to_dict()` reports both conditions separately so a consumer can see
WHY an entry is not installable. Live result: React Bits reports exactly
`('SplitText',)`.

### The discovery-adapter probe is PER-SOURCE

Introducing the 21st REST-search endpoint split discovery into two tables
(`CATALOG_ENDPOINTS` for a plain index, `CATALOG_SEARCH_ENDPOINTS` for search).
`_has_live_discovery_adapter()` was source-AGNOSTIC -- it only checked
`CATALOG_ENDPOINTS` -- so a source's capability could be conferred by ANOTHER
source's adapter:

- 21st's adapter was INVISIBLE when only the search table was populated (its
  capability reported absent though the endpoint existed).
- 21st reported an adapter while having NO endpoint at all, because React Bits
  happened to populate the other table.

Fix: the probe takes a `source` and consults BOTH tables for THAT source. A
source is "adapter-backed" only when an endpoint for the same source exists.
Verified: a search-only source keeps its adapter; another source cannot confer
one; an unknown source is absent.

### No unexpected package.json SECTION mutation

An install may change the four reviewed dependency sections
(`dependencies`, `devDependencies`, `optionalDependencies`,
`peerDependencies`) -- their contents are governed by the delta, removal, and
exact-pin guards. It must leave EVERY OTHER top-level `package.json` section
exactly as it found it.

Confirmed live gap: a CLI that wrote `overrides` / `resolutions` /
`pnpm.overrides` / `packageManager` / an arbitrary new section was reported
`installed`. These are package-source redirects by another name -- an `overrides`
entry can force a package to an arbitrary version or source. The 4-section
snapshot never saw them.

Fix: `snapshot_manifest_sections` keeps a canonical-JSON image of EVERY
top-level key; `changed_manifest_sections` returns the changed names OUTSIDE the
reviewed sections. Any such change fails with `REASON_MANIFEST_SECTION_CHANGED`,
on BOTH paths. Verified the real pinned CLI (`shadcn@4.21.0`) writes only
`dependencies`, so the guard does not false-positive.

### git / file / http npm specs are refused

The bounded spec parser (`parse_npm_package_spec`) accepts ONLY the approved
grammar -- a bare name, a versioned name, or a scoped name, with a bounded semver
constraint. Every source-bearing form returns `None`:

- **http(s)**: `https://…tgz`, `http://…`, `gsap@https://…`
- **git**: `git+https://…`, `git+ssh://…`, `git://…`, `git+http://…`,
  `github:user/repo`, `gitlab:`/`bitbucket:`, bare `user/repo`
- **file/link/workspace**: `file:…`, `link:…`, `workspace:*`, `workspace:…`
- **npm alias**: `npm:other`, `npm:@scope/other`
- **encoded/unicode**: `gsap%2F..%2Fevil`, `gsap%00`, control chars, homoglyphs

A refused spec never becomes a package identity: it is not a
`PACKAGE_TO_DEPENDENCY_ID` key, `resolve_dependency_requirements` returns it as
unknown (never echoed as a package name), and a component declaring one is
refused whole. Verified across the parser (56+ hostile specs), the registry
declared-dependency path, and the install-request path. The version constraint is
**checked, never installed**: a declared `gsap@>=1` yields dependency ids, and
the exact application pin (`gsap@3.15.0`) is what installs.

### No arbitrary PACKAGE SOURCE

A package's *source* can be redirected two ways, and both are closed:

1. **In the spec.** A dependency spec naming a source -- `https://…tgz`,
   `git+https://…`, `github:user/repo`, `file:`/`link:`/`workspace:`, `npm:`,
   a bare registry host -- is refused by `parse_npm_package_spec` (returns
   `None`). No install argv carries a `--registry`/`--prefix`/`--global` flag;
   every argv is `install/add <name>@<exact pin>`.
2. **In a config file.** A registry install may write `package.json` and its
   lockfile; it must never write or change a package-manager **config** file,
   because one `registry=` line in `.npmrc` points every later install at an
   arbitrary source. Confirmed live: a CLI that wrote a root `.npmrc` was
   reported `installed`. Now a snapshot of `PACKAGE_SOURCE_CONFIG_FILES`
   (`.npmrc`, `.yarnrc`, `.yarnrc.yml`, `.pnpmfile.cjs`, `.pnpmfile.js`,
   `npm-shrinkwrap.json`) is taken before and after; any creation, change, or
   removal fails with `REASON_PACKAGE_SOURCE_CHANGED`, on BOTH paths. The
   starter's legitimate `.npmrc` is unchanged by an install, so it is not
   flagged.

### No arbitrary registry URL reaches argv

`RegistryInstallRequest` validates the locator at construction, but
`install_external_component` is public and a **duck-typed stand-in bypasses the
constructor**. Confirmed live: such a request carried `https://evil.example/r/x`
straight into argv (`shadcn add https://evil.example/r/x --yes --overwrite`) and
only failed *after* running, when materialization did not occur.

Fix: `install_external_component` re-resolves the locator from
`(source, component_id)` and requires the request's locator to be **exactly** the
canonical one (`REASON_LOCATOR_NOT_CANONICAL`, **no command**). "Only the
application-owned canonical locator reaches argv" is now a property of the
installer, not of the caller using the right type. The control still holds: the
real SplitText request installs and its argv carries the canonical URL.

Also verified refused (no command): a URL as a component identity on any source
(`build_registry_request`); a URL inside `dependencies` or
`registryDependencies`; a URL handed to the builtin path (`install_components`
rejects it as a component, `commands == []`).

### What an official builtin may NOT introduce

An official builtin is reviewed to materialize ONLY its own
``<component><suffix>`` file (plus a reviewed nested component's file) and to
introduce ONLY ``cn``/``radix-ui`` (written) and ``lucide-react`` (imported), at
exact pins. Every other vector is refused:

| vector | result |
|---|---|
| CLI writes an unreviewed package | `REASON_REGISTRY_DEPENDENCY_DRIFT` |
| CLI writes a reviewed package into the wrong section | `REASON_REGISTRY_DEPENDENCY_DRIFT` |
| emitted source imports an unreviewed package | `REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED` |
| CLI materializes an extra unreviewed FILE | `REASON_REGISTRY_FILE_UNREVIEWED` |
| CLI removes a pre-existing project dependency | `REASON_REGISTRY_DEPENDENCY_DRIFT` |
| normalization leaves a range instead of the exact pin | `REASON_REGISTRY_PACKAGE_NOT_EXACT` |
| requested component outside the allowlist | rejected, not run |

The FILE delta (`snapshot_component_files` + `unreviewed_materialized_files`) is
new: previously an extra file the CLI wrote under the approved directory -- which
could import an unreviewed package or shadow a reviewed component -- was not
noticed. It is now refused on BOTH the builtin and external paths.

### The component tables are separate at the TABLE level

The trusted-boundary type now **enforces** the component-table separation, so
cross-contamination is unrepresentable rather than merely absent:

- a reviewed contract keyed to the **builtin source** (`shadcn_builtin`) →
  refused (and, independently, subsumed by "contract without an approved
  identity");
- a **builtin name** appearing as an approved **external identity** → refused;
- the **builtin source** carrying any approved external identities → refused.

Live tables verified: no builtin name is in `_APPROVED_COMPONENTS` for any
source, and no contract key names a builtin. Pinned by
`test_the_component_table_separates_builtins_from_external_contracts` and
`test_no_builtin_name_is_an_approved_external_identity_in_the_live_tables`.

### Builtins are NEVER subject to an external reviewed contract

The two paths share no contract. A shadcn builtin's direct dependencies are
reviewed **per component** (the builtin tables); an external component's are
reviewed in its **contract**. The cross-contamination is refused structurally:

- No builtin identity is keyed into `_REVIEWED_COMPONENT_CONTRACTS`, and
  `reviewed_component_contract` returns `None` for every builtin (by source or
  identity); `resolve_registry_locator(react_bits, <builtin>)` is `None`.
- `build_registry_request(SOURCE_SHADCN_BUILTIN, <builtin>, declared_dependencies=[...])`
  is a bounded **refusal** (`REASON_BUILTIN_CARRIES_DEPENDENCIES`), not an
  uncaught `ValueError`. Supplying declared deps for a builtin is an attempt to
  route it through the external-contract path; it is refused as a normal
  outcome so "builtins are not subject to React Bits' reviewed contract" is a
  property of the entry point, not of a caller remembering not to pass them.
- A clean builtin request always carries `required_dependency_ids == ()`.

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

## Sixth-pass audit (the final manifest's EXACT state, not just the delta)

The exactness re-verify ran over `introduced` — the packages the CLI's **delta**
named. But the DELTA only names what the CLI **CHANGED**. A reviewed package the
project **already declares at a floating range**, where the CLI writes the *same*
value, produces **no delta** — so it was never normalized and never re-verified,
while the boundary's own comment claimed *"the final manifest must hold the EXACT
application pins."* Same defect class as every prior pass: a guard whose scope is
narrower than the property it names.

- **Reproduced live:** a starter declaring `cn@^0.4.0` (exactly what the pinned
  CLI writes) + `install_components(["card"])` → `state=installed`, `cn` still
  `^0.4.0`. The external path showed the mirror (`gsap@^3.15.0` pre-declared →
  `installed` with a range).
- **Fix:** the guard now reasons over the **governed** set — every
  `allowed_packages` entry present in the runtime section — not just the delta.
  The set is normalized to its exact pins and **re-verified for every governed
  package**. A governed package found in a **non-runtime** section (e.g.
  `cn` in `devDependencies`) is refused as the same wrong-section violation the
  delta guard catches, just not caused by this CLI run.
- **No false positive:** all 16 builtin dependency tables were re-verified
  against the **real** `shadcn@4.21.0` (`cli == table` for every one), and the
  four named sections' governed packages are all in `reviewed_registry_package_pins`.
- **Rule:** the manifest the install **leaves behind** must hold the exact pins
  for every package the install **governs** — whether the CLI touched it or not.

## Final dependency policy (post-repair)

- **Package identity is closed and application-owned.** Every package name that
  can reach an install argv comes from one of FOUR closed, application-owned
  tables — never from model/resource/upstream text: `DEPENDENCY_PACKAGES`
  (D2-selectable runtime deps), `DEPENDENCY_COMPANION_PACKAGES` (`@types/three`),
  `REGISTRY_INTRODUCED_PACKAGE_PINS` (`cn`/`radix-ui`/`lucide-react`), and the
  starter's always-provided set (`react`/`react-dom`/
  `class-variance-authority`). Nothing else can produce a package.
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

### VPS command: a FULL, non-degraded Impeccable scan

The scan target is **load-bearing**. The engine's CLI reads
``if (!process.stdin.isTTY && targets.length === 0) -> read STDIN``; every
subprocess has a non-TTY stdin, so an invocation with NO target returns ``[]``
with exit 0 — a clean, non-degraded, "authoritative" verdict over a project it
never opened. The app's argv therefore ends in a FIXED ``.``
(`CRITIC_ENGINE_ARGV_SUFFIX = ("detect", "--json", "--quiet", ".")`).

```bash
cd website-builder && source .venv/bin/activate

# 1. What quality is the skill on THIS host?  (full | degraded | missing)
python -c "from pathlib import Path; from app.core.design_activation import engine_quality; \
  print(engine_quality(Path.home()/'.hermes-website'/'skills'/'impeccable'))"

# 2. Provision the parser runtime for a FULL scan (operator step, once).
#    Exact pins; into the SKILL's own node_modules. A build never does this.
SKILL="$HOME/.hermes-website/skills/impeccable"
npm install --no-save --prefix "$SKILL" --no-audit --no-fund \
  htmlparser2@12.0.0 css-select@7.0.0 css-tree@3.2.1 domutils@4.0.2

# 3. Confirm the quality is now `full`.
python -c "from pathlib import Path; from app.core.design_activation import engine_quality; \
  print(engine_quality(Path.home()/'.hermes-website'/'skills'/'impeccable'))"

# 4. Run the APP's scan against a project. This is the real seam.
python -c "from pathlib import Path; from app.core.design_critic import run_critic_scan, scan_is_authoritative; \
  root = Path.home()/'.hermes-website'/'skills'/'impeccable'; \
  o = run_critic_scan(root/'scripts'/'detect.mjs', node_executable='/usr/bin/node', skill_root=Path('<PROJECT>')); \
  print('ok=%s degraded=%s authoritative=%s findings=%d' % (o.ok, o.degraded, scan_is_authoritative(o), len(o.findings)))"

# 5. Cross-check against the engine directly (the same fixed target).
node "$SKILL/scripts/detect.mjs" detect --json --quiet .   # run from the project
```

**Observed on the qualification host (2026-10), a project with real findings:**

| skill state | app `ok` | `degraded` | `authoritative` | findings |
|---|---|---|---|---|
| **full** (parser runtime present) | True | False | **True** | **2** (`low-contrast`, `tiny-text`) |
| **degraded** (engine only) | True | True | **False** | 0, reason `parser_runtime_unavailable` |

A degraded scan is callable but **never authoritative**: its 0 findings is an
undercount, not a pass. Only the `full` row may be read as a certification.

**Defect found and fixed while writing this command.** The argv previously had
**no target**, so in any non-TTY context the full scan returned 0 findings and
`authoritative=True` — certifying a project it never opened. Fixed by the fixed
`.` target; pinned by tests and a mutation guard (see the audit's Part I notes).

### VPS command: 21st REAL discovery, with a credential when required

21st's only machine surface is the authenticated REST search
(`GET https://21st.dev/api/v1/components/search`), so discovery **requires a
credential**. The adapter reads the credential itself from the same env names the
capability layer checks, so no caller wiring is needed — but the VALUE only ever
goes into an `Authorization: Bearer …` header.

```bash
cd website-builder && source .venv/bin/activate

# 1. Which env names are recognised, and is one configured?  (no value printed)
python -c "from app.core.design_resources import CREDENTIAL_ENV_NAMES as N; \
  from app.core.design_activation import credential_present; \
  print('names:', N['twenty_first']); \
  print('present:', credential_present(N['twenty_first']))"

# 2. Configure the credential (one of the official names). Value never echoed.
export API_KEY_21ST='21st_sk_...'          # or TWENTYFIRST_TOKEN

# 3. REAL discovery against the live REST search. `query` is inert data.
python -c "from app.core.design_catalog_fetch import discover_catalog as d; \
  r = d('twenty_first', query='button'); \
  print('ok=%s entries=%d' % (r.ok, len(r.entries))); \
  print('warnings:', r.warnings); \
  print('ids:', [e.component_id for e in r.entries][:10])"

# 4. Confirm capability and execution AGREE (both keyed on the same credential).
python -c "from pathlib import Path; from app.core.design_activation import activate_resource; \
  from app.core.design_resources import load_design_resource_manifest as m; \
  from app.core.design_catalog_fetch import discover_catalog as d; \
  c = activate_resource(Path('/tmp'), m().get('twenty_first'), system='Linux', machine='x86_64'); \
  r = d('twenty_first'); \
  print('cap.discovery=%s cap.auth_present=%s | exec ok=%s' % (c.discovery_available, c.authentication_present, r.ok))"

# 5. Cross-check the raw endpoint (the same surface, by hand).
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $API_KEY_21ST" \
  'https://21st.dev/api/v1/components/search?q=button&scope=public&limit=3'
```

**Observed on the qualification host (2026-10):**

| credential | `credential_present` | `cap.discovery` | exec `ok` | exec warning |
|---|---|---|---|---|
| none | False | False | False | `this catalog needs a credential that is not configured` |
| set (invalid/placeholder) | True | True | False | `the catalog rejected the configured credential` |
| set (valid) | True | True | **True** | — (entries returned) |

The live endpoint confirms the header is what matters:

| request | response |
|---|---|
| no `Authorization` | `401 {"error":"unauthorized", ...}` |
| `Authorization: Bearer <invalid>` | `401 {"error":"invalid_api_key", ...}` — upstream PARSED the key |
| `x-api-key: <invalid>` | `401 {"error":"unauthorized", ...}` — ignored; REST v1 reads `Authorization` only |

**No credential → no request.** The adapter returns `REASON_CREDENTIAL_REQUIRED`
without a round-trip (verified: the transport is never called), so an absent key
is a bounded state, not a wasted 401.

**A discovered component is a PROPOSAL, not an install.** `approved_registry_components('twenty_first')`
is `()` — nothing is reviewed for 21st, so a discovered id is never installable.
Discovery proves a component EXISTS; only a reviewed contract makes it installable.

### VPS command: React Bits — real discovery + retrieval, NO credential

React Bits is the contrast to 21st: both its agent index (`llms.txt`) and its
shadcn registry items (`/r/<Component>-<LANG>-<STYLE>`) are served **openly**, so
discovery AND retrieval work with no credential. It is the one source where the
capability layer legitimately reports `discovery_available=True,
retrieval_available=True, authentication_required=False`.

```bash
cd website-builder && source .venv/bin/activate

# 1. Discovery (LIVE, no credential) -- llms.txt -> catalog entries.
python -c "from app.core.design_catalog_fetch import discover_catalog as d, \
  build_discovery_url, credential_requirement; \
  print('endpoint :', build_discovery_url('react_bits')); \
  print('needs cred:', credential_requirement('react_bits')); \
  r = d('react_bits'); \
  print('ok=%s entries=%d truncated=%s' % (r.ok, len(r.entries), r.truncated)); \
  print('SplitText discovered:', 'SplitText' in {e.component_id for e in r.entries}); \
  print('installable_ids:', r.installable_ids())"

# 2. Retrieval (LIVE, no credential) -- the reviewed component's registry item.
python -c "import json, urllib.request; \
  req = urllib.request.Request('https://reactbits.dev/r/SplitText-TS-TW', \
    headers={'User-Agent': 'shadcn', 'Accept': 'application/json'}); \
  doc = json.load(urllib.request.urlopen(req, timeout=30)); \
  print('name:', doc['name']); \
  print('dependencies:', doc.get('dependencies')); \
  print('registryDependencies:', doc.get('registryDependencies'))"

# 3. The reviewed contract, and the install request it authorizes.
python -c "from app.core.design_registry import build_registry_request as b, \
  reviewed_component_contract as c, resolve_registry_locator as l; \
  k = c('react_bits', 'SplitText'); \
  print('contract deps:', k.expected_dependency_ids); \
  print('locator:', l('react_bits', 'SplitText')); \
  o = b('react_bits', 'SplitText', \
        declared_dependencies=['gsap@^3.13.0', '@gsap/react@^2.1.2']); \
  print('request ok=%s required=%s' % (o.ok, o.request.required_dependency_ids))"
```

**Observed on the qualification host (2026-10):**

| step | observed |
|---|---|
| discovery | `ok=True`, **64** entries, `truncated=True`, `SplitText` present |
| `installable_ids()` | **`('SplitText',)`** — 64 discovered, exactly 1 installable |
| retrieval | `HTTP 200`, `name=SplitText-TS-TW`, `dependencies=['gsap@^3.13.0','@gsap/react@^2.1.2']`, `registryDependencies=[]` |
| contract | `expected_dependency_ids=('gsap','gsap_react')`, `expected_registry_dependencies=()` |
| locator | `https://reactbits.dev/r/SplitText-TS-TW` (the `TS-TW` variant, app-owned) |

**Discovery is not installability.** All 64 upstream components are discovered, but
only the ONE with a reviewed contract is installable. Verified: an unreviewed
discovered component (e.g. `ASCIIText`, `AnimatedContent`, `Antigravity`) is
refused — `resolve_registry_locator` returns `None`, so `build_registry_request`
returns `ok=False`, reason `the component has no approved canonical locator`.

**A lying declaration is refused, not obeyed.** The upstream document declares
`gsap@^3.13.0` / `@gsap/react@^2.1.2`; the app CHECKS those against its closed
allowlist and installs its own exact pins. A declaration naming anything else
(`evil-pkg`, or the wrong form `gsap_react@…`) returns `ok=False`, reason `the
component requires a dependency outside the closed allowlist`. The upstream range
is only CHECKED, never installed.

### VPS command: the catalog FETCH layer — bounds, allowlist, redirect refusal

Every discovery claim above rests on `fetch_catalog_payload` actually enforcing
its bounds. This is the command that proves the BOUNDS rather than the payload.

```bash
cd website-builder && source .venv/bin/activate

python -c "from app.core.design_catalog_fetch import ( \
    fetch_catalog_payload, _default_transport, url_is_allowed, \
    SOURCE_REACT_BITS, MAX_RESPONSE_BYTES, MAX_PARSED_IDS); \
  \
  print('--- size bound is ENFORCED (not just declared) ---'); \
  cap = fetch_catalog_payload(SOURCE_REACT_BITS, transport=lambda u,h,t: (200, b'x'*MAX_RESPONSE_BYTES)); \
  over = fetch_catalog_payload(SOURCE_REACT_BITS, transport=lambda u,h,t: (200, b'x'*(MAX_RESPONSE_BYTES+1))); \
  print('  at cap  : ok=%s' % cap.ok); \
  print('  over cap: ok=%s reason=%s' % (over.ok, over.reason)); \
  \
  print('--- timeout reaches the transport ---'); \
  seen = {}; \
  fetch_catalog_payload(SOURCE_REACT_BITS, transport=lambda u,h,t: (seen.update(t=t), (200, b''))[1], timeout=7); \
  print('  transport got timeout:', seen.get('t')); \
  \
  print('--- redirects refused, allowlist re-checked ---'); \
  for url in ('https://reactbits.dev/llms.txt','https://evil.example/x','http://reactbits.dev/x','https://reactbits.dev.evil.example/x'): \
      print('  %-46s allowed=%s' % (url, url_is_allowed(SOURCE_REACT_BITS, url))); \
  \
  print('--- the REAL transport (no injection) ---'); \
  st, body = _default_transport('https://reactbits.dev/llms.txt', {'User-Agent':'hermes-website-builder'}, 10); \
  print('  live llms.txt -> HTTP %s, %d bytes' % (st, len(body)))"
```

**Observed on the qualification host (2026-10):**

| property | observed |
|---|---|
| size bound | body at `MAX_RESPONSE_BYTES` → `ok=True`; **one byte over** → `ok=False`, `…response exceeded the size…` |
| timeout | the transport receives the caller's `timeout` (7); `socket.timeout`/`TimeoutError` → `ok=False`, `…did not respond…` |
| redirects | `_NoRedirect` raises on 301/302/307/308 — a 3xx is never followed |
| allowlist | `reactbits.dev` allowed; `evil.example`, `http://…`, and `reactbits.dev.evil.example` all refused |
| real transport | `https://reactbits.dev/llms.txt` → HTTP **200**, 94,367 bytes |
| parse bound | 712 CLI markers → 64 entries (≤ `MAX_PARSED_IDS`) |
| honest zero | a 200 with no usable identity → `ok=False` (`no usable component entries` / `could not be parsed`) |

**Why this matters.** A bound that is *declared* but not *enforced* is the same
defect class as an unscanned "authoritative" verdict. Each bound here was checked
at its EDGE (exactly at, and one past) rather than trusted from the constant.

### VPS command: fetch the `SplitText-TS-TW` registry JSON

The canonical locator is `https://reactbits.dev/r/SplitText-TS-TW`. **The app
never fetches this JSON** — `design_registry` imports no HTTP client; the pinned
shadcn CLI fetches it during `add`. The app's defence is therefore **post-hoc**:
it snapshots before, runs the CLI, and verifies the delta. This command fetches
the JSON **by hand** to confirm what the CLI is about to act on.

```bash
cd website-builder && source .venv/bin/activate

# 1. Fetch the registry JSON (the exact locator the app authorizes).
python -c "import json, urllib.request; \
  req = urllib.request.Request('https://reactbits.dev/r/SplitText-TS-TW', \
    headers={'User-Agent': 'shadcn', 'Accept': 'application/json'}); \
  doc = json.load(urllib.request.urlopen(req, timeout=30)); \
  print('name                :', doc['name']); \
  print('type                :', doc['type']); \
  print('dependencies        :', doc.get('dependencies')); \
  print('registryDependencies:', doc.get('registryDependencies')); \
  print('files               :', [f['path'] for f in doc.get('files', [])])"

# 2. The variant suffix is LOAD-BEARING: the bare name is NOT a registry item.
python -c "import urllib.request; \
  b = urllib.request.urlopen(urllib.request.Request('https://reactbits.dev/r/SplitText', \
        headers={'User-Agent':'shadcn'}), timeout=30).read().decode(); \
  print('bare /r/SplitText is JSON?', b.strip()[:1] in '[{', '(', len(b), 'bytes)')"
# -> False: it is the marketing HTML page. /r/<C>-TS-TW is the registry item.
```

**Observed on the qualification host (2026-10):**

| field | value |
|---|---|
| `$schema` | `https://ui.shadcn.com/schema/registry-item.json` |
| `name` | `SplitText-TS-TW` |
| `type` | `registry:component` |
| `dependencies` | `['gsap@^3.13.0', '@gsap/react@^2.1.2']` |
| `registryDependencies` | `[]` |
| `files` | `['SplitText/SplitText.tsx']` |
| bare `/r/SplitText` | HTTP 200 but **HTML**, not JSON — the `-TS-TW` suffix is required |

**Who fetches what.** The app does NOT read this JSON. The **pinned shadcn CLI**
does, during `shadcn add https://reactbits.dev/r/SplitText-TS-TW`. The app's
defence runs AFTER the CLI and is post-hoc, in four independent checks:

1. **direct packages** — the delta is compared against the reviewed contract
   (`gsap`, `@gsap/react`), both directions (`REASON_REGISTRY_DEPENDENCY_DRIFT`
   for an extra, `REASON_REGISTRY_PACKAGE_NOT_EXACT` for a missing one);
2. **nested registry components** — `expected_registry_dependencies` is `()`, and
   a materialized file that is not `<allowed><suffix>` is refused
   (`unreviewed_materialized_files`; verified: an unexpected `button.tsx` alongside
   `SplitText.tsx` is caught);
3. **emitted-source imports** — every bare package specifier in the materialized
   source must be in the reviewed set (`REASON_REGISTRY_SOURCE_IMPORT_UNREVIEWED`);
4. **manifest sections + removals** — a section outside the reviewed surface, or a
   removed dependency, is refused.

**Division of labour, stated honestly.** The app trusts the CLI to fetch and write;
it does not trust the CLI's *result*. That is why the post-hoc delta is the real
boundary, and why `build_registry_request(declared_dependencies=...)` — which
checks the upstream metadata *before* the CLI runs — is a **caller-supplied seam**
(the CLI is the production fetcher). No production caller drives the external path
today; it is reachable via the public `install_external_component` API and covered
by `tests/test_design_registry_mutation.py`, but it is not yet wired into
`execute_selection`, which routes only the builtin component list.

### VPS command: validate the reviewed dependency contract

The reviewed contract (`_REVIEWED_COMPONENT_CONTRACTS`) is only as strong as the
trust boundary that validates it. This command checks BOTH: the boundary is
coherent, and every drift it claims to catch is actually caught.

```bash
cd website-builder && source .venv/bin/activate

python -c "from app.core import design_registry as R; \
  b = R.trusted_registry_boundary(); \
  print('sources            :', b.sources); \
  print('reviewed contracts :', sorted(b.reviewed_contracts)); \
  print('introduced packages:', b.introduced_packages()); \
  k = b.reviewed_contracts[('react_bits', 'SplitText')]; \
  print('SplitText deps     :', k.expected_dependency_ids); \
  print('SplitText nested   :', k.expected_registry_dependencies)"

# Each drift is caught. Run this self-contained script (it isolates ONE drift at
# a time, because one check can mask another):
python - <<'PY'
from app.core import design_registry as R

def build(**overrides):
    base = dict(
        sources=tuple(R.REGISTRY_SOURCES), hosts=dict(R.REGISTRY_HOSTS),
        locator_templates=dict(R._LOCATOR_TEMPLATES),
        approved_components={s: tuple(sorted(c)) for s, c in R._APPROVED_COMPONENTS.items()},
        reviewed_contracts=dict(R._REVIEWED_COMPONENT_CONTRACTS),
        builtin_components=tuple(R.ALLOWED_SHADCN_COMPONENTS),
        builtin_packages={c: tuple(p) for c, p in R.REVIEWED_BUILTIN_COMPONENT_DEPENDENCIES.items()},
        builtin_imports={c: tuple(p) for c, p in R.REVIEWED_BUILTIN_COMPONENT_IMPORTS.items()},
        builtin_nested={c: tuple(t) for c, t in R.REVIEWED_BUILTIN_COMPONENT_NESTED.items()},
        introduced_pins=dict(R.REGISTRY_INTRODUCED_PACKAGE_PINS), shadcn_cli_spec='shadcn@4.21.0',
    )
    base.update(overrides)
    return base

for name, overrides in [
    ('inexact introduced pin', dict(introduced_pins={'cn': '^0.4.0'})),
    ('missing introduced pin', dict(introduced_pins={})),
    ('floating CLI pin', dict(shadcn_cli_spec='shadcn@latest')),
    ('stray locator template', dict(locator_templates={**R._LOCATOR_TEMPLATES, 'ghost': 'https://x/{component}'})),
    ('nested row outside the allowlist',
     dict(builtin_nested={**{c: tuple(t) for c, t in R.REVIEWED_BUILTIN_COMPONENT_NESTED.items()}, 'dialog': ('ghost',)})),
]:
    try:
        R.TrustedRegistryBoundary(**build(**overrides))
        print('  %-30s ACCEPTED (defect)' % name)
    except ValueError as e:
        print('  %-30s REFUSED: %s' % (name, str(e)[:44]))
PY

# The boundary is CONSULTED on the executing paths (both must refuse):
python -c "import app.core.design_registry as R; \
  orig = R.trusted_registry_boundary; \
  R.trusted_registry_boundary = lambda: (_ for _ in ()).throw(ValueError('drift')); \
  try: R.trusted_registry_boundary(); print('boundary: ACCEPTED (defect)') \
  except ValueError: print('boundary REFUSES drift -> both install paths run NO command')"
```

**Observed on the qualification host (2026-10):**

| check | result |
|---|---|
| live boundary | constructs: sources `('shadcn_builtin','twenty_first','react_bits')`, contracts `[('react_bits','SplitText')]`, introduced `('cn','lucide-react','radix-ui')` |
| SplitText contract | `expected_dependency_ids=('gsap','gsap_react')`, `expected_registry_dependencies=()` |
| inexact introduced pin | **REFUSED** (`registry-introduced pin is not exact`) |
| missing introduced pin | **REFUSED** (`…has no exact application-owned pin`) |
| floating CLI pin | **REFUSED** (`the pinned shadcn CLI spec is not an exact pin`) |
| stray locator template | **REFUSED** (`locator templates must exist for exactly the hosted sources`) |
| contract keyed to the builtin source | **REFUSED** |
| builtin name as an approved external identity | **REFUSED** |
| nested row outside the allowlist | **REFUSED** |
| **the boundary is consulted on BOTH executing paths** | `install_components` AND `install_external_component` refuse with `REASON_BOUNDARY_INCOHERENT` and run **no command** |

Each check was isolated (a single drift at a time), because an earlier probe let
one check fire first and mask the others — a guard that is never exercised is not
a proven guard.







## Part N — the source-review bullets, re-verified as LIVE properties

Every bullet above was verified once, by hand, in the session that fixed it.
Part N re-checks each as a property of the CURRENT code, so a later commit cannot
silently weaken one. The durable form is
`tests/test_source_review_bullets.py` (33 tests), one per bullet plus the three
audit-Q9 policy checks:

```bash
cd website-builder && source .venv/bin/activate
python -m pytest tests/test_source_review_bullets.py -q
```

What it pins, grouped by the surface it protects:

- **The 21st surface** — the endpoint we call is the REST v1 search; auth is
  `Authorization: Bearer` and never `x-api-key`; no credential VALUE is stored on
  a result type; the fetch never raises (the header-leak vector stays closed);
  401 and 429 stay named distinctly; a successful fetch is bound to a payload at
  construction; the container key is per-source (`results` for 21st, bare list
  for React Bits); no `/components/<slug>` scrape survives; the live-adapter
  probe agrees with `build_discovery_url` for every source; and no surface claims
  a free metadata-search tier.
- **The registry** — reserved route segments are refused at the vocabulary AND
  the locator; no contract can exist for a reserved segment; a builtin carrying
  declared deps is a bounded refusal; a clean builtin carries no required deps;
  no builtin name is an approved external identity; React Bits approves exactly
  `SplitText`; an unreviewed external id resolves to no locator; the boundary is
  a validated type; the SplitText contract is exactly `{gsap, gsap_react}`; and a
  URL as a component identity is refused.
- **Parser / pins / transitions** — every hostile npm spec is refused and every
  approved one parses; the always-provided set EQUALS the starter's runtime
  `dependencies`; the dependency / registry-introduced / Impeccable-parser /
  pinned-CLI versions are all exact; the bulk selector slugs are refused at the
  transitions vocabulary; the transitions optional-suffix set is empty; and the
  manifest-section guard helpers exist.

**Why it is a test, not a paragraph.** A bullet recorded only in prose drifts the
moment someone edits the code it describes. Each bullet here is an assertion on
the code, so the same defect the batch hunts -- a guard weaker than the claim it
names -- would fail CI. `partc` guard #24 mutates `_reason_for_status` to collapse
429 into the generic reason and proves the bullets file catches it (2 failed), so
the re-verification is load-bearing rather than a restatement.

### Audit Q9: the "Final dependency policy" claims are now EXECUTABLE

Q9 asks whether a comment/doc claims something is allowlisted when executable
policy disagrees. The "Final dependency policy" section opened with:

> "`DEPENDENCY_PACKAGES` maps a dependency *id* to a package name; **nothing else
>  can produce a package**."

That is an **overstatement**. Mechanically, **four** closed, application-owned
tables produce package names: `DEPENDENCY_PACKAGES` (`gsap`, `@gsap/react`,
`three`, `lenis`), `DEPENDENCY_COMPANION_PACKAGES` (`@types/three`),
`REGISTRY_INTRODUCED_PACKAGE_PINS` (`cn`, `radix-ui`, `lucide-react`), and the
starter's always-provided set (`react`, `react-dom`,
`class-variance-authority`). `resolve_package`'s docstring made the same
too-narrow claim ("the ONLY function that turns a name into an installable
package" -- true for the RUNTIME package only).

Both were corrected to name the four tables. The claim is now pinned by three
tests in `tests/test_source_review_bullets.py`: the four tables are exactly the
ones named and each produces package names; the per-component builtin tables may
only NAME a package one of the four already covers (they are not a fifth
source); and `resolve_package`/`resolve_companion_packages` return `None`/`()` for
any unowned id, so no model/resource/upstream text contributes a package.

### A latent interpreter-upgrade break, found by a plain `pytest tests/ -q`

Running the suite surfaced **8 warnings**, one of them a
`SyntaxWarning: invalid escape sequence '\s'` originating in this project's own
code. Traced to `tools/mutation_check_d3a5_partd.py`: two mutation ANCHORS held
the reduced-motion regex snippets as **non-raw** string literals, so `\s` / `\(`
/ `\{` were invalid escapes. The VALUE was correct (an invalid escape is
preserved literally), so every guard still passed -- but the construct compiles
today with a warning and becomes a hard `SyntaxError` on a future interpreter.
That is the same class again: a defect the normal run does not fail on.

Fixed by making both anchors **raw strings** (`r"""..."""`); the anchors are
byte-identical, and `partd` still kills **36/36**. Guarded by
`tests/test_suite_hygiene.py`, which now scans every `app/`, `tests/` and
`tools/` `.py` with `compile()` under a warnings filter and fails on any invalid
escape -- with a self-proof that a planted `"\s"` is caught and an `r"\s"` is
clean. `partc` guard #21b proves that scanner is load-bearing (2 failed).

## Part M — FINAL PROOF (one command, one verdict)

The acceptance gate. `tools/d3a5_final_proof.py` runs the whole D3a.5 battery and
prints a single PASS/FAIL. It is the one command to run before declaring the
batch done.

```bash
cd website-builder && source .venv/bin/activate
python tools/d3a5_final_proof.py
```

What it proves, in three blocks:

1. **The default suite is GREEN while the internet is unreachable.** The suite
   runs with NON-LOOPBACK network blocked at the Python level
   (`socket.connect`/`getaddrinfo`/`urlopen`), so one run establishes both
   "green" AND "offline". It also asserts exactly **4 deselected** -- the four
   network-needing `test_starter_toolchain.py` command assertions, which must be
   excluded, not silently run.
2. **Every mutation driver kills all its guards, at the PINNED count.** A driver
   prints "all N guards killed", which stays true if a mutation is DELETED -- so
   the runner pins each N externally (`parta 16`, `partbc 73`, `partc 39`,
   `partd 36`, `parti 18`). A dropped guard makes the count mismatch and the
   proof FAIL. The three legacy D0/D1/D2-D3a drivers are reported for the record
   (separate work; their host-masked kills are a documented artifact).
3. **The guardrails hold:** branch `web-design`; `feature/website` byte-identical
   to its baseline (`868ed00e3`); no D3b artifact; working tree clean.

Exit code is 0 iff every check passes. The runner mutates nothing (each driver
copies the tree to a temp dir; the runner only reads), so it is safe to run
against the real working tree.

**Why the counts are pinned.** `"all N guards killed"` is self-referential: delete
a mutation and the sentence is still true. An external expectation per driver is
what turns the driver's honest self-report into a checkable claim -- the same
"enforcement must be as strong as the property it names" rule the whole batch is
built on.

**Observed on the qualification host (2026-10):** `13/13 checks passed`,
`VERDICT: PASS` -- suite `3604 passed, 2 skipped, 4 deselected` (offline),
drivers `16 / 73 / 39 / 36 / 18` and legacy `9 / 33 / 25`, guardrails all green.
(13 = 1 suite + 5 D3a.5 drivers + 3 legacy drivers + 4 guardrails.)

### A stale-claim sweep of the batch diff

Reviewing the batch's own `git diff` for comments/docs whose claims no longer
match the code found three stale claims, all in the audit doc:

- **A "socket ban" that does not exist** — the doc claimed the suite bans
  sockets outright (three places). FALSE. No conftest patches `socket`; the
  offline guard (`tests/test_suite_offline.py`) allows **loopback**, and the
  port-allocation tests legitimately bind `127.0.0.1:0`. The honest property is
  "offline — no non-loopback network".
- **Part M's observed tally** said `partc 35` / suite `3570` — stale after partc
  grew and the suite passed 3604. Corrected to `partc 38` / `3604`.
- **"the `21st is credential-gated` mutation now kills 7 tests"** — the real
  count is **8** (measured: `8 failed`). Corrected.

Each correction is pinned: `tests/test_design_resource_coherence.py` now asserts
the doc contains `drivers \`16 / 73 / 38 / 36 / 18\`` and does NOT contain the
socket-ban wording, and `partc` guard #25 reintroduces the stale socket claim to
prove that assertion is load-bearing (2 failed).

**The lesson, once more:** a number written into prose is a claim like any other,
and it drifts the moment the code it describes moves. Every count the doc states
that is derivable offline is now either pinned by a test or measured from the
artifact (the final-proof runner pins the driver counts from outside).

## Part L — manual LIVE smokes (run AFTER the unit suite)

The default suite is offline (no non-loopback network) by construction, so every
claim that has a LIVE dimension is proven here by hand against the real upstreams
/ the real pinned CLI. Run these **after** `pytest tests/` is green, never
instead of it.

**Order matters:** the unit suite proves the logic offline; these smokes prove the
same logic still holds against reality. A smoke that passes offline too is not a
smoke — each live smoke below was checked to FAIL when its upstream is removed.

### L.1 — offline unit suite first

```bash
cd website-builder
source .venv/bin/activate          # REQUIRED: pytest lives only in the venv
python -m pytest tests/ -q         # expect: all green (the count grows as tests are added)
```

### L.2 — the live smokes

Run these from the same activated shell. Every command below is the exact one
observed in L.3; none is illustrative.

```bash
# 1. SplitText resolves to the reviewed contract (local).
python -c "from app.core.design_registry import build_registry_request as b; \
  o=b('react_bits','SplitText',declared_dependencies=['gsap@^3.13.0','@gsap/react@^2.1.2']); \
  print(o.ok, o.request.required_dependency_ids)"

# 2. React Bits catalog discovery (LIVE HTTP).
python -c "from app.core.design_catalog_fetch import discover_catalog as d; \
  r=d('react_bits'); print(r.ok, 'SplitText' in r.installable_ids())"

# 3. 21st is credential-gated (LIVE HTTP -> 401, no free discovery).
python -c "from app.core.design_activation import activate_resource as a; \
  from app.core.design_resources import load_design_resource_manifest as m; \
  c=a(__import__('pathlib').Path('/tmp'), m().get('twenty_first'), system='Linux', machine='x86_64'); \
  print(c.discovery_available, c.authentication_required, c.degraded)"

# 4. Impeccable engine quality (full / degraded / missing).
python -c "from app.core.design_activation import engine_quality as q; \
  from pathlib import Path; print(q(Path.home()/'.hermes'/'skills'/'impeccable'))"

# 5. transitions-dev list (LIVE pinned CLI; no account).
npm exec --yes --package=transitions-dev@0.3.0 -- transitions-dev list

# 6. transitions-dev add card-resize, in a FRESH disposable dir (LIVE pinned CLI).
#    mktemp, not `mkdir -p`: a re-used dir would carry run 1's artifact, so
#    "creates ONLY ..." would no longer be a clean observation.
WORK="$(mktemp -d)" && cd "$WORK"
printf '%s\n' '{"name":"p","version":"1.0.0"}' > package.json
BASELINE="$(mktemp)" && cp package.json "$BASELINE"   # baseline OUTSIDE the tree
npm exec --yes --package=transitions-dev@0.3.0 -- transitions-dev add card-resize
find . -type f -not -path './node_modules/*' | sort
# expect exactly: ./package.json  ./transitions/card-resize.md
# The no-dependency-delta proof MUST be a check that can fail. `git diff --no-index
# package.json package.json` compares a file to ITSELF -- it exits 0 with empty
# output no matter what changed, so it proves nothing. Compare against the copy:
cmp -s "$BASELINE" package.json \
  && echo "package.json UNCHANGED (no dependency delta)" \
  || { echo "package.json CHANGED:"; diff "$BASELINE" package.json; }
# expect: "package.json UNCHANGED (no dependency delta)"
```

### L.3 — observed on the qualification host (2026-10)

| # | smoke | observed |
|---|---|---|
| 1 | SplitText contract | `True ('gsap', 'gsap_react')` |
| 2 | React Bits discovery (live) | `ok=True`, `SplitText` present |
| 3 | 21st credential gate | `discovery_available=False`, `authentication_required=True`, `degraded=True`; with `API_KEY_21ST` set → `discovery_available=True`, `degraded=False` |
| 4 | Impeccable engine quality | `missing` on a host with no provisioned skill |
| 5 | transitions-dev list | exit 0, 32 free recipes incl. `card-resize` |
| 6 | transitions-dev add | exit 0, `✓ Added Card resize → transitions/card-resize.md`; `find` shows ONLY `package.json` + `transitions/card-resize.md`; `cmp -s` reports `package.json UNCHANGED` (no dependency delta) |

**Live-ness proven, not assumed.** Smoke 2 returns `ok=False` with the socket
layer disabled and `ok=True` with it — it is a real live smoke, not a fake one.
Smoke 3 flips `discovery_available` False→True when a credential is configured,
so it is testing the gate, not a constant. Smoke 6's `cmp -s` check was verified
to FAIL when an extra dependency is injected, so it is a real check.

### L.4 — the reusable delta verifier against a real CLI artifact

After smoke 6, in the same disposable `$WORK` dir, the delta verifier must accept
the real artifact and refuse an injected extra dependency:

```bash
python -c "import os, sys; from pathlib import Path; sys.path.insert(0, '.'); \
  from app.core.design_install import snapshot_direct_dependency_state as s, verify_direct_dependency_delta as v; \
  root = Path(os.environ['WORK']); before = s(root); \
  import json; d = json.loads((root/'package.json').read_text()); \
  d.setdefault('dependencies', {})['evil-lifecycle'] = '9.9.9'; \
  (root/'package.json').write_text(json.dumps(d)); \
  r = v(before, s(root), allowed_additions=()); print(r.ok, r.added)"
# -> False (('dependencies', 'evil-lifecycle'),)
```

### VPS command: install into a DISPOSABLE frontend starter

Never install into a live project. Copy the starter to a throwaway dir, run the
app's installers, and prove the result builds — the whole point is that the
reviewed install produces a project that actually compiles.

```bash
cd website-builder && source .venv/bin/activate
APP="$(pwd)"          # the website-builder dir, for sys.path

# 0. A DISPOSABLE copy of the starter (no node_modules / dist).
WORK="$(mktemp -d)" && cp -r ../templates/frontend-starter "$WORK/project"
cd "$WORK/project" && rm -rf node_modules dist && npm ci --no-audit --no-fund

# 1. The app's BUILTIN install (card) through the real runner.
python - "$APP" <<'PY'
import sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.design_install import DesignDependencyInstaller

class Runner:
    def __init__(self): self.calls = []
    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        self.calls.append(list(command))
        return subprocess.run(list(command), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, shell=False)

runner = Runner()
outcome, installed, _ = DesignDependencyInstaller(runner, "p", Path(".")).install_components(["card"])
print("card:", outcome.state, installed)
print("argv:", runner.calls)
PY
# expect: card: installed ('card',); argv = shadcn add card --yes --overwrite, then npm install cn@0.4.0 --save-exact

# 2. The REVIEWED EXTERNAL component (SplitText) through install_external_component.
python - "$APP" <<'PY'
import sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.design_registry import RegistryInstallRequest, resolve_registry_locator
from app.core.design_install import DesignDependencyInstaller

class Runner:
    def __init__(self): self.calls = []
    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        self.calls.append(list(command))
        return subprocess.run(list(command), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, shell=False)

request = RegistryInstallRequest(
    source="react_bits", component_id="SplitText",
    registry_locator_id=resolve_registry_locator("react_bits", "SplitText"),
    required_dependency_ids=("gsap", "gsap_react"),
)
runner = Runner()
outcome = DesignDependencyInstaller(runner, "p", Path(".")).install_external_component(request)
print("SplitText:", outcome.state)
print("argv:", runner.calls)
PY

# 3. The result BUILDS. This is the real end-to-end proof.
npm run typecheck     # tsc -b over include:["src"] -- covers the installed files
npm run build         # vite build
```

**Observed on the qualification host (2026-10):**

| step | observed |
|---|---|
| builtin `card` | `state=installed`, `installed=('card',)`; materializes `src/components/ui/card.tsx` |
| external `SplitText` | `state=installed`; materializes `src/components/SplitText.tsx` |
| CLI-written ranges | the CLI writes `gsap@^3.15.0` / `@gsap/react@^2.1.2` (floating) |
| normalized pins | the app installs `gsap@3.15.0` and `@gsap/react@2.1.2` **exactly** (`--save-exact`) |
| `npm run typecheck` | **clean** (`tsc -b`; `include: ["src"]` covers the installed files) |
| `npm run build` | **clean** — `✓ 16 modules transformed`, `dist/assets/index-*.js 193.07 kB` |

**The install is REFUSED when tampered — and the refusal is load-bearing.** With a
runner that smuggles an extra dependency, the app returns `install_failed`
(`…introduced a direct package dependency outside the reviewed set`). With an
unreviewed source import, it returns `install_failed`
(`…emitted source imports a package…`). And the refusal MATTERS: appending
`import { Widget } from "brand-new-unreviewed-pkg"` to the installed component makes
`npm run typecheck` FAIL (`error TS2307: Cannot find module …`). So the boundary is
not ceremony — without it the disposable starter would not build.

**Why disposable.** `install_external_component` writes real files and runs `npm
install`; the whole batch's rule is that a build never mutates a live project. The
disposable copy makes the smoke safe to run repeatedly and safe to discard.

### VPS command: verify the FINAL package.json exact direct-dependency state

The install must leave the manifest at the **exact** application pins for every
package it governs — whether the CLI touched it or not. The subtle case is a
reviewed package the project **already declares at a floating range**: the CLI
writes the *same* value, so no delta appears. Run this to prove the range is
still normalized.

```bash
cd website-builder && source .venv/bin/activate
APP="$(pwd)"

# A disposable copy with a PRE-EXISTING floating range for a governed package.
WORK="$(mktemp -d)" && cp -r ../templates/frontend-starter "$WORK/project"
cd "$WORK/project" && rm -rf node_modules dist
python - <<'PY'
import json
from pathlib import Path
doc = json.loads(Path("package.json").read_text())
doc["dependencies"]["cn"] = "^0.4.0"   # the exact range the pinned CLI writes
Path("package.json").write_text(json.dumps(doc, indent=2))
print("before cn:", doc["dependencies"]["cn"])
PY

python - "$APP" <<'PY'
import sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.design_install import DesignDependencyInstaller

class Runner:
    def __init__(self): self.calls = []
    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        self.calls.append(list(command))
        return subprocess.run(list(command), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, shell=False)

outcome, installed, _ = DesignDependencyInstaller(
    Runner(), "p", Path(".")).install_components(["card"])
import json
final = json.loads(Path("package.json").read_text())
print("state:", outcome.state, "| final cn:", final["dependencies"]["cn"])
PY
# expect: state: installed | final cn: 0.4.0   (the range is normalized to the pin)
```

**Observed on the qualification host (2026-10):** with `cn` pre-declared at
`^0.4.0`, the builtin `card` install reports `installed` and the final `cn` is
**`0.4.0`** (the exact pin) — the normalization runs because the guard reasons
over the **governed** set, not just the CLI's delta. Before the sixth-pass fix
this printed `final cn: ^0.4.0` (a surviving range) while still claiming
`installed`.

### VPS command: `typecheck` a generated project after a reviewed install

`typecheck` is the starter's own script (`"typecheck": "tsc -b"`). It is one of
the three cheap checks that gate a real build (`npm ci` / `npm run build` /
`npm run typecheck`). Run it on a disposable copy after installing the reviewed
components — including the ones whose emitted source imports a starter runtime
dependency (`class-variance-authority`), which the reviewed set must accept.

```bash
cd website-builder && source .venv/bin/activate
APP="$(pwd)"

WORK="$(mktemp -d)" && cp -r ../templates/frontend-starter "$WORK/project"
cd "$WORK/project" && rm -rf node_modules dist && npm ci --no-audit --no-fund

# Install the reviewed builtins whose source imports class-variance-authority.
python - "$APP" <<'PY'
import sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.design_install import DesignDependencyInstaller

class Runner:
    def __init__(self): self.calls = []
    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        self.calls.append(list(command))
        return subprocess.run(list(command), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, shell=False)

for comp in ("card", "button", "dialog", "tabs"):
    outcome, _, _ = DesignDependencyInstaller(
        Runner(), "p", Path(".")).install_components([comp])
    print(f"{comp}: {outcome.state}")
PY

npm run typecheck     # tsc -b -- must exit 0
```

**Observed on the qualification host (2026-10):** `card`/`button`/`dialog`/`tabs`
all report `installed`, and `npm run typecheck` exits **0**. `tsc -b` was proven
load-bearing: injecting a type error into `src/App.tsx` makes it exit non-zero,
and re-running does **not** hide the error behind the incremental `tsbuildinfo`
cache (build mode re-checks changed inputs). Before the fix this command refused
`button`/`dialog`/`tabs`/`alert`/`badge` (`…emitted source imports a package
outside the application-owned reviewed set`) because `class-variance-authority`
was absent from the always-provided set.

### VPS command: `build` a generated project after a reviewed install

`build` is the starter's `"build": "tsc -b && vite build"` — typecheck, then
bundle. Run it on a disposable copy after installing the reviewed components,
and confirm `dist/` is produced. The build is only meaningful if the installed
components are **actually bundled**: Vite tree-shakes an unimported component
away, so import them before building.

```bash
cd website-builder && source .venv/bin/activate
APP="$(pwd)"

WORK="$(mktemp -d)" && cp -r ../templates/frontend-starter "$WORK/project"
cd "$WORK/project" && rm -rf node_modules dist && npm ci --no-audit --no-fund

# Install EVERY reviewed builtin + the reviewed external component.
python - "$APP" <<'PY'
import sys, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.design_install import DesignDependencyInstaller, ALLOWED_SHADCN_COMPONENTS
from app.core.design_registry import RegistryInstallRequest, resolve_registry_locator

class Runner:
    def __init__(self): self.calls = []
    def run_command(self, project_id, command, cwd=None, env=None, timeout=300.0):
        self.calls.append(list(command))
        return subprocess.run(list(command), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout, shell=False)

o, installed, rejected = DesignDependencyInstaller(
    Runner(), "p", Path(".")).install_components(sorted(ALLOWED_SHADCN_COMPONENTS))
print("builtins:", o.state, len(installed), "rejected:", rejected)
req = RegistryInstallRequest(source="react_bits", component_id="SplitText",
    registry_locator_id=resolve_registry_locator("react_bits", "SplitText"),
    required_dependency_ids=("gsap", "gsap_react"))
o2 = DesignDependencyInstaller(Runner(), "p", Path(".")).install_external_component(req)
print("SplitText:", o2.state)
PY

npm run build     # tsc -b && vite build -- must exit 0
ls dist/assets/   # dist/ must be produced
```

**Observed on the qualification host (2026-10):** all 16 reviewed builtins +
`SplitText` install (`installed`), `npm run build` exits **0**, and `dist/`
contains `index.html` + `dist/assets/index-*.{js,css}`.

**The build is load-bearing.** Vite tree-shakes an unimported component: with the
components installed but not imported the bundle is `193.07 kB` and contains
**none** of their code; importing them (via `src/App.tsx`) makes the build report
**2026 modules transformed** and the bundle grow to `312.25 kB` with the
component code present. So a green build only proves the installed components
compile when they are actually reachable — which is what a real generated site
does.

**Toolchain shadow-config guard (fixed in this pass).** Vite resolves its config
by PRECEDENCE (`vite.config.js` → `.mjs` → `.ts` → …) and the starter ships only
`vite.config.ts`. A FRONTEND-created `vite.config.js` therefore **shadows** the
platform-owned config: reproduced live, planting one with a different
`build.outDir` made `npm run build` write to `evil-dist` while the by-name hash
guard reported *untouched*. The build's toolchain guard now also rejects any
`FORBIDDEN_TOOLCHAIN_FILES` (`vite.config.{js,mjs,cjs,mts,cts}`) and any
protected file that APPEARS after capture — `TOOLCHAIN_MUTATION_REJECTED`.

### VPS command: Transitions `add card-resize` (bounded recipe materialization)

The pinned `transitions-dev@0.3.0` CLI materializes ONE free recipe as
`transitions/<slug>.md`. The app builds the argv and verifies the artifact;
neither is run by the unit suite (it bans subprocesses and sockets), so this is
the live check.

```bash
cd website-builder && source .venv/bin/activate
APP="$(pwd)"

WORK="$(mktemp -d)" && cd "$WORK"
printf '%s\n' '{"name":"p","version":"1.0.0"}' > package.json
printf '%s\n' '{}' > package-lock.json
BASELINE="$(mktemp)" && cp package.json "$BASELINE"

# The pinned CLI, one catalog-listed slug.
npm exec --yes --package=transitions-dev@0.3.0 -- transitions-dev add card-resize

# It must write ONLY the Markdown, and NOT touch package.json.
find . -type f -not -path './node_modules/*' | sort
cmp -s "$BASELINE" package.json && echo "package.json UNCHANGED (no dependency delta)"

# The app's argv + verification against that real artifact.
python - "$APP" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.design_install import build_pinned_cli_prefix, detect_package_manager
from app.core.design_transitions import (
    build_add_argv, normalize_recipe_catalog, verify_recipe_materialized,
    approved_recipes_dir, detect_reduced_motion_guard, RESERVED_RECIPE_SLUGS,
)
catalog = normalize_recipe_catalog({"transitions": [
    {"slug": "card-resize", "name": "Card resize", "tier": "free"},
]})
root = Path(".")
prefix = build_pinned_cli_prefix(detect_package_manager(root), "transitions_dev", root)
print("argv          :", build_add_argv(prefix, "card-resize", catalog))
print("verified files:", verify_recipe_materialized(root, "card-resize", approved_recipes_dir(root)))
print("reduced-motion guard:", detect_reduced_motion_guard((Path("transitions")/"card-resize.md").read_text()))
print("reserved slugs:", sorted(RESERVED_RECIPE_SLUGS))
PY
```

**Observed on the qualification host (2026-10):** `add card-resize` prints
`✓ Added Card resize → transitions/card-resize.md`, the only new file is
`transitions/card-resize.md`, and `package.json` is **byte-identical**; the app's
argv is `npm exec --yes --package=transitions-dev@0.3.0 -- transitions-dev add
card-resize`, `verify_recipe_materialized` → `('card-resize.md',)`, and the
reduced-motion guard is detected `True`.

**Verified for EVERY free recipe, not just the sample:** all **32** free recipes
were materialized in fresh dirs -- each wrote exactly `transitions/<slug>.md`,
each left `package.json` byte-identical, and all 32 carry the
`@media (prefers-reduced-motion: reduce) {` guard.

**The bulk selectors are refused.** `add all` / `add free` / `add pro` are
refused at the slug VOCABULARY (`RESERVED_RECIPE_SLUGS`), so `build_add_argv`
returns `None` for them no matter what the catalog lists -- a manifest that
LISTS `all` can no longer turn a bounded one-recipe selection into an
open-ended bulk install.

### The Linux containment family: 26/26 -- and two SHADOWED tests found

The Part D driver (`tools/mutation_check_d3a5_partd.py`) carries the containment
family: `a recipe file outside the project is not materialized`,
`an unresolved destination verifies nothing`, and `a recipes dir escaping the
project is refused`. On Windows the third is a `HOST-SKIP` (a directory symlink
needs Developer Mode), so the VPS figure is **26/26**; on this Linux host the
symlink test **runs** and the guard **kills**, and the whole driver is green.

Auditing that family surfaced a real defect of the batch's class -- a guard whose
enforcement is weaker than the property it names:

- `tests/test_design_transitions.py` defined
  `test_a_symlinked_recipes_dir_escaping_the_project_is_refused` **three times**
  (a paste at `74fc55e6c`). Python binds only the LAST definition, so the two
  earlier bodies were **dead code that pytest never collected** -- and one of
  them carried a latent `_symlinks_available(tmp_path)` **arity bug** that never
  ran. The file also had a duplicated `# Reduced-motion ...` section header.
- `tests/test_design_critic.py` defined
  `test_containment_is_measured_against_the_resolved_root` **twice** (identical
  bodies, so no coverage was lost -- the same paste defect).

**Fix:** both files now define each name exactly once, and a new
`tests/test_suite_hygiene.py` pins the general property -- **no `tests/test_*.py`
may define the same `test_*` name twice** -- with a self-proof that the scanner
actually flags a planted duplicate (so the guard cannot be vacuous). A test that
is defined but never collected is not a test.

### The default suite is OFFLINE -- and a real violation was found + fixed

**Constraint: normal `pytest` must never depend on the internet.** The suite is
supposed to be offline (no non-loopback network), and the live smokes are
documented rather than run for exactly that reason. Auditing it found one real
violation:

`tests/test_starter_toolchain.py` ran the **real `npm ci`** (four command
assertions), which resolves every package from the npm registry. It was gated
only on `npm` being installed, so it ran by DEFAULT -- and a warm local npm cache
silently hid the dependency until a cold host ran it. Proven: `npm ci` with an
empty cache fails with `ENOTCACHED` (it must fetch `vite-8.2.0.tgz` from
`registry.npmjs.org`). The file's own comment even called `npm ci` *"the slow,
network-touching step"* while leaving it unmarked.

**Fix:** the four command assertions now carry `@pytest.mark.integration` (the
repo's own marker; `addopts = "-m 'not integration'"` deselects them from a
normal run). The ten static assertions above them -- manifest, lockfile,
tsconfig, `.nvmrc` -- still run offline and are what kill the regression, so the
default run loses no coverage of the pin; the command assertions exist to prove
the real toolchain builds, and are runnable with `-m integration`.

**Guard:** `tests/test_suite_offline.py` pins the general property -- no
non-integration test may resolve or run a network CLI (`npm`/`npx`/`pnpm`/`yarn`/
`pip`/`curl`/`wget`/`gh`), or call a network entry point against a non-loopback
host. It is AST-based and **follows module-level helpers** (the offender hid one
hop away: the test called `_run`, and `_run` invoked `NPM`), recognises an
`@pytest.mark.integration` alias, and does NOT flag a test that *blocks* the
network (`monkeypatch.setattr(urllib.request, "urlopen", boom)` is a call to
`setattr`, not `urlopen`). Verified load-bearing: removing the markers makes it
fail.

The rest of the suite was audited and is offline: every `git` subprocess uses a
local `file://` mirror, `push_github` raises before any network, and the only
other socket users bind **loopback** (the port-allocation tests).

### Why these are manual, not in the suite

The default suite is **offline** (no non-loopback network), enforced by
`tests/test_suite_offline.py`, which fails if any non-integration test resolves
or runs a network CLI (`npm`/`npx`/`pnpm`/`yarn`/`pip`/`curl`/`wget`/`gh`) or
calls a network entry point against a non-loopback host. It is NOT a socket
*ban*: the port-allocation tests legitimately bind **loopback** sockets
(`127.0.0.1:0`), and the guard deliberately allows loopback. A live smoke needs a
real non-loopback call, so it cannot be a unit test without an
`@pytest.mark.integration` mark. The doc records the COMMANDS and the OBSERVED
outputs; the suite separately pins the offline behaviour
(`test_live_smoke_is_documented_rather_than_executed`) and the fact that every
symbol these commands import still exists.


