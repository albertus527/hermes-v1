# Website Builder R1 — canonical LIVE URL, GitHub LIVE-source publication history, approve = approve + publish

## Scope

Three R1 finishing changes only:

- **A** Canonical public LIVE URL, separated from the deployment-specific Vercel hostname.
- **B** Publish the exact LIVE source to a friendly `<project-slug>` branch in `albertus527/website`, as a **linear, human-readable publication history** (write-only for source; no hydration).
- **C** An approval intent immediately publishes the exact approved preview (no second confirmation).

Explicitly **out of scope / unchanged**: FRONTEND capability, QA, preview screenshot flow, Vercel
promotion identity, rollback semantics, project isolation, reviewer role, and any GitHub
read-back of source content / revision hydration (deferred to **R2**).

Canonical source files: `website-builder/app/deploy/adapters.py`, `app/projects/promote.py`,
`app/deploy/git_output.py`, `app/channels/dispatch.py`, `app/runtime.py`, `app/core/state.py`,
`config/default.yaml`.

---

## 1. Decisions (settled with the user)

| # | Decision |
|---|---|
| 1 | GitHub auth = **SSH deploy key** on the VPS, remote `git@github.com:albertus527/website.git`. No token in app, config, argv, or state. |
| 2 | `canonical_production_url` = **verified read + deterministic fallback**. New read-only adapter method; if the domain read is ambiguous/unavailable, fall back to `https://<verified expected_name>.vercel.app/`. |
| 3 | A verified **custom domain is never preferred** — canonical is always the project's own `<expected_name>.vercel.app` default domain. |
| 4 | Approve→publish = **one dispatch claim**: the `approve` action approves then promotes. |
| 5 | A duplicate approval of an already-LIVE exact preview **re-sends the current canonical LIVE URL** (no second promotion, no state change). |
| 6 | Branch name = the already-bound canonical project slug (`expected_name`); for a legacy hash-named project that is `wb-<sha256[:40]>`, so branch and canonical host always agree. |
| 7 | The friendly branch is **append-only publication history**. Each LIVE publication adds its own commit whose **tree is the exact tested snapshot tree**; the branch is advanced by a **plain fast-forward push**. `--force` is **never** the normal publication path. |
| 8 | Post-push crash recovery: on a fast-forward rejection only, one `git ls-remote` of the branch ref (a 40-char SHA, no content) re-parents and re-pushes once. |

---

## 2. The publication-chain invariant (change B core)

Two different commit objects, related by **tree equality**, never by identity:

```
tested_commit        immutable, authoritative internally
                     (OutputGitRepository.commit -> refs/heads/preview/<sha>/<sha>)

publication_commit   the commit the friendly branch points at
                     tree(publication_commit) == tree(tested_commit)
                     parent(publication_commit) == previous SYNCED publication_commit
                     parent(publication_commit) == null on first publication
```

- `publication_commit != tested_commit` (different parent/message ⇒ different SHA).
- The tree is **taken from** the tested commit (`rev-parse {tested_commit}^{tree}`) and reused as-is.
  Files are never rebuilt from the workspace, `git add .` is never run, and the workspace is never
  read during publication.
- The publication **parent is the persisted `state.repository.publication_commit` of the last
  SYNCED publication** — not the local git ref and not a re-read of the remote. This keeps the
  branch honest: a revision whose push failed never enters the friendly branch's ancestry.
- `git commit-tree` is already deterministic in this repo (author/committer name, email and dates
  are pinned in `OutputGitRepository._run`), so a retry after a failure recreates the byte-identical
  publication commit and the same SHA.

Consequence: `refs/heads/<slug>` grows one commit per successful LIVE revision and `git log` on the
branch reads as the project's published history. Revision 1 remains in revision 2's ancestry.

---

## 3. Task list

### T1 — `VercelAdapter.canonical_production_url` (new, read-only)

`app/deploy/adapters.py`. Signature:
`canonical_production_url(self, app_id, project, *, expected_name=None) -> OperationResult`

