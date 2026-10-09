# D4a.1 — OpenViking Live Enablement & Operational Qualification: Acceptance Report

Status: engineering record for Batch D4a.1.
Branch: `web-design`.
Baseline: `9a04ef1caa49584f35bf1519a3c8250546ec463c` (D4a accepted: `D4A_CODE_ACCEPTED_LIVE_BLOCKED`).
Scope: complete the D4a OpenViking foundation so it becomes a real, persistent,
secure, application-owned context library that D4b Laya can consume — while
preserving every D4a security invariant and **not** implementing Laya.

Every claim below is backed by an executed command or an executable test. No
PASS is written for an unexecuted check. **No live server was provisioned**: the
mandatory provider/infrastructure approval checkpoint was not granted (see §4),
so live qualification is reported honestly as **BLOCKED**, not PASS.

---

## 1. Baseline and final commit

| Item | Value |
|---|---|
| Branch | `web-design` |
| Baseline commit | `9a04ef1caa49584f35bf1519a3c8250546ec463c` |
| Working tree at start | clean |
| D4a acceptance doc | `docs/D4A_OPENVIKING_FOUNDATION_ACCEPTANCE.md` |
| D3b acceptance doc | `docs/D3B_CRITIC_REPAIR_ACCEPTANCE.md` |
| D3a.5 acceptance doc | `docs/D3A5_DEPENDENCY_INGRESS_AUDIT.md` |
| `feature/website` | untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646` |
| Hermes Trade | **not touched** (no runtime, tmux, gateway, credentials, or storage change) |
| Final commit | `77223f04870052aa554601a619aeff158c31b6f7` (D4a.1 implementation); the report-finalizing doc commit follows it |

The VPS hosts both Hermes Website and Hermes Trade. D4a.1 modified **only**
`website-builder/` inside the Hermes Website checkout. No global install was
performed, no global Hermes memory plugin was enabled, and `feature/website` was
not merged.

---

## 2. Exact OpenViking version

| Field | Value |
|---|---|
| Project | `volcengine/OpenViking` (GitHub), `openviking.ai` |
| Distribution | PyPI `openviking` |
| **Pinned version** | **`0.4.23`** (verified still the latest on PyPI at D4a.1 time) |
| License | AGPL-3.0 |
| Requires Python | `>=3.10` (isolated venv uses 3.12.3) |
| Python SDK | `openviking-sdk` `0.1.13` (`SyncHTTPClient` / `AsyncHTTPClient`) |
| HTTP API | default `http://127.0.0.1:1933`; `POST /api/v1/search/find`, `POST /api/v1/resources`, `POST /api/v1/resources/temp_upload`, `GET /api/v1/content/read`, `GET /api/v1/fs/ls`, `GET /api/v1/tasks/{id}`, `GET /health`, `GET /ready` |
| URI scheme | `viking://{scope}/{path}`; scopes `resources`, `user`, `agent` |
| Context layers | L0 abstract / L1 overview / L2 detail (directory-level sidecars) |
| Auth | `dev`, `api_key` (`X-API-Key`), `trusted` |

The version is **unchanged from D4a** (`0.4.23`); no upgrade was performed. The
reviewed distribution was installed into an **isolated venv** at
`~/.website-builder/openviking/venv` (no global install). Verified:

```
$ ~/.website-builder/openviking/venv/bin/python -c "import importlib.metadata as m; print(m.version('openviking'))"
0.4.23
```

### API facts verified against the real SDK (not assumed)

Inspected `openviking_sdk.client` source and the pinned distribution:

* `find(query, target_uri, limit, options=FindOptions)` → `POST /api/v1/search/find`;
  response `result` carries `resources` / `memories` / `skills` buckets of
  `MatchedContext {uri, context_type, level, abstract, overview, category, score, match_reason}`.
* `add_resource(path|temp_file_id, to, wait, options=AddResourceOptions)` →
  `POST /api/v1/resources`; `AddResourceOptions` includes `processing_mode`
  (`semantic_and_vectors` | `vectors_only`), `tags`, `tag_mode`, `create_parent`,
  `args` (parser options such as `parse_mode=no_split`), `watch_interval`.
