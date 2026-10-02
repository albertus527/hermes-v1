# FRONTEND watchdog: separate observable activity from forward progress

Scope: `website-builder/app/hermes/watchdog.py`, `agent/progress_events.py`,
`hermes_cli/oneshot.py`, `run_agent.py::_touch_activity`, the three streaming
emitters, and two tests. No canonical-URL code. No p18 retry. No Batch D.

---

## 1. Findings — the exact emitters

### 1.1 `"receiving stream response"` is emitted per *stream item*, before any content check

It is **not** the poll heartbeat. It fires from three iteration loops, and in
all three the touch happens **above** the loop body that inspects what arrived:

| Path | Emitter | What "one event" actually is |
|---|---|---|
| `chat_completions` | `agent/chat_completion_helpers.py:4088` (inside `for chunk in _iter_provider_stream_chunks(...)`, started at 4083) | Any SDK chunk object — including `chunk.choices == []`, which the loop itself handles at 4136-4149 for usage-only / error-shape chunks. The touch is at 4088, **before** that branch. |
| `anthropic_messages` | `agent/chat_completion_helpers.py:4612` (inside `for event in stream:`, 4609) | Any Anthropic stream event. The loop only handles `content_block_start` (4624) and `content_block_delta` (4632); **every other event falls through and has already touched activity** — including Anthropic `ping`. |
| `codex_responses` | `agent/codex_runtime.py:1619-1622`, `def _on_event(event)` | One call per raw SSE event, with **no type inspection at all**. |

So the answer to **(1)** is: neither pure polling nor pure tokens. It is
"a stream item arrived". For two of the three paths this provably includes
provider keep-alive frames, and this repo already says so:

- `agent/chat_completion_helpers.py:3777-3780` — *"last_chunk_time … the outer
  poll loop uses this to detect stale connections that keep receiving **SSE
  keep-alive pings but no actual data**."*
- `agent/chat_completion_helpers.py:1697-1698` — *"**Valid keepalive /
  in_progress frames refresh `_codex_stream_last_event_ts`** and should not be
  killed."* That timestamp is set in `codex_runtime._on_event`, i.e. the exact
  callback that emits `receiving stream response`.

Which of the three paths p18 ran is **not determinable from this repo** — the
FRONTEND role's `api_mode` is resolved at runtime from the configured provider
(`website-builder/app/hermes/adapter.py:925`), and the receipt records neither
provider nor api_mode. The receipt also cannot answer it retroactively. Treat
that as an open question, not a settled fact (§6).

### 1.2 `"waiting for stream response (Ns, no chunks yet)"` is a 30-second poll heartbeat

Single emitter: `agent/chat_completion_helpers.py:5169`, inside the poll loop
started at 5134 (`_HEARTBEAT_INTERVAL = 30.0`, line 5133). It is gated twice:

```
_hb_now - _last_heartbeat >= 30.0                  # at most once per 30s
...
if _waiting_secs >= 30:  _emit_wait_notice(...)     # NO _touch_activity at all
else:                      _touch_activity(...)      # line 5169
```

Two consequences that matter:

- It can fire **at most once per 30 seconds**, not once per 5. The user's "every
  ~5 seconds" is the `_PROGRESS_ACTIVE_COALESCE_S = 5.0` cadence
  (`hermes_cli/oneshot.py:244`), which applies only to `phase == "active"`
  (`oneshot.py:266-275`). `MODEL/started` is never coalesced. The p18 receipt's
  `recent_activity[].offset_seconds` will settle this in one read: a
  `waiting for stream response` entry must sit **≥30s** after its predecessor.
- The description is inverted: the branch that emits it is the one where a chunk
  **did** arrive within the last 30s. In the genuinely-silent branch the loop
  emits a display notice and touches nothing. This is why p18 shows the pair
  (`…no chunks yet` + `receiving stream response`) cycling.

### 1.3 Why `model_started = 69` vs `model_completed = 11`

