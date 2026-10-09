# D4a — OpenViking Context Library Foundation: Acceptance Report

Status: engineering record for Batch D4a.
Branch: `web-design`. Baseline: `a1400b959b17f8b25b624eea812d9e49a77dc4fa` (D3b accepted).
Scope: establish OpenViking as a **bounded, read-only context retrieval
foundation** for Hermes Website Builder R2. This batch does **not** implement
Laya, does **not** modify the FAST decision-making contract, and does **not**
merge into `feature/website`.

Every claim below is backed by an executed command or an executable test. No
PASS is written for an unexecuted check.

---

## 1. Baseline

| Item | Value |
|---|---|
| Branch | `web-design` |
| Baseline commit | `a1400b959b17f8b25b624eea812d9e49a77dc4fa` |
| Working tree at start | clean |
| D3b acceptance doc | `docs/D3B_CRITIC_REPAIR_ACCEPTANCE.md` |
| D3a.5 acceptance doc | `docs/D3A5_DEPENDENCY_INGRESS_AUDIT.md` |
| `feature/website` | untouched at `868ed00e3f24e06f1dcf9944d6d031105dff0646` |
| Hermes Trade | **not touched** (no runtime, tmux, gateway, credentials, or storage change) |

The VPS hosts both Hermes Website and Hermes Trade. D4a modified **only**
`website-builder/` inside the Hermes Website checkout. No global install was
performed, no global Hermes memory plugin was enabled, and `feature/website`
was not merged.

---

## 2. Reviewed OpenViking version

| Field | Value |
|---|---|
| Project | `volcengine/OpenViking` (GitHub), `openviking.ai` |
| Distribution | PyPI `openviking` |
| **Pinned version** | **`0.4.23`** (latest stable at review time; `0.4.17.1` was the latest at first inspection) |
| License | AGPL-3.0 |
| Requires Python | `>=3.10` (host has 3.12.3) |
| Python SDK | `openviking-sdk` (`SyncHTTPClient` / `AsyncHTTPClient`) |
| HTTP API default | `http://localhost:1933`, route `POST /api/v1/search/find` |
| URI scheme | `viking://{scope}/{path}`; scopes `resources`, `user`, `agent` |
| Context layers | L0 abstract / L1 overview / L2 detail |
| Docs reviewed | `docs/en/api/06-retrieval.md`, `docs/en/api/03-filesystem.md`, `docs/en/api/12-content.md`, `docs/en/concepts/03-context-layers.md`, `docs/en/concepts/04-viking-uri.md`, `docs/en/guides/04-authentication.md` |

The version is **recorded as a fact**, never auto-installed. Nothing in this
repository installs or upgrades OpenViking. Confirmed after implementation:
`python -c "import openviking"` → `ModuleNotFoundError`, and `pip freeze` shows
no `openviking` entry — the toolchain is unmutated (§11).

### What was verified about the API (not assumed)

* Resources are addressed by `viking://` URIs under a **shared** `resources`
  scope; there is no per-project scope, so the application adds its own
  `projects/<id>/` segment beneath `viking://resources/website-builder/`.
* Retrieval is `find()` (single query, QUICK mode, no session) or `search()`
  (needs session context + LLM intent analysis). D4a uses the **single-query
  `find`** shape only — no session, no intent LLM, no rerank dependency.
* `find` accepts `query`, `target_uri`, `limit`, `level`, `context_type`,
  `tags`. It returns `MatchedContext` records carrying `uri`, `level`,
  `abstract`, `overview`, `score`, `match_reason`.
* L0/L1 are **directory-level** sidecars (`.abstract.md`, `.overview.md`); L2 is
  the file body. Only the levels that exist are readable.
* Authentication is API-key mode (`X-API-Key`) by default when a root key is
  configured; trusted/dev modes also exist. D4a binds to localhost and supports
  an API key.

### Deliberate non-use of the upstream Hermes integration

OpenViking publishes a Hermes integration (`docs/en/agent-integrations/05-hermes.md`)
that installs a **global** Hermes memory plugin (`hermes plugins install
openviking --enable`, `hermes memory setup openviking`). D4a **does not use
it**: the mission forbids a global Hermes memory plugin, and the plugin path
would give a model-adjacent component authority over context that this batch
requires the *application* to own. The application-owned adapter here is the
only integration.

---

## 3. Architecture map

```
USER → Laya (D4b, NOT built here) → OpenViking retrieval (D4a)
         → bounded context pack (D4b) → FAST (unchanged) → FRONTEND (unchanged)
         → existing QA pipeline (unchanged)
```

