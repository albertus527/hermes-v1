# Website Builder — Final Local Handoff (Phases 9–16)

This is the single authoritative, final report for the local
implementation pass covering Phases 9–16 of the Website Builder. It
supersedes all prior narrative in this file. Every claim below was
verified in this session; nothing here is inferred or copy-forwarded
from earlier passes without re-checking.

**Nothing in this codebase has been validated against live external
services.** All tests run against local fakes/mocks/stubs. No live
Vercel, Telegram, Web3Forms, DNS, browser, or model-provider API calls
have ever been made from this test suite.

No git commit or push was performed. The working tree itself is the
deliverable (see "How to apply" below).

---

## 1. Phase status summary

| Phase | Area                                                                                                        | Status                   |
| ----- | ----------------------------------------------------------------------------------------------------------- | ------------------------ |
| 9     | Preview (sandboxed dev server + adapters)                                                                   | Implemented, unit-tested |
| 10    | Revisions (`app/projects/revise.py`)                                                                        | Implemented, unit-tested |
| 11    | Promotion (`app/projects/promote.py`)                                                                       | Implemented, unit-tested |
| 12    | Authorization + design references (`app/core/authz.py`, `app/core/references.py`, `app/core/design_dna.py`) | Implemented, unit-tested |
| 13    | Lightweight directions (`app/projects/directions.py`)                                                       | Implemented, unit-tested |
| 14    | Contact form (`app/core/contact_form.py`)                                                                   | Implemented, unit-tested |
| 15    | (explicitly skipped per prior scope decision — not implemented in this or prior passes)                     | Skipped                  |
| 16    | Custom domain (`app/projects/domain.py`, `app/deploy/adapters.py` Vercel domain APIs)                       | Implemented, unit-tested |

All of the above are **contract-level implementations validated against
local fakes** (fake Vercel API responses, fake Telegram dispatch,
mocked subprocess/git/npm calls). None of them have exercised a real
external endpoint. Treat "implemented" as "code + unit-test contract
complete," not "field-proven."

---

## 2. Test suite results (this pass)

### Canonical command

```
"D:\Code\Git\bin\bash.exe" scripts/run_tests.sh website-builder/tests -j 1
```

### Result

```
24 files, 557 tests passed, 0 failed, 16 skipped (100% complete) in 27.8s (1 workers)
```

Zero failures. Zero regressions found or needed fixing — the suite was
already green at the start of this pass. No skips were investigated
further; skips are environment-gated (e.g. `_HAS_PIL`, Windows-symlink
skip in `test_snapshot.py`) and not indicative of missing coverage on
this host.

This was the **only** test command run in this pass, as scoped. The
full repo-root test suite was intentionally **not** run (pre-existing
unrelated venv gaps: `rich`, `concurrent_log_handler` — irrelevant to
website-builder, per task scope).

---

## 3. Frontend starter build/typecheck (this pass)

Location: `templates/frontend-starter/` (repo root, referenced by
`app/projects/build.py::_STARTER_PATH`; this is intentionally outside
`website-builder/` — it is the fixed canonical starter every generated
project copies).

Environment: Node **v26.5.0**, npm 11.17.0 (matches the starter's
declared `engines.node: ">=26 <27"`).

| Step      | Command                                  | Result                                                                                        |
| --------- | ---------------------------------------- | --------------------------------------------------------------------------------------------- |
| Install   | `npm ci`                                 | **PASS** — 40 packages installed, 0 vulnerabilities                                           |
| Build     | `npm run build` (`tsc -b && vite build`) | **PASS** — built in 153ms, emitted `dist/index.html`, `dist/assets/*.css`, `dist/assets/*.js` |
| Typecheck | `npm run typecheck` (`tsc -b`)           | **PASS** — no type errors, no output (clean)                                                  |

No blockers found. This is a real, honest PASS — not asserted without
running it.

---

## 4. Secret scan (this pass)

Scanned `website-builder/` (excluding `.git`) for patterns resembling
committed credentials: `VERCEL_TOKEN=`, `TELEGRAM_BOT_TOKEN=`,
`WEB3FORMS_ACCESS_KEY=`, `sk-...`, `ghp_...`, `xox[baprs]-...`,
`AKIA...`, PEM private-key headers.