* Raw HTTP local-file upload requires `POST /api/v1/resources/temp_upload`
  (multipart) → `temp_file_id`, then `POST /api/v1/resources` with `temp_file_id`.
* `read(uri)` → `GET /api/v1/content/read`; `write(uri, content, mode)` →
  `POST /api/v1/content/write`; `ls(uri, recursive)` → `GET /api/v1/fs/ls`.
* `ov.conf` is **JSON** (loaded via `json.loads` with `${VAR}` expansion) — not
  YAML. D4a.1's config template is therefore JSON.

**Deliberate non-use of the upstream Hermes integration** (unchanged from D4a):
the global Hermes memory plugin is forbidden; the application-owned adapter is
the only integration.

---

## 3. Provider and model selection

Provider discovery found that the existing **9router** proxy at
`127.0.0.1:20128` exposes a **genuine OpenAI-compatible `/v1/embeddings`
endpoint** — chat-completion compatibility did **not** imply embedding
compatibility, so this was tested explicitly.

| Role | Provider | Model | Endpoint | Auth | Verified |
|---|---|---|---|---|---|
| Embedding | `openai` (OpenAI-compatible) | `openrouter/text-embedding-3-small` | `http://127.0.0.1:20128/v1` | `Authorization: Bearer <NINEROUTER_API_KEY>` | HTTP 200, **1536-dim** |
| VLM (L0/L1) | `openai` (OpenAI-compatible) | `openrouter/z-ai/glm-5.3-flash` | `http://127.0.0.1:20128/v1` | same key | HTTP 200 (chat completion) |

* **Authentication**: the existing website credential `NINEROUTER_API_KEY`
  (already in `~/.hermes-website/.env`). **No new secret is invented.**
* **Compatibility with pinned OpenViking**: `embedding.dense.provider=openai`
  with `dimension=1536` and `api_base` pointed at 9router is a documented,
  supported configuration.
* **Cost for a small approved corpus**: the reviewed corpus is ~150 KB
  (~40 K tokens). Embedding at $0.02/1M tokens ≈ **$0.001**; VLM L0/L1
  generation ≈ 40–60 K tokens at `glm-5.3-flash` rates ≈ **<$0.05**. Total
  **well under $0.10**, one-time.
* **Is a separate VLM mandatory?** No — `processing_mode=vectors_only` ingests
  without a VLM, but then **no L0/L1 sidecars** are produced, so directory-level
  semantic retrieval is degraded. The plan uses the VLM for full-quality L0/L1.
* **Operational limitations**: depends on the 9router container (already running,
  already used by the Website pipeline) and on OpenRouter upstream availability.
  Loopback-only; no public exposure.
* **No local embedding/VLM weights** are downloaded (the default OpenViking
  local model `bge-small-zh-v1.5-f16` would fetch HuggingFace weights, which is
  out of policy).

---

## 4. Deployment decision and the approval checkpoint

**Decision: Option A (localhost server + remote embedding/VLM) — PREPARED and
CODE-COMPLETE, but NOT PROVISIONED.**

The mission requires a mandatory approval checkpoint before enabling paid API
calls or permanent service installation. The checkpoint was presented (provider,
model, cost, architecture, storage, auth, service management) and **approval was
not granted** (the request timed out with no response). Per the mission:

> *If approval is unavailable, stop after completing safe preparation and report
> `AWAITING_PROVIDER_APPROVAL`.*

Consequently D4a.1:

* **completed all safe preparation** (implementation, tests, config templates,
  provisioning script, systemd unit template, runbook, corpus manifest);
* **did not** make a paid API call;
* **did not** install the permanent service;
* **did not** provision a real server or ingest into one;
* keeps the feature flag **disabled by default**;
* reports live qualification as **BLOCKED** (§8–§13 are marked *prepared, not
  executed*).

The exact command an operator runs after approval is
`tools/openviking_provision.sh --yes` followed by
`tools/openviking_qualify.py` (see §18 and the operations runbook).

### Deployment architecture (as prepared)