`MODEL/started` is not a boundary — it is a *phase*, and three unrelated
descriptions map to it (`agent/progress_events.py:77-80`):

| Description | Emitter | Count per model call |
|---|---|---|
| `starting API call #N` | `agent/conversation_loop.py:2061` | 1 per loop iteration |
| `waiting for provider response (streaming)` | `chat_completion_helpers.py:3970` | 1 per stream attempt |
| `waiting for stream response (Ns, no chunks yet)` | `chat_completion_helpers.py:5169` | **≈1 per 30s for the whole streaming call** |

`MODEL/completed` is exactly one per successful call
(`agent/conversation_loop.py:4447`). At `elapsed = 1553.8s`, 11 completions,
≈141s/call → ≈4.7 heartbeats per call. `11 × (1 + 1 + 4.3) ≈ 69`. The ratio is
**fully explained by the heartbeat, with no cross-agent inflation** —
`progress_callback` is passed only at `hermes_cli/oneshot.py:652` and is *not*
propagated to `delegate_task` children (`tools/delegate_tool.py:2002` builds
child agents without it), so a multi-agent explanation is ruled out.

### 1.4 **(3) Yes — every accepted event refreshes `last_activity_at` unconditionally**

`FrontendInvocation.note_activity` (`website-builder/app/hermes/watchdog.py:346-369`)
sets `self.last_activity_at = now` for **every** accepted event, any kind, any
phase, `UNKNOWN` included. `is_in_flight` (429-438) then measures the
`max_single_operation_seconds` backstop from `no_progress_for(now)` — i.e. from
`last_activity_at`. A heartbeat therefore slides the protection window forward
forever.

### 1.5 **(4) Yes — one in-flight MODEL operation can be re-announced indefinitely**

`note_operation` (411-424) only re-arms when the *kind* changes:

```python
if phase == _PHASE_STARTED:
    if self.active_operation != kind:      # same kind -> no-op
```

So a repeated `MODEL/started` heartbeat leaves `active_operation == "MODEL"`
untouched, and per §1.4 it also refreshes the clock that measures the operation's
silence. The net effect: `FRONTEND_MAX_SINGLE_OPERATION_SECONDS` (900s) is
really "900s since the last event of *any* kind", not "900s of silence from the
operation".

### 1.6 **(5) No — this does not explain `test_timeout_leaves_no_orphan_tree`**

That test (`website-builder/tests/test_frontend_watchdog.py:1324-1399`) writes a
**static** two-line progress file before spawning, then drives a real clock with
`idle=1.0`, `max_single_operation=2.0`, `poll=0.1`. There is no heartbeat, no
stream, and no agent in it, so §1.4/§1.5 are not in play. The flake is a
**startup race inside the test's own 2-second budget**:

```
t=0.0  spawn child  ── interpreter boot ── Popen(2nd interpreter) ── write marker
t≈2.0  supervisor: no_progress >= 1.0 AND no_progress >= 2.0  ->  IDLE_TIMEOUT, kill tree
```

`supervise_frontend_run` returns, and the test immediately does
`int(grandchild_marker.read_text().strip())` (line 1386). If two CPython boots
have not finished inside 2.0s under full-suite parallel load, the marker does
not exist → `FileNotFoundError`. Secondary vector: on POSIX an unreaped
**zombie** grandchild still answers `os.kill(pid, 0)` (line 1388-1392), so
`_alive()` can report a survivor that is already dead.

Important compatibility fact: because the single `MODEL/started` line in that
test **is** genuine forward progress, the change in §3 leaves this test's
timing **identical**. Treating `MODEL/started` as non-advancing would cut the
kill to ~1.0s and make the flake *worse* — a constraint on the design, not a
detail.

---

## 2. The honest limit of this fix (read before approving)

The p18 receipt also shows `tool_started=25 / tool_completed=22` and
`model_completed=11`. Under the rule requested in §3 — *real tool lifecycle
progress and real model completion advance* — those **31 events are genuine
forward progress**. So:

- **If** p18's `stream_active=290` were ping/empty-chunk frames, this fix ends
  that run at ≈900s instead of 2700s. Not at 180s: `MODEL/started` claims
  `active_operation = "MODEL"`, so the idle branch (`no_progress >= idle AND
  not in_flight`, `watchdog.py:1228`) is suppressed until
  `max_single_operation_seconds` is also exceeded (§5.3). The 180s idle bound
  applies only to a keep-alive-only run with **no** operation claimed.
- **If** they were real content tokens, p18 was *active but not converging* (a
  model reading/searching/looping for 26 minutes, `design_dna.json` never
  written). No liveness rule can end that. It needs the convergence guard, which
  memory records as explicitly **out of scope** for this line of work
  (`frontend_diagnostics_scope`).

Nothing in the receipt discriminates the two. The change in §3 is also the
diagnostic that settles it: from the next run forward, `counters.advance` vs
`counters.keepalive` answers it in one read. **Do not re-run p18 to validate
this fix** — the fix is validated by the tests in §5.

---

## 3. Design

### 3.1 One new wire field, fail-safe in both directions

`agent/progress_events.py`:

- New module constant `ADVANCE_KEY = "advance"`; `make_progress_payload(desc, *, advance: Optional[bool] = None)`
  adds `"advance": <bool>`. When `advance is None`, it is derived from a new
  `_KEEPALIVE_PREFIX_RULES` table checked **before** `_PREFIX_RULES`. When the
  table does not match, the default is `True`.
- **The table may contain only globally-unambiguous keep-alive descriptions** —
  ones whose exact text is emitted by a heartbeat and never by a genuine
  boundary. It is a convenience default for new sites; it is not allowed to be
  the mechanism for a description that is ambiguous (§3.3). Ambiguous
  descriptions pass `advance=` explicitly at the call site and are absent from
  the table.
- The table is never the mechanism for stream events: the stream loops (§3.3)
  pass `advance` explicitly, because `receiving stream response` alone cannot
  say whether the item carried payload.

`run_agent.py::_touch_activity` (4063): add one keyword-only param
`advance: Optional[bool] = None`, passed straight into `make_progress_payload`.
It does **not** touch `_last_activity_ts` — the internal clock
(`tools/delegate_tool.py:4256` stale monitor, gateway inactivity monitor,
compression budget, SessionDB projection) must keep seeing keep-alives exactly
as today. This is the whole point of the split.

`hermes_cli/oneshot.py::_OneshotProgressEmitter._emit` (263): include
`"advance": bool(payload.get("advance", True))` in the line dict (279-287).

**Compatibility.** Extra JSON key ⇒ an old supervisor ignores it. Absent key ⇒
`payload.get("advance", True)` and, in the watchdog,
`event.get("advance", True)` ⇒ a new supervisor driving an old child behaves
exactly as today. Every hand-written progress line in the existing tests keeps
its current meaning, including `test_valid_progress_re_protects_a_long_running_operation`
and `test_longest_gap…`, which script `STREAM/active` by hand. `_PROGRESS_MAX_EVENT_BYTES`
(1024) has ~850 bytes of headroom over the real payload.

### 3.2 Watchdog: two clocks

`website-builder/app/hermes/watchdog.py`:

- New field `last_progress_at`, initialized to `started_at` alongside
  `last_activity_at` (1158-1167). Add `forward_progress_count` and
  `keepalive_event_count`.
- `note_activity(now, kind, advance)` → refresh `last_activity_at` always (and
  keep `longest_activity_gap_seconds` / `progress_event_count` exactly as-is);
  refresh `last_progress_at`, `longest_forward_progress_gap_seconds`,
  `forward_progress_count` only when `advance`. Increment `keepalive_event_count`
  otherwise.
- `no_progress_for(now)` now reads `last_progress_at` — it is the
  bound-driving clock, and `is_in_flight` (§1.4/§1.5) follows automatically.
  Add `no_activity_for(now)` for the reporting view.