D4a implements **only** the OpenViking retrieval foundation (the boxed layer).
It is deliberately NOT wired into FAST, FRONTEND, QA, or the Design-DNA context
pack.

```
                    ┌───────────────────────────────────────────┐
                    │  app/runtime.py :: compose()               │
                    │    config.openviking_config (disabled)     │
                    │            │                               │
                    │            ▼                               │
                    │  build_openviking_adapter()                │
                    │            │                               │
                    │            ▼                               │
   ┌────────────────┴───────────────────────────────────────────┴──────────┐
   │  app/core/openviking_retrieval.py                                        │
   │    OpenVikingRetrievalAdapter.retrieve_context(query, project_id,        │
   │                                                scope, budget)            │
   │      ├─ feature flag check        → status=disabled (no backend call)     │
   │      ├─ bounded query + retries   → timeout / unavailable                │
   │      ├─ normalize matches:                                               │
   │      │     isolation · provenance · cross-tenant · credential · category │
   │      ├─ budget enforcement: items · bytes · tokens                       │
   │      └─ ContextRetrievalResult (items, provenance, truncation, status)   │
   │                          │                                               │
   │                          ▼                                               │
   │  app/core/openviking_library.py                                          │
   │    • schema: categories, trust, levels, Viking-URI helpers               │
   │    • ingestion: allowlist · idempotent · bounded · provenance records    │
   │    • OpenVikingBackend protocol                                          │
   │        ├─ FakeOpenVikingBackend   (deterministic, offline — the tested    │
   │        │                            ingestion/retrieval backend)          │
   │        └─ HttpOpenVikingBackend   (real server; retrieval only; live      │
   │                                    ingestion REFUSED — not qualified)     │
   └───────────────────────────────────────────────────────────────────────────┘
```

| # | Responsibility | File |
|---|---|---|
| 1 | Schema, trust, levels, URI helpers | `app/core/openviking_library.py` |
| 2 | Ingestion (allowlist, idempotent, provenance) | `app/core/openviking_library.py::ingest_sources` |
| 3 | Backend protocol + deterministic fake | `app/core/openviking_library.py::OpenVikingBackend`, `FakeOpenVikingBackend` |
| 4 | Retrieval adapter + budgets + fail-closed | `app/core/openviking_retrieval.py::OpenVikingRetrievalAdapter` |
| 5 | Live HTTP backend (retrieval only) | `app/core/openviking_retrieval.py::HttpOpenVikingBackend` |
| 6 | Configuration + feature flag | `app/core/openviking_retrieval.py::OpenVikingConfig`; `config/default.yaml` |
| 7 | Composition seam | `app/runtime.py::compose` (`RuntimeComposition.openviking`) |

---

## 4. Deployment decision

**Decision: Option C — a deterministic mocked adapter for development**, with
Option A (local server + remote embedding/VLM APIs) **prepared but not
provisioned**, and live qualification reported as **BLOCKED**.

### Resource preflight (measured, not assumed)

| Resource | Measured value | Assessment |
|---|---|---|
| CPU | 4 vCPU | adequate |
| RAM | 7.7 GiB total, ~6.5 GiB available | adequate for a light service; **NOT** for local VLM weights |
| Disk | 96 GiB, 66 GiB free (32% used) | adequate |
| Inodes | 12.98 M total, 7% used | adequate |
| Load average | 0.66 / 0.23 / 0.14 | idle |
| Python | 3.12.3 (OpenViking needs ≥3.10) | compatible |
| Node | 22.23.2 | unrelated to OpenViking |
| Port 1933 | free | available for a localhost server |
| Existing workloads | Hermes Website + Hermes Trade both running | must not be disturbed |

### Why not provision a live server now

A real OpenViking server requires an **embedding model** and (optionally) a
**VLM** for semantic processing. The supported providers are remote APIs
(Volcengine ARK, OpenAI, Kimi, GLM) or local Ollama weights. Provisioning a
paid provider would incur cost, and the mission requires reporting provider,
model, expected indexing volume and cost assumptions **before** provisioning
any paid model. No such paid provider was configured for OpenViking, and
**downloading local embedding/VLM weights was explicitly withheld** (no
approval given).

Consequently D4a:

* implements and tests the adapter against a **deterministic fake backend**
  (Option C);
* ships a **real HTTP backend** (`HttpOpenVikingBackend`) behind the same
  protocol, exercised only by a bounded **loopback transport probe** (§10), so
  the live path is not merely asserted but the *retrieval* transport is real;
