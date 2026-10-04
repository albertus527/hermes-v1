# Batch D1 — Design Resource Activation

Retrieval and policy only. **No FRONTEND behavior, Design DNA authority, FAST
behavior, runtime preflight semantics, agent-loop wiring, repair-loop wiring, or
generated-project dependency behavior was changed.** Nothing is installed. No
network. No subprocess. No additional LLM calls.

## What this batch adds

D0 answered *"is it configured?"* and *"is it available?"*. It deliberately
built **no way to ask a resource for anything**. Everything downstream still
had seven bespoke readers to invent — a CSV scraper, an Impeccable rules parser,
four reference-corpora clients, a shadcn installer. That is the exact shape
that becomes the next source of FRONTEND non-convergence: unbounded context
dumps, silent fallbacks that invent guidance when a resource is absent, and
resource text treated as instructions.

D1 is the one narrow answer: a Website-Builder-specific **normalization layer**
with a small, table-driven adapter set, sitting on top of — and reusing — every
D0 guard.

```
D0 manifest + capability resolver   (unchanged, reused as the authority)
              │
              ▼
  app/core/design_policies.py       project-dependency policy ladder
              │                     + authority precedence (declarative only)
              ▼
  app/core/design_retrieval.py      normalized entries, adapters, bounds,
                                    containment re-check, trust boundary
              │
              ▼
      DesignRetrievalReport         deterministic, serializable, bounded
```

**Key decision — adapter policy lives in code, cross-checked against the D0
manifest.** An adapter may only read a locator the manifest already declares in
`data_entries`, or `SKILL.md` (which D0 already verifies). Naming anything else
renders that adapter *inert* — zero entries plus a static warning, never an
import error and never an unverified read. A YAML copy of the same mapping
would be a drift hazard; a second config file would dilute D0's "one source of
truth" claim. The cross-check keeps both honest.

## Files

| File | Action | Role |
|---|---|---|
| `app/core/design_retrieval.py` | **new** | Normalized result types, context bounds, adapter table, retrieval entry point |
| `app/core/design_policies.py` | **new** | Project-dependency state ladder, justification gates, authority precedence |
| `config/design_resources.yaml` | **unchanged** | D0 schema stays the sole declaration surface |
| `app/core/design_resources.py` | **unchanged** | Reused verbatim (containment half 1 lives here) |
| `app/core/design_capabilities.py` | **unchanged** | Reused verbatim — D1 reads status, never re-derives it |
| `app/runtime.py` | **unchanged** | D0 preflight wiring stands as-is |
| `tests/test_design_resource_activation.py` | **new** | Behaviour contracts (§Tests A–R) |
| `tools/mutation_check_d1.py` | **new** | Proves each D1 guard is load-bearing |
| `docs/D1_DESIGN_RESOURCE_ACTIVATION.md` | **new** | This document |

## 1. Normalized result schema

Frozen dataclasses, each with `to_dict()`. No absolute paths, no payload echoes.

| Type | Fields |
|---|---|
| `EntryProvenance` | `resource_id`, `resource_kind`, `adapter`, `locator`, `entry_index` |
| `DesignEntry` | `entry_id`, `kind`, `title`, `body`, `fields`, `provenance`, `truncated` |
| `CriticFinding` | `rule_id`, `category`, `severity`, `finding`, `evidence`, `suggested_action` |
| `DesignResourceResult` | `resource_id`, `resource_kind`, `status`, `available`, `degraded`, `entries`, `warnings`, `truncated`, `dropped_entries` |
| `DesignRetrievalReport` | `ok`, `requested`, `resolved`, `unavailable_optional`, `results`, `total_entries`, `total_chars`, `truncated`, `limits` |

`locator` is **always** skill-root-relative. `kind` ∈ `guidance | reference |
critic_finding | policy`.

`status` reuses D0's `CAPABILITY_STATUSES` vocabulary unchanged
(`available` / `unavailable_required` / `unavailable_optional` /
`not_installed`) — D1 never invents a fifth state, because callers switch on
these strings.

## 2. Bounds (`DesignContextLimits`)