- `_read_progress_events` (855): `advance = bool(event.get("advance", True))`
  and thread it into `note_activity`. Default `True` = today's behavior.
- `diagnostics()` (440): `idle_for_seconds` keeps the `no_progress_for` key name
  and now reports *seconds since genuine forward progress*; add
  `activity_idle_for_seconds` alongside it so the old reading is still there.
- `_build_receipt` (983): add `counters.advance` and `counters.keepalive`, and
  `activity.longest_forward_progress_gap_seconds` next to the existing
  `longest_gap_seconds`. **Bump `FORENSICS_SCHEMA` to `frontend_forensics/2`**
  (`watchdog.py:170`) — additive but a field-set change.
- Unchanged, by requirement: the hard fuse still reads the raw clock
  (`elapsed >= policy.hard_max_runtime_seconds`, 1221), `_terminate_tree` is
  untouched, and workspace sampling still runs *after* the progress read and is
  never consulted by a bound (1189-1193). Workspace mutation stays a receipt
  field only.

### 3.3 Emitters: mark the keep-alives

**Group A — unambiguous keep-alive text.** Each exact string is emitted by a
heartbeat/timer only and never by a genuine boundary, so it is safe in
`_KEEPALIVE_PREFIX_RULES` *and* gets an explicit `advance=False` at the site.

| File:line | Text | In default table |
|---|---|---|
| `agent/chat_completion_helpers.py:5169` | `waiting for stream response (Ns, no chunks yet)` | yes |
| `agent/tool_executor.py:905` | `sequential tool running (Ns): <name>` | yes |
| `agent/tool_executor.py:1674` | `concurrent tools running (Ns, N remaining: …)` | yes |
| `agent/conversation_loop.py:3549` | `retry backoff (i/n), Ns remaining` | yes |
| `agent/conversation_loop.py:6696` | `error retry backoff (i/n), Ns remaining` | yes |
| `agent/conversation_loop.py:7978` | `empty response retry backoff (i/n), Ns remaining` | yes |
| `agent/chat_completion_helpers.py:5227` | `stale stream detected after Ns, reconnecting` | yes |
| `agent/chat_completion_helpers.py:796` | `stale non-streaming call killed after Ns` | yes |
| `agent/chat_completion_helpers.py:1678` | `codex stream killed after Ns with no first byte` | yes |
| `agent/chat_completion_helpers.py:1724` | `codex stream killed after Ns with no SSE events` | yes |
| `agent/stream_diag.py:265` | `stream retry i/n after <Err>` | yes |

**Group B — ambiguous text, explicit `advance=False` only, NOT in the table.**

| File:line | Text | Why ambiguous |
|---|---|---|
| `agent/chat_completion_helpers.py:1224` | `waiting for non-streaming API response` | The identical string is the genuine request boundary at `:1129` and `:1595`. Only the 15s direct-API heartbeat thread (`_DIRECT_API_ACTIVITY_HEARTBEAT_SECONDS = 15.0`, `:1045`) passes `advance=False`; `:1129`/`:1595` keep the default `True`. |

Nothing changes in `progress_events.py`'s kind table: `advance` is per-call, so
one string is `False` from the heartbeat thread and `True` from the request
start. This is why Group B must not be folded into the prefix table — a
description-keyed rule cannot separate the two call sites, and getting it wrong
would demote a genuine request boundary to non-progress.

**Stream-item discrimination** (the part that actually closes the p18 hole).

All three paths use a **denylist of positively-identified non-advancing
shapes**, never an allowlist of content/tool-call fields:

```
known keepalive / usage-only / empty frame  -> advance=False
known real content / reasoning / tool delta  -> advance=True
unknown or unrecognised event shape         -> advance=True
```

A new real provider delta must never be classified as non-progress. The
fail-safe side is `True`; only shapes the implementation can positively
identify as carrying nothing advance `False`.