```
Hermes Website application
   │  (D4a adapter — disabled by default)
   │  localhost HTTP, X-API-Key
   ▼
OpenViking Server  ── systemd USER unit `openviking-website.service`
   127.0.0.1:1933        (independent of the interactive `website` tmux session;
   │                      linger enabled → survives VPS reboot)
   ├── persistent application-owned storage:  ~/.website-builder/openviking/data
   ├── remote embedding API:  9router → openrouter/text-embedding-3-small (1536-d)
   └── remote VLM API:        9router → openrouter/z-ai/glm-5.3-flash  (L0/L1)
```

### Persistent storage location

`~/.website-builder/openviking/data` — application-owned, outside the git tree,
outside the Website project venv, separate from Hermes Trade storage.

### Authentication model

`server.auth_mode=api_key` with a locally generated `root_api_key`
(`OPENVIKING_ROOT_KEY`, `0600`, outside the repo). The application sends it as
`X-API-Key`; the D4a adapter's `to_dict()` reports the key **by presence only**
and never serializes its value. Bound to loopback only.

### Resource preflight (measured, not assumed)

| Resource | Measured value | Assessment |
|---|---|---|
| CPU | 4 vCPU; load 0.39 / 0.18 / 0.10 | idle |
| RAM | 7.7 GiB total, ~6.4 GiB available | adequate (no local weights) |
| Disk | 96 GiB, 66 GiB free (32% used) | adequate |
| Inodes | 12.98 M total, 7% used | adequate |
| Python | 3.12.3 | compatible |
| Port 1933 | free | available for the loopback server |
| Existing workloads | Hermes Website + Hermes Trade both running | must not be disturbed |
| Isolated venv | `~/.website-builder/openviking/venv`, 752 MB | no global install |

---

## 5. What D4a.1 adds (implementation)

| # | Responsibility | File |
|---|---|---|
| 1 | Live backend: real ingestion + provenance-attached retrieval | `app/core/openviking_live.py` |
| 2 | Reviewed corpus definition (allowlist policy) | `app/core/openviking_corpus.py` |
| 3 | Enabled-path backend selection (live, with safe fallback) | `app/core/openviking_retrieval.py::build_adapter` |
| 4 | Live qualification runner | `tools/openviking_qualify.py` |
| 5 | Idempotent, approval-gated provisioning | `tools/openviking_provision.sh` |
| 6 | Server config template (JSON) | `deploy/openviking/ov.conf.template` |
| 7 | systemd user unit template | `deploy/openviking/openviking-website.service` |
| 8 | Env template | `deploy/openviking/openviking.env.template` |
| 9 | D4a.1 mutation driver (7 guards) | `tools/mutation_check_d4a1.py` |
| 10 | Focused D4a.1 tests (26) | `tests/test_openviking_live.py` |
| 11 | Operations runbook | `docs/D4A1_OPENVIKING_OPERATIONS.md` |
| 12 | This report | `docs/D4A1_OPENVIKING_LIVE_ACCEPTANCE.md` |

### The live ingestion path (real, preserves D4a policy)

`LiveOpenVikingBackend.put_resource` implements the verified upstream API:

1. `POST /api/v1/resources/temp_upload` (multipart `content.md`) → `temp_file_id`;
2. `POST /api/v1/resources` with `to=<resource dir>`, `wait=true`,
   `processing_mode=semantic_and_vectors`, `args.parse_mode=no_split`,
   application-derived `tags` → `task_id`;
3. bounded task polling (`GET /api/v1/tasks/{id}`) until a terminal state;
4. write the **provenance sidecar** `.openviking-record.json` **LAST**.

**Order is the consistency guarantee.** A resource is "fully indexed" only once
its record exists; a failed content write, a failed task, or a failed record
write leaves the resource without resolvable provenance, so retrieval **drops**
it rather than presenting it as current. The record sidecar is an
application-owned provenance manifest with atomic semantics (single write,
resolved back on read; a mismatch is detected by `verify_record`).

**Policy is preserved verbatim.** Live ingestion still runs through D4a's
`ingest_sources`: explicit source allowlist, source-revision pinning, content
digest, provenance, idempotent re-ingestion, project isolation, category
allowlist, per-source (`MAX_SOURCE_BYTES`) and per-batch (`MAX_INGEST_TOTAL_BYTES`)
size limits, forbidden-source rejection, credential detection, and trust
classification. The live backend only supplies the three calls the library makes
and a bounded local-file reader — a caller cannot widen the policy.