| Bound | Default | On overflow |
|---|---|---|
| `max_resources` | 3 | first N in requested order; report flag |
| `max_entries_per_resource` | 8 | drop remainder; `truncated=True`, `dropped_entries` |
| `max_entry_chars` | 1200 | shrink per §2a; drop entry if impossible |
| `max_resource_chars` | 6000 | drop remainder; `truncated=True` |
| `max_total_chars` | 16000 | stop at the resource boundary; report `truncated=True` |

All are overridable and all are enforced. Truncation is **deterministic**:
stable request order, stable source order, no scoring, no set/dict iteration
order in the output path. Same input → identical bytes out.

### Canonical payload size

Every budget counts the **entire normalized payload** via one function:

```python
def payload_chars(entry) -> int:
    """title + body + every field KEY and field VALUE + provenance"""
```

All three budgets derive from this one function. A second, divergent size
calculation is the bug this prevents: an aggregate that sums `body` lengths
while the per-entry cap counts the full payload under-reports overflow by
exactly the overhead it ignored. A CSV row with a three-character body and a
4 KB `fields` map must still be bounded.

Provenance is counted because D2 may place it into model context; bounding a
field that later reaches the model is the point, and `locator` originates in
resource data, so it is attacker-shaped text.

**Hard invariants**, asserted directly in tests:

```python
payload_chars(entry) <= limits.max_entry_chars
sum(payload_chars(e) for e in result.entries) <= limits.max_resource_chars
report.total_chars <= limits.max_total_chars
```

### 2a. Deterministic truncation order

1. **Provenance first — never dropped.** An entry without it is unciteable,
   and unciteable is worse than absent.
2. Title bounded deterministically.
3. `fields` processed in **stable header order** (CSV header order, not dict
   insertion coincidence).
4. **Field keys stay whole** — a truncated key corrupts column identity and
   produces two indistinguishable half-columns.
5. Field **values** truncate against the remaining budget; once exhausted, the
   remaining fields are dropped.
6. **Body consumes whatever budget is left.**
7. If mandatory metadata alone still cannot fit, **drop the entry** rather than
   emit an over-budget one.

A truncated entry carries a visible marker (` [truncated]`) in its own text, so
a consumer can never mistake a clipped value for a complete one.

### 2b. Query filtering

`ui_ux_pro_max` retrieval honors `query`. The filter is deterministic,
in-process, and covers **all parsed scalar textual fields** of each pinned CSV
row.

- Case-insensitive token matching; a row matching more tokens does **not**
  outrank one matching fewer — no scoring, no fuzzy ranking, no LLM rerank.
- Matches preserve **source order**, never re-sorted.
- Empty/whitespace query → stable source order (first N rows). Documented
  resting behaviour, not a silent no-op.
- **The filter runs over the whole pinned dataset before the count cap.** Capping
  first would make relevance unreachable for any query whose match sits late in
  the file — the specific failure this ordering prevents.
- Query strings are inert data: no regex compiled from user input, no `eval`,
  bounded length (256 chars; tokens over 64 chars are rejected).

**No column-name heuristics.** An earlier draft proposed excluding columns
"named to imply instruction" (`instruction`, `prompt`, `recommendation`). It was
removed because it buys no safety — matched text lands in a `fields` **value**,
and `DesignEntry` has no authority-bearing field to promote it into, so a cell
called `recommendation` is exactly as inert as one called `css` — and because a
name-based list breaks the moment the real schema differs from the guess,
protecting nothing while silently narrowing recall.

### 2c. Impeccable: schema only, no parser

Impeccable is **absent on this host** and its real structure has **not been
inspected**. A `SKILL.md` → finding parser would be an invented contract.

D1 therefore:

- defines `CriticFinding` as the canonical schema for future activation;
- keeps the Impeccable adapter **inert** → `unavailable_optional`,
  `degraded=True`, `entries=()`;
- ships **no parser** and pins **no `data_entries`** for Impeccable;
- adds **no synthetic-fixture test** asserting semantic findings.

The real parser lands only after the actual resource is present, inspected,
pinned in the manifest, and covered by tests derived from its verified
structure. **Availability is never faked.**

