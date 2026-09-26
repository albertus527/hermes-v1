# p12 — Normalize Google Fonts referenced through CSS `@import`

## Root cause (confirmed against real code, not inferred)

The p8/p12-shaped generated site references Google Fonts **only from `src/index.css`**
(`D:\Code\Hermes\_scratch_p8\src\index.css:9`):

```css
@import url('https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600;700&family=Nunito+Sans:wght@400;600;700;800&display=swap');
```

Its `index.html` has **no** fonts `<link>` (only favicon/meta/script). Therefore:

- The normalizer never fires. `normalize_artifact` only dispatches `.html`/`.htm`
  targets (`app/core/selfcontained.py:1033-1057`) and its only rewriter is
  `_normalize_html_font_links`, which is wired exclusively to `<link>` tags
  (`selfcontained.py:1089-1134`).
- The validator *does* fire. `_scan_css` (`selfcontained.py:445-460`) matches
  `_CSS_IMPORT_RE` **and** `_CSS_URL_RE` against the same statement, so one
  `@import url(...)` yields exactly two findings — `external_css_import` and
  `external_css_url`, `host=fonts.googleapis.com`, `file=src/index.css`. That is
  byte-for-byte the p12 evidence. The scanned file name `src/index.css` comes
  from `_source_scan_files` (`selfcontained.py:526-550`), confirming the gate ran
  after `npm run build` with `dist/` present.
- Families/weights survive normalization by *name*: the site consumes them via
  `@theme { --font-sans: 'Nunito Sans', ... }`, and the existing
  `_font_face_css` emits `font-family: "<name>"` per face. So the chosen
  typography is preserved without any system-font substitution.

**Conclusion: this is a carrier gap in Layer 1 (normalization), not a validation
defect.** Fix by extending the existing allowlisted Google Fonts fetch/vendor
path to CSS text. `check_self_contained` stays exactly as-is.

## Decisions (resolved)

1. **Scope: `.css` files *and* inline `<style>` blocks.** One pure CSS-text
   rewriter serves all carriers, including the HTML `<style>` body the validator
   already scans (`_scan_html` → `_style_body_ranges`, `selfcontained.py:292-294`).
2. **Compile-repair issue: `executed: false` is correct; the dropped reason is
   the defect.** Persist a bounded, sanitized repair reason (see Task 4).
3. No per-URL vendor cache, no prompt/instruction changes, no budget changes, no
   fetch-bound relaxations, no new subsystem.

---

## Task 1 — `website-builder/app/core/selfcontained.py`: one CSS-text rewriter

**1a. Normalization-specific `@import` regex.** `_CSS_IMPORT_RE`
(`selfcontained.py:242-246`) matches the statement *without* its trailing `;`.
Replacing only the match would emit `@font-face {…};` — a dangling semicolon after
a block, which can make the CSS parser drop the remainder of the sheet. Add a
module-level `_CSS_IMPORT_NORM_RE` = same alternation, IGNORECASE, plus
`[ \t]*;?` so the terminator is consumed. The detector regex is untouched.

**1b. `_normalize_css_font_imports(text, rel_name, *, vendored, resolver, connection_factory) -> (str, dict)`** —
sibling of `_normalize_html_font_links`, same counter keys
(`stylesheets`, `unsupported_stylesheets`, `vendor_failures`, `families`,
`assets`, `stylesheets_list`, `removed_list`) so `NormalizationResult`,
`to_dict()` and the `normalize_self_contained` log line are unchanged. Per match:

- non-external URL → leave untouched;
- external but not an allowlisted Google Fonts CSS2 URL
  (`_validate_google_fonts_url` raises) → leave the original text, count
  `unsupported_stylesheets` (same policy as the HTML path, `selfcontained.py:1090-1093`);