* **refuses** the live ingestion write path (`OpenVikingLiveNotQualified`)
  rather than pretend it works;
* reports live retrieval qualification as **BLOCKED**, never as PASS.

The service is not exposed publicly: the default `base_url` is
`http://localhost:1933`, and the config comment states the service must bind to
localhost with authentication where supported.

---

## 5. Context library schema

A versioned (`LIBRARY_SCHEMA_VERSION = 1`), project-scoped library expressed in
OpenViking's **native** directory semantics. There is no invented virtual-path
scheme: every category is a real directory under the project root.

```
viking://resources/website-builder/projects/<project_id>/
    design_dna/     # Design DNA and style references
    components/     # component reference documentation
    motion/         # motion and interaction guidance
    briefs/         # project briefs and accepted requirements
    decisions/      # revision decisions and approved preferences
```

* **Categories** (`CATEGORIES`) are a closed set of five.
* **Trust** (`TRUST_LEVELS`) is a closed, ordered set:
  `reviewed` (application-owned, verified) > `internal` (accepted project
  state) > `external` (third-party reference — lower trust by definition).
* **Levels** mirror OpenViking L0/L1/L2 exactly (`LEVEL_ABSTRACT/OVERVIEW/DETAIL`).
* **Project ids** are constrained (`^[a-z0-9][a-z0-9_-]{0,63}$`) and used
  **verbatim** as a URI segment, so no caller-supplied path ever reaches a URI.

Every indexed resource preserves a `ResourceRecord`:

| Field | Meaning |
|---|---|
| `source_id` | deterministic application-owned identity |
| `canonical_locator` | canonical source location (no `..`, bounded) |
| `source_revision` | first 12 hex of the content digest |
| `project_id` | project scope |
| `category` | one of the five categories |
| `content_type` | declared content type |
| `trust` | one of the three trust levels |
| `ingested_at` | ISO-8601 UTC ingestion timestamp |
| `digest` | SHA-256 of the content |
| `byte_size` | content size |
| `uri` | the resource's `viking://` URI |

Global reviewed design references vs project-specific context are separated by
**scope**: global references are indexed under a shared
`viking://resources/website-builder/global/` root (application-owned), while
project context lives under `.../projects/<id>/`. Retrieval is always scoped to
a single project root, so cross-project and cross-tenant retrieval are
structurally impossible (§7).

### Never indexed

Enforced by `is_forbidden_source` on the canonical locator **before any read**,
and re-checked on the resolved URI:

* `.env` files and any `*.env`; `.key`, `.pem`, `.p12`, `.pfx`, `.crt`;
* `id_rsa`, `id_ed25519`, `.npmrc`, `.netrc`, `credentials`, `secrets`,
  `app.env`;
* `*.log` (private runtime logs);
* `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.pytest_cache`
  (generated dependency folders);
* `*.pyc`, `*.so`, `*.dll`, `*.dylib`, `*.exe` (unreviewed executable content).

Arbitrary user files without an approved ingestion path cannot be indexed:
ingestion reads **only** through an application-supplied `reader` and only for
`SourceSpec`s in the caller's allowlist.

---

## 6. Ingestion contract

`ingest_sources(backend, sources, *, reader, project_id, clock)`:

* **Reviewed source allowlist.** Only the `SourceSpec`s passed in are ever
  considered; the reader is the only read path.
* **Source revision pinning.** Each resource stores a `source_revision`
  (content digest prefix); re-ingesting a changed source advances it.
* **Idempotent.** Re-ingesting an **unchanged** source (same canonical locator
  and digest) is `skipped_duplicate` — no write.
* **Deterministic source identity.** The resource URI is derived from
  `(project_id, category, slug(source_id))`, so the same inputs always map to
  the same URI.
* **Content-size limits.** Per source `MAX_SOURCE_BYTES = 256 KiB`; per batch
  `MAX_INGEST_TOTAL_BYTES = 4 MiB`; at most `MAX_INGEST_RESOURCES = 32`.
* **Failure isolation.** One failing write is recorded as `error` for that
  source only; the batch continues.
* **Duplicate detection.** By `(canonical_locator, digest)`.
* **Provenance preservation.** The full `ResourceRecord` travels with the
  resource.
* **No arbitrary URL crawling.** There is no crawler and no URL fetcher in the
  module; `is_forbidden_source` + the allowlist forbid it structurally.
