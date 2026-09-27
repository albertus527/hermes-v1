# Website Builder R2 — Batch A: Credential Isolation

Repository: `albertus527/hermes-v1`  ·  Branch: `feature/website`
Status: implemented, tested, **not committed / not pushed / not deployed**

> **R2-B2 follow-up (risks #2 and #5)** is included below: provider-scoped
> credential narrowing, the generation-profile guards, the relocation of the
> Vercel bypass secret store, and a new residual risk that is **explicitly
> unresolved** — same-user execution isolation, §7 risk 10. Read that one before
> enabling the Hostinger/Strix adapters or exposing Website Builder to
> untrusted users.

---

## 1. Exact existing leak paths found

Audit of every subprocess/process-creation site reachable from FAST, FRONTEND,
VISION, npm/build/typecheck, Git and Vercel.

| # | Site | Before | Severity |
|---|------|--------|----------|
| **L1** | `app/hermes/adapter.py::_run_hermes_cli` | `env = os.environ.copy()` → the FRONTEND/VISION child inherited **every** privileged credential in the parent process. FRONTEND holds `file`, `terminal`, `skills` toolsets, so one terminal call could read/exfiltrate a deploy token. | **Critical** — the leak R1 review identified |
| **L2** | `app/sandbox/runner.py::build_hermes_env` | `env = os.environ.copy()` — same broad inheritance on the second Hermes env builder. | **High** |
| **L3** | `app/deploy/git_output.py::OutputGitRepository._run` | `env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}` — every **non-Git** credential (Vercel/Hostinger/Strix tokens) was handed to every `git` invocation. | **High** |
| **L4** | `app/qa/screenshot.py::_run_browser_command` | `subprocess.run(...)` with **no `env=`** → `agent-browser` (which launches Chromium) inherited the full parent environment. | Medium |
| **L5** | `app/runtime.py::preflight_node_toolchain` | Two `subprocess.run([node/npm, --version])` with no `env=`. | Low |
| **L6** | `app/sandbox/runner.py::is_source_repo_clean` | `subprocess.run(["git","status","--porcelain"])` with no `env=`. | Low |

Already correct, and **preserved unchanged**: `ProjectRunner._build_project_env`
(strict benign allowlist) — the good pattern this batch generalises; and the
in-process FAST/VISION path, which spawns no child.

### 1a. R2-B2 — leaks found on the FILE and PROFILE layer

Environment scoping cannot see either of these, and both are invisible to a
canary that only inspects a child environment.

| # | Site | Before | Severity |
|---|------|--------|----------|
| **L7** | `app/runtime.py` → `BypassSecretStore(config.hermes_home / "vercel-bypass")` | The per-project **Vercel automation-bypass secret** was written to `~/.hermes-website/vercel-bypass/<prj_id>.json`. `HERMES_HOME` *is* `~/.hermes-website` for the FRONTEND child, the terminal tool reads any absolute path under `HERMES_YOLO_MODE=1`, and `get_read_block_error` does not cover that path. Self-inflicted and automatic — written for every project with a preview. | **High** — see §7 risk 9 |
| **L8** | `$HERMES_HOME/.env` | The `hermes -z` child re-runs `load_hermes_dotenv()`, which loads it with `override=True` and **unfiltered** *inside the child*. A privileged credential there never passes through `agent_env`, so no amount of parent scoping catches it. | **Medium** — see §7 risk 5 |
| **L9** | `TELEGRAM_BOT_TOKEN` | Excluded from a generation child by allowlist *omission* only, so `assert_no_privileged` could not assert it: a future allowlist widening would leak the bot token with every canary still green. | **Low** — unenforced |

---

## 2. Files changed

**New**
- `website-builder/app/core/credentials.py` — the single policy authority
- `website-builder/tests/test_r2_credential_isolation.py` — 73 canary tests
  (34 from R2-B1, 39 from R2-B2)

**Modified**
- `website-builder/app/hermes/adapter.py` — role-scoped env + fail-closed assertions
  (R2-B2: provider narrowing, profile guards, residual warning)
- `website-builder/app/sandbox/runner.py` — delegate to policy; scope both env builders
- `website-builder/app/core/secrets.py` — `BypassSecretStore` gains `legacy_roots`
  so the store can be relocated without losing a provisioned secret (R2-B2), plus
  `migrate_legacy_bypass_secrets()` — the R1 → R2 startup migration that moves a
  pre-R2 store out of the profile home (R2-B3)
- `website-builder/app/runtime.py` — bypass store root moved to `state_root`;
  `preflight_role_validation` also gates the profile (R2-B2) and runs the legacy
  bypass migration before that gate (R2-B3)
- `website-builder/docs/R2_BATCH_A_CREDENTIAL_ISOLATION.md` — this document
- `website-builder/app/deploy/git_output.py` — Git adapter-scoped env
- `website-builder/app/qa/screenshot.py` — strict shell env for `agent-browser`
- `website-builder/app/runtime.py` — strict shell env for toolchain probes
- `website-builder/tests/test_runner.py` — updated the test that encoded the leak
- `website-builder/tests/test_frontend_role_cli.py` — mock the real execution seam
  (section 6a); no production behaviour changed

No `os.environ.copy()` remains in any live path (verified by grep; remaining
matches are comments describing the fix).

### 2a. R2-B3 — the R1 → R2 upgrade path

R2-B2 left an un-migrated R1 install **unable to start**: it had provisioned
secrets under the profile home, and `assert_profile_home_clean` (run at startup
preflight, before `compose()`) refuses a profile that still holds that
directory. The `legacy_roots` read path Batch A added could therefore never be
exercised, and the operator's only apparent recourse was deleting provisioned
secrets.

`migrate_legacy_bypass_secrets(hermes_home, state_root)` — in
`app/core/secrets.py`, the store's own module, so path/permission/validation
logic cannot drift from the store contract — runs as the **first** step of
`preflight_role_validation` and is the *only* call site. It has one deliberate,
verified on-disk side effect: it creates `<state_root>/vercel-bypass`, and only
when the known legacy directory exists.

* **The destination is proven to be outside the profile home first**, before a
  single byte is written or renamed: if the resolved `state_root` is the
  resolved `HERMES_HOME`, or anywhere underneath it, the migration fails closed
  and touches nothing. This is checked by the migration rather than by
  `assert_profile_home_clean`, which is deliberately **not** extended into a
  recursive walk — it has to stay O(entries) over a profile that holds sessions
  and caches. A direct-children guard cannot see
  `HERMES_HOME/state/vercel-bypass`, and a state root equal to the profile would
  rename the store aside under a name the guard does not recognise while the
  secret stayed behind, satisfying the guard instead of the boundary.
* **Moved, never deleted.** The legacy directory is retired with one atomic
  `os.replace` to `<state_root>/vercel-bypass-migrated-<UTC timestamp>[-N]`.
  Nothing is lost; the operator can delete that directory once satisfied.
* **Cross-device retirement is refused, not emulated.** `os.replace` cannot
  span filesystems, so `EXDEV` fails closed with the legacy directory untouched
  and the primary entries already written left valid. There is deliberately no
  copy+delete fallback: it would trade a recoverable refusal for a window in
  which the only copy of a secret is in neither place. The operator points
  `WEBSITE_BUILDER_STATE_ROOT` (or `state_root` in `config.yaml`) at a directory
  on the same filesystem as the profile home for this one-time migration.
* **Fail closed on anything unverifiable** — a subdirectory, a symlink, a
  non-`.json` name, a `.json` whose stem is not a valid project id, unparsable
  JSON, a pathologically nested document (the JSON decoder's `RecursionError`),
  a missing/blank secret, or a read-back mismatch aborts with the legacy
  directory **untouched** and the startup refused. A `*.tmp` leftover (the
  store's own `mkstemp` crash artifact) is the single tolerated case: it is
  carried into quarantine and reported by count.
* **Primary wins.** Where the state root already holds a project, the entry is
  only verified readable — never rewritten, never merged. An unreadable primary
  next to a valid legacy copy fails closed naming both paths.
* **Idempotent / crash-safe.** Writes are atomic and re-read before the
  retirement, so an interrupted run is simply re-run by the next start.
* **`legacy_roots` is now belt-and-braces** — a read path for a store that
  reappears (an operator-restored profile backup, a fresh state root), not the
  migration mechanism.
* **No secret, ever, in a log or error** — ids, counts and paths only.

No operator CLI, no `--dry-run` (D4): the migration happens on a real start.
`tests/r1_harness.py` still builds a store at the pre-migration path — that is a
fixture simulating the R1 layout, not production wiring, and is out of scope.

---

## 3. Resulting environment contract per role

One shared benign base (identical to the allowlist that already shipped in
`_build_project_env`): `PATH`, `HOME`, `TEMP`, `SystemRoot`, toolchain managers
(`NVM_*`, `VOLTA_HOME`, `FNM_*`), plus prefixes `XDG_`, `PROGRAMFILES`,
`PROGRAMDATA`, `LOCALAPPDATA`, `APPDATA`.

| Role | Receives | Never receives |
|------|----------|-----------------|
| **FAST** | benign + model/provider creds + `HERMES_*` runtime | all deploy + messaging creds |
| **FRONTEND** | identical to FAST, **narrowed to its own provider** | all deploy + messaging creds (**incl. `terminal` tool**) |
| **VISION** | identical to FAST | all deploy + messaging creds |
| **build** (npm ci/build/typecheck, vite preview) | benign + `HERMES_HOME`/`PROJECT_ID`/`WORKSPACE_ROOT` | deploy creds **and** model creds |
| **shell** (agent-browser, node/npm probes) | benign only | everything privileged |
| **git** | benign + Git/SSH config only | Vercel/Hostinger/Strix, model creds |
| **vercel** | benign + Vercel's own creds | GitHub/Hostinger/Strix |
| **hostinger / strix** | *registered seam, no adapter implemented* | every other adapter's creds |

Key design points:

- **Allowlist, never denylist** — an unanticipated credential cannot leak.
- **Model allowlist is derived from Hermes' own registry**
  (`OPTIONAL_ENV_VARS[category == "provider"]` + `_EXTRA_ENV_KEYS`, 69 names), so
  adding a provider does not silently break a role.
- **Fail-closed guards** — unknown role, unknown adapter, and any attempt to
  inject a privileged name through the `extra` hook all raise. `_run_hermes_cli`
  refuses to spawn rather than run weakened. R2-B2 adds two more fail-closed
  guards on the *profile* rather than the environment: a profile `.env` carrying
  a privileged credential, and a privileged secret store inside a profile home
  (see §7 risks 5 and 9).
- **Parent is never mutated** — isolation is per-child; no global deletion
  (requirement 7), so normal developer/runtime shell behaviour is untouched.
- **Git key stays adapter-bound** — a *path* in `GIT_SSH_COMMAND`; material never
  read, exported, placed in argv/URL/config, or made model-visible (requirement 5).
- **Vercel unchanged** — the token still reaches `VercelAdapter` in-process as a
  constructor arg and is applied as an `Authorization` header. It is deliberately
  *not* exported into any environment, since no Vercel subprocess needs it.

No credentials were placed in prompts, agent-visible context, Design DNA,
generated source, diagnostics payloads, ordinary logs, or argv.

---

## 4. Tests added

`tests/test_r2_credential_isolation.py` (**73 tests**, R2-B1's 34 plus the 39
added by the R2-B2 follow-up on remaining risks #2 and #5) using the required
canaries (`WEBSITE_BUILDER_GITHUB_SSH_KEY`, `VERCEL_TOKEN`, `HOSTINGER_TOKEN`,
`STRIX_TOKEN`, `TELEGRAM_BOT_TOKEN`, …):

- FAST/FRONTEND/VISION receive no privileged canary (values, not just names)
- generation-role env is allowlisted, not inherited from the parent
- generation role still keeps its model credential (guards over-correction)
- unknown role fails closed; `extra` cannot smuggle a credential
- build shell + `shell_env` receive no privileged canary, and no model credential
- real `ProjectRunner._build_project_env` has no privileged canary
- `agent-browser` subprocess passes an explicit, clean env
- Git adapter still gets `GIT_SSH_COMMAND` with the key path + `BatchMode`/`IdentitiesOnly`
- Git env carries Git creds but no other adapter's
- real `git` subprocess env is adapter-scoped
- Vercel deployment still works: `Authorization: Bearer <token>` still set
- future Hostinger/Strix seam is registered and isolated
- unknown adapter fails closed
- no canary in serialized diagnostics or watchdog receipts
- parent environment never mutated
- **regression:** generation roles keep `PYTHONPATH`/`VIRTUAL_ENV`/etc. so the
  child can still `import hermes_cli`; proven end-to-end with a real subprocess,
  and the fix is not a re-opening of the credential hole

### R2-B2 additions (risks #2 and #5)

*What the agent's own tools can see* — the half of the boundary that scoping a
child environment does not cover:

- the real `tools.environments.local._sanitize_subprocess_env` (the exact factory
  the terminal backend calls) drops every model credential from the FRONTEND
  child env, except the registered exceptions, and the survivors are exactly
  those exceptions — no silent extras
- **invariant, not a snapshot:** every provider credential Hermes *currently*
  declares is either scrubbed or registered in
  `CREDENTIALS_NOT_SHELL_PROTECTED`; the failure message lists offenders
- `CLAUDE_CODE_OAUTH_TOKEN` is a registered, warned exception (Hermes
  deliberately excludes it from its own scrub — see §7 risk 8)
- with the role's provider resolved, no secret-valued model credential survives
  the scrub at all, and `assert_model_credentials_shell_invisible` passes
- narrowing limits the child env to that provider's own names, consults **both**
  Hermes provider registries (so plugin providers like `openrouter` narrow too),
  never widens the allowlist, and an unknown provider still receives its key
- **characterization:** the terminal scrub does *not* strip
  `VERCEL_AUTOMATION_BYPASS_SECRET` / `HOSTINGER_*` / `STRIX_*` /
  `WEBSITE_BUILDER_GITHUB_SSH_KEY` — which is precisely why the two guards below
  exist. Marked as a limit, not as desired behaviour.

*The generation profile itself* — where a credential on disk is readable no
matter how the environment was scoped:

- `assert_profile_dotenv_clean` rejects every privileged credential name in
  `$HERMES_HOME/.env` (parametrized over the whole canary set), and the
  diagnostic names the variable but never its value
- a model-only `.env` is accepted; an absent `.env` is not an error
- `assert_profile_home_clean` refuses a profile home containing a registered
  privileged secret subdirectory
- the real `agent.file_safety.get_read_block_error` refuses the website
  profile's `.env` — proven, because `~/.hermes-website` is **not** under the
  default Hermes root, so only the secret-bearing-basename arm can cover it
- the production `BypassSecretStore` root is outside every Hermes profile, and
  a secret written at the pre-migration location is still readable (no silent
  loss), with the primary root winning and new writes landing only in the
  primary
- a malformed secret file produces no log output containing the value
- **fail-closed at the real seam:** with a `.env` carrying a deploy credential,
  or a `vercel-bypass/` directory in the profile, the production
  `HermesAdapter._run_hermes_cli` returns a failure and
  **`subprocess.run` is never reached**; a clean profile still spawns normally
- messaging/channel credentials are *registered* as privileged, so
  `assert_no_privileged` can assert them rather than relying on allowlist
  omission, and `extra` refuses them on every path
- the new gates read key names only and never mutate the parent

`tests/test_runner.py` — replaced `test_hermes_env_preserves_credentials`, which
**asserted the vulnerability** (that a Telegram token reaches the Hermes env).
It now asserts the R2 contract, plus a new test that the model credential
survives and one that the parent is not mutated.

### R2-B3 additions (the R1 → R2 upgrade path)

`tests/test_bypass_legacy_migration.py` (28 tests) — real `BypassSecretStore`
writes and real files, mocks only for injected faults:

- a clean install is a genuine no-op: nothing is created anywhere, and
  `assert_profile_home_clean` still passes
- **the state root is validated before anything moves:** a root that equals
  `HERMES_HOME`, sits one level under it, or is deeply nested under it fails
  closed with a byte-identical profile and nothing created; a sibling whose
  name merely shares a string prefix with the profile is accepted, so the check
  is containment and not a string comparison
- a valid legacy secret lands in the state root with the store's 0600/0700
  modes and the exact payload shape; the profile no longer holds the directory
  and the guard is **satisfied, not weakened** — it is proven to still refuse
  the very same directory name, and `BYPASS_STORE_DIRNAME` is asserted to be
  the one `PRIVILEGED_SECRET_SUBPATHS` refuses
- primary wins: a pre-existing valid primary entry is left byte-identical and
  reported as `already_current`
- repeated startup is a no-op with exactly one quarantine directory; new writes
  land only in the state root
- fail-closed, with the legacy directory and every file byte-identical and
  nothing retired: malformed JSON, non-object JSON, blank secret, a file
  declaring another project id, a subdirectory, a symlinked entry, a symlinked
  store directory, `..%2fescape.json`, `bad name.json`, `.json`, `notes.txt`
- a corrupt/unreadable primary beside a valid legacy copy fails closed naming
  the project id and **both** paths, touching neither
- interruption: a write that fails mid-copy, a write that does not read back
  identically, and a refused retirement all leave the legacy directory in place
  and the already-migrated entries valid; a clean re-run completes the move
- a cross-device (`EXDEV`) retirement is refused with its own actionable
  message — distinct from a name collision, not retried, legacy intact, primary
  preserved, and fixed by correcting the path layout
- a pathologically nested legacy document fails closed through the sanitized
  `LegacyBypassMigrationError` instead of escaping as a `RecursionError`
  traceback out of startup
- a concurrent start that already retired the directory is a success no-op
- a taken quarantine name rolls to the next suffix rather than overwriting an
  earlier migration
- a `*.tmp` leftover is retained, unread, in quarantine and reported by count
- **no canary value reaches any log record or exception message on any branch**,
  successful or failing — every branch is driven in one test so a leak only has
  to happen once, including the primary-wins/`already_current` branch with two
  *different* live secret values (the legacy canary and `PRIMARY_SECRET`)

`tests/test_role_preflight.py` — the real upgrade-shape regression, through the
real `preflight_role_validation` with the state root **outside** the profile
(§2a):

- `test_r1_upgrade_shape_migrates_then_role_preflight_succeeds` — a profile
  provisioned through a real `BypassSecretStore` at the pre-migration path: the
  guard refuses it before the upgrade, preflight then returns `True`, the
  legacy directory is gone, the secret resolves from the primary with 0600, one
  quarantine directory holds the retired copy, and a second start changes
  nothing
- `test_r1_upgrade_shape_with_a_malformed_legacy_secret_refuses_startup` —
  negative control: preflight returns `False`, the legacy file is byte-identical,
  no secret was written, and the log names the file without its contents

---

## 5. Focused test results

```
tests/test_r2_credential_isolation.py                     34 passed
tests/test_frontend_role_cli.py                           13 passed
both together                                              47 passed
tests/test_hermes_adapter, test_runner, test_frontend_watchdog,
tests/test_runtime, test_role_preflight,
tests/test_r1_live_publish_finishing.py                   287 passed, 3 subtests
tests/test_runner, test_git_output, test_build, test_qa  201 passed
```

All green. `test_role_preflight` exercises real `resolve_runtime_provider`
against temp `HERMES_HOME` profiles, and `test_r1_live_publish_finishing`
exercises real Git publication — both confirm the Git and model-credential paths
still function under the new boundary.

### R2-B2 results (remaining risks #2 and #5)

```
tests/test_r2_credential_isolation.py                     73 passed
tests/test_frontend_role_cli.py, test_runner,
test_hermes_adapter, test_runtime                         255 passed, 3 subtests
test_frontend_watchdog.py + test_r2_credential_isolation  127 passed  (ordering check)
tests (full suite)                                1770 passed, 16 skipped
```

### R2-B3 results (the R1 → R2 upgrade path)

```
tests/test_bypass_legacy_migration.py                     35 passed
tests/test_bypass_legacy_migration.py + test_role_preflight  67 passed
test_r2_credential_isolation, test_bypass_provisioning,
test_bypass_contract, test_bypass_leakage, test_runtime   219 passed
tests (full suite)                                1808 passed, 16 skipped
```

Same invocation as R2-B2 (`py -3 -m pytest tests -q -p no:randomly` from
`website-builder/`; `scripts/run_tests.sh` needs a venv this machine does not
have — see §6). Zero failures; 30 new tests in the R2-B3 batch plus 8 in the
hardening follow-up, and no existing test changed except `runtime_config()` in
`test_role_preflight.py`, which gained an optional `state_root` parameter. Its
**default is now a sibling of the profile** (`<tmp>/.website-builder/state`),
because the previous default (`<profile>/state`) was the very shape the
hardening refuses — the fixture now models the valid production contract instead
of the vulnerable one.

### 6c. Guard verification — mutation-checked

A guard that cannot fail is not a guard, so each new guard was disabled in turn
and the suite re-run. Every one goes red:

| Guard disabled | Result |
|---|---|
| `assert_profile_dotenv_clean` | **14 failed** — all 11 `.env` parametrised cases, the value-free-diagnostic case, the end-to-end spawn-seam case, and the parent-not-mutated case |
| `assert_profile_home_clean` | **3 failed** — the unit case, the spawn-seam case, the parent case |
| `provider=` narrowing at the adapter | **1 failed** — `test_spawn_seam_uses_the_role_provider_for_narrowing`, and the residual `WARNING` visibly fired with `NOUS_API_KEY, WEB3FORMS_ACCESS_KEY`, proving the reporting path is live |
| `migrate_legacy_bypass_secrets()` call in `preflight_role_validation` (R2-B3) | **2 failed** — both `test_r1_upgrade_shape_*` cases; the second one's log showed the R2-B2 guard refusing the un-migrated profile, which is the exact regression |
| the retirement `os.replace` inside the migration (R2-B3) | **18 failed** — every test that pins the legacy directory as gone, plus both upgrade-shape cases |
| the state-root containment guard, `_assert_state_root_outside_profile` (hardening) | **5 failed** — the equal / one-level-nested / deeply-nested cases (`DID NOT RAISE`), the actionable-message case, and the preflight-level case. With the guard gone, the nested shape migrates a secret into `HERMES_HOME/state/vercel-bypass` and the profile guard still passes |
| the `EXDEV` branch in the retirement handler (hardening) | **1 failed** — the cross-device case, which then only got the generic "could not be moved aside … (OSError). fix the filesystem" message and no longer proved a single non-retried attempt |
| `except RecursionError` in `_strict_read` (hardening) | **2 failed** — the nested-document regression and the leakage matrix, both with `RecursionError: maximum recursion depth exceeded while decoding a JSON array` escaping as a traceback, exactly the startup behaviour being removed |

All mutations were reverted (`secrets.py` verified byte-identical to its
pre-mutation hash) and the suite is green again.

### 6d. One ordering defect the canaries caught

`test_frontend_watchdog.py` and `test_r2_credential_isolation.py` were run
together and the new credential tests failed, while each file passed alone. Root
cause: Hermes' `hermes_cli.plugins.discover_plugins()` mutates
`hermes_cli.config_defaults.OPTIONAL_ENV_VARS` **in place**, adding
provider-category entries (`CLAUDE_CODE_OAUTH_TOKEN` among them) *after* this
module was imported — so a walk of the import-time `_PROVIDER_ENV` snapshot sees
a different universe depending on whether discovery already ran.

Two consequences, both fixed:

1. `credentials._current_provider_env_names()` re-derives from the live registry
   for all *accounting*, so the property under test no longer depends on session
   order. `agent_env`'s injection still uses the import-time `_PROVIDER_ENV`,
   so risk 4's documented behaviour is unchanged.
2. The defect surfaced a **real, previously invisible residual**:
   `CLAUDE_CODE_OAUTH_TOKEN` is a genuine secret that Hermes deliberately
   excludes from its own terminal scrub, and which a snapshot-based canary would
   never have seen. It is now registered as a warned exception (risk 8).

A walk of the whole live registry found no other unaccounted name (85 declared,
75 scrubbed, 10 registered exceptions, 3 of them secret-valued).

---

## 6. Full-suite results

`scripts/run_tests.sh website-builder/tests` could **not** be used as specified:
the script requires a venv (probes `.venv`, `venv`, `~/.hermes/hermes-agent/venv`)
and none exists on this machine. Equivalent invocation used instead:

```
cd website-builder && py -3 -m pytest tests -q -p no:randomly
```

| | Baseline (pre-change) | After initial R2-B1 | After R2-B1 | After R2-B2 (risks #2, #5) |
|---|---|---|---|---|
| Passed | 1693 | 1723 | 1731 | **1770** |
| Failed | 2 | 2 | 0 | **0** |
| Skipped | 16 | 16 | 16 | 16 |
| Duration | 187.59s | 185.14s | 215.66s | 194.55s |

**Zero failures.** The two pre-existing failures were investigated and are now
fixed — see section 6a. They were genuinely unrelated to credentials, so the
fix was confined to the test file; no production credential logic was altered
to accommodate them.

R2-B2 adds 39 canaries, all against real production seams, and adds no new
infrastructure. See section 6c for the mutation-check that proves the new guards
can fail, and 6d for the ordering defect the canaries caught.

### 6a. The two `test_frontend_role_cli.py` failures — root cause and fix

First proven pre-existing by stashing all R2 changes and re-running against the
pristine tree: **2 failed, 11 passed** — identical. Not caused by this batch.

Root cause: both tests mocked `app.hermes.adapter.subprocess.run` and then
called `frontend_build(...)`, which passes `supervise=True`. Since the
activity-aware watchdog landed, `supervise=True` routes through
`_run_hermes_cli_supervised` → `watchdog.supervise_frontend_run` →
`subprocess.Popen`. The mock was therefore never consulted, the *real*
`python -m hermes_cli.main` was launched, and it failed with
`ModuleNotFoundError: No module named 'hermes_cli'`. The tests had been
silently exercising a real subprocess instead of their own mock since the
watchdog change.

Fix (test-only, in `tests/test_frontend_role_cli.py`):
- patch the real seam, `app.hermes.watchdog.supervise_frontend_run`, returning a
  real `wd.SupervisedRun` for a cleanly-exited child;
- assert the argv handed to the supervisor (now positional, not
  `runner.call_args.args` of a `subprocess.run`);
- compare `cwd` as a path (the supervisor stringifies only at the `Popen` boundary).

The `load_config` scoping assertion (H-3) and every other assertion in both
tests is unchanged and still meaningful.

### 6b. A real production regression found and fixed during this work

Chasing the above surfaced a genuine defect **introduced by this batch**: the
allowlist dropped `PYTHONPATH` / `VIRTUAL_ENV`, but the agent child is spawned
as `sys.executable -m hermes_cli.main`. `hermes_cli` is importable only when the
repo root is reachable, so any deployment running Hermes from a virtualenv — or
from a working directory that is not the repo root — would have had **every
FRONTEND build fail** with `No module named 'hermes_cli'`. The pre-existing
tests had been masking this by never actually spawning the child.

Fixed by forwarding the Python interpreter-resolution variables
(`PYTHONPATH`, `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`, `PYTHONHOME`, …) in the
benign base. They are lookup paths, not credentials, and are on Hermes' own
`secret_scope._GLOBAL_ENV_EXACT` for exactly that reason.

Covered by 6 new regression tests, including an end-to-end one that spawns a
real child with the scoped environment and requires `import hermes_cli` to
succeed. Verified these tests **fail** when the fix is removed (6 failed) and
pass when restored — so they genuinely pin the behaviour rather than merely
passing.

---

## 7. Remaining risks

1. **`_run_fast_programmatic` is in-process, not a subprocess.** FAST/VISION
   inherit the *application* environment by design and are isolated by
   `enabled_toolsets=[]` (zero tools) instead. The FRONTEND-directions call
   reuses this path. This is safe today, but if a future batch gives the FAST
   path any tool, environment isolation must be revisited — a process-level
   boundary cannot help an in-process agent.
2. **Model credentials in the FRONTEND child environment — RESOLVED (R2-B2), one
   bounded residual.** The original wording ("a determined prompt-injection could
   ask FRONTEND's `terminal` tool to print them") was **wrong about the
   environment-variable path**, and that has now been measured rather than
   assumed. Hermes already strips `_HERMES_PROVIDER_ENV_BLOCKLIST` from every
   shell it spawns (`tools/environments/local.py::_sanitize_subprocess_env`),
   that blocklist is derived from `PROVIDER_REGISTRY` plus `OPTIONAL_ENV_VARS`
   tool/messaging entries, and `tools/env_passthrough.py` refuses to re-allow
   any of them (GHSA-rhgp-j443-p4rf). So `env`/`printenv` from a FRONTEND
   terminal call cannot print a model key.

   R2-B2 additionally **narrows** the child env to the resolved role's own
   provider (`agent_env(role, provider=...)`, consulting both Hermes provider
   registries so plugin providers narrow too). `openrouter` — one of the most
   commonly configured providers and a *plugin* profile, absent from
   `PROVIDER_REGISTRY` — now injects `OPENROUTER_API_KEY` and nothing else,
   which means **no secret-valued model credential survives the scrub at all**
   on that path.

   Bounded residual: an *unrecognised* provider (custom/self-hosted entry, a
   `model_aliases` alias, a partial Hermes tree) falls back to the full provider
   set, because starving a role of its model key breaks every build — a far worse
   outcome than bounded extra breadth. Three names are genuinely secret-valued
   and are not scrubbed by Hermes, and are registered so the spawn reports them
   (`SECRET_VALUED_MODEL_CREDENTIALS_NOT_SHELL_PROTECTED`):
   `NOUS_API_KEY`, `WEB3FORMS_ACCESS_KEY` (both neutralised by the narrowing
   whenever the provider resolves) and `CLAUDE_CODE_OAUTH_TOKEN` (see risk 8).
   The report is a value-free `WARNING` naming variables; it never refuses to
   spawn, so provider resolution cannot be broken by this policy.
3. **Bypass/agent tools are outside this boundary.** Hermes'
   `tools/environments/local.py` also does `os.environ.copy()` for its own
   spawned shells. Website Builder does not use that path (FRONTEND runs through
   the `hermes -z` CLI boundary), but a future adoption would need the same
   treatment at that layer.
4. **Provider allowlist is derived at import time** from Hermes' registry. If
   `OPTIONAL_ENV_VARS` is unavailable (Hermes tree missing), the fallback set is
   used — narrower, which is fail-safe, but could starve a role in a partial
   environment. *R2-B2 note:* plugin discovery mutates `OPTIONAL_ENV_VARS`
   **in place** after import, so the snapshot is narrower than the live registry
   for `agent_env`'s injection (still fail-safe — a narrower child env cannot
   leak). The R2-B2 *accounting* deliberately re-derives from the live registry
   (`_current_provider_env_names()`) so the canaries do not depend on whether
   another test already triggered discovery.
5. **The `hermes -z` child re-runs `load_hermes_dotenv()` — RESOLVED (R2-B2)
   for the profile's own `.env`; two Hermes-core channels remain accepted.** The
   previous wording called this "the intended model-credential path" and left
   the reintroduction question as an assurance. It was in fact real: `.env` is
   loaded with `override=True` and **unfiltered**, *inside* the child, so a
   privileged credential written there never passes through `agent_env` at all
   and the parent's scoping cannot catch it. R2-B2 adds
   `assert_profile_dotenv_clean`, which parses key **names** only (never values)
   and refuses — at startup preflight *and* again at the spawn seam, before
   `subprocess.run` — for any profile `.env` declaring a privileged credential
   (GitHub, Vercel, Hostinger, Strix, Telegram, WhatsApp). The error names the
   variable and never its value.

   Two further reintroduction channels exist and are **accepted, not fixed**,
   because both are Hermes-core behaviour identical for every Hermes process and
   changing them is a core edit, not a Website Builder fix:
   - **managed-scope `.env`** (`env_loader.py:605-614`) — machine-global
     (`/etc/hermes`, or `$HERMES_MANAGED_DIR`), applied **last** with
     `override=True`, so an administrator-pinned key lands in any `hermes -z`
     child. Note `HERMES_MANAGED_DIR` is not in the benign allowlist, so it
     cannot be inherited from the parent, but the `/etc/hermes` default needs no
     environment variable at all.
   - **external secret sources** (`env_loader.py:537-538`) — Bitwarden /
     1Password / plugin vaults configured in `config.yaml[secrets]`.
6. **Windows env-name case-insensitivity** is handled by case-insensitive
   matching, but a variable set with unexpected casing will be dropped rather
   than leaked — fail-safe, though it could surprise an operator.
7. **The interpreter-resolution allowlist is a maintenance surface.** A future
   batch that changes how the agent child is launched (e.g. an absolute
   `hermes` entry point instead of `python -m hermes_cli.main`) should revisit
   whether these are still needed. The end-to-end regression test in section 6b
   is what will catch it, which is why it spawns a real child rather than
   asserting on a dictionary.
8. **`CLAUDE_CODE_OAUTH_TOKEN` is classified as a provider credential
   (`password: True`) yet deliberately excluded from Hermes' terminal scrub.**
   `tools/environments/local.py:425-434` discards it because stripping it broke
   agent-spawned `claude` CLIs — the token belongs to the user's own Claude Code
   install, not to Hermes (#55878). It is therefore scrubbed by nothing, by
   upstream choice, and it only enters the registry once plugin discovery mutates
   `OPTIONAL_ENV_VARS`. R2-B2 registers it as a known, **warned** exception so a
   canary surfaces it if it ever reaches a generation role. Closing it properly
   is a Hermes-core decision, out of this batch's scope.
9. **A privileged secret stored on disk under `$HERMES_HOME` was reachable by a
   generation role — RESOLVED (R2-B2).** `HERMES_HOME` is the one directory the
   generation plane is explicitly pointed at: the child is launched with it, and
   the profile path is written verbatim into the `website-builder-environment`
   skill, so a model reading that skill knows exactly where to look. The terminal
   tool reads any absolute path and runs under `HERMES_YOLO_MODE=1`, which
   removes the human approval gate; file mode `0600` does not help, because the
   child is the same OS user. The live instance was **self-inflicted, not
   operator error**: `BypassSecretStore(config.hermes_home / "vercel-bypass")`
   stored the per-project Vercel automation-bypass secret there automatically, for
   every project with a preview. R2-B2 moves the store to the application's own
   `state_root` (never placed in any child environment), keeps the old path as a
   read-only legacy root so no provisioned secret is silently lost, and adds
   `assert_profile_home_clean` so a second secret store under a profile home
   cannot be added without tripping a fail-closed guard.
   *R2-B3:* those two changes together made an un-migrated R1 install unbootable
   — the guard runs before anything could move the file — so startup now runs
   `migrate_legacy_bypass_secrets` first (§2a). It MOVES the store, never
   deletes it: the retired directory is renamed to
   `<state_root>/vercel-bypass-migrated-<UTC timestamp>` and **can be deleted by
   the operator** once the migration is confirmed. Anything it cannot verify
   fails closed with the legacy directory untouched and the start refused, so
   re-running is always the recovery and no repair command is needed. The
   quarantine keeps a second copy of bypass material under the state root; it is
   outside `HERMES_HOME`, and the same-user readability limitation is risk 10
   below. **Operator cleanup:** delete
   `~/.website-builder/state/vercel-bypass-migrated-*` once
   `~/.hermes-website/vercel-bypass` is gone and preflight reports the
   migration. **Operator prerequisite:** `WEBSITE_BUILDER_STATE_ROOT` (or
   `state_root` in `config.yaml`) must point outside the profile home and, for
   the one-time retirement rename, on the same filesystem as it — the migration
   now refuses both shapes by name instead of moving a secret somewhere the
   guard cannot see.
   *Residual:* relocating a file reduces its reachability; it does not create an
   OS boundary. A determined agent that guesses an absolute path can still read
   any file the operator's account can read — see risk 10.

10. **UNRESOLVED — same-user execution isolation. This is a prerequisite, not a
    solved credential-isolation property.** The environment contract in this
    document is complete for what crosses a *process* boundary. It cannot bound
    what a same-OS-user agent reads, and this batch deliberately does not attempt
    to (no container, no separate user, no broker — all out of scope, and none is
    architectural complexity this codebase should take on unilaterally).

    On Linux, a shell inside the FRONTEND child can read its **parent's** raw
    environment via `/proc/$PPID/environ`. That parent is the Website Builder
    application process, whose environment holds `VERCEL_TOKEN`,
    `TELEGRAM_BOT_TOKEN` and `WEBSITE_BUILDER_GITHUB_SSH_KEY`. So on a Linux
    host, every deploy credential is obtainable from a FRONTEND terminal call
    **regardless of how correctly the child environment is scoped**. The only
    remedies are clearing the application environment — which violates
    requirement 7 and would break the Git/Vercel adapters that legitimately need
    those values — or a genuine privilege boundary.

    **This must be treated as a blocking prerequisite** before either of:
    - **enabling the privileged Hostinger/Strix adapters.** The `hostinger` and
      `strix` seams are registered and isolated *in name only*; no adapter is
      implemented. Implementing one against the current execution model would
      add a credential that a same-user agent can reach by reading
      `/proc/$PPID/environ` on any Linux host. Do not enable them until an
      execution-isolation boundary exists.
    - **exposing Website Builder to untrusted or public users.** Today the
      threat model is a trusted operator on a single-tenant VPS: prompt injection
      is a defence-in-depth concern, not an active adversary. Under an untrusted
      or public user, `/proc` visibility (Linux) plus same-user file access make
      the environment contract in §3 necessary but **not sufficient**, and no
      amount of additional environment scoping changes that.

    The credential-canary suite proves the environment and profile-file
    boundaries. It does **not** test this, because it is not a credential-flow
    property — it is an OS-level one, and it is unproven by construction.
