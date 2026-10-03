# Batch D0.1–D0.2 — Design Capability Bootstrap

Capability plumbing only. **No FRONTEND design behavior, Design DNA contract,
prompt, publish, hydration, release identity, watchdog, convergence, canonical
URL, or lifecycle semantics were changed.**

## What this batch does

It answers two questions that were previously indistinguishable:

1. **What is configured?** — `config/design_resources.yaml`, one authoritative
   declaration of every design resource and how each is resolved.
2. **What is actually available on this machine?** —
   `app/core/design_capabilities.py`, which inspects the real profile and
   reports what it finds.

Conflating those is the failure this layer exists to prevent. Before D0, the
only statement about UI UX Pro Max was prose in `skills/README.md` — nothing
verified it existed, and nothing could tell "declared" apart from "present".

## Files

| File | Role |
|---|---|
| `config/design_resources.yaml` | Single source of truth for design-resource configuration |
| `app/core/design_resources.py` | Manifest model + fail-closed loader + the one definition of the profile skills dir |
| `app/core/design_capabilities.py` | Profile-local resolution, availability verification, preflight |
| `app/hermes/adapter.py` | `_profile_skills_dir()` now delegates to the shared definition (one-line change) |
| `app/runtime.py` | One `preflight_design_capabilities()` call after role validation |
| `tests/test_design_resources.py` | Focused behaviour-contract tests |
| `tools/mutation_check_d0.py` | Proves each guard is actually load-bearing |

Nothing is installed, cloned, or fetched. No network. No subprocess.

## Manifest schema

```yaml
version: 1
data_entries_max: 32
resources:
  <resource_id>:                      # ^[a-z0-9_]+$, unique YAML key
    kind: skill | reference | registry | npm_optional
    required: true | false
    resolution: profile_skill | deferred
    skill_name: <str>                 # kind: skill only
    install_mode: project_on_demand   # kind: registry | npm_optional only
    data_entries: [<relpath>]         # bounded; resolution: profile_skill only
```

Ten resources: `ui_ux_pro_max` (skill, required), `impeccable` (skill),
`refero` / `twenty_first` / `react_bits` / `transitions_dev` (reference),
`shadcn` (registry), `gsap` / `three` / `lenis` (npm_optional).

`ui_ux_pro_max` pins its real local data — `scripts/search.py` and
`data/styles.csv`. A required skill verified only by `SKILL.md` is a
placeholder directory, which is precisely the case this batch distinguishes
from a usable capability. The full dataset is deliberately not pinned.

### Validation is fail-closed

`DesignResourceManifestError` on: unsupported version, empty/missing
`resources`, unknown top-level or per-resource keys, unknown `kind` or
`resolution`, non-boolean `required`, missing/extra `skill_name`, missing/extra
`install_mode`, unknown install mode, `data_entries` on a non-profile skill,
over-length `data_entries`, and **duplicate YAML mapping keys**.

Duplicate detection matters because `yaml.safe_load` silently keeps the *last*
value. A manifest declaring `gsap` twice with different `required` flags would
otherwise load cleanly and apply the wrong one — on the exact field that
decides whether a missing capability blocks startup.

### `data_entries` containment

Two halves, both required:

- **Syntactic (load time, before any filesystem access).** Entry must be
  relative, non-empty, not `.`/`./`, and free of `..` under **both** separators
  — `scripts\..\..\etc` is rejected alongside `scripts/../../etc`. Absolute
  POSIX, Windows drive-anchored, and `~`-anchored forms are rejected.
- **Resolved (verification time).** `entry_path.resolve()` must be
  `relative_to` the resolved skill directory.

The second half is what makes the first non-bypassable: a symlink *inside* the
skill directory pointing at `/etc/passwd` is relative, has no `..`, and exists —
it passes every string check and is caught only once resolved.

## Capability result schema

