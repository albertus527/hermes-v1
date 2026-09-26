# FRONTEND activity-aware watchdog (Website Builder R1)

Replace the fixed 900s FRONTEND wall-clock kill with a per-invocation, activity-aware
watchdog that terminates only the exact invocation that goes idle.

## Why (measured, p10)

`website-builder/app/hermes/adapter.py:469` runs the FRONTEND child as
`subprocess.run(..., capture_output=True, timeout=timeout_seconds)` with
`timeout_seconds=900` hardcoded at `adapter.py:1013`. p10 showed OpenRouter activity
continuing right up to the instant the 900s timeout fired, so a still-working build was
killed. Elapsed time is the wrong liveness signal for this workload.

## Preserved (explicit non-goals)

FRONTEND toolsets `["file","terminal","skills"]`, the three profile skills, design
freedom, reasoning depth, `MAX_COMPILE_REPAIR_ATTEMPTS = 1`, QA and its repair budget,
preview/deploy, Vercel promotion. No turn/call budget is added. No early stop when
`App.tsx` exists. No OpenRouter/global activity is consulted.

## Key findings that shaped the design

1. The child is `python -m hermes_cli.main -z <prompt>`. `hermes_cli/oneshot.py:232`
   calls `logging.disable(logging.CRITICAL)` and lines 269-277 redirect **stdout and
   stderr to devnull** for the whole call tree. A progress channel therefore cannot ride
   stdout/stderr and cannot be child logging.
2. Plugin hooks are insufficient: `pre_tool_call`/`post_tool_call` are per tool call, but
   `pre_llm_call` (`agent/turn_context.py:1329`) and `post_llm_call`
   (`agent/turn_finalizer.py:631`) fire **once per turn**, not per model request. A single
   slow provider request would be indistinguishable from idle — the exact p10 hang.
3. `AIAgent._touch_activity` (`run_agent.py:4060`) is the finest existing seam, already
   fired at model-request start (`conversation_loop.py:2061`), model completion
   (`:4447`), tool execution (`tool_executor.py:1040`), and stream/terminal activity. It is
   non-raising and thread-safe.
4. `AIAgent` already has the exact shape we need: `event_callback: Optional[Callable[[str, dict], None]]`
   (`agent/agent_init.py:584`, stored at `:882`). It is **not** reusable — its only
   current events are compression lifecycle, and `gateway/run.py:5332` fans every
   `event_callback` event into the gateway's async hook bus, so high-frequency progress
   events there would pollute every gateway session. Hence a **separate** channel.
5. `agent/deadline.py` already owns the repo's conventions and must be reused, not
   reimplemented: `kill_process_tree` (`:551`, Windows `taskkill /F /T`; POSIX psutil
   snapshot + group signal) and `resolve_timeout`/`clamp_timeout` (`:243`).
6. Process-group precedent for supervised spawns: `cron/scheduler.py:4396-4411`
   (`start_new_session=True` on POSIX; `windows_hide_flags() | CREATE_NEW_PROCESS_GROUP`
   on Windows, from `hermes_cli/_subprocess_compat.py:250`).
7. All four FRONTEND call sites funnel through one method — `build.py:520` (compile
   repair), `build.py:640` (initial), `qa/orchestrator.py:426` (QA repair),
   `projects/revise.py:360` (revision). Supervision belongs inside `frontend_build`, so all
   four are covered by one change.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Activity source | Minimal additive core seam: new `AIAgent.progress_callback`, notified from `_touch_activity`, wired in `hermes_cli/oneshot.py::_run_agent` | Only seam with per-model-request granularity; user-selected |
| Child→parent transport | Append-only JSONL file at a per-invocation path | stdout/stderr are devnull'd and logging is disabled; a file is crash-survivable, replayable, and has no fd-inheritance footgun |
| Reuse of `event_callback` | Rejected | Compression-only today; gateway fans it into the async hook bus |
| Watchdog scope | FRONTEND only. Non-FRONTEND `_run_hermes_cli` callers keep `subprocess.run` + `timeout_seconds` | Preserves the 300s default contract and its existing tests |
| Clock | Injected monotonic clock, default `time.monotonic` | Fake clock in tests, zero real waiting |
| Hard fuse scope | Per invocation, not per build operation | Spec: watchdog is local to that exact subprocess. Note: initial + 1 compile repair can reach 2 × 45 min |
| Unconfirmed progress channel | Idle enforcement **disabled**, hard fuse only | Never kill earlier than today's 900s when the channel is missing |

## Tasks

### 1. Core seam: `AIAgent.progress_callback`

- `agent/agent_init.py`: add `progress_callback: Optional[Callable[[str, dict], None]] = None`
  immediately after `event_callback` (line 584); store as `agent.progress_callback` beside
  `agent.event_callback = event_callback` (line 882). Additive optional kwarg only — no
  removals, no renames, no `PLUGIN_API_VERSION` bump (AGENTS.md native-plugin compat policy).
- `run_agent.py::_touch_activity` (line 4060): after stamping `_last_activity_ts` /
  `_last_activity_desc`, notify `self.progress_callback(kind, payload)` inside a
  `try/except` that never raises into the agent loop, matching the existing
  `event_callback` defensive style and `_touch_activity`'s own "never let the bridge break
  the agent loop" contract. `None` is the default so every existing caller is unaffected.
- Payload contract (bounded, whitelisted — no prompt, source, keys, tokens, or model output):
  `{"kind": "MODEL"|"TOOL"|"STREAM"|"UNKNOWN", "phase": "started"|"completed"|"active",
  "desc": <already bounded to 120 chars by `bound_activity_description`>}`.
  Kind mapping from a small documented prefix table over existing descriptions:
  `starting API call` → MODEL/started; `waiting for provider response` → MODEL/started;
  `API call #N completed` → MODEL/completed; `receiving stream response` → STREAM/active;
  `executing tool:` → TOOL/started; anything else → UNKNOWN/active.
  **UNKNOWN refreshes the activity clock but claims and clears no operation** — fail-safe
  in the safe direction (a mis-mapped description can never cause a premature kill).
- Hot-path cost: `_touch_activity` fires per stream delta. The notify must be a single
  small `write()`; coalescing/rate-limiting lives in the emitter (task 2), never as a lock
  that could block the agent loop.

### 2. Core seam: oneshot progress emitter

- `hermes_cli/oneshot.py::_run_agent` (agent built at lines 476-502): when the internal env
  var `HERMES_ONESHOT_PROGRESS_FILE` names an output path, build a progress emitter and pass
  `progress_callback=` to `AIAgent(...)`. **Strict no-op when the env var is absent** — must
  be covered by a test, since every `hermes -z` caller shares this path.
- Emitter: append one JSON line per event, `O_APPEND`, flush per line, never raise, never
  block. Always forward boundary events (`started`, `completed`); coalesce `active` events to
  at most one per `_PROGRESS_COALESCE_SECONDS`. Stamp each line with the invocation id read
  from the same env block so the parent can reject stale ids.
- Write a `CHANNEL_READY` line at install time so the parent can confirm the channel is live.
- Env var rationale: this is an internal mechanism, not user config. The **timeouts live in
  Website-Builder `config.yaml`**, satisfying AGENTS.md's "no new user-facing `HERMES_*` env
  vars for non-secret config".

### 3. `website-builder/app/hermes/watchdog.py` (new)

Named constants (defaults, all overridable from Website-Builder `config.yaml` via the
existing loader at `app/runtime.py:150`):

```python
FRONTEND_IDLE_TIMEOUT_SECONDS = 180            # idle watchdog
FRONTEND_HARD_MAX_RUNTIME_SECONDS = 2700       # hard safety fuse (45 min)
FRONTEND_MAX_SINGLE_OPERATION_SECONDS = 900    # in-flight staleness backstop
PROGRESS_POLL_INTERVAL_SECONDS = 1.0
PROGRESS_ACTIVE_COALESCE_SECONDS = 5.0
ACTIVITY_LOG_MIN_INTERVAL_SECONDS = 30.0      # rate-limits the per-activity INFO line
OUTPUT_CAPTURE_LIMIT_CHARS = 2000              # bounded stdout/stderr, head+tail
TERMINATE_GRACE_SECONDS = 10.0                 # SIGTERM -> SIGKILL escalation
```

Config keys: `frontend.idle_timeout_seconds`, `frontend.hard_max_runtime_seconds`.
Invalid values fall through to the constant with a warning (mirror `resolve_timeout`).