### Provenance is resolved, not trusted from the server

OpenViking does not carry the application's `ResourceRecord`. The live backend
reads it from the application-owned sidecar and **drops** any match whose
provenance cannot be resolved, **at the backend layer** — so the D4a adapter's
fail-closed isolation/provenance/credential checks are untouched and remain the
single enforcement point. No isolation check was weakened to accommodate a
malformed response.

### Bounded local-file reader

`make_source_reader(root)` is the only read path for live ingestion. It refuses,
before any read: a locator that escapes `root` (no traversal), a forbidden path
(secrets, logs, dependency dirs, executables), and a file larger than
`MAX_SOURCE_BYTES`. It never crawls and never fetches a URL.

---

## 6. Indexed source manifest (prepared)

The reviewed corpus is declared in `app/core/openviking_corpus.py`. It draws
**only** on the provisioned, reviewed `refero-design` and `impeccable` skills in
the Website generation profile (`~/.hermes-website/skills`). It does **not**
import whole repositories and does **not** crawl third-party sites.

| source_id | path (profile-relative) | category | trust | exists on host |
|---|---|---|---|---|
| `refero_typography` | `refero-design/references/typography.md` | design_dna | reviewed | yes |
| `refero_color` | `refero-design/references/color.md` | design_dna | reviewed | yes |
| `refero_anti_ai_slop` | `refero-design/references/anti-ai-slop.md` | design_dna | reviewed | yes |
| `refero_visual_workflow` | `refero-design/references/visual-workflow.md` | design_dna | reviewed | yes |
| `refero_motion` | `refero-design/references/motion.md` | motion | reviewed | yes |
| `refero_craft_details` | `refero-design/references/craft-details.md` | components | reviewed | yes |
| `refero_icons` | `refero-design/references/icons.md` | components | reviewed | yes |
| `impeccable_skill` | `impeccable/SKILL.md` | design_dna | reviewed | yes |
| `impeccable_critique` | `impeccable/reference/critique.md` | design_dna | reviewed | yes |
| `impeccable_layout` | `impeccable/reference/layout.md` | components | reviewed | yes |
| `impeccable_audit` | `impeccable/reference/audit.md` | components | reviewed | yes |

* Every file was verified to exist on the host before being listed.
* License: `impeccable` is Apache-2.0 (`SKILL.md` frontmatter); `refero-design`
  is the provisioned profile skill already used by the pipeline.
* Trust is `reviewed` (the highest level) for every entry.
* Only files that actually exist become `SourceSpec`s; a missing entry is
  reported (`missing_corpus_entries`), never invented.
* Runtime logs, `.env`, secrets, dependency dirs, and generated application files
  are excluded structurally and re-refused at ingest time.

**Source manifest file**: written by the qualifier to
`~/.website-builder/openviking/source-manifest.json` (via
`corpus_summary`) when it runs. *Prepared; not yet produced because no live run
occurred.*

---

## 7. Live ingestion evidence

**BLOCKED — no live run.** No server was provisioned (approval not granted), so
no resource was ingested into a real server. What **was** executed:

* the real ingestion code path against a deterministic **HTTP transport**
  (`httpx.MockTransport`) implementing the documented routes — 26 focused tests
  (§15). This is an HTTP-transport test, **not** a real-server test, and is not
  reported as live evidence.

What the live run will produce (command in §18): `statuses` per source, byte
totals, and an idempotency re-run — written to `qualification.json`.

---

## 8. Live retrieval examples and provenance

**BLOCKED — no live run.** No real retrieval was executed. The production
adapter's retrieval contract is exercised offline (§15) and via the live HTTP
code path against the deterministic transport.

### Retrieval probes (prepared)