* **No execution of retrieved instructions.** The module imports no
  `subprocess` and runs nothing (pinned by test).

Statuses: `indexed`, `skipped_duplicate`, `skipped_forbidden`,
`rejected_allowlist`, `rejected_unreadable`, `rejected_oversize`, `error`.

### Seed corpus

D4a seeds **no corpus into a live server** (none is provisioned). The intended
initial corpus is a **small approved set of existing Hermes design references**
— e.g. `skills/refero-design/references/{typography,color,motion,craft-details,anti-ai-slop}.md`
and the `impeccable` reference set — each ingested as a `reviewed` `SourceSpec`.
No bulk import of the repository or external sites is performed. External
reference content would be classified `external` (lower trust). Existing
verified design resource files are **read-only** to ingestion: the reader
returns their bytes; nothing overwrites them, and generated summaries never
replace them.

---

## 7. Retrieval contract

```python
retrieve_context(query, project_id, scope=None, budget=None) -> ContextRetrievalResult
```

`ContextRetrievalResult` includes:

| Field | Meaning |
|---|---|
| `status` | one of `ok`, `disabled`, `unavailable`, `timeout`, `error`, `isolation_violation` |
| `items` | bounded, ordered `ContextItem` tuple |
| per item: `uri` | source URI (`viking://…`) |
| per item: `source_revision` | source revision |
| per item: `trust` | trust level |
| per item: `score` | relevance information (where the backend supplies it) |
| per item: `level` | L0/L1/L2 actually loaded |
| `estimated_tokens` | bounded token estimate |
| `truncated` | truncation status |
| `error_reason` | static reason when not ok |
| `warnings` | static, payload-free labels |

`ContextItem` carries **no authority-bearing field** (no `instruction`,
`system`, `requirement`, `override`, `command`) — the same trust boundary
`DesignEntry` uses in D1.

### Enforced bounds (application-owned; a caller cannot widen past the ceiling)

| Bound | Default | Ceiling |
|---|---|---|
| result count | 8 | `MAX_RETRIEVAL_RESULTS = 50` |
| context bytes | 32 KiB | `MAX_CONTEXT_BYTES = 64 KiB` |
| estimated tokens | 8 000 | `MAX_CONTEXT_TOKENS = 16 000` |
| request timeout | 10 s | config-owned |
| retries | 1 | ≤ 5 |
| traversal | none — a single `find` call, no recursion | — |

### L0/L1 vs L2

L0/L1 summaries are loaded by default (`allow_detail=False`); L2 detail is
loaded **only** when a caller sets `allow_detail=True`. A generated summary is
never treated as authoritative evidence — items always carry their
`source_revision` and `trust`.

---

## 8. Isolation boundaries

The load-bearing guarantee. Enforced **in the application**, never trusted from
the server:

1. **Scope containment.** Every returned URI is validated with
   `is_uri_within_scope(uri, project_root_uri(project_id))` — a single
   URI-validation point (`_validated_viking_segments`) that rejects a foreign
   scheme, any `..`/`.`/unsafe segment, and any path not strictly under the
   project root. `…/projects/ab` is **not** inside `…/projects/a`.
2. **Cross-tenant records.** A record whose `project_id` differs from the
   requested project is refused, even if the URI is in-scope.
3. **Provenance.** An item with no record, or a record lacking
   `source_id`/`source_revision`, is refused.
4. **Credentials.** Credential-shaped content (PEM blocks, `AKIA…`,
   `sk-ant-`/`sk-proj-`, `ghp_…`, `xoxb-…`, `*_KEY=`/`*_TOKEN=`/`PASSWORD=`
   assignments, high-entropy blobs co-occurring with a key word) is refused,
   and the matched value is **never** echoed into a result or a log.
5. **Category allowlist.** A category outside the request is filtered.

All five are **fail-closed**: a violation refuses the *entire* result (zero
items, non-ok status). A retrieval failure can never bypass a security check,
and no result can ever change a security outcome.

---

## 9. Failure / fallback behaviour

| Condition | Status | Items | Effect on existing behaviour |
|---|---|---|---|
| Feature flag disabled | `disabled` | 0 | none — no backend call |
| No backend configured | `unavailable` | 0 | none |
| Service outage / transport error | `unavailable` | 0 | none |
| Timeout | `timeout` | 0 | none |
| Malformed response | `ok` (or `error`) | 0 / dropped | none |
| Isolation / cross-tenant / provenance / credential violation | `isolation_violation` / `error` | 0 | hard failure, reported |