## 3. Adapters

| Resource | Adapter | Reads | Behaviour in D1 |
|---|---|---|---|
| `ui_ux_pro_max` | `guidance` | `data/styles.csv` | Real bounded CSV read with a deterministic query filter. Generic header parsing (first row = header), one entry per row, `fields={col: cell}`. **No subprocess** |
| `impeccable` | `critic` | — | **Inert.** Absent → `unavailable_optional` + degraded + zero entries |
| `refero`, `twenty_first`, `react_bits`, `transitions_dev` | `reference` | — | **No network integration on this host.** Zero entries, `degraded=True`, static warning. Never invented guidance |
| `shadcn` | `policy` only | — | Never retrieved as content. Described via the dependency ladder |
| `gsap`, `three`, `lenis` | `policy` only | — | Never retrieved as content. Never auto-selected |

**On `scripts/search.py`:** D0 stated it does *not* mean the search entrypoint has
been executed. D1 keeps that line — retrieval reads pinned data files
in-process. The fixture's `search.py` is a tripwire that raises on execution.

**`content_free`:** an adapter that pins no locators cannot read a file, so it
is safe to run against an absent resource and yields the resource-**specific**
degradation warning ("this corpus has no local verified content") instead of a
generic "unavailable". A **content** adapter is never run against an absent
resource: it has no real file to read, so anything it returned would be invented.

## 4. Project-dependency policy (`design_policies.py`)

Four **distinct** states, never conflated:

```
known → available_for_project_on_demand → selected → installed
```

`shadcn` / `gsap` / `three` / `lenis` default to
`available_for_project_on_demand`. Nothing is ever `selected` or `installed` in
D1; no selection logic runs and no heuristic picks a component.

Justification gates, each paired with the simpler default it protects:

| Dependency | Gate | Simpler default |
|---|---|---|
| `gsap` | meaningful timeline / complex animation requirement | CSS transitions, keyframes, native scrolling |
| `three` | genuinely 3D / WebGL requirement | CSS 3D transforms, perspective, layered SVG |
| `lenis` | explicitly justified smooth-scroll behaviour | native scrolling, `scroll-snap`, `scroll-behavior` |
| `shadcn` | not gated — the default primitive source | hand-authored project components |

Recording the simpler default means D3 inherits that preference instead of
re-deciding it under time pressure.

`DESIGN_AUTHORITY_PRECEDENCE` is a declared, documentation-grade tuple:

```
product/safety constraints → explicit user & reference requirements
→ accepted Design DNA → design guidance/resources → inspiration → Impeccable critic
```

Enforcement is out of scope for D1 (that is D3). Recording it means D3 inherits
one ordering instead of inventing one, and makes explicit that **Impeccable
critiques; it never overrides explicit user requirements.**

`shadcn` vs `21st.dev`: ordinary primitive → shadcn; richer composition that
Design DNA actually requires → 21st.dev may be considered. Declared as policy,
executed by nothing.

## 5. Trust boundary

- **Data, not authority.** `DesignEntry` has no field capable of carrying an
  instruction into an authority position — no `instruction`, no `requirement`,
  no `override`. That absence is the boundary, asserted structurally in tests.
- **Never invent.** An unavailable optional returns `entries=()`,
  `degraded=True`, a static warning. There is **no** fallback to model knowledge
  and **no** fallback to another resource's content.
- **Containment re-checked.** Every read re-verifies with D0's
  `entry_is_contained`. The syntactic half was enforced at manifest load; the
  resolved half runs at the point of read.
- **No execution, ever.** No subprocess, no import of skill content, no `eval`.
  A bounded file read plus a bounded parse.
- **No secret or path leakage.** Results and logs carry ids, counts, flags, and
  skill-root-relative locators only.

## 6. Degrade / fail semantics