1. `_project_valid(project, app_id, expected_name=expected_name)` — otherwise `PROJECT_IDENTITY_MISMATCH`.
2. `name = expected_name or self.project_name_for(app_id)`; if `name` is not a valid Vercel project
   label (`^[a-z0-9][a-z0-9-]{0,99}$`) → fail `CANONICAL_URL_UNRESOLVED`.
3. `GET /v9/projects/{quote(name)}/domains` (read-only).
   - **Proven hit**: a record whose `name` is exactly `name + '.vercel.app'` and `verified is True`
     → `ok({'canonical_production_url': f'https://{name}.vercel.app/', 'canonical_source': 'VERCEL_PROJECT_DOMAIN'})`.
   - **Read fine but no such verified default domain**, or **read failed/malformed/non-list** → fall through.
4. **Fallback**: `ok({'canonical_production_url': f'https://{name}.vercel.app/',
   'canonical_source': 'VERIFIED_PROJECT_NAME'})`; INFO-log when the fallback is used. `expected_name`
   is already verified against the owned project by `lookup_project`, so the fallback is gated on
   that proof, not on the URL.
5. Never derive the canonical URL by stripping/mangling a deployment hostname.

Never consults a custom domain (decision 3); never fails closed on a merely ambiguous domain read
(decision 2). `CANONICAL_URL_UNRESOLVED` is reachable only when no valid project name exists at all.

### T2 — Real deployment URL on the reconcile path

`reconcile_production_deployment` returns no URL today, which is why `promote.py` fabricates
`https://<deployment_id>.vercel.app` via `_production_url_for`. Add to its `PROMOTED` result:
`'deployment_url': 'https://' + body['url']`, validated with `_safe_origin` (return
`PROMOTE_RECONCILIATION_REQUIRED` if it fails). `reconcile_external_promotion` and
`promote_deployment` already return a real `production_url` — read it as the deployment URL.

Keep `_production_url_for` **only** as a last-resort value for the internal
`promotion_intent.deployment_url` diagnostics field when the adapter gave no real URL. It must
never reach smoke, `state.production_url`, `last_live_deployment.production_url`, or Telegram.

### T3 — `_post_promote` ordering (A + B)

`app/projects/promote.py`; signature gains `deployment_url` alongside `production_url`.

1. Resolve canonical: `self._resolve_canonical(app_id, vercel_project, expected_name)`.
   - Failure → `OperationResult.fail("CANONICAL_PRODUCTION_URL_UNRESOLVED")`, **before** any smoke,
     leaving `PUBLISHING` + the intact `promotion_intent` so `resume_publish` re-enters the same
     operation. **No rollback** (the remote promotion is confirmed good; only our local URL
     resolution failed) and **no LIVE transition**.
2. Production smoke against the **canonical** URL (unchanged bypass wiring; `*.vercel.app` is still
   in the bypass scope).
3. Smoke failure → existing `_rollback_and_fail` path, unchanged.
4. LIVE transition; persist:
   - `state.production_url = canonical`
   - `state.deployment["last_live_deployment"]` = existing keys, with
     `"production_url": canonical` **and** `"deployment_url": deployment_url`.
   - `promotion_intent`: `production_url` = canonical, `deployment_url` = deployment URL,
     `stage = "live"`.
5. **GitHub publication** (T5) — best-effort, after LIVE, before Telegram. Never rolls anything back.
6. Telegram `f"🚀 Live: {canonical}"`.
7. Return `ok({"production_url": canonical, "deployment_url": ..., "source_sync": <status>, ...})`.

### T4 — `state.repository` metadata (B)

`app/core/state.py` already has an unused `repository: Dict[str, Any]`. Populate in T3.5:

```python
state.repository = {
    "provider": "github",
    "repo": "albertus527/website",      # owner/name only, no scheme, no key path
    "branch": "<expected_name>",
    "tested_commit": "<immutable TestedSnapshot commit sha>",
    "publication_commit": "<friendly-branch head commit sha>",
    "source_revision": <live revision>,
    "sync_status": "SYNCED" | "SOURCE_SYNC_REQUIRED",
    "synced_at": <ts>,                  # success only
    "last_error_code": <code>,          # failure only
}
```

`publication_commit` is the **parent authority** for the next publication. No URL, no key path,
no credentials.

### T5 — `OutputGitRepository.publish_project_branch` (new; B)