**Fail open on availability, fail closed on isolation.** Availability problems
are returned as values (never raised), so a caller proceeds on the existing
context path; existing Design DNA retrieval continues working unchanged; FAST
receives no fabricated context; no project lifecycle state changes; and no
automatic retry storm occurs (bounded retries only, ≤ 1 by default).

---

## 10. Test and mutation results

All commands run from `website-builder/` with the project venv.

| Suite | Result |
|---|---|
| Focused D4a (`test_openviking_library` 68 + `test_openviking_retrieval` 37 + `test_openviking_composition` 9) | **114 passed** |
| Full default offline suite (network blocked at the Python level) | **3866 passed, 2 skipped, 4 deselected, 60 subtests passed** |
| D3a.5/D3b regression subset (20 files) | **1029 passed, 1 skipped, 19 subtests passed** |
| D4a mutation driver | **all 17 guards killed** |
| D3b mutation driver | all 14 guards killed |
| D3a.5 mutation drivers | all 16 / 73 / 39 / 36 / 18 guards killed |

The default suite stays offline and deterministic: the four network-needing
tests carry the repo's `integration` marker and are deselected by
`addopts = "-m 'not integration'"`. The D4a proof re-runs the whole suite with
non-loopback network blocked, proving both "green" and "offline" in one run.

### Deterministic test coverage (D4a, 114 tests)

* correct scoped retrieval; per-project isolation; category filtering;
* retrieval budget enforcement (count, bytes, tokens; ceiling clamps);
* source provenance (all record fields; revision advancement on change);
* L0/L1/L2 loading policy;
* duplicate ingestion (idempotent) and changed-source revision;
* forbidden-source skip (secrets/logs/deps/executables), oversize, unreadable;
* timeout, outage, malformed response (all fail open, zero items);
* cross-project / cross-tenant / traversing-URI / missing-provenance fail-closed;
* prompt injection in retrieved documents round-trips as inert DATA;
* credential leakage prevention (content never echoed into result/summary);
* disabled-feature fallback (no backend call, zero items);
* no dependency or toolchain mutation; no lifecycle/publication side effect;
* composition root exposes the adapter, disabled by default, not wired into
  FRONTEND/FAST/QA.

### Mutation coverage (17 guards, all killed)

```
[KILLED] a malformed or foreign-scheme URI is refused
[KILLED] scope containment is exact, not a string prefix
[KILLED] the OpenViking feature flag defaults to disabled
[KILLED] a disabled adapter retrieves nothing
[KILLED] a malformed match is dropped
[KILLED] a retrieved URI outside the project scope fails closed
[KILLED] an item with no provenance fails closed
[KILLED] a cross-tenant record fails closed
[KILLED] credential-shaped content fails closed
[KILLED] an out-of-scope category is filtered
[KILLED] the result-count budget is enforced
[KILLED] the context byte budget is enforced
[KILLED] the context token budget is enforced
[KILLED] a forbidden source path is never ingested
[KILLED] re-ingesting an unchanged source is a no-op
[KILLED] an oversize source is refused
[KILLED] L2 detail is loaded only when justified
```

---

## 11. Live qualification evidence

**Live qualification status: BLOCKED.** No real OpenViking server was
provisioned, because doing so requires an embedding/VLM provider that would
incur cost (or local weight downloads, which were withheld). No live retrieval
against a real corpus is claimed.

What *was* executed against the **real HTTP transport path** (bounded, honest):

| Probe | Result |
|---|---|
| `HttpOpenVikingBackend` against `http://localhost:1933` (no server running) | `status=unavailable`, `items=0`, `reason=BACKEND_FAILURE`, **no raise** — the real transport fails open |
| Live write path (`put_resource`/`get_record` on the HTTP backend) | raises `OpenVikingLiveNotQualified` — live ingestion is not qualified and refuses rather than pretending |

No measured live latency or live resource consumption is reported, because no
live server ran. The adapter's correctness is established against the
deterministic backend and the real HTTP *client* path; the *server* path is not
qualified.

---

## 12. Remaining limitations

* **Live retrieval is not qualified.** The adapter is code-accepted against a
  deterministic backend and a real HTTP client, but no real server was
  provisioned. Enabling the flag in production requires a live qualification
  pass first (§13).
* **Live ingestion is deliberately refused.** `HttpOpenVikingBackend` raises
  `OpenVikingLiveNotQualified` for writes. Live ingestion (and its duplicate /
  revision behaviour against a real server) is unqualified.
