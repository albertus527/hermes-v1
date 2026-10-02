# Graceful Shutdown of an Active Supervised FRONTEND Run (p19)

## Goal

Make Ctrl+C (and SIGTERM) actually **stop** the in-flight supervised FRONTEND invocation instead of
only setting a flag nobody reads. The child is currently spawned into a detached process group, so it
survives both the signal and the runtime's own exit.

Out of scope (explicit): stream-frame discriminator, canonical URL, convergence policy, Batch D, the
legacy degraded fallback path, second-signal escalation.

---

## 1. Investigation findings (current code)

**F1 — The signal handler only stops the receive loop.**
`app/runtime.py:2856-2861`:

```python
def shutdown(signum, frame):
    logger.info("Received signal %d, shutting down...", signum)
    loop.stop()          # -> TelegramReceiveLoop._stop_event.set()   (runtime.py:1504-1506)
```

`_stop_event` is read in exactly two places: `TelegramReceiveLoop.run()` at `runtime.py:2674` and
`runtime.py:2680`. Nothing on the FRONTEND supervision path reads it. **That is the whole bug.**

**F2 — The blocking path is synchronous and on the main thread.**

```
main() -> loop.run()                          runtime.py:2673
  -> self._process_update(update)             runtime.py:2662
  -> _dispatch_project_turn(...)              runtime.py:2364
  -> TelegramDispatcher.dispatch(...)         runtime.py:2309 (intake) / 2597 (approve)
  -> FrontendBuilder.build(...)               projects/build.py:615
  -> hermes_adapter.frontend_build(...)       hermes/adapter.py:1251
  -> _run_hermes_cli(supervise=True)          hermes/adapter.py:413 -> :560
  -> _run_hermes_cli_supervised(...)          hermes/adapter.py:603
  -> wd.supervise_frontend_run(...)           hermes/watchdog.py:1197
  -> blocking `while True:` poll loop         hermes/watchdog.py:1283-1338
```

Every hop is a plain synchronous call, so the poll loop owns the main thread. `run()`'s
`_stop_event` checks are unreachable until the whole build returns.

**F3 — The handler really does fire; it just has no effect.**
Python runs signal handlers on the main thread between bytecodes, so the handler executes *inside*
the poll loop. That is why p19 shows `Received signal 2, shutting down...` repeatedly (one per
Ctrl+C) while `FRONTEND activity ...` keeps streaming: the handler runs, sets an event, returns, and
the loop resumes. PEP 475 then restarts `time.sleep` for its remaining time.

**F4 — The child is detached, so nothing else can reach it.**
`_spawn_kwargs()` (`watchdog.py:909-928`) sets `start_new_session=True` on POSIX and
`CREATE_NEW_PROCESS_GROUP | windows_hide_flags()` on Windows. Consequences:
- the terminal's SIGINT never reaches the child (wrong session / wrong console group);
- if the runtime died outright, the child (and its Hermes tool grandchildren) would be **orphaned**.

`kill_process_tree` (`agent/deadline.py:548`) exists precisely to walk that tree, and
`_spawn_kwargs` exists precisely so it can. The mechanism is in place; nothing calls it on shutdown.

**F5 — The exact-invocation termination primitive already exists.**
`_terminate_tree(proc, grace_seconds, kill_tree)` (`watchdog.py:989-1042`) does
SIGTERM -> `proc.wait(timeout=grace)` -> strongest kill, and its docstring states the invariant:
*"Only this* invocation's *own child pid is ever signalled; no unrelated Website Builder or Hermes
process is touched."* The post-loop block at `watchdog.py:1340-1352` already calls it whenever
`outcome is not None` and the child is still alive.

⇒ A cancellation is exactly **one more reason for the poll loop to set an outcome**. No new kill
code, no new process matching, requirement 3 satisfied by construction.

**F6 — Every cleanup/observability guarantee is already outcome-driven and shared.**

| Guarantee | Existing site |
|---|---|
| reader-thread join (bounded, daemon) | `watchdog.py:1353-1358` (`finally`) |
| progress-file unlink | `watchdog.py:1359-1362` (`finally`) |
| receipt built unconditionally | `watchdog.py:1372-1387` |
| receipt written on every terminal path | `watchdog.py:1388-1400` |
| `terminated` / `tree_kill_attempted` | `watchdog.py:1435-1436` |

Adding a fourth outcome inherits all of it — requirement 5 for free, no duplicated cleanup.

**F7 — The adapter's outcome mapping is wrong for a cancellation, and one field is load-bearing.**
`adapter.py:688-696`:

```python
if run.outcome is not None:
    return HermesResult(..., exit_code=124, error_code=run.outcome, timed_out=True, ...)
```