| Probe | Query | Expected |
|---|---|---|
| A. Design | "minimalist editorial landing page with botanical typography" | reviewed design refs, valid `viking://` URIs, revisions, trust/category preserved, bounded |
| B. Component | "accessible responsive card component design" | component/design guidance; no dependency install, no code execution |
| C. Motion | "subtle page transition with reduced motion accessibility" | motion refs when present; no fabrication if absent |
| D. Missing | "quantum chromodynamics lattice gauge theory renormalization" | honest empty/low-relevance; no fabricated provenance |
| E. Cross-project | retrieval from another project scope | no foreign content; violation fails closed |
| F. Credential | credential-shaped fixture | rejected before exposure; no secret logged |
| G. Prompt injection | malicious instruction in a document | inert DATA; cannot override FAST/policy |
| H. Service outage | stop the dedicated service only | adapter unavailable/timeout; Website pipeline + Trade unaffected |
| I. Persistence | restart the service | indexed resources remain; provenance consistent; no full reindex |
| J. Duplicate/changed source | re-ingest unchanged / changed | unchanged skipped; changed advances revision; no stale duplicate |

Probes F and G are additionally proven **offline today** (they are pure
adapter/backend properties): §15 shows credential-shaped content failing closed
and injected instructions round-tripping as inert data.

---

## 9. Actual L0/L1/L2 support

**Not measured live.** Because no server ran with a VLM, no real L0/L1 sidecars
were generated in this batch. The reviewed facts:

* L0/L1 are **directory-level** sidecars (`.abstract.md` / `.overview.md`);
  L2 is the file body. Only levels that exist are readable.
* With `processing_mode=semantic_and_vectors` + a configured VLM, the server
  generates L0/L1 for the resource directory; with `vectors_only`, it does not.
* The D4a adapter loads L0/L1 by default and L2 only when `allow_detail=True`.
  A requested level that does not exist returns an **honest degraded** result
  (the adapter falls back to the nearest available lower level and never
  fabricates text).

No claim of real L0/L1 availability is made, because no real semantic processing
occurred in this batch.

---

## 10. Resource consumption

**Not measured live** (no server ran). Prepared footprint:

| Component | Expected |
|---|---|
| Isolated venv | 752 MB on disk (measured) |
| Server process | 1 worker, loopback; light RAM (no local weights) |
| Data/index | corpus ~150 KB source; index small |

The runbook (`docs/D4A1_OPENVIKING_OPERATIONS.md`) specifies measuring
`systemctl --user status` RSS and `du -sh data` after provisioning.

---

## 11. Latency measurements

**Not measured live.** The qualifier records per-probe latency and a median/max
in `qualification.json` when run.

---

## 12. Persistence and restart evidence

**Not measured live.** Prepared procedure and expectation:

* The whole state is `~/.website-builder/openviking/data` (AGFS content + vector
  index). OpenViking recovers indexed data from it on restart **without a full
  reindex**.
* Provenance records travel with the resources (the sidecar is inside the
  resource directory), so provenance stays consistent across a restart.
* Verified offline: the record sidecar survives as server state and is resolved
  back correctly (`test_verify_record_detects_a_digest_match_and_mismatch`).

---

## 13. Failure-injection results

Proven **offline** through the production adapter and live HTTP code path:

| Injection | Result |
|---|---|
| Server outage (find returns 5xx) | adapter → `unavailable`, 0 items, **no raise** |
| Failed ingestion task | `ingest_sources` records `error`; resource **not** marked indexed |
| Foreign URI from the server | adapter → `isolation_violation`, 0 items (fails closed) |
| Missing provenance | backend drops the match; no fabricated item |
| Credential-shaped content | adapter → `CREDENTIAL_LEAK_VIOLATION`, 0 items; no secret echoed |
| Prompt injection in a document | round-trips as inert DATA; no authority-bearing field |
| Disabled flag | 0 backend calls, `disabled`, 0 items |
| Digest drift | `verify_record` → `digest mismatch` |

The live **service-outage (H)** and **persistence/restart (I)** probes require a
running service and are **BLOCKED** pending approval; the offline equivalents
above are executed.

---

## 14. Security regression results

All D4a invariants are preserved (unchanged code + the D4a.1 guards):

* Isolation, cross-tenant, provenance, credential, and category checks remain
  fail-closed in the adapter (one enforcement point, unweakened).