- else `_vendor_google_fonts(url, …)`; for each `(face, asset_name)` append
  `_font_face_css(face, "/" + asset_name)` (root-absolute, exactly as the HTML
  path at `selfcontained.py:1123`) and `assets[asset_name] = payload`; record
  families through `_extract_families`;
- on `FontVendorError` → count `vendor_failures` + `unsupported_stylesheets`, log a
  warning, and **keep the original external statement** so preflight still fails
  explicitly (identical to `selfcontained.py:1111-1120`). Never silently drop.

Replacement is the generated `@font-face` blocks **in place of the `@import`**
(in-place, so cascade order is preserved, output is deterministic, and a second
pass has nothing left to match).

**1c. `_normalize_html_style_blocks(text, rel_name, …)`** — reuse
`_style_body_ranges` (`selfcontained.py:409-434`); rewrite each body with 1b and
splice **in reverse range order** so earlier offsets stay valid. Do not
hand-roll a `<style>…</style>` regex; the existing bounded range helper is the
proven shape in this module.

**1d. `normalize_artifact` target set** (`selfcontained.py:988-1060`):

- keep every existing HTML carrier and its `rel_name` semantics unchanged
  (deployed `dist/**` HTML, root `index.html`, `public/**.html` as
  public-relative) — existing tests assert those names;
- **add** CSS carriers: every `.css` in `_deployed_files(workspace)` (the built
  bundle, e.g. `assets/index-<hash>.css`) and every `.css` from
  `_source_scan_files(workspace)` (`src/**`, `public/**`; already skips
  `node_modules`/`dist`/etc.), workspace-relative names, deduped by resolved path;
- keep the existing `"fonts.googleapis.com" not in text` fast path for every
  file (this is also what makes pass 2 free);
- dispatch `html` → `_normalize_html_font_links` + `_normalize_html_style_blocks`;
  `css` → `_normalize_css_font_imports`; write only when `updated != text`.

**1e. Unchanged (assert this in review):** `check_self_contained` and every
scanner/detector regex; `_fetch` and the whole safety policy (DNS pinning,
no redirect, HTTPS-only, allowlisted hosts, content-type/magic/size limits,
`_MAX_FONTS_PER_FAMILY`, timeouts); `KIND_*` values; `sync_vendored_assets`
(`selfcontained.py:1265-1298` already writes `public/<name>` **and**
`dist/<name>`, which is exactly what the emitted `/fonts/...` URLs need);
`check_self_contained` strength. Only the module docstring changes — one
sentence naming CSS `@import` (file and inline `<style>`) as supported carriers.

**Why `/fonts/…` is safe for a later `npm run build`:** `vite.config.ts` sets no
`base` (default `/`), and Vite 8.2.0's CSS `urlResolver` resolves a root-absolute
`url()` through `checkPublicFile` (`vite/dist/node/chunks/node.js:22505-22523`),
so a `/fonts/x.woff2` that exists in `public/` stays same-origin in the rebuilt
bundle. Phase 8 QA repair re-runs `npm run build` and then re-runs the gate
(`app/qa/orchestrator.py:499-529`), so the source rewrite is what keeps that
path green.

## Task 2 — Source/dist consistency (covered by Task 1, verified by Task 3)

`src/index.css` normalized **and** the current `dist` bundle normalized **and**
`public/fonts/*` written = a rebuild cannot reintroduce the dependency. No
separate code step; the assertions live in the tests below.

## Task 3 — Tests: `website-builder/tests/test_self_contained.py`

Add a p8-shaped workspace builder beside `_make_p7_workspace`
(`test_self_contained.py:154`): `src/index.css` with the exact real single-quoted
`@import url('…Rubik…Nunito+Sans…')` + `@theme { --font-sans: 'Nunito Sans', … }`,
`dist/assets/index-<hash>.css` carrying the same import, `dist/index.html` with no
fonts link, `index.html` at root. Extend the fixture (`_fixture()`,
`test_self_contained.py:129`) with Rubik/Nunito Sans `@font-face` blocks and
`woff2` payloads so no real network is involved. Required cases:

1. Pre-normalization `check_self_contained` fails with `external_css_import` +
   `external_css_url` for **both** `src/index.css` and the dist bundle.
2. `@import url("https://fonts.googleapis.com/css2?…")` (double-quoted `url()`
   form) normalizes.
3. `@import "https://fonts.googleapis.com/css2?…"` (bare string form) normalizes.
4. Source CSS **and** generated dist both end up free of `fonts.googleapis.com` /
   `fonts.gstatic.com`, contain `@font-face` with `font-family: "Rubik"` and
   `"Nunito Sans"` at the requested weights, reference `/fonts/…` local assets,
   and `public/fonts/*` + `dist/fonts/*` exist on disk;
   `normalize_and_check_self_contained` returns `ok`.
5. Rebuild-cannot-reintroduce: normalize a source-only workspace, then materialize
   a dist bundle from the *normalized* source text and re-run the gate → `ok`.
6. Idempotent second pass: `changed is False`, `assets == {}`, fingerprint
   unchanged (mirror `test_repeated_normalization_is_idempotent`,
   `test_self_contained.py:705`).
7. Existing HTML `<link>` normalization still works **and composes** with the CSS
   import in one workspace (`TestGoogleFontsNormalization`, `…:198`).
8. Unsupported external CSS still fails and is left byte-identical:
   `@import url("https://cdn.example.com/theme.css")`,
   `@import url("https://fonts.example.com/css2?family=X")` →
   `EXTERNAL_RUNTIME_DEPENDENCY` with findings.
9. Vendor/network failure keeps the existing classification: patch
   `selfcontained._fetch` to raise `FontVendorError("FONT_VENDOR_TIMEOUT")` →
   the import survives verbatim and the report is `EXTERNAL_RUNTIME_DEPENDENCY`
   (artifact); and the infrastructure path still yields
   `PREVIEW_NORMALIZATION_INFRASTRUCTURE` (existing test at
   `test_self_contained.py:895` plus one for the CSS carrier).
10. Inline `<style>` Google Fonts `@import` in an HTML file normalizes (chosen
    scope), while a `<style>` block with no fonts import is untouched.
11. Regression guards: `@import 'tailwindcss';` is preserved byte-for-byte in the
    rewritten file; no `};` sequence is introduced (the dangling-terminator
    hazard from 1a); `test_local_only_site_is_a_strict_noop` (`…:731`) still
    passes.

`tests/test_build_self_contained.py` needs no change — it patches
`normalize_artifact`, which keeps its signature.

## Task 4 — `website-builder/app/projects/build.py`: persist the compile-repair reason

Behavior-neutral. `executed: false` for p12 was **correct**: `frontend_build()`
returned `success=False`, and `build()` correctly preserved the initial failing
checks, set `final_stage="repair_execution_failed"` and consumed the single
attempt (`build.py:855-865`). Two facts narrow the cause without guessing:

- 12s rules out the watchdog (idle 180s, hard fuse 45min) — `run.outcome` was
  `None` (`adapter.py:621`);
- on the repair path the workspace artifacts are *already* complete, so
  `if result.timed_out and self._has_complete_frontend_artifacts(workspace)`
  (`adapter.py:1238`) would have converted any timeout into success — so it was
  not a timeout either.

That leaves exactly two mechanisms, indistinguishable today: (i) child exit
non-zero → `HermesResult.error` = stderr, or (ii) exit 0 with a final response
JSON containing an `"error"` key → `_parse_frontend_response`
(`adapter.py:1741-1769`) returns `success=False`. Both are *expected repair
rejection* semantics, not faulty plumbing. The genuine defect is that the reason
is thrown away, so the answer is unrecoverable from persisted state.