- `chat_completion_helpers.py:4088` — `False` only for the known usage-only
  frame: `not chunk.choices and getattr(chunk, "usage", None)`. A chunk with
  choices advances — including an in-stream error-shape chunk
  (`:4140-4149`, `choices=None` with no usage), which is a real state change
  that ends the stream. Keep it O(1) and exception-swallowed: this is the hottest
  loop in the agent (the `:4097-4101` comment already prices a full `repr()` at
  5.5-8.8 µs and avoids it).
- `chat_completion_helpers.py:4612` — read `event_type` **above** the touch.
  `False` for the known non-output frame types: `ping`, `message_start`,
  `message_delta`, `message_stop`, `content_block_stop`. Everything else
  advances: `content_block_delta` of **any** subtype (`text_delta`,
  `thinking_delta`, `signature_delta`, `input_json_delta` — the last is a tool
  argument), `content_block_start`, and any unrecognised type.
- `codex_runtime.py:1619-1622` — the implementation must read the actual event
  shapes reaching `_on_event` (via `relay_llm.stream` /
  `_consume_codex_event_stream`) and encode **only** those it can positively
  identify as keep-alives (e.g. an `in_progress`/`ping` frame). Every real
  output/reasoning/function-argument delta and every unrecognised shape
  advances. If no shape can be positively identified, pass `True` unchanged.

---

## 4. Tasks