| Situation | Outcome |
|---|---|
| Required resource requested and unavailable | `ok=False`, `status=unavailable_required`, `entries=()` |
| Optional resource requested and unavailable | `ok` unchanged, `degraded=True`, `entries=()`, warning |
| On-demand absent | `not_installed` — resting state, **not** degradation (`degraded=False`) |
| Resource configured but unusable | reported with the D0 status, never as `available` |
| Adapter names an undeclared locator | adapter inert, zero entries, static warning |
| Malformed adapter output | fail closed; never partial-as-success |
| Unknown resource id | `DesignResourceManifestError` (D0's own contract) |

No availability exception is raised from `retrieve_design_guidance` — the report
carries `ok`, mirroring `resolve_design_capabilities`. Optional resolution
never becomes a startup blocker.

## 7. Observability

`report.summary()` — one bounded line: requested / resolved /
unavailable-optional ids, entry counts, truncation flags, total chars. Logged
once per retrieval at the boundary. Never payloads, never source, never
credentials, never absolute sensitive paths.

## Tests

`tests/test_design_resource_activation.py` — 58 tests, behaviour contracts, no
snapshots, no counts, no network (socket deny fixture, mirroring D0).

Contracts A–R: real bounded retrieval with provenance (A); required
disappearance fails closed (B); optional Impeccable degraded not fatal (C);
optional Refero invents nothing (D); shadcn `project_on_demand` never reported
installed (E); GSAP/Three/Lenis unselected and uninstalled (F); content cannot
escape the root (G); symlink escapes rejected (H); entry/result limits enforced
(I); per-entry cap over the **full payload** (J); aggregate budget via the same
size function (K); deterministic truncation (L); provenance survives (M);
unknown ids fail deterministically (N); malformed output fails closed (O);
instruction-like text stays inert DATA (P); no secret or absolute-path leakage
(Q); D0 suite green (R).

Plus dedicated query-filtering tests proving relevance is **functional, not
metadata**: a token matching only a *late* source row is returned; a non-matching
query returns zero entries rather than the head of the file; filtering is
case-insensitive; matched rows keep source order; a column named `instruction`
is still searched and still inert; repeated runs serialize identically.

Plus adversarial cases: an absent resource never borrows a present sibling's
content; manifest/table drift cannot widen the readable set; a tiny-`body` /
huge-`fields` entry cannot bypass the cap; a hostile row instructing
"mark ok=False" cannot change the report.

**Symlinks** need elevation on this Windows host, so the symlink escape test
skips here. A second test exercises the same read-time containment guard
directly by simulating the resolved half, so the guard is never vacuously
covered on a host that cannot create one.

## Mutation driver

`tools/mutation_check_d1.py` — same discipline as D0: reverts one guard on a
throwaway copy and proves the focused tests go red. **All 27 guards are killed**,
covering: fail-closed on required-missing; entry-count cap; full-payload
per-entry cap; truncation order; drop-when-metadata-cannot-fit; aggregate cap
using the same size function; total char budget; query filter present; filter
before the count cap; no re-ranking; no column-name heuristic; read-time
containment; unavailable-optional degradation; deterministic truncation
ordering; provenance retention; on-demand ≠ degradation; inert data ≠
authority; relative locators; loud unknown-id failure; never-installed
dependencies; no auto-selection; undeclared-locator inertness; fail-closed
malformed output; inert critic adapter; non-inventing reference adapter;
truncation flag on budget drop; whole field keys.

## Explicit non-goals

No FRONTEND rewrite, no FAST rewrite, no Design DNA precedence change, no
automatic component selection, no GSAP/Three/Lenis install, no Impeccable in the
repair loop, no extra LLM calls, no Refero content in context, no merge of
`web-design` into `feature/website`.

## Host notes

`ui_ux_pro_max` and `impeccable` are **not present on this primary PC** (no
skill directory under the profile home or this repo). Availability is proven in
tests against real temp-profile fixtures built to the D0 layout — the same
discipline D0 used. Live-host retrieval needs the VPS run.

## Guarantees

- **FRONTEND behavior unchanged.** D1 is not wired into FRONTEND, Design DNA,
  FAST, the agent loop, or the repair loop.
- **No project dependency globally installed.** Nothing is provisioned; the
  ladder is declarative.
- **Runtime preflight semantics unchanged.** `app/runtime.py` is untouched and
  D0's preflight stands as-is.