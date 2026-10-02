# Stream Frame Shape Diagnostics (p19 false-progress forensics)

## Goal

Prove **empirically, on the next FRONTEND run**, which post-provider-close frame shape is
emitted as `STREAM / active / advance=true` and keeps p19 alive. Diagnostics only — the
classification policy, the timeouts, and convergence behaviour are all unchanged by this work.

Out of scope (explicit): timeout policy, convergence guard, canonical-URL code, Batch D.

---

## 1. Investigation findings (current code)

**F1 — the discriminator is a `choices`-keyed denylist.**
`agent/chat_completion_helpers.py:673` `_openai_chunk_advances()`:

```python
choices = getattr(chunk, "choices", None)
if choices: return True
if getattr(chunk, "usage", None): return False
return True
```

Two content-free shapes are therefore **not** demoted and both emit `advance=true`:

- `choices=[]` (or absent) with no `usage` → falls through to `True`.
- `choices=[{delta:{content:""}}]` (empty-content keep-alive frame) → `True` at `if choices`.

Either is a provider keep-alive that reads as forward progress forever.

**F2 — only three emit sites produce `receiving stream response`.**
`chat_completion_helpers.py:4150` (`chat_completions`), `chat_completion_helpers.py:4679`
(`anthropic_messages`), `agent/codex_runtime.py:1655` (`codex_responses`). Each already passes an
explicit `advance=` from its own discriminator. The MODEL heartbeats that were correctly
`advance=false` come from separate call sites (`progress_events.py:117` `_KEEPALIVE_PREFIX_RULES`).

**F3 — no local iterator can fabricate frames.**
`_iter_provider_stream_chunks` (`chat_completion_helpers.py:332`) is a bare `yield from`.
In the non-Relay path `ManagedLlmStream.__next__` (`agent/relay_llm.py:656`) drains
`iter(raw_stream)` and raises `StopIteration` at exhaustion; the Relay fallback
`_preserve_pending_provider_chunks` (`agent/relay_llm.py:735`) switches to a **finite** list.
⇒ continuing events mean the SDK iterator really is still yielding frames; the *classifier* is
what mislabels them. That matches the reported evidence.

**F4 — transport constraints that decide the design.**
`hermes_cli/oneshot.py:389` disables logging for the whole call tree and `:424` redirects
stdout **and** stderr to devnull, so the FRONTEND child cannot report through `agent.log` or a
stderr line. `progress.jsonl` is the only child→supervisor channel; the supervisor unlinks it
(`website-builder/app/hermes/watchdog.py:1360`) and the adapter `rmdir`s `runs/`
(`website-builder/app/hermes/adapter.py:684`). The durable sink is the per-project receipt under
`~/.hermes-website/diagnostics/<project_id>/` (`adapter.py:637`, built unconditionally at
`watchdog.py:1374`).

**F5 — coalescing makes counts lower bounds, not attribution wrong.**
`hermes_cli/oneshot.py:272` drops any second `active` event within 5s. The **newest** event wins,
so the shape recorded on a retained line is the shape that was actually firing.

---

## 2. Decisions (settled)

- **D1 — Transport:** extend the existing progress channel with one optional bounded
  `stream_diag` object on `STREAM` events; the supervisor aggregates it into the persisted
  receipt. No new env var, no new file, no second channel.
- **D2 — Scope:** full 8-way OpenAI taxonomy, plus `api_mode` and the frame-shape string on all
  three emit sites (Anthropic/Codex reuse the event type they already read — no new
  classification logic, no policy change).
- **D3 — Two frame fingerprints:** `rep` (this frame is the *same Python object* as the previous
  one → a local re-emitter) and `map` (the frame was a Mapping, not an attribute object → the
  `getattr`-on-dict hole, which classifies everything as advancing).

---

## 3. Design

### 3.1 New module `agent/stream_shapes.py` (pure, total, I/O-free)

```python
SHAPE_TEXT_DELTA      = "text_delta"        # non-empty content delta
SHAPE_REASONING_DELTA = "reasoning_delta"   # non-empty reasoning_content/reasoning delta
SHAPE_TOOL_CALL_DELTA = "tool_call_delta"   # delta.tool_calls present
SHAPE_FINISH_ONLY     = "finish_only"       # choices present, finish_reason set, no delta payload
SHAPE_EMPTY_DELTA     = "empty_delta"       # empty choices, or delta with nothing in it
SHAPE_USAGE_ONLY      = "usage_only"        # no choices, usage set
SHAPE_ERROR_SHAPE     = "error_shape"       # error_type/error_message/error payload
SHAPE_UNKNOWN         = "unknown"           # unreadable or unrecognised
```