`app/deploy/git_output.py`. **Separate from `push_github()`**, which keeps its immutable
`preview/<sha>/<sha>` contract and its `https://` opt-in regex unchanged.

```python
def publish_project_branch(self, tested_commit, branch, url, *,
                           previous_publication_commit=None, ssh_key=None):
    -> dict(branch=, repo=, tested_commit=, publication_commit=, tree=,
            parent=, reparented=bool)
```

1. Validate `url` against `^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$` → else `ValueError`.
2. Validate `branch`: `^[a-z0-9][a-z0-9._-]{0,99}$`, and **reject** any branch starting with
   `preview/` (the internal ref must never be exposed as the friendly branch).
3. Validate `tested_commit` is 40-hex, `cat-file -e {tested_commit}^{commit}` succeeds, and
   `for-each-ref` proves some `refs/heads/preview/<sha>/<sha>` points at it — i.e. the commit we
   publish is provably an immutable `TestedSnapshot` commit, never anything else.
4. `tree = rev-parse {tested_commit}^{tree}`. **This exact tree object is reused verbatim.**
5. `parent = previous_publication_commit`; when set it must be 40-hex and
   `cat-file -e {parent}^{commit}` must succeed — a parent we do not hold is a hard error
   (raise; never guess, never fetch).
6. Build the publication commit:
   `commit-tree <tree> [-p <parent>] -m <deterministic message>`, where the message is bounded,
   secret-free and human-readable: project slug, live revision, full `tested_commit`,
   `source_sha256`, `artifact_sha256`. (Fixed author/committer + fixed dates ⇒ deterministic SHA.)
7. Assert the invariant cheaply: `rev-parse {publication}^{tree} == tree`, else raise.
8. **Fast-forward push** (the normal path, zero reads):
   `push --porcelain -- <url> {publication}:refs/heads/{branch}`.
   **No `--force`, no `--force-with-lease`, no `+` refspec, no `push --all`, no tags.**
9. **Bounded reconcile, only on a fast-forward rejection** (decision 8). `_run` uses `check=True`,
   so a rejection surfaces as `subprocess.CalledProcessError`; inspect `exc.stdout` (never
   `exc.stderr`, never the message — it can embed the remote and local paths). If the porcelain
   output carries a `! [rejected]` line with `non-fast-forward` / `fetch first`:
   a. `ls-remote --heads <url> refs/heads/<branch>` → remote head SHA (or absent).
   b. If the remote head equals the current parent (or both absent) → not our case, re-raise.
   c. If the remote head is an object we hold locally → rebuild the publication commit re-parented
      onto it, then **one** plain fast-forward push. Set `reparented=True`.
   d. If the remote head is unknown locally → raise (no fetch). Caller persists
      `SOURCE_SYNC_REQUIRED`; operator action required.
   At most **one** reconcile round, and never more than **two** push invocations.
10. On success: `update-ref refs/heads/{branch} {publication}` (unconditional — the local ref is
    object retention and a human-visible mirror, **not** an authority) and return the dict above.
    `_run(..., extra=ssh_env)` with
    `ssh_env = {'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o IdentitiesOnly=yes -i <key>'}`
    when `ssh_key` is set, passed through the existing `extra` hook (applied after the `GIT_*` env
    strip). `BatchMode=yes` is mandatory: without it a missing key or unknown host hangs the worker.

### T6 — `PromoteDeps` + config + composition (B)

- `PromoteDeps` gains optional `output_repo: Any = None`, `source_repo_url: Optional[str] = None`,
  `source_branch_for: Any = None` (`callable(project_id, state, expected_name) -> Optional[str]`),
  `source_ssh_key: Optional[Path] = None`. All default to `None` → publication disabled and every
  existing call site behaves exactly as before.
- `config/default.yaml` gains `website_builder.github:` with `enabled: true`,
  `repo: 'git@github.com:albertus527/website.git'`, `deploy_key_path: null`. Non-secret config in
  `config.yaml` per repo policy; the private key itself never enters the app.
- `load_runtime_config` adds `github_source_repo` / `github_ssh_key` (optional env overrides
  `WEBSITE_BUILDER_GITHUB_REPO`, `WEBSITE_BUILDER_GITHUB_SSH_KEY`).