`FrontendInvocation` dataclass — created per `frontend_build` call, held as a **local**
(never module-level, never a class attribute; this is what guarantees isolation):

```python
invocation_id, project_id, build_operation_id, child_pid,
started_at, last_activity_at, active_operation, active_operation_started_at,
progress_channel_confirmed, progress_offset, output_buffers
```

- `invocation_id`: `uuid4().hex`, minted per call.
- `build_operation_id`: new optional `frontend_build(..., build_operation_id=None)` kwarg.
  The four call sites already hold the writer lock when they bump `source_revision`
  (`build.py:631`, `build.py:516`, `qa/orchestrator.py:423`, `revise.py:339`) and pass
  `str(state.revisions.source_revision)` from there. Omitted → falls back to `invocation_id`
  (still fully isolated).

### 4. `HermesAdapter` — supervised execution boundary

In `_run_hermes_cli` (`adapter.py:390`), keep the existing `subprocess.run` path unchanged
for callers that pass no watchdog config. When FRONTEND supplies one, use a `Popen` path:

- Spawn with the `cron/scheduler.py:4396-4411` group precedent so the child leads its own
  group/session and its descendants stay reachable.
- Two reader threads drain stdout/stderr into bounded head+tail buffers, preserving today's
  `capture_output=True` diagnostics with an explicit bound.
- Poll the progress file by byte offset every `PROGRESS_POLL_INTERVAL_SECONDS`. Ignore any
  line whose `invocation_id` does not match, ignore unparseable lines, ignore lines at or
  below the already-consumed offset.
- On each accepted event: `last_activity_at = now`; set/clear `active_operation` only on
  typed `started` / `completed`; honour in-flight protection only while
  `now - active_operation_started_at < FRONTEND_MAX_SINGLE_OPERATION_SECONDS`.

Timeout rules, evaluated independently per invocation:

- `now - started_at >= FRONTEND_HARD_MAX_RUNTIME_SECONDS` → `FRONTEND_HARD_TIMEOUT`
- `now - last_activity_at >= FRONTEND_IDLE_TIMEOUT_SECONDS` and not in-flight →
  `FRONTEND_IDLE_TIMEOUT` (skipped entirely when `progress_channel_confirmed` is False)

Neither rule can read another invocation's state: the record is a local and the progress
stream is filtered by invocation id.

### 5. Process-tree cleanup

Reuse `agent.deadline.kill_process_tree` — do not reimplement.

1. `kill_process_tree(proc.pid, sig=SIGTERM)` (POSIX) / the Windows `taskkill /F /T` path.
2. Bounded `proc.wait(timeout=TERMINATE_GRACE_SECONDS)`.
3. `kill_process_tree(proc.pid)` (SIGKILL) if still alive.
4. `proc.wait()`, close both pipes, delete the invocation's progress file — all in `finally`.

Only `proc.pid`, this invocation's own child, is ever signalled. No unrelated Website Builder
or Hermes process is touched.

### 6. Distinct failure metadata + preserved artifact recovery

- `HermesResult` gains `error_code: Optional[str] = None`, `timed_out: bool = False`, and a
  bounded `invocation: Optional[dict]` (invocation id, project id, build operation id, pid,
  elapsed, idle_for, last operation, cleanup result). No stdout/stderr bodies in `invocation`.
- `FRONTEND_IDLE_TIMEOUT` and `FRONTEND_HARD_TIMEOUT` are distinct codes; a normal nonzero
  exit is neither. `timed_out` is True only for the two watchdog codes, so
  `frontend_build`'s recovery gate at `adapter.py:1021` changes from `exit_code == 124` to
  `result.timed_out` — both timeout classes keep the recovery path, a normal nonzero exit
  does not.
- `_has_complete_frontend_artifacts` (`adapter.py:1032`) is **unchanged and not weakened**.
  Recovery records which timeout it recovered from.
- `frontend_build` returns the code on failure so callers can map copy. In `build.py:671-673`,
  prefer `frontend_result.get("error_code")` when present and keep the existing
  `TOOLCHAIN_MUTATION_REJECTED` substring logic as the fallback — no new substring matching.

### 7. Logging