Public surface:

- `classify_openai_chunk(chunk) -> str` — reads attributes **or** mapping keys (a Mapping frame
  must classify, not blow up). Never `repr()`/`str()`/hash the payload; presence checks only.
  Precedence: error → usage-only → choices-bearing (tool > reasoning > text > finish-only >
  empty) → empty-choices → unknown.
- `classify_anthropic_event(event_type) -> str` / `classify_codex_event(event_type) -> str` —
  pass the **already-read** bounded type string through, normalized to a bounded enum
  (e.g. `a:content_block_delta`, `c:response.in_progress`, `c:unreadable`); no payload access.
- `advance_for_shape(shape) -> bool` — **the current behaviour, unchanged**: only
  `usage_only` is `False`, everything else (including `unknown`) is `True`. `_openai_chunk_advances`
  becomes a thin wrapper over `classify_openai_chunk` + `advance_for_shape`, and
  `test_streaming.py::test_openai_chunk_advance_is_a_denylist` must still pass verbatim.
- `build_stream_diag(*, mode, shape, frame_index, stream_seconds, repeat_frame, mapping_frame,
  fields) -> Dict[str, Any]` — fixed keys, bounded scalars only.

Wire form (≈130 bytes, inside the existing `_PROGRESS_MAX_EVENT_BYTES = 1024`):

```json
"stream_diag": {"mode":"chat_completions","shape":"empty_delta","n":1234,"t":61.2,
                "rep":false,"map":false,
                "d":{"content":true,"reasoning":false,"tool":false,"finish":false}}
```

- `mode` — `agent.api_mode` (bounded string, default `""`).
- `shape` — one of the 8 constants.
- `n` — monotonic per-attempt frame index (`_diag["chunks"]` pre-increment).
- `t` — seconds since this attempt opened, 1 decimal.
- `rep` / `map` — the D3 fingerprints.
- `d` — delta **field-presence** booleans + `finish_reason` presence. Presence only: never a
  length, never a value.

### 3.2 Emit sites (advance values unchanged)

- `chat_completion_helpers.py:4149` — compute the diag once per frame, pass
  `advance=_openai_chunk_advances(chunk)` **and** `stream_diag=...` to `_touch_activity`.
  Frame counter = `_diag["chunks"]`; elapsed = `now - _diag["started_at"]`; keep one local
  `_prev_frame` for `rep`. The existing `_diag["chunks"] += 1` below stays as-is.
- `chat_completion_helpers.py:4679` — same, `shape=classify_anthropic_event(_event_type)`.
- `codex_runtime.py:1655` — same, `shape=classify_codex_event(_event_field(event, "type", ""))`,
  with a closure frame counter.

### 3.3 Payload plumbing (additive, optional)

- `agent/progress_events.py:167` `make_progress_payload(..., stream_diag=None)` — adds the
  `stream_diag` key **only when a dict is supplied**; every other caller is byte-identical.
- `run_agent.py:4119` — `_touch_activity(..., stream_diag=None)` forwards it inside the existing
  `try/except` that already guards `progress_callback`.
- `hermes_cli/oneshot.py:263` — `_emit(..., stream_diag=None)` and `:313` `on_progress` read
  `payload.get("stream_diag")`. Serialized inside the existing byte cap; a non-dict is dropped.

### 3.4 Supervisor aggregation (`website-builder/app/hermes/watchdog.py`)

- `FrontendInvocation` (`:330`) additive fields: `stream_shape_counts`, `stream_shape_advance_counts`,
  `api_mode_counts`, `repeat_frame_count`, `mapping_frame_count`, `last_stream_shape`,
  `last_stream_shape_offset_seconds`, `stream_shape_first_offset` / `..._last_offset`.
  New bounds: `MAX_STREAM_SHAPE_KEYS = 12`, `MAX_API_MODE_KEYS = 4`, each with a `*_truncated` flag.