**Result: no real leaked credentials found.** All matches are inside
`tests/test_runner.py` and `tests/test_revise.py` and use obvious fake
placeholder values for testing env-var isolation/redaction behavior
(e.g. `"secret-token-123"`, `"vercel-789"`, `"or-key-456"`,
`"9router-key-000"`, and a `{{WEB3FORMS_ACCESS_KEY}}` template
placeholder asserted to be absent from rendered output). No `.env`
files, no real-looking API key strings, no private key material is
committed anywhere under `website-builder/`. No fix was needed.

---

## 5. Scope check (this pass)

`git status --porcelain -- . ":!website-builder"` (repo root, excluding
website-builder/) returned **empty** — confirmed zero changes outside
`website-builder/`. All modified (10 files) and untracked (29 files/
dirs, plus this doc) paths are under `website-builder/`. No revert was
necessary.

Modified files (pre-existing, edited this pass or earlier in the
Phases 9–16 effort):
`app/core/intake.py`, `app/core/state.py`, `app/hermes/adapter.py`,
`app/projects/build.py`, `app/qa/orchestrator.py`,
`app/sandbox/runner.py`, `tests/test_build.py`,
`tests/test_hermes_adapter.py`, `tests/test_intake.py`,
`tests/test_qa.py`.

New files: the full set of Phase 9–16 modules under `app/channels/`,
`app/core/` (authz, composition, contact_form, design_dna, references),
`app/deploy/` (adapters, git_output, preview, snapshot),
`app/projects/` (directions, domain, promote, references, revise), and
their corresponding test files under `tests/`.

---

## 6. Patch artifact

A patch capturing every change under `website-builder/` (10 modified +
29 new files, 8050 insertions / 43 deletions across 42 files) was
generated and written to:

```
website-builder/PHASE9-16.patch
```

Generated via:

```
git add -A -- website-builder
git diff --cached -- website-builder > website-builder/PHASE9-16.patch
git reset -- website-builder   # unstaged again immediately — no commit made
```

**The working tree itself is also directly usable** — every file the
patch describes is present, uncommitted, in this checkout right now.
Either apply the `.patch` file on the VPS, or `rsync`/copy the
`website-builder/` directory wholesale. Pick whichever is more
convenient; both represent the identical end state.

### To apply on the VPS (two equivalent options)

**Option A — apply the patch:**

```bash
cd /path/to/repo/on/vps
git apply website-builder/PHASE9-16.patch
# or, if you copied the .patch file separately:
git apply /path/to/PHASE9-16.patch
```

**Option B — copy the directory directly** (simplest if you have
direct filesystem/rsync access to this workspace):

```bash
rsync -av --exclude='.git' website-builder/ user@vps:/path/to/repo/website-builder/
```

Neither option includes a commit — you will need to `git add` /
`git commit` on the VPS yourself once you've reviewed the applied
changes, per your own workflow.

---

## 7. What remains — external live validation

Nothing in this pass touched live infrastructure. Before Phases 9–16
can be considered field-proven (not just unit-tested), the following
external validations require real credentials that were not available
in this sandbox and were never used:

- **Vercel**: real `VERCEL_TOKEN` + real project to validate
  `app/deploy/adapters.py::VercelAdapter` end-to-end (deploy, domain
  add/verify, CNAME recommendation) against the live API, not fakes.
- **Telegram**: real bot token to validate `app/channels/dispatch.py`
  end-to-end message round-trips (send/receive, dispatch commands).
- **Web3Forms**: real `WEB3FORMS_ACCESS_KEY` to validate
  `app/core/contact_form.py` actually delivers a submitted form to a
  real inbox.
- **DNS**: a real domain + real DNS provider access to validate the
  full `domain_prepare` → manual CNAME/A-record setup → `domain_verify`
  → `domain_connect` flow against real propagation, not the smoke-test
  fakes in `app/deploy/adapters.py`'s fake mode.
- **Browser/screenshot QA** (`app/qa/`): validated only against fixed
  local fixtures/mocked screenshot capture — never run against a real
  headless browser driving a real generated site.
- **Model-provider calls** (`app/hermes/adapter.py` FRONTEND role):
  validated only via mocked `subprocess.run` — never actually invoked
  the real `hermes` CLI against a real provider/model.

None of the above can be validated from this sandbox; they require the
user's own credentials and a real deployment target (the VPS).