- `compose()` passes `output_repo=output_repo` plus the resolved repo/branch/key into `PromoteDeps`.
  The branch resolver reuses `_bound_slug_for` semantics: bound `vercel_slug` when present, else the
  same `expected_name` already verified against the owned Vercel project.
- **Unconfigured ≠ failed**: when disabled, or no deploy key is configured, nothing is attempted, a
  clear WARNING is logged, and `sync_status` is left untouched. `SOURCE_SYNC_REQUIRED` is persisted
  only for an *attempted* publication that failed.

### T7 — Where the tested commit comes from (B)

`state.deployment["preview_intent"]["git"]` (`{path, branch, commit, source_sha256, artifact_sha256}`),
written by `preview.py` at `stage="committed"`. Before publishing, `promote.py` must verify:

- `preview_intent.operation_id == approval.operation_id`
- `git.source_sha256 == approval.source_sha256`
- `git.artifact_sha256 == approval.artifact_sha256`
- the branch name equals the verified `expected_name`

Any mismatch → no push, `SOURCE_SYNC_REQUIRED`, and a clear log. Never push a guessed commit.

### T8 — GitHub failure behaviour (B)

Wrapped in `try/except` in `promote.py` (the adapter may raise `subprocess.CalledProcessError`,
whose message can embed the remote and local paths — log only the exception **type name**):

- do **not** roll back production, do **not** re-smoke, do **not** un-transition LIVE;
- persist `sync_status = "SOURCE_SYNC_REQUIRED"` + `last_error_code` (T4). On a failure that is *not*
  a successful publication, keep the previous `publication_commit` value untouched so the next LIVE
  revision still parents onto the last genuinely published commit;
- return `ok({... "source_sync": "SOURCE_SYNC_REQUIRED"})` so the user still gets the truthful
  `🚀 Live: <canonical>`;
- `logger.error("GitHub publication FAILED project=%s branch=%s (type=%s)", ...)`.

### T9 — Approve = approve + publish (C)

`app/channels/dispatch.py`:

- `dispatch(...)` gains an optional `on_approved=None` callback, invoked **after** `approve()`
  succeeds and **before** `promote()`. Follow the existing `on_remote_boundary` precedent
  (`inspect.signature` capability probe; legacy test doubles keep working).
- The `approve` branch body becomes the same body as `publish`:
  `approve()` → `on_approved()` → `promote()`.
- The authz guard for `approve` **stays `require_mutating_role`** so a reviewer can still bind an
  approval exactly as today; their `promote()` then fails closed with the existing owner-only
  `UNAUTHORIZED_ROLE` copy. Nothing is weakened and no capability is removed.
- `publish` keeps its `require_owner_role` guard and identical body → same idempotent no-op path.
- One Telegram event → one claim → one promotion. Replay returns `{"duplicate": True}`.

`app/runtime.py`:

- `_LIFECYCLE_INTENTS` unchanged (`APPROVE` and `PUBLISH` are both already offered in `PREVIEW_READY`).
- `_handle_approve` dispatches `"approve"` with
  `on_approved=lambda: self._send_approval_ack_once(project_id, chat_id)`.
- `_send_approval_ack_once` copy becomes exactly `'✅ Preview approved. Publishing...'`
  (the `'Kalau sudah siap ditayangkan, bilang "publish".'` sentence is deleted). The at-most-once
  `PENDING`/`SENT`/`NOT_SENT` machinery is unchanged, so a duplicate approval of the same identity
  is still silent.
- `_handle_publish` behaviour unchanged (backward compatibility).
- **Idempotent no-op result (decision 5):** `promote()`'s existing LIVE short-circuit already
  returns `ok`; extend it to return `production_url` (canonical), `deployment_id`, `operation_id`.
  `_post_promote` must not run again. In `_handle_approve`/`_handle_publish`, when the result is ok
  and the lifecycle is already LIVE with the same identity, re-send `🚀 Live: <canonical>`.
- Failure path unchanged: `_send_error_reply(conversation_id, result.error_code)`.
- `ERROR_MESSAGES` gains truthful copy for `CANONICAL_PRODUCTION_URL_UNRESOLVED`; the text must
  never claim the site is live and must keep the existing "Preview kamu tetap aman" reassurance.

