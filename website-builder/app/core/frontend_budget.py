"""Durable per-operation FRONTEND invocation budget (V4-B2).

WHY THIS EXISTS
---------------
Before this module a single build/revision operation could invoke FRONTEND up to
seven times: one initial generation plus INDEPENDENT per-stage repair budgets for
the Phase-7 compile repair (1), the Phase-8 deterministic QA repair (2), the
Impeccable critic repair (2), and the design-fidelity repair (1). Those budgets
were per STAGE, so no combination of stages was globally bounded.

This module provides ONE authoritative, durable budget per build/revision
operation, enforced at the single common FRONTEND boundary
(``HermesAdapter.frontend_build``): the initial generation, the compile repair,
the QA repair, the critic repair, and the fidelity repair all consume from the
SAME budget.

DESIGN NOTES
------------
* Stdlib only. No new dependency, no new model call, no new abstraction layer.
* Durable: a reservation is fsynced to disk BEFORE the FRONTEND model runs, so a
  crash, restart, timeout, or an ambiguous outcome can never refund it.
* Its OWN lock file (NEVER ``ProjectStateStore.acquire_writer``): the revision
  path holds the project writer lock ACROSS the FRONTEND call, so reusing that
  lock here would invert lock order and deadlock. The budget lock is held only
  for the tiny read-modify-write of the ledger, then released before the model
  runs.
* Disabled by default: ``limit is None`` means "no global cap", which is exactly
  the pre-existing production behaviour. Only the isolated staging E2E sets 2.
* Fail closed: a corrupt ledger, an unreachable lock, or a configured limit with
  no budget supplied all REFUSE the invocation rather than allowing an uncapped
  call.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# The staging E2E cap: one initial generation + AT MOST ONE repair across every
# stage combined.
DEFAULT_MAX_FRONTEND_INVOCATIONS = 2

# Bounded wait for the ledger's own lock file. The critical section is a tiny
# read-modify-write, so this is generous.
_LOCK_TIMEOUT_SECONDS = 30.0

_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize_token(token: Any) -> str:
    cleaned = _TOKEN_UNSAFE.sub("_", str(token))[:120].strip("_")
    return cleaned or "default"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class FrontendInvocationBudget:
    """A durable, atomic, per-operation FRONTEND invocation budget.

    The budget is identified by ``(project_id, operation_token)``. The token is
    STABLE for the whole operation: within one build every stage shares it, and a
    resumed revision re-uses the token derived from its already-durable ``seq``.
    A genuinely NEW operation (a new build, or the next revision seq) gets its own
    token and therefore its own ledger, so it never resets or clobbers an
    incomplete operation's accounting.
    """

    def __init__(
        self,
        ledger_path: Path,
        *,
        project_id: str,
        operation_token: str,
        limit: int,
    ):
        self.ledger_path = Path(ledger_path)
        self.project_id = project_id
        self.operation_token = operation_token
        self.limit = int(limit)
        self._lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def for_operation(
        cls,
        store,
        project_id: str,
        operation_token: str,
        limit: int,
    ) -> "FrontendInvocationBudget":
        """Build the budget for one operation, rooted in the store's state root."""
        root = Path(getattr(store, "root"))
        op_dir = root / ".frontend_budget" / _sanitize_token(project_id)
        op_dir.mkdir(parents=True, exist_ok=True)
        ledger = op_dir / f"{_sanitize_token(operation_token)}.json"
        return cls(
            ledger,
            project_id=project_id,
            operation_token=operation_token,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Locking (own lock file; never the project writer lock)
    # ------------------------------------------------------------------
    def _acquire_file_lock(self) -> bool:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(
                    str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                try:
                    payload = json.dumps(
                        {"pid": os.getpid(), "time": time.time()}
                    ).encode("utf-8")
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                if self._try_reap_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.02)

    def _try_reap_stale_lock(self) -> bool:
        try:
            raw = self._lock_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            pid = int(data.get("pid", 0))
        except Exception:  # noqa: BLE001 - a lock we cannot read is not reapable
            return False
        if pid and pid != os.getpid() and not _pid_alive(pid):
            try:
                self._lock_path.unlink()
                return True
            except FileNotFoundError:
                return True
        return False

    def _release_file_lock(self) -> None:
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Ledger I/O (atomic + fsync)
    # ------------------------------------------------------------------
    def _read_locked(self) -> Dict[str, Any]:
        """Read the ledger under the lock. Raises on a corrupt ledger."""
        if not self.ledger_path.exists():
            return {
                "project_id": self.project_id,
                "operation_token": self.operation_token,
                "limit": self.limit,
                "used": 0,
                "reservations": [],
                "refusals": [],
                "created_at": time.time(),
            }
        with self.ledger_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "used" not in data:
            raise ValueError("frontend budget ledger is malformed")
        return data

    def _write_locked(self, data: Dict[str, Any]) -> None:
        data["updated_at"] = time.time()
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.ledger_path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.ledger_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ------------------------------------------------------------------
    # The single reservation primitive
    # ------------------------------------------------------------------
    def try_acquire(
        self, *, stage: str, operation_id: Optional[str] = None
    ) -> bool:
        """Reserve ONE FRONTEND invocation, durably, or refuse.

        Returns True only when a slot was reserved AND fsynced to disk. Every
        refusal (limit reached, lock unreachable, corrupt ledger) returns False
        so the caller FAILS CLOSED and makes no model call.
        """
        entry = {
            "stage": str(stage),
            "operation_id": (
                str(operation_id) if operation_id is not None else None
            ),
            "ts": time.time(),
        }
        with self._thread_lock:
            if not self._acquire_file_lock():
                # Cannot prove current usage -> refuse rather than over-run.
                return False
            try:
                try:
                    data = self._read_locked()
                except Exception:  # noqa: BLE001 - corrupt ledger fails closed
                    return False
                used = int(data.get("used", 0))
                if used >= self.limit:
                    data.setdefault("refusals", []).append(
                        {**entry, "reason": "EXHAUSTED", "used": used}
                    )
                    try:
                        self._write_locked(data)
                    except Exception:  # noqa: BLE001 - evidence best-effort
                        pass
                    return False
                data["used"] = used + 1
                data.setdefault("reservations", []).append(
                    {**entry, "slot": used + 1}
                )
                # fsync BEFORE the caller runs the model: a crash, timeout, or
                # ambiguous outcome can never refund this slot.
                self._write_locked(data)
                return True
            finally:
                self._release_file_lock()

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------
    def has_capacity(self) -> bool:
        """A best-effort, lock-free peek: True unless the cap is already used up.

        Used ONLY to skip a stage's repair cheaply. The authoritative decision
        is always ``try_acquire`` at the FRONTEND boundary, so a stale peek can
        never cause an over-run: the worst case is a wasted attempt that the
        boundary then refuses. A corrupt/unreadable ledger returns True so the
        boundary (which fails closed) makes the real call.
        """
        try:
            with self.ledger_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return int(data.get("used", 0)) < self.limit
        except Exception:  # noqa: BLE001 - defer the decision to try_acquire
            return True

    def snapshot(self) -> Dict[str, Any]:
        """A secret-free view of this operation's budget for diagnostics."""
        try:
            with self.ledger_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {
                "operation_token": self.operation_token,
                "limit": self.limit,
                "used": 0,
                "remaining": self.limit,
                "reservations": [],
                "refusals": [],
                "corrupt": False,
            }
        except Exception:  # noqa: BLE001
            return {
                "operation_token": self.operation_token,
                "limit": self.limit,
                "used": None,
                "remaining": None,
                "reservations": [],
                "refusals": [],
                "corrupt": True,
            }
        used = int(data.get("used", 0))
        return {
            "operation_token": self.operation_token,
            "limit": self.limit,
            "used": used,
            "remaining": max(0, self.limit - used),
            "reservations": data.get("reservations", []),
            "refusals": data.get("refusals", []),
            "corrupt": False,
        }


def budget_for_operation(
    store,
    project_id: str,
    operation_token: str,
    limit: Optional[int],
) -> Optional[FrontendInvocationBudget]:
    """Return a budget when *limit* is a positive int, else ``None``.

    ``None`` (the production default) means NO global cap: the caller passes no
    budget to ``frontend_build`` and every pre-existing path is unchanged.
    """
    if limit is None:
        return None
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        return None
    if limit_i <= 0:
        return None
    return FrontendInvocationBudget.for_operation(
        store, project_id, operation_token, limit_i
    )