Five INFO lines on a dedicated logger, exactly as specified:
`FRONTEND started/activity/idle-timeout/hard-timeout/completed` with
`project=<id> invocation=<id> pid=<pid> type=<MODEL|TOOL|FILE> idle_for=<n>s elapsed=<n>s`.

Never log prompts, source, API keys, tokens, protected URLs, or model response bodies.
Boundary events (started/completed/timeouts) always log; the `activity` line is rate-limited
to one per `ACTIVITY_LOG_MIN_INTERVAL_SECONDS` per invocation.

## Tests

New `website-builder/tests/test_frontend_watchdog.py` — fake monotonic clock, fake child,
fake progress stream, zero real waiting. Required scenarios:

1. Run A last activity `t=0`; Run B emits at `t=60,120,180,…`; advance past A's idle deadline
   → **A → `FRONTEND_IDLE_TIMEOUT`, B still running.** This is the headline isolation test.
2. One run emitting before each idle deadline may exceed 900s total without timing out.
3. One exact run going idle terminates only that run.
4. Long total runtime + continuous legitimate activity continues until the hard ceiling.
5. Hard ceiling → `FRONTEND_HARD_TIMEOUT` (assert it is *not* reported as idle/stall).
6. Completed subprocess → normal success.
7. Timeout with complete artifacts → existing artifact-recovery path still works, and records
   the timeout kind.
8. Timeout with incomplete artifacts → failure.
9. Progress events carrying a stale/previous invocation id are ignored.
10. Two invocations of the same project at different operation ids cannot refresh each other.
11. Unconfirmed progress channel → idle enforcement disabled, hard fuse still applies.
12. In-flight operation beyond `FRONTEND_MAX_SINGLE_OPERATION_SECONDS` stops being protected
    (the stuck-operation backstop).

Additions to `website-builder/tests/test_hermes_adapter.py`:

- Rewrite `test_frontend_build_requests_extended_timeout` (currently asserts
  `timeout_seconds == 900`) to assert the idle/hard watchdog config and the **absence** of a
  fixed 900s wall clock. This is the one existing test whose intent legitimately changes.
- `timeout_seconds` default/override/timeout-error tests keep passing untouched — they patch
  `subprocess.run` on the non-watchdog path, which this change preserves.

Core-seam tests:

- `progress_callback` defaults to `None` and `_touch_activity` is unchanged when unwired.
- The emitter is a strict no-op when `HERMES_ONESHOT_PROGRESS_FILE` is absent.
- Emitted events are bounded, JSON-parseable, and carry no prompt/secret-shaped fields.

Process-tree test (real processes; the only test permitted real time, kept short and
cross-platform via `sys.executable` children that spawn a grandchild): force an idle timeout,
then assert the child and grandchild PIDs are both gone and no pipe handle leaks. Tag it so
the serial runner picks it up cleanly.

## Risks

- `hermes_cli/oneshot.py` is shared by every `hermes -z` caller. The wiring must be a strict
  no-op without the env var — covered by a dedicated test.
- `_touch_activity` is hot. The notify adds a small `write()` per event; keep it allocation-light
  and never take a lock the agent loop can block on.
- `AIAgent.__init__` gains a parameter in a ~60-parameter constructor. Additive and optional,
  so no existing caller breaks.
- Hard fuse is per invocation, so a build with a compile repair can reach 90 min. Intended
  per spec; noted so it is not mistaken for unbounded operation.

## Validation

```bash
# focused first
bash scripts/run_tests.sh website-builder/tests/test_frontend_watchdog.py -q --file-retries=0
bash scripts/run_tests.sh website-builder/tests/test_hermes_adapter.py -q --file-retries=0
bash scripts/run_tests.sh tests/run_agent/ -q --file-retries=0     # progress_callback seam

# then the full website-builder suite, serially (memory: the full suite has frozen Windows before)
HERMES_TEST_WORKERS=1 bash scripts/run_tests.sh website-builder/tests -q --file-retries=0
```

Do not commit, push, or deploy.

## Report the implementation must state

Exact old timeout behavior; exact new watchdog behavior; how invocation isolation is
guaranteed; activity source used; idle timeout value; hard ceiling value; process-tree cleanup
behavior; tests added; focused test result; full-suite result; and whether p10 should be
retried or p11 created fresh.