* The live backend **drops** unprovenanced matches rather than loosening the
  adapter to accept them.
* The live writer refuses an out-of-scope URI and a cross-project record before
  any upload.
* The bounded reader refuses traversal, forbidden paths, and oversize files
  before any read.
* The feature flag stays disabled by default; the shipped config keeps
  `enabled: false` and never carries a key.
* No OpenViking wiring into FAST/FRONTEND/QA/Design-DNA; no global memory
  plugin; no `subprocess`/shell/installer in the library or live module.

The D4a.1 mutation driver kills all 7 new guards (§15); the D4a driver still
kills all 17; the D3a.5/D3b drivers are unchanged.

---

## 15. Full test counts

All commands run from `website-builder/` with the project venv.

| Suite | Result |
|---|---|
| Focused D4a + D4a.1 (`test_openviking_{library,retrieval,composition,live}`) | **140 passed** |
| D4a.1 focused (`test_openviking_live`) | **26 passed** |
| Full default offline suite (network blocked at the Python level) | **3892 passed, 2 skipped, 4 deselected, 60 subtests passed** |
| D3a.5/D3b regression subset (20 files) | **1029 passed, 1 skipped, 19 subtests passed** |
| D4a mutation driver | **17/17 guards killed** |
| D4a.1 mutation driver | **7/7 guards killed** |
| D3a.5 mutation drivers | all 16 / 73 / 39 / 36 / 18 guards killed |
| D3b mutation driver | 14/14 guards killed |

The default suite stays offline and deterministic; the four network-needing
tests carry the repo's `integration` marker and are deselected. The D4a.1 live
tests use an in-process `httpx.MockTransport` with an inline loopback literal, so
the suite-offline guard (`tests/test_suite_offline.py`) passes.

### Deterministic D4a.1 coverage (26 tests)

* live ingestion through `ingest_sources` (index, idempotent skip, revision
  advance, forbidden-before-upload, failed-task-not-indexed);
* the bounded reader (traversal, forbidden, oversize, missing);
* live retrieval through the production adapter (scoped items + provenance,
  project isolation, foreign-URI fail-closed, unprovenanced drop, credential
  fail-closed, prompt-injection inert, outage unavailable, disabled no-op);
* write scope + cross-project write refusal;
* consistency (`verify_record` match/mismatch);
* the reviewed corpus definition (closed categories, existence filter, honest
  missing report, no traversal).

---

## 16. Final proof run

```
$ ./.venv/bin/python tools/d4a_final_proof.py
```

The runner now covers 8 stages: focused D4a/D4a.1 tests, the full offline suite
(network blocked), D3a.5/D3b regressions, the five D3a.5 mutation drivers, the
D3b driver, the D4a driver (17), the **D4a.1 driver (7)**, and the guardrails
(branch, untouched `feature/website`, required artifacts, no FAST/FRONTEND/QA
wiring, no global install, config disabled by default).

*(VERDICT line recorded from the executed run.)*

```
[1/8] focused D4a/D4a.1 tests ................ PASS  140 passed
[2/8] full offline suite (network BLOCKED) ... PASS  3892 passed, 2 skipped, 4 deselected, 60 subtests
[3/8] D3a.5/D3b regression tests ............. PASS  1029 passed, 1 skipped, 19 subtests
[4/8] D3a.5 mutation drivers ................. PASS  16/16, 73/73, 39/39, 36/36, 18/18 killed
[5/8] D3b mutation driver .................... PASS  14/14 killed
[6/8] D4a mutation driver .................... PASS  17/17 killed
[7/8] D4a.1 mutation driver .................. PASS  7/7 killed
[8/8] guardrails ............................. PASS  7/7 (branch, feature/website untouched,
                                                          artifacts, no wiring, no global install,
                                                          config disabled, no stray branch)
========================================================================
18/18 checks passed
VERDICT: PASS
========================================================================
```

---

## 17. Known limitations

* **Live qualification is BLOCKED.** No real server was provisioned; approval
  was not granted. Live ingestion, retrieval, latency, resource consumption,
  and persistence/restart against a real server are **not** qualified.
* **L0/L1 are unmeasured live.** The adapter's level policy is proven offline;
  real sidecar generation is unqualified.