1. `agent/progress_events.py` — `advance` in the payload, `_KEEPALIVE_PREFIX_RULES`
   containing **only** the Group A text from §3.3, docstring update (the
   "UNKNOWN is safe" rule becomes "an unrecognised description advances,
   because advancing is the fail-safe side").
2. `run_agent.py::_touch_activity` — `advance: Optional[bool] = None` kwarg.
3. `hermes_cli/oneshot.py` — write `advance` on the wire; no-op when
   `HERMES_ONESHOT_PROGRESS_FILE` is unset (existing coverage must still pass).
4. The 12 keep-alive call sites in §3.3 (11 Group A + 1 Group B) → `advance=False`.
5. The 3 stream loops in §3.3 → denylist-derived `advance`, unknown shape → `True`.
6. `website-builder/app/hermes/watchdog.py` — dual clock, counters, receipt
   fields, `FORENSICS_SCHEMA` → `frontend_forensics/2`, docstring.
7. Flake fix in `test_timeout_leaves_no_orphan_tree` (see §5.3).
8. Validation (§5.4).

## 5. Tests

### 5.1 Core seam — `tests/run_agent/test_progress_channel.py`

- `advance` defaults to `True` for every existing description; a
  `_KEEPALIVE_PREFIX_RULES` match yields `False`; an explicit `advance=` wins.
- **Group A** table-driven: every §3.3 Group A description classifies as
  non-advancing **by default**. This is the audit, encoded as a test, so a new
  heartbeat site cannot silently regress.
- **Group B, both directions**: `waiting for non-streaming API response` is
  `False` from `:1224` and `True` from `:1129`/`:1595`, and is **absent from
  `_KEEPALIVE_PREFIX_RULES`** — assert the table does not match it, so a future
  edit cannot reintroduce the ambiguity as a description-keyed rule.
- `_touch_activity(..., advance=False)` still stamps `_last_activity_ts` and
  still notifies `progress_callback` (only the flag differs).
- Payload stays bounded and still carries no prompt/args/source field.

### 5.2 Streaming discriminators — `tests/run_agent/test_streaming.py` (+ anthropic block)

- Anthropic stream of **only `ping` events**: `touch_calls.count("receiving stream response") == len(events)` (the existing invariant at line 821 keeps holding) **and** zero of those touches carry `advance=True`.
- Anthropic `content_block_delta` with `text_delta`, `thinking_delta`, `signature_delta`, or `input_json_delta` → `advance=True` (each subtype separately: the point is that the denylist cannot be an allowlist of `text_delta`).
- Anthropic `content_block_start` and an unrecognised event type → `advance=True`.
- `chat_completions`: usage-only chunk (`choices=[]`, `usage` set) → `advance=False`; in-stream error-shape chunk (`choices=None`, no usage) → `advance=True`; text/tool-call chunk → `advance=True`. This replaces the blanket `touch_calls.count(...) == len(events)` assertion at line 764 with the two-part form above.
- Codex: an unrecognised event shape → `advance=True` (fail-safe), and a positively-identified keep-alive frame → `advance=False`.

### 5.3 Watchdog — `website-builder/tests/test_frontend_watchdog.py`

New, all fake-clock. Note the bound interaction that these tests pin down: the
idle branch (`watchdog.py:1228`) requires `not in_flight`, and `is_in_flight`
holds while `no_progress_for(now) < max_single_operation_seconds`. So a
keep-alive-only run **with a claimed operation** is protected until the 900s
backstop, and a keep-alive-only run **with none** is cut at the 180s idle bound.

- **The p18 shape, with an in-flight MODEL operation**: genuine
  `MODEL/started` at t=0 (`advance` absent ⇒ advancing), then 1400s of
  alternating `advance=False` `MODEL/started` (the 30s heartbeat) and
  `advance=False` `STREAM/active`. Expect `OUTCOME_IDLE_TIMEOUT` at
  **`elapsed ≈ max_single_operation_seconds` (900s), not at the 180s idle
  bound** — assert `elapsed == pytest.approx(900.0, abs=2)` and
  `elapsed > idle_timeout_seconds`, so a future change that drops in-flight
  protection fails this test instead of silently restoring the 180s kill.
  Assert `active_operation == "MODEL"` in the diagnostics: this is the §1.5
  regression (one operation re-announced forever, never completed).
- **The p18 shape with no operation claimed**: only `advance=False`
  `STREAM/active` events, no `started` boundary ⇒ `active_operation is None` ⇒
  `OUTCOME_IDLE_TIMEOUT` at **`elapsed ≈ idle_timeout_seconds` (180s)**.
- **Genuine progress is never cut, at any duration (the
  `max_single_operation_seconds` semantics correction)**: a `MODEL/started` at
  t=0 followed by advancing `STREAM/active` every 60s for 2000s — well past
  both the 900s backstop and the old 900s wall clock — ⇒ `outcome is None`,
  `elapsed > 1900`. This is the test that proves the bound is *maximum duration
  of no genuine forward progress while in flight*, **not** a maximum operation
  duration. It mirrors the existing
  `test_valid_progress_re_protects_a_long_running_operation`, which must keep
  passing unchanged.
- **Back-compat**: a hand-written progress line with no `advance` key advances
  (static-file style already used throughout the file) — this is also the guard
  that `test_timeout_leaves_no_orphan_tree`'s timing is unchanged.
- **Receipt**: `counters.advance` + `counters.keepalive` sum to
  `total_progress_events`; `activity.longest_forward_progress_gap_seconds >`
  `activity.longest_gap_seconds` on a keep-alive-heavy run.
- **Preserved**: the hard fuse still fires on a run that makes genuine progress
  for 2700s; `_terminate_tree`/`tree_kill_attempted` still true on every timeout
  path (already covered — do not weaken).

Flake fix for §1.6, `test_timeout_leaves_no_orphan_tree`:

- Raise the test's own bounds so child startup is not inside the kill budget: `idle_timeout_seconds=2.0`, `max_single_operation_seconds=8.0`. Intact: real processes, real tree kill, both PIDs asserted dead. Cost: ~8s instead of ~2s for the one real-time test in the file.
- Replace the unconditional `read_text()` with a bounded wait for the marker (deadline already exists at line 1384) so a slow boot degrades into a clear assertion message instead of `FileNotFoundError`.
- Treat a zombie as dead: `_alive` returns `False` when `/proc/<pid>/stat` state is `Z` on Linux. Guard the `/proc` read so non-Linux hosts are unaffected.

### 5.4 Validation

```bash
bash scripts/run_tests.sh website-builder/tests/test_frontend_watchdog.py -q --file-retries=0
bash scripts/run_tests.sh tests/run_agent/test_progress_channel.py -q --file-retries=0
bash scripts/run_tests.sh tests/run_agent/test_streaming.py -q --file-retries=0
bash scripts/run_tests.sh tests/run_agent/ -q --file-retries=0
HERMES_TEST_WORKERS=1 bash scripts/run_tests.sh website-builder/tests -q --file-retries=0
```

The last run must report **zero** `⚠ FLAKY` entries — the whole point of the
§5.3 flake fix is that a `⚠ FLAKY` line is a bug, not noise. Then re-run the
watchdog file 3× to confirm the tree test is stable.

## 6. Risks, open questions, out of scope

- **`max_single_operation_seconds` semantics, stated exactly.** Its meaning is
  now, and must be documented in the code comment and the PR notes as:

  > `max_single_operation_seconds` = the maximum duration of **no genuine forward
  > progress** while an operation remains in flight. It is **not** a maximum
  > operation duration.

  A genuine stream of any length is therefore **never** terminated: real content,
  reasoning, and tool-argument deltas all advance, so the 900s window keeps
  sliding and the hard 45-minute fuse is the only end. The newly-terminated
  class is narrow and worth naming in the PR: *an operation whose only output
  for 900s is keep-alive events* (retry backoff, an idle socket with pings,
  a stale-reconnect loop). TTFB behaviour is unchanged (`starting API call #N`
  advances; the silent branch of the poll heartbeat already touched nothing).
  Operators who need more headroom raise
  `website_builder.frontend_watchdog.max_single_operation_seconds`.
- **Unknown emitters and unknown event shapes.** A heartbeat description added
  later that is neither in `_KEEPALIVE_PREFIX_RULES` nor passes `advance=False`
  keeps advancing; an unrecognised stream event shape advances. Both degrade to
  today's behaviour — fail-open, never a premature kill. §5.1/§5.2 are the
  mitigation.
- **Group B is per-call-site, not per-description.** `waiting for non-streaming
  API response` cannot be table-driven (§3.3). If that heartbeat is later moved
  or duplicated onto a new thread, the new site must pass `advance=False`
  explicitly; §5.1's Group A test cannot catch it, so the Group B test asserts
  the call sites, not just the string.
- **Open (needs one read, no re-run):** was p18's `stream_active=290` real content or keep-alive frames? Read `recent_activity[].offset_seconds` in the p18 receipt — a `waiting for stream response` entry must be ≥30s after its predecessor (§1.2). After this change, `counters.advance` vs `counters.keepalive` answers it directly on the next run.
- **Open (cannot be answered from this repo):** which `api_mode` p18 used. Not recorded in the receipt. Adding provider/api_mode to the receipt is *not* in this change — decide separately whether the bounded receipt may carry a provider name.
- **Out of scope:** any convergence guard; touching `_last_activity_ts` semantics for the delegation/gateway/compression consumers; canonical-URL code; re-running p18; Batch D.

## 7. What the implementation must report back

- Confirmation that all 12 keep-alive call sites (11 Group A + 1 Group B) and
  all 3 stream loops were changed, with the diff hunks.
- The §5.1 audit results: every Group A description → `advance=False` by
  default; `waiting for non-streaming API response` absent from the table and
  split `False`/`True` across `:1224` vs `:1129`/`:1595`.
- The exact codex event shapes encoded in `_on_event`, and confirmation that
  every unrecognised shape advances.
- Full-suite `website-builder/tests` output with the FLAKY section, plus the 3×
  rerun of the tree test.
- **PR note, verbatim intent:** "`max_single_operation_seconds` is the maximum
  duration of *no genuine forward progress* while an operation is in flight. It
  is not a maximum operation duration; a genuinely advancing stream of any
  length is never cut." Confirm a green run of the §5.3 'genuine progress is
  never cut, at any duration' test as the evidence for that claim.