```python
{
  "ok": bool,                     # every REQUIRED resource available
  "manifest_version": 1,
  "profile_skills_dir": str,       # the one path ever surfaced
  "resources": {
    "<resource_id>": {
      "kind": str,
      "required": bool,
      "configured": bool,          # declared in the manifest
      "available": bool,           # VERIFIED on this filesystem
      "status": str,               # closed set, below
      "detail": str,               # static sanitized label
    }, ...
  },
  "failures": [...],               # required + unavailable -> ok False
  "degraded":  [...],              # optional + unavailable (not on-demand)
}
```

| status | meaning | fatal |
|---|---|---|
| `available` | verified present and readable | no |
| `unavailable_required` | required, not verified present | **yes** |
| `unavailable_optional` | optional, not provisioned | no |
| `not_installed` | `install_mode: project_on_demand`, absent as expected | no |

`detail` is always one of a small set of static labels — never a path, never
file content. The single surfaced path is `profile_skills_dir`, matching the
one-line pattern `preflight_role_validation` already uses.

### What `available` does and does not mean

It means **statically verified local resource layout**: present, contained,
readable. It does **not** mean the UI UX Pro Max search command has been
*executed successfully*. Resolution is stdlib/local — no subprocess, no
import of the skill's own code, no network. Executing the search entrypoint is
a separate layer, proven later by the D0 integration smoke test.

## Required / optional semantics

- required + unavailable → `ok=False`, listed in `failures`, startup refuses.
- optional + unavailable → `ok` unchanged, listed in `degraded`, logged once
  as a warning. Never fatal.
- on-demand + absent → `not_installed`. **Not** a degradation.

### `not_installed` is keyed off `install_mode`, not `kind`

`shadcn` is a **registry**, not an npm package, and is provisioned per project
exactly as the npm resources are — so it shares their resting state and reports
`not_installed` identically. Scoping this status to `npm_optional` would report
a permanent, unactionable degradation for every registry resource on every run,
which is a field operators learn to ignore.

### Names are never evidence

A resource becomes `available` only by being explicitly provisioned. Mentioning
`shadcn` / `gsap` / `three` / `lenis` in a `SKILL.md` body, a prompt, a source
file, or a row of `data/styles.csv` proves nothing. This is a live concern
here: UI UX Pro Max's dataset *names* all four as guidance, and a resolver that
scanned for those names would report them installed.

## Resolution is root-relative

The profile home arrives as `RuntimeConfig.hermes_home`, already
`expanduser()`-ed and env/config-resolved. This layer never reads
`HERMES_HOME` from the environment and never constructs a home path — resolving
the profile is the runtime's job, and a resolver that guessed its own would
disagree with the runtime the first time they diverged.

`app/core/design_resources.design_profile_skills_dir` is the **only** place
`$HERMES_HOME/skills` is written down;
`HermesAdapter._profile_skills_dir()` delegates to it. Two independent
spellings is how a capability check ends up verifying a path the runtime never
loads from.

## Verification

```bash
scripts/run_tests.sh website-builder/tests -j 1     # full suite
python -m pytest tests/test_design_resources.py -q # focused
python tools/mutation_check_d0.py                  # guard proofs
```

### Mutation results

All nine guards killed by the focused suite — see `tools/mutation_check_d0.py`.
Each corresponds to a specific wrong outcome (a self-contradicting manifest
loading, a missing required capability passing preflight, on-demand resources
degrading forever, capability data readable outside the skill root, a
placeholder directory passing as a capability, the data check going vacuous).

## Known environment limitation

The symlink-escape test skips on Windows hosts without symlink privilege. The
guard is therefore **also** tested directly via `entry_is_contained()` against
real out-of-root paths, so containment is exercised regardless of host
privilege — the skip does not leave it unverified.

## Not in this batch

- No resource was cloned, imported, or installed (Impeccable, Refero, 21st.dev,
  React Bits, Transitions.dev, shadcn, GSAP, Three.js, Lenis).
- No npm package was added to any manifest.
- No prompt, skill content, or Design DNA contract was changed.
- No live E2E was run.
- D0.3 (integration smoke test, actual search execution) is not started.