### T10 — Explicitly NOT added (R2)

No `fetch`, `clone`, `pull`, `archive`, or any **content** read-back; no GitHub-sourced workspace
restore; no revision behaviour change. The only GitHub read is the single ref-head `ls-remote`
reconcile in T5.8, which returns a 40-char SHA and no content. Proven behaviourally by test 15.

---

## 4. Failure modes

| Condition | Behaviour |
|---|---|
| Canonical domain read ambiguous | Deterministic fallback from the verified `expected_name`; INFO log. |
| No valid `expected_name` at all | Fail closed `CANONICAL_PRODUCTION_URL_UNRESOLVED` **before** smoke; PUBLISHING + intent intact; resumable. No rollback, no LIVE. |
| Production smoke fails on canonical | Existing rollback + FAILED path, unchanged. |
| Publication fails / raises | LIVE stands; `SOURCE_SYNC_REQUIRED` persisted; previous `publication_commit` kept as the parent authority; failure logged by type only; user still gets `🚀 Live: <canonical>`. |
| Publication not configured | Not attempted; WARNING; `sync_status` untouched. |
| `preview_intent` git identity absent/mismatched | No push; `SOURCE_SYNC_REQUIRED`; never push a guessed commit. |
| Persisted `publication_commit` not held locally | Hard error; no fetch; `SOURCE_SYNC_REQUIRED`; operator action. |
| Push rejected as non-fast-forward (post-push crash, W2) | One `ls-remote` ref-head reconcile, re-parent, one plain push. Bounded; no clobber. |
| Remote head is an object we do not hold | No fetch; `SOURCE_SYNC_REQUIRED`; operator action. Never forced. |
| Duplicate approve of a LIVE exact preview | One claim, no second promotion, no re-smoke, no state change; current canonical LIVE URL re-sent. |
| Replayed Telegram update | Dispatcher `{"duplicate": True}`; no second ack, no second promotion. |
| Stale preview (newer revision/revise) | `STALE_APPROVAL` fail-closed, unchanged. |
| Reviewer approves | Approval binds; `promote()` fails `UNAUTHORIZED_ROLE`; error copy sent. |
| `publish` after already-LIVE approval | Idempotent no-op returning the current canonical LIVE result. |

---

## 5. Tests

New file `website-builder/tests/test_r1_live_publish_finishing.py` unless an item names an existing
file. Reuse the fakes in `tests/test_promote.py` (`FakeVercel`, `FakeTelegram`, `FakeSmoke`,
`_approved_state`, `_make_workspace`) and `tests/r1_harness.py` patterns; extend the fakes rather
than duplicating them. `tests/test_git_output.py` covers the git plumbing directly (it already
constructs a real bare `OutputGitRepository`).

