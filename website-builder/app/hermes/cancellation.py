"""Operator cancellation registry for supervised FRONTEND runs.

Why
---
A supervised FRONTEND child is spawned into its own process group
(``start_new_session`` on POSIX, ``CREATE_NEW_PROCESS_GROUP`` on Windows), so a
Ctrl+C aimed at the runtime's own terminal never reaches it, and it survives the
runtime's own exit. Application shutdown therefore has to ask the supervisor to
stop the exact invocation it is running.

This module is the rendezvous for that request. It is deliberately NOT
supervision state:

- It holds only ``invocation_id -> threading.Event``. It never holds the child
  handle, a clock, a bound, or any liveness input.
- The supervising poll loop stays the sole owner of its own ``proc`` and performs
  teardown through the watchdog's own ``_terminate_tree``. That is what makes
  "only this exact invocation is terminated" structural rather than merely
  intended.
- Nothing in here is ever read by a timeout bound, so cancelling one invocation
  cannot influence another one's liveness.

``cancel_all`` requests that every *registered* invocation stop. That is not
process-name matching: every entry is a live child this process spawned itself,
and each is still signalled only by its own pid.

Deliberately dependency-free (stdlib only) so ``app.hermes.adapter`` can import
it at module scope. ``app.hermes.watchdog`` pulls in ``hermes_cli.oneshot``, and
the adapter must stay importable in environments where the Hermes tree is only
partially available.
"""

from __future__ import annotations

import threading
from typing import Dict, List

__all__ = ["FrontendRunCanceller"]


class FrontendRunCanceller:
    """Registry of the FRONTEND invocations currently supervised by this process.

    Usage is strictly paired: :meth:`register` before spawning the child,
    :meth:`release` once the supervising call has returned. Releasing is what
    keeps a late shutdown signal from targeting a run that has already finished.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[str, threading.Event] = {}
        self._reasons: Dict[str, str] = {}

    def register(self, invocation_id: str) -> threading.Event:
        """Start tracking *invocation_id* and return its cancellation event.

        Called BEFORE the child is spawned so there is no window in which a live
        child is unreachable from a shutdown request. Re-registering an id
        replaces any previous event.
        """
        event = threading.Event()
        with self._lock:
            self._events[invocation_id] = event
            self._reasons.pop(invocation_id, None)
        return event

    def release(self, invocation_id: str) -> None:
        """Stop tracking *invocation_id*. Idempotent; never raises."""
        with self._lock:
            self._events.pop(invocation_id, None)
            self._reasons.pop(invocation_id, None)

    def cancel(self, invocation_id: str, *, reason: str = "") -> bool:
        """Request cancellation of exactly one invocation.

        Returns ``True`` when a live invocation was signalled. An unknown id —
        a run that already finished, or one that was never registered — is
        ``False``, so a late shutdown signal is a no-op rather than an error.

        *reason* is recorded for the forensic receipt and is read back only by
        :meth:`reason_for`; the first reason wins, because the first request is
        the one that actually stopped the run.
        """
        with self._lock:
            event = self._events.get(invocation_id)
            if event is None:
                return False
            if reason:
                self._reasons.setdefault(invocation_id, reason)
        event.set()
        return True

    def cancel_all(self, *, reason: str = "") -> List[str]:
        """Request cancellation of every registered invocation.

        Returns the invocation ids that were live and signalled. Safe to call
        when nothing is running: it then returns an empty list.
        """
        with self._lock:
            events = list(self._events.items())
            if reason:
                for invocation_id, _event in events:
                    self._reasons.setdefault(invocation_id, reason)
        for _invocation_id, event in events:
            event.set()
        return [invocation_id for invocation_id, _event in events]

    def reason_for(self, invocation_id: str) -> str:
        """Why *invocation_id* was asked to stop, or ``""`` if unrecorded.

        Must be read while the invocation is still registered — the supervisor
        reads it at the moment it observes the cancellation, before it releases.
        """
        with self._lock:
            return self._reasons.get(invocation_id, "")

    def active_ids(self) -> List[str]:
        """Invocation ids currently registered, sorted for stable output."""
        with self._lock:
            return sorted(self._events)