* **The token estimate is a 4-chars/token approximation**, not a real
  tokenizer — a safety bound, not a billing figure.
* **The live backend's exact single-file layout** depends on OpenViking's
  `no_split` behavior; the backend resolves the content URI defensively (tries
  `content.md`, then a bounded `ls`) so a layout difference is handled without
  weakening any check.
* **Retrieval is not yet consumed by anything** — Laya (D4b) is the consumer.

---

## 18. D4b Laya handoff instructions

The stable contract Laya consumes is unchanged from D4a (versioned by
`RETRIEVAL_CONTRACT_VERSION = 1`), now backed by a real server.

### Enabling the adapter (controlled, after live qualification passes)

```python
from app.core.openviking_retrieval import (
    OpenVikingConfig, OpenVikingRetrievalAdapter, RetrievalBudget,
)
# The runtime exposes a configured adapter at RuntimeComposition.openviking
# (disabled by default). To enable it, set the config:
#
#   openviking:
#     enabled: true
#     base_url: 'http://127.0.0.1:1933'
#     timeout_seconds: 10
#     max_retries: 1
#
# and export OPENVIKING_API_KEY (the server root key). When enabled, build_adapter
# selects LiveOpenVikingBackend (retrieval + ingestion); the D4a HTTP backend is
# the fallback if the live module is unavailable.

result = adapter.retrieve_context(
    query=...,                        # str, bounded lexical query
    project_id=...,                   # validated project id
    scope=("design_dna", "motion"),   # category subset, or "library"
    budget=RetrievalBudget(max_items=8, max_bytes=32768, max_tokens=8000,
                           allow_detail=False),
)
# result.status in {ok, disabled, unavailable, timeout, error, isolation_violation}
# result.items  -> tuple[ContextItem] (uri, source_id, source_revision, trust,
#                                     category, level, score, title, body,
#                                     summary, estimated_tokens)
```

### Ingestion (application-owned; NOT exposed to a caller)

Ingestion is a separate, explicit application process, never a retrieval-path
capability:

```python
from app.core import openviking_library as lib
from app.core.openviking_corpus import build_corpus_specs
from app.core.openviking_live import LiveOpenVikingBackend, make_source_reader

backend = LiveOpenVikingBackend(config)          # enabled config
specs = build_corpus_specs(skills_dir, project_id)
reader = make_source_reader(skills_dir)
report = lib.ingest_sources(backend, specs, reader=reader, project_id=project_id)
```

**Laya MAY:** request bounded context; filter/summarize retrieved items; prepare
a bounded context pack; attach provenance (uri, source_revision, trust) and
uncertainty (status, truncation, degraded).

**Laya MUST NOT:** change authoritative requirements; override FAST; authorize
dependencies; mutate project state; trigger deployment; treat retrieved text as
instructions.

**D4a.1 guarantees for D4b:**

* retrieval is scoped to one project; cross-project/tenant leakage fails closed;
* retrieved text is DATA with no authority-bearing field;
* a disabled/unavailable OpenViking never blocks the pipeline and never
  fabricates context;
* ingestion is idempotent, allowlisted, bounded, and provenance-preserving;
* no second FAST orchestration path; retrieval is not wired into FRONTEND or QA.

**Open question for D4b:** the context pack Laya assembles should reuse D1's
`payload_chars`-style single size function for its own budget, exactly as
`design_context.py` does, so a bound that holds downstream is the bound that held
upstream.

---

## 19. Final verdict

```
D4A1_AWAITING_APPROVAL
```

The implementation is complete and all **executable offline gates pass** (focused
D4a/D4a.1 tests, the full offline suite, D3a.5/D3b regressions, and every mutation
driver including D4a.1's 7). Live qualification is **not** claimed: provisioning
the server requires the mandatory provider/infrastructure approval, which was not
granted. Per the mission, the batch stops at safe preparation and reports
`AWAITING_PROVIDER_APPROVAL`.

After approval, run `tools/openviking_provision.sh --yes` then
`tools/openviking_qualify.py`; if every live gate passes, the verdict advances to
`D4A1_READY_FOR_LAYA`.