| # | Test |
|---|---|
| 1 | Deployment-specific URL never appears in any outbound text; the final LIVE message carries only the canonical URL. |
| 2 | The production smoke is invoked with the canonical `https://<slug>.vercel.app/` URL (assert the URL passed to `smoke.run`). |
| 3 | `state.production_url == canonical` after LIVE. |
| 4 | `last_live_deployment` contains **both** `production_url` (canonical) and `deployment_url` (deployment-specific). |
| 5 | First successful LIVE publication creates branch `<slug>` whose head is a **root** publication commit, and `tree(head) == tree(tested_commit)`. |
| 6 | A later successful LIVE revision advances the same branch, and `git merge-base --is-ancestor <pub1> <pub2>` holds — revision 1 stays in revision 2's ancestry. |
| 7 | Every publication commit's tree is exactly its tested snapshot commit's tree (parameterized over revisions 1..N), and `publication_commit != tested_commit` each time. |
| 8 | The normal publication push carries **no** `--force`, no `--force-with-lease`, no `+` refspec (assert the recorded git argv), and a changed tree still fast-forwards. |
| 9 | Persisted `state.repository` carries `provider`/`repo`/`branch`/`tested_commit`/`publication_commit`/`source_revision`/`sync_status`, and `publication_commit` is the parent used for the next publication. |
| 10 | Preview-only, failed, rejected (stale) and unapproved revisions never move the friendly branch and never write `SYNCED`. |
| 11 | Pushed source is the `preview_intent.git.commit` of the approved operation, and its hashes match the approval. |
| 12 | Publication failure after LIVE persists `SOURCE_SYNC_REQUIRED`, keeps the project LIVE (no rollback promote, no re-smoke), and leaves the previous `publication_commit` intact. |
| 13 | W2: when the remote head has advanced, exactly one `ls-remote` ref-head reconcile re-parents and one plain push succeeds; no `fetch`/`clone`/`pull` is ever invoked. |
| 14 | W2 unwedged case: a remote head we do not hold locally fails closed to `SOURCE_SYNC_REQUIRED` with **no** force push. |
| 15 | Approve immediately invokes the existing publish flow (promote called; lifecycle LIVE). |
| 16 | No second-publish confirmation: the ack is `✅ Preview approved. Publishing...` with no "say publish" sentence, and the only later message is `🚀 Live: <canonical>`. |
| 17 | Duplicate approval of the same exact preview creates no second promotion (one dispatch claim; `promote` invoked once) and re-sends the canonical LIVE URL. |
| 18 | A stale approval still fails closed (`STALE_APPROVAL`, no promote, branch unmoved). |
| 19 | Explicit `publish` after already-LIVE approval is an idempotent no-op (no promote, no state change) returning the canonical live result. |
| 20 | No GitHub read-back: a recording output-repo double records **every** method invoked; across preview + publish + a later revision, only the tested-snapshot `commit` and the branch publication appear (plus the conditional ref-head reconcile) — no fetch/clone/pull/hydrate/archive. Behavioural, no source reading. |

Adjust existing tests that assert the old contract:
`tests/test_approve_acknowledgement.py` (ack text now "Publishing…"; approve now publishes — the
"approve must NOT publish" assertions become "approve publishes", while the at-most-once ack,
duplicate, ambiguity and unauthorized cases stay), `tests/test_publish_dedup.py`,
`tests/test_promote*.py` (`state.production_url` and the LIVE message now use the canonical URL),
`tests/test_git_output.py` (add `publish_project_branch` validation tests next to the existing
`push_github` ones; the existing `push_github` tests must still pass unchanged).

Per repo rules: no source-reading tests, no change-detector assertions, no bare `pytest` — use
`scripts/run_tests.sh`.

---

## 6. Validation

```bash
# focused
bash scripts/run_tests.sh website-builder/tests/test_r1_live_publish_finishing.py -q --file-retries=0
bash scripts/run_tests.sh website-builder/tests/test_git_output.py -q --file-retries=0
bash scripts/run_tests.sh website-builder/tests/test_promote.py website-builder/tests/test_publish_dedup.py website-builder/tests/test_approve_acknowledgement.py -q --file-retries=0

# full suite
HERMES_TEST_WORKERS=1 bash scripts/run_tests.sh website-builder/tests -q --file-retries=0
```

Do **not** commit, push, or deploy.

---

## 7. Risks

- **The publication commit is a new object per LIVE revision.** One extra commit per publish in a
  private archive repo; bounded by the number of LIVE revisions, and the tree is shared (no
  duplicated blobs).
- **A reviewer approve now also attempts a publish** and will surface `UNAUTHORIZED_ROLE`. Truthful
  and unchanged in authority, but a new message sequence for that role.
- **Canonical host depends on the Vercel project name** staying equal to the bound slug. It is
  frozen at first bind (`set_vercel_slug_once`) and never renamed in R1, so the two cannot drift.
- **Legacy hash-named projects** get a `wb-<hash>` branch and `wb-<hash>.vercel.app` canonical host
  — consistent, but not human-friendly. Migration is an R2 concern.
- **`--porcelain` stderr/stderr redaction**: push diagnostics can contain the remote and local paths.
  Only `exc.stdout` is inspected, only for the `! [rejected]` marker; nothing is logged verbatim.
- **The local `refs/heads/<slug>` is not an authority.** It is updated after a confirmed push for
  object retention and human inspection only; the persisted `publication_commit` decides the parent.