- `_attempt_compile_repair` (`build.py:482-570`): add `error` (bounded via the
  module's existing `_bounded_output`), `error_code`, and `invocation` to each
  returned dict — sourced from `repair_result` on the `not success` branch, and
  the bounded code only on the `validate_composed_dna` ValueError branch.
- `build()` (`build.py:855-891`): when `final_stage == "repair_execution_failed"`,
  persist `failure["repair"] = {"error", "error_code", "invocation"}`, mirroring
  the initial-build path (`build.py:703-704`). `invocation` is the bounded
  `frontend_forensics/1` receipt — counters, timestamps, normalized descriptions,
  hashes only; never prompts, source, model output, tool args, or URLs.
- Unchanged: `executed` semantics, `MAX_COMPILE_REPAIR_ATTEMPTS`, the user-facing
  `CHEAP_CHECKS_FAILED:<first failing check>`, and the QA-repair / revise paths
  (out of scope — this closes the gap for the Phase-7 compile-repair path only).

**Operator recipe for p12 (on the host that ran it):** read
`~/.hermes-website/diagnostics/<project_id>/<invocation_id>.json` for the second
invocation (the repair). `returncode` + `counters.model_started` discriminate the
two mechanisms: `model_started == 0` → the child died before any model call
(startup/role/provider/credential); `model_started >= 1` with
`tool_started == 0` → the model answered with an error JSON without touching
files. Cross-check `~/.hermes-website/logs/errors.log` for the child's stderr.
Note `MAX_RECEIPTS_PER_PROJECT = 20` (`app/hermes/watchdog.py:167`) prunes older
receipts.

## Task 5 — Tests for Task 4: `website-builder/tests/test_build.py`

- Extend `test_repair_execution_failure_fails_closed_with_initial_error`
  (`test_build.py:1704`): assert `state.failure["repair"]` carries the repair's
  `error`/`error_code`, and that `final_stage`, `compile_repair_attempts == 1`,
  `CHEAP_CHECKS_FAILED:npm_build` and the single-`frontend_build`-call behavior
  are unchanged.
- New case: a repair returning `error_code` + `invocation` → both persisted and
  bounded (no prompt/stdout leakage in the serialized failure).

## Validation

```bash
# focused (runner is file-granular)
bash scripts/run_tests.sh website-builder/tests/test_self_contained.py
bash scripts/run_tests.sh website-builder/tests/test_build_self_contained.py
bash scripts/run_tests.sh website-builder/tests/test_build.py
bash scripts/run_tests.sh website-builder/tests/test_frontend_watchdog.py

# full Website Builder suite
bash scripts/run_tests.sh website-builder/tests
```

No real network, no real Vercel, no real Telegram: font payloads come from the
existing `fixture_payloads` seam. No `npm run build` in the suite (the repo uses
stub runners for cheap checks); the Vite `url()` behavior is verified by reading
Vite 8.2.0's `urlResolver` (Task 1 rationale) rather than by a live build.

Do not commit, push, or deploy.

## Risks

| Risk | Mitigation |
|---|---|
| Dangling `;` after the in-place rewrite silently truncating the sheet | 1a consumes the terminator; test 11 asserts no `};` and an intact sheet tail |
| Touching a non-Google `@import` (Tailwind's `@import 'tailwindcss'`) | only allowlisted Google Fonts CSS2 URLs are replaced; explicit regression test |
| Silently shipping a font-less artifact when vendoring fails | the original statement is preserved verbatim (same policy as the HTML path), so preflight still fails explicitly |
| Vite rebasing `/fonts/...` to a hashed `assets/` URL on rebuild | either outcome is same-origin; the gate only forbids *external* references |
| Extra font variants per family | `_MAX_FONTS_PER_FAMILY` and every fetch bound are unchanged |

## Out of scope

Repair-instruction prompt text, FRONTEND tools/capability, QA-repair and revise
persistence of invocation diagnostics, any new vendor subsystem or cache, and any
relaxation of `check_self_contained`.