- `note_event_detail` (`:423`) takes the validated diag and folds it in **after** the liveness
  decision — same discipline as the existing counters. Validation is total: a missing, non-dict,
  over-long, or wrong-typed `stream_diag` is ignored; it can never raise into the poll loop.
- `_read_progress_events` (`:931`) passes `event.get("stream_diag")` through; absent stays absent
  (an older child keeps working).
- `_build_receipt` (`:1065`) adds a `stream_frames` block:
  `{shapes, advancing_by_shape, api_modes, repeat_frame_count, mapping_frame_count,
    last_shape, first_offset_seconds, last_offset_seconds, ..._truncated}`.
- Bump `FORENSICS_SCHEMA` → `frontend_forensics/3` (`:200`), mirroring the v1→v2 precedent, and
  update the `/2` mentions in `app/projects/build.py:304` and `app/projects/revise.py:92`.

### 3.5 Reading the result

| receipt evidence | conclusion |
|---|---|
| `shapes.empty_delta` dominant, `rep=false`, `n` and `t` both large | provider keep-alive frames on an open connection — F1's first hole |
| `rep=true` on any line | a **local** emitter is re-yielding a cached frame — F3 needs revisiting |
| `map=true` | frames arrive as Mappings, so `getattr` misses and *everything* advances |
| `shapes.text_delta` dominant, `t` large | the provider really was producing output — the "streaming had stopped" premise needs re-examination |

---

## 4. Tasks (ordered)

1. **`agent/stream_shapes.py`** — the 8 constants, `classify_openai_chunk`,
   `classify_anthropic_event`, `classify_codex_event`, `advance_for_shape`, `build_stream_diag`.
   No imports beyond `typing`/`collections.abc`.
2. **Classifier tests** — `tests/run_agent/test_streaming.py`: parametrized taxonomy (one case per
   category incl. `choices=[]`, `delta.content=""`, `finish_reason`-only, `usage`-only,
   `error_type`, `object()`, `None`, a `dict`-shaped chunk), plus the **parity** test
   `advance_for_shape(classify_openai_chunk(c)) is _openai_chunk_advances(c)` over the same
   battery, plus a totality test over hostile inputs (`MagicMock`, dict, `None`, raising
   `__getattr__`). Assert no payload value appears in any produced string.
3. **chat_completions emit site** — `chat_completion_helpers.py:4144-4152` (+ `_prev_frame`).
4. **Anthropic emit site** — `chat_completion_helpers.py:4672-4682`.
5. **Codex emit site** — `codex_runtime.py:1652-1658`.
6. **Plumbing** — `progress_events.py:167`, `run_agent.py:4119`, `oneshot.py:263/313`.
7. **Supervisor** — `watchdog.py` `:330` fields, `:423` fold, `:931` read, `:1065` receipt,
   `:200` schema bump, docstrings in the module header + `build.py` + `revise.py`.
8. **Supervisor/progress tests** — `website-builder/tests/test_frontend_watchdog.py`: a
   shape-carrying line lands in `stream_frames`; malformed/oversized/absent `stream_diag` is
   ignored and changes no bound; counts stay lower bounds; `tests/run_agent/test_progress_channel.py`:
   the payload carries `stream_diag` only when supplied, and the serialized line stays under the cap.
9. **Validation** — see §5.

## 5. Validation

```bash
scripts/run_tests.sh tests/run_agent/test_streaming.py tests/run_agent/test_progress_channel.py -q
scripts/run_tests.sh website-builder/tests/test_frontend_watchdog.py -q
```

Then one real FRONTEND invocation and read
`~/.hermes-website/diagnostics/<project_id>/<invocation_id>.json` → `stream_frames` (the progress
JSONL is already unlinked by then, which is the point).

## 6. Invariants this change must preserve

- `_openai_chunk_advances`, `_anthropic_event_advances`, `_codex_event_advances` produce
  **identical booleans** for every input (enforced by the parity test).
- No bound, clock, prompt, toolset, skill, convergence rule, or user-facing reply reads the new
  fields. They are observation only.
- Nothing new is logged: no content, reasoning, tool arguments, prompts, URLs, payload bodies, or
  credentials. Shapes are fixed enums; `d` is presence booleans only.
- Hot-path cost stays a handful of attribute reads per frame — no `repr`, no hashing, no I/O.
- No new env var, no new file, no new dependency.