* **No rerank / intent analysis.** D4a uses single-query `find` only. The
  richer `search` path (LLM intent analysis + rerank) is unused, so no
  `query_planner`/rerank provider is required.
* **No L2 semantic processing.** Because no server ran, no real L0/L1 sidecars
  were generated; the level policy is exercised against the fake backend.
* **The token estimate is a 4-chars/token approximation**, not a real
  tokenizer — it is a safety bound, not a billing figure.
* **Retrieval is not yet consumed by anything.** This is intentional: Laya
  (D4b) is the consumer. Until D4b, the adapter is a tested seam, not a live
  data flow.

---

## 13. D4b integration handoff contract

The stable, versioned contract Laya consumes. Bump `RETRIEVAL_CONTRACT_VERSION`
(and `LIBRARY_SCHEMA_VERSION` on a shape change) when either changes.

```python
from app.core.openviking_retrieval import (
    OpenVikingConfig, OpenVikingRetrievalAdapter, RetrievalBudget,
)
# The runtime already exposes a configured adapter at
# RuntimeComposition.openviking (disabled by default).

result = adapter.retrieve_context(
    query=...,            # str, bounded lexical query
    project_id=...,       # validated project id
    scope=("design_dna", "motion"),   # category subset, or "library"
    budget=RetrievalBudget(max_items=8, max_bytes=32768, max_tokens=8000,
                           allow_detail=False),
)
# result.status in {ok, disabled, unavailable, timeout, error, isolation_violation}
# result.items  -> tuple[ContextItem]  (uri, source_revision, trust, score,
#                                       level, title, body, summary, tokens)
# result.available / result.ok / result.truncated / result.error_reason
```

**Laya MAY:**

* request relevant context (bounded `retrieve_context`);
* filter or summarize retrieved items;
* prepare a bounded context pack;
* attach provenance (`uri`, `source_revision`, `trust`) and uncertainty
  metadata (status, truncation, degraded) to what it forwards.

**Laya MUST NOT:**

* change authoritative requirements;
* override FAST;
* authorize new dependencies;
* mutate project state;
* trigger deployment.

**D4a guarantees for D4b:**

* retrieval is scoped to one project; cross-project/tenant leakage fails closed;
* retrieved text is DATA with no authority-bearing field;
* a disabled/unavailable OpenViking never blocks the existing pipeline and never
  fabricates context;
* D4a creates **no** second FAST orchestration path and does not integrate
  retrieval into FRONTEND or QA.

**Open question for D4b:** the context pack Laya assembles should reuse D1's
`payload_chars`-style single size function for its own budget, exactly as
`design_context.py` does, so a bound that holds downstream is the bound that
held upstream.

---

## 14. Files changed

New:

* `app/core/openviking_library.py` — schema, ingestion, backend protocol,
  deterministic fake backend, credential-shape detection, URI isolation.
* `app/core/openviking_retrieval.py` — the retrieval adapter, budgets, feature
  flag, live HTTP backend, contract version.
* `tests/test_openviking_library.py` — 68 tests.
* `tests/test_openviking_retrieval.py` — 37 tests.
* `tests/test_openviking_composition.py` — 9 tests.
* `tools/mutation_check_d4a.py` — 17-guard mutation driver.
* `tools/d4a_final_proof.py` — the D4a acceptance runner.
* `docs/D4A_OPENVIKING_FOUNDATION_ACCEPTANCE.md` — this document.

Modified:

* `app/runtime.py` — `RuntimeConfig.openviking_config`; `compose()` builds the
  adapter; `RuntimeComposition.openviking`. No other behaviour changed.
* `config/default.yaml` — the disabled-by-default `openviking` block.

Nothing outside `website-builder/` was changed. The FAST contract, Design DNA,
the D1/D2/D3a.5/D3b modules, and `feature/website` are untouched.

---

## 15. Final verdict

```
D4A_CODE_ACCEPTED_LIVE_BLOCKED
```

The implementation, the focused tests (114), the full offline suite (3866), the
D3a.5/D3b regressions (1029), and all mutation drivers (D4a 17, D3b 14,
D3a.5 16/73/39/36/18) pass. The D4a acceptance runner (`tools/d4a_final_proof.py`)
reports `VERDICT: PASS` for the code battery.

Full acceptance (`D4A_FULLY_ACCEPTED`) is **withheld** because no live
OpenViking server was provisioned and no live retrieval was executed — per the
mission, full acceptance is not claimed without executed live qualification.