`timed_out=True` is **not** cosmetic — `frontend_build` (`adapter.py:1305`) uses it to admit
*artifact recovery*:

```python
if result.timed_out and self._has_complete_frontend_artifacts(workspace):
    return self._parse_frontend_response("", workspace)
```

If a cancelled run were reported as a timeout, a workspace that happens to look complete would be
recovered as a **success** and the pipeline would continue into Phase 8 QA / preview *during
shutdown*. It must not. And `exit_code=124` is excluded by the adapter's own documented invariant at
`adapter.py:588`: *"timed_out is a true invariant: every 124 this method returns is a timeout."*

**F8 — One adapter instance owns every supervised invocation.**
`compose()` creates exactly one `HermesAdapter` (`runtime.py:718`) and injects that same object into
`FrontendBuilder` (`runtime.py:896`), `RevisionOrchestrator` (`runtime.py:918`), and — through the
builder — every `QAOrchestrator` (`projects/build.py:997`). So a registry owned by the adapter covers
the initial build, the Phase-7 compile repair (`build.py:558`), QA repairs
(`qa/orchestrator.py:429`), and revisions (`projects/revise.py:597`) with no wiring changes.
`main()` already has the adapter in scope as `composition.hermes`.

`ProjectRunner` holds a single worker slot (`_active_project`, `sandbox/runner.py`), so in
production "the active invocation" is unambiguous — but the design is keyed per `invocation_id`
regardless, so isolation does not depend on that.

**F9 — The user-facing code path needs an entry.**
`runtime.py:2373` passes the raw build error code into `_send_error_reply` ->
`render_error_message` (`runtime.py:2663`), which falls through to `_FALLBACK_ERROR_TEXT` for an
unknown code. `runtime.py:1148` states the rule: *"When adding an error code, add it here."*

**F10 — The legacy degraded path is not part of this bug.**
`_run_hermes_cli_legacy` (`adapter.py:706`) uses `subprocess.run(...)` **without**
`start_new_session`, so the child is in the runtime's own process group / console group and receives
the terminal's Ctrl+C directly on POSIX. It is only reached when `kill_process_tree` is unavailable
(`WatchdogUnavailable`). Left unchanged.

---

## 2. Decisions (settled)

- **D1 — The signal handler only sets a flag; the poll loop performs the kill.** A handler that
  called `_terminate_tree` directly would do a blocking `proc.wait(timeout=10)` in signal context,
  re-entering code the interrupted loop is about to run. Flag-only is also idempotent, so the
  repeated Ctrl+C in the p19 evidence becomes a harmless no-op.
- **D2 — The registry is per-`HermesAdapter` instance, not module state.** `watchdog.py` is
  deliberately built so supervision state is *never* module/class state
  (`watchdog.py:14-20`, `watchdog.py:333-336`). The cancellation registry lives in its own
  dependency-free module so the adapter can import it at module scope — `adapter.py` deliberately
  does **not** import `watchdog` at module scope (`adapter.py:49-54`, `adapter.py:619-621`), because
  `watchdog.py:89` imports `hermes_cli.oneshot`.
- **D3 — The registry stores only `invocation_id -> threading.Event`. It never holds the child
  handle.** The poll loop stays the sole owner of `proc` and performs teardown through the existing
  `_terminate_tree`. This is what makes "only the exact invocation is terminated" *structural* rather
  than merely intended.
- **D4 — `OUTCOME_CANCELLED = "FRONTEND_CANCELLED"`, deliberately excluded from
  `TIMED_OUT_OUTCOMES`** (`watchdog.py:207-209`). Cancellation is not a timeout.
- **D5 — Adapter maps cancellation to `timed_out=False`, `exit_code=130`** — never `124` — so the
  artifact-recovery path (`adapter.py:1305`) cannot admit a cancelled run as a success.
- **D6 — Cancellation outranks the timeout bounds on the same poll iteration, but never a real child
  exit.** Deterministic code, and the operator's stop is the proximate cause of termination. A child
  that already exited reports its true result.
- **D7 — Register *before* spawn.** Otherwise a signal landing between `spawn()` and registration
  would leave a live, unreachable child — exactly the failure being fixed.
- **D8 — No second-signal escalation.** Repeated Ctrl+C is idempotent (same Event). Worst-case
  shutdown latency is `poll_interval_seconds` (1.0s) + `TERMINATE_GRACE_SECONDS` (10s) + strongest
  kill, which is the same bound a timeout already pays.

---

## 3. Design

### 3.1 New module `website-builder/app/hermes/cancellation.py`

Zero imports beyond `threading`/`typing` (so it is safe to import at `adapter.py` module scope).
A *control-plane rendezvous*, explicitly **not** supervision state: nothing in it is ever read by any
bound, clock, or liveness decision.

```python
class FrontendRunCanceller:
    """Per-process registry of the FRONTEND invocations currently supervised.

    Holds only ``invocation_id -> threading.Event``. It deliberately does NOT
    hold the child handle: the supervising poll loop remains the sole owner of
    its own child and performs teardown through ``_terminate_tree``. That is
    what makes "only this exact invocation is terminated" structural.

    ``cancel_all`` requests that every *registered* invocation stop. That is
    not process-name matching: each entry is a live child this process spawned,
    and each is still signalled by its own pid.
    """

    def register(self, invocation_id: str) -> threading.Event: ...
    def release(self, invocation_id: str) -> None: ...
    def cancel(self, invocation_id: str, *, reason: str = "") -> bool: ...
    def cancel_all(self, *, reason: str = "") -> List[str]: ...
    def active_ids(self) -> List[str]: ...
```

`cancel()` returns `False` for an unknown id (already finished) so a late shutdown signal is a
no-op rather than an error. All methods take a `threading.Lock` and never raise.

### 3.2 `website-builder/app/hermes/watchdog.py`

1. `OUTCOME_CANCELLED = "FRONTEND_CANCELLED"` next to the existing codes (`watchdog.py:202-209`).
   **Not** added to `TIMED_OUT_OUTCOMES`.
2. New keyword-only parameter on `supervise_frontend_run` (`watchdog.py:1197-1216`):
   `run_canceller: Optional[FrontendRunCanceller] = None` (imported under `TYPE_CHECKING` so the
   module keeps its current import surface). Default `None` ⇒ cancellation is impossible and every
   existing caller and test behaves **byte-for-byte as before**.
3. Register before spawn (D7); release on spawn failure and in the existing `finally`
   (`watchdog.py:1353-1362`).
4. Inside the poll loop, inside `if not exited:` (`watchdog.py:1294`), **before** the bound
   evaluation:
   ```python
   if cancel_event is not None and cancel_event.is_set():
       outcome = OUTCOME_CANCELLED
   elif not invocation.progress_channel_confirmed:
       ... existing, unchanged ...
   else:
       ... existing, unchanged ...
   ```
5. `_build_receipt` gains two additive keys — `cancelled: bool` and `cancel_reason: str | None` — so
   an operator can tell *operator-stopped* from *gave-up*. `FORENSICS_SCHEMA` stays
   `frontend_forensics/2`: the module's own rule (`watchdog.py:196`) bumps only on **incompatible**
   changes, and `test_build.py:1781` pins the literal. `outcome` alone already records the fact.
6. A `logger.info` branch beside the three timeout branches (`watchdog.py:1402-1422`) so a cancelled
   run is never logged as `FRONTEND completed`.
7. Module docstring gains a short "Operator cancellation" note next to the failure-mode section.

### 3.3 `website-builder/app/hermes/adapter.py`

1. Module-scope `from app.hermes.cancellation import FrontendRunCanceller`.
2. `HermesAdapter.__init__`: `self.frontend_runs = FrontendRunCanceller()`.
3. `_run_hermes_cli_supervised` (`adapter.py:603`) passes `run_canceller=self.frontend_runs` into
   `wd.supervise_frontend_run` (`adapter.py:649-660`).
4. New public method (the shutdown entry point):
   ```python
   def cancel_active_frontend_runs(self, *, reason: str = "") -> List[str]:
   ```
5. Outcome mapping (`adapter.py:688-696`) branches so `FRONTEND_CANCELLED` gets
   `timed_out=False` and `exit_code=130`; the three timeout codes keep `124` / `True`.

### 3.4 `website-builder/app/runtime.py`

1. Extract the handler body into a module-level, unit-testable function:
   ```python
   def _handle_shutdown_signal(loop, hermes, signum, _frame=None) -> None:
       """Signal-handler body. Flag-only: it never terminates a process itself."""
       logger.info("Received signal %d, shutting down...", signum)
       loop.stop()                                    # unchanged behaviour
       if hermes is None:
           return
       try:
           ids = hermes.cancel_active_frontend_runs(reason=f"signal {signum}")
       except Exception:
           logger.warning("FRONTEND cancellation on signal %s failed", signum, exc_info=True)
           return
       if ids:
           logger.warning(
               "Shutdown cancelled in-flight FRONTEND invocation(s): %s", ", ".join(sorted(ids))
           )
   ```
   Never raises — a handler that raised would replace the shutdown path with a traceback.
2. `main()` (`runtime.py:2856-2861`) registers it for `SIGINT` and `SIGTERM` unchanged.
3. `ERROR_MESSAGES["FRONTEND_CANCELLED"]` per the rule at `runtime.py:1148` (F9).

### 3.5 What deliberately does NOT change

- `TelegramReceiveLoop.run()` / `stop()` — already correct once the run unblocks.
- `watchdog.supervise_frontend_run`'s existing signature defaults, bounds, and poll cadence.
- `_run_hermes_cli_legacy` (F10).
- `FrontendBuilder` / `QAOrchestrator` / `RevisionOrchestrator` — they already propagate
  `error_code`, so `FRONTEND_CANCELLED` lands in `state.failure` and `BuildResult.error_code`
  (`build.py:721-761`) with no edits.
- `kill_process_tree` — called only with `proc.pid`, exactly as today.

---

## 4. Tests

### `website-builder/tests/test_frontend_watchdog.py` (append one clearly-delimited section; **no existing test is edited**)

1. `test_cancellation_terminates_the_tree_and_reports_its_own_code` — cancel requested from the
   harness `sleep` hook mid-run; assert `outcome == OUTCOME_CANCELLED`, `terminated is True`,
   `tree_kill_attempted is True`, and `killed == [(pid, SIGTERM), (pid, None)]`
   (requirement 3: SIGTERM -> grace -> strongest).
2. `test_cancellation_is_distinct_from_every_timeout_code` — `OUTCOME_CANCELLED` not in
   `TIMED_OUT_OUTCOMES` and unequal to all three (requirement 6).
3. `test_cancellation_outranks_a_bound_that_fires_on_the_same_poll` — schedule the idle bound to
   trip on the cancel tick; assert `FRONTEND_CANCELLED`, not `FRONTEND_IDLE_TIMEOUT` (D6).
4. `test_a_child_that_exits_before_cancellation_is_not_reported_as_cancelled` — the honesty edge.
5. `test_cancelled_run_records_cancellation_in_its_receipt` — `diagnostics_dir` set; assert the
   persisted JSON has `outcome == "FRONTEND_CANCELLED"` and `cancelled is True` (requirement 7c).
6. `test_cancelled_run_unlinks_the_progress_file` (requirement 7d).
7. `test_registry_is_released_after_success_and_after_cancellation` — no leak, so a later shutdown
   signal cannot target a finished run.
8. `test_cancelling_one_invocation_never_requests_another` — two entries in one canceller; cancel A;
   assert B's event is unset and `cancel_all` reports only live ids.
9. `test_cancelled_run_signals_only_its_own_child_pid` — the fake `kill_tree` spy for the cancelled
   run records exactly one distinct pid (requirement 4).
10. **Real-process** `test_shutdown_cancellation_leaves_no_orphan_tree` — mirrors
    `test_timeout_leaves_no_orphan_tree`: real child + real grandchild, cancellation requested from
    an injected `sleep` once the grandchild marker appears; assert both PIDs dead (requirement 7e).
11. **Real-process** `test_cancelling_one_run_leaves_an_unrelated_run_alive` — a second registered
    child + grandchild that is never cancelled must both still be alive afterwards
    (requirements 4, 5, 7b).

### `website-builder/tests/test_hermes_adapter.py`

12. `test_cancelled_supervision_is_not_reported_as_a_timeout` — `error_code ==
    "FRONTEND_CANCELLED"`, `timed_out is False`, `exit_code != 124` (F7 / D5).
13. `test_cancelled_build_is_not_admitted_by_artifact_recovery` — with complete artifacts present, a
    cancelled result still returns `success: False` (the `adapter.py:1305` regression guard).
14. `test_cancel_active_frontend_runs_returns_only_live_invocations` — adapter-level registry surface.

### `website-builder/tests/test_runtime.py`

15. `test_shutdown_handler_cancels_active_frontend_runs` — calls `_handle_shutdown_signal` with a
    fake loop + fake adapter; asserts the loop was stopped **and** cancellation was requested with
    the signal number.
16. `test_shutdown_handler_survives_a_failing_adapter` — an adapter that raises must not escape the
    handler.
17. `test_cancel_requested_with_no_active_run_is_a_no_op`.

---

## 5. Verification

```
cd website-builder
python -m pytest tests/test_frontend_watchdog.py -q          # all pre-existing tests unchanged + new
python -m pytest tests/test_hermes_adapter.py tests/test_runtime.py tests/test_build.py \
                   tests/test_crash_recovery.py -q
python -m pytest tests/ -q
```

Regression guard: `git diff --stat` must show **no modification** to any existing test body — only
appended tests.

---

## 6. Out of scope / follow-ups (recorded, not implemented)

- Second-signal escalation to an immediate hard kill (D8; the bounded 11s window is already the
  timeout path's own bound).
- Cancellation of `_run_hermes_cli_legacy` (F10 — not detached, already reachable by Ctrl+C).
- Per-project operator-facing cancellation (there is no Telegram cancel verb; `ProjectLifecycle.
  CANCELED` is user-driven and unrelated).