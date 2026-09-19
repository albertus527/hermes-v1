"""Phase 2 tests: lifecycle, state persistence, locking, deduplication."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.core.lifecycle import (
    LifecycleError,
    ProjectLifecycle,
    can_transition,
    transition,
)
from app.core.state import ProjectStateStore


class TestLifecycle(unittest.TestCase):
    def test_valid_transitions(self):
        self.assertTrue(
            can_transition(ProjectLifecycle.DISCOVERING, ProjectLifecycle.WAITING_INPUT)
        )
        self.assertTrue(
            can_transition(ProjectLifecycle.DISCOVERING, ProjectLifecycle.READY)
        )
        self.assertTrue(
            can_transition(ProjectLifecycle.READY, ProjectLifecycle.QUEUED)
        )
        self.assertTrue(
            can_transition(ProjectLifecycle.QUEUED, ProjectLifecycle.RUNNING)
        )
        self.assertTrue(
            can_transition(ProjectLifecycle.RUNNING, ProjectLifecycle.PREVIEW_READY)
        )
        self.assertTrue(
            can_transition(
                ProjectLifecycle.PREVIEW_READY, ProjectLifecycle.REVISION_REQUESTED
            )
        )
        self.assertTrue(
            can_transition(ProjectLifecycle.PREVIEW_READY, ProjectLifecycle.PUBLISHING)
        )
        self.assertTrue(
            can_transition(ProjectLifecycle.PUBLISHING, ProjectLifecycle.LIVE)
        )

    def test_invalid_transitions(self):
        self.assertFalse(
            can_transition(ProjectLifecycle.DISCOVERING, ProjectLifecycle.LIVE)
        )
        self.assertFalse(
            can_transition(ProjectLifecycle.LIVE, ProjectLifecycle.DISCOVERING)
        )
        self.assertFalse(
            can_transition(ProjectLifecycle.CANCELED, ProjectLifecycle.RUNNING)
        )

    def test_transition_raises_on_invalid(self):
        with self.assertRaises(LifecycleError):
            transition(ProjectLifecycle.DISCOVERING, ProjectLifecycle.LIVE)

    def test_transition_returns_target_on_valid(self):
        result = transition(ProjectLifecycle.DISCOVERING, ProjectLifecycle.READY)
        self.assertEqual(result, ProjectLifecycle.READY)


class TestProjectStateStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load(self):
        with self.store.acquire_writer("proj-1") as state:
            state.brief = {"name": "Northcut", "what": "barbershop"}
            state.revisions.requirements_version = 1
            self.store.save(state)

        loaded = self.store.load("proj-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.brief["name"], "Northcut")
        self.assertEqual(loaded.revisions.requirements_version, 1)

    def test_lifecycle_transition_persisted(self):
        self.store.transition_lifecycle("proj-2", ProjectLifecycle.READY)
        loaded = self.store.load("proj-2")
        self.assertEqual(loaded.lifecycle, ProjectLifecycle.READY.value)

    def test_invalid_lifecycle_transition_rejected(self):
        self.store.transition_lifecycle("proj-3", ProjectLifecycle.READY)
        with self.assertRaises(LifecycleError):
            self.store.transition_lifecycle("proj-3", ProjectLifecycle.LIVE)

    def test_one_writer_lock(self):
        """Only one writer holds the lock at a time; the contender times out.

        Synchronized on an Event that the first writer sets only AFTER it
        has actually acquired the lock (entered the `with` body), so the
        second writer never starts racing for the lock until the first
        writer provably holds it -- no wall-clock timing assumption.
        """
        acquired = []
        first_holds_lock = threading.Event()
        release_first = threading.Event()

        def first_writer():
            with self.store.acquire_writer("proj-4", timeout=2.0):
                acquired.append("first")
                first_holds_lock.set()
                # Hold the lock until the contender has proven it timed out.
                release_first.wait(5)

        def second_writer():
            assert first_holds_lock.wait(5)
            try:
                with self.store.acquire_writer("proj-4", timeout=0.1):
                    acquired.append("second")
            except TimeoutError:
                pass
            finally:
                release_first.set()

        t1 = threading.Thread(target=first_writer)
        t2 = threading.Thread(target=second_writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Only one should have acquired the lock
        self.assertEqual(len(acquired), 1)
        self.assertEqual(acquired[0], "first")

    def test_event_deduplication(self):
        self.assertFalse(self.store.is_event_processed("proj-5", "evt-1"))
        self.store.mark_event_processed("proj-5", "evt-1")
        self.assertTrue(self.store.is_event_processed("proj-5", "evt-1"))
        self.assertFalse(self.store.is_event_processed("proj-5", "evt-2"))

    def test_revision_fields(self):
        with self.store.acquire_writer("proj-6") as state:
            state.revisions.requirements_version = 2
            state.revisions.design_dna_version = 1
            state.revisions.source_revision = 3
            state.revisions.qa_revision = 1
            state.revisions.preview_revision = 2
            state.revisions.approved_revision = 1
            self.store.save(state)

        loaded = self.store.load("proj-6")
        self.assertEqual(loaded.revisions.requirements_version, 2)
        self.assertEqual(loaded.revisions.design_dna_version, 1)
        self.assertEqual(loaded.revisions.source_revision, 3)
        self.assertEqual(loaded.revisions.qa_revision, 1)
        self.assertEqual(loaded.revisions.preview_revision, 2)
        self.assertEqual(loaded.revisions.approved_revision, 1)


class TestNestedWriterDeadlock(unittest.TestCase):
    """Test that lifecycle transitions inside acquire_writer do not deadlock."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_transition_locked_does_not_reacquire(self):
        """transition_lifecycle_locked operates on already-locked state."""
        with self.store.acquire_writer("proj-nested") as state:
            # This should NOT deadlock or timeout
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.READY)
            self.store.save(state)

        loaded = self.store.load("proj-nested")
        self.assertEqual(loaded.lifecycle, ProjectLifecycle.READY.value)

    def test_transition_locked_validates(self):
        """transition_lifecycle_locked still rejects invalid transitions."""
        with self.store.acquire_writer("proj-nested-2") as state:
            # DISCOVERING -> LIVE is invalid
            with self.assertRaises(LifecycleError):
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.LIVE)

    def test_multiple_locked_transitions(self):
        """Multiple sequential locked transitions work correctly."""
        with self.store.acquire_writer("proj-nested-3") as state:
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.READY)
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.QUEUED)
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.RUNNING)
            self.store.save(state)

        loaded = self.store.load("proj-nested-3")
        self.assertEqual(loaded.lifecycle, ProjectLifecycle.RUNNING.value)

    def test_transition_lifecycle_still_works_for_unlocked_callers(self):
        """The convenience transition_lifecycle still acquires its own lock."""
        self.store.transition_lifecycle("proj-unlocked", ProjectLifecycle.READY)
        loaded = self.store.load("proj-unlocked")
        self.assertEqual(loaded.lifecycle, ProjectLifecycle.READY.value)


# ---------------------------------------------------------------------------
# HIGH-5: Crash-recoverable writer locks
# ---------------------------------------------------------------------------


class TestCrashRecoverableWriterLocks(unittest.TestCase):
    """Writer locks record pid+time so a crashed owner can be recovered,
    without ever reaping a verified live owner."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ProjectStateStore(Path(self.tmpdir) / "state")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_lock(self, project_id="proj", payload=None, raw=None):
        lock = self.store._lock_path(project_id)
        lock.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            lock.write_text(raw)
        else:
            lock.write_text(json.dumps(payload))
        return lock

    @staticmethod
    def _dead_pid() -> int:
        """Spawn a process that exits immediately and return its (now-dead) pid."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=10)
        return proc.pid

    def test_lock_payload_records_owner_pid_and_time(self):
        """A freshly-acquired writer lock records the owning pid + timestamp."""
        with self.store.acquire_writer("proj"):
            payload = json.loads(self.store._lock_path("proj").read_text())
        self.assertEqual(payload["pid"], os.getpid())
        self.assertGreater(payload["time"], 0)

    def test_dead_pid_lock_reaped_and_acquired(self):
        """A lock whose owner pid is dead is reaped so the writer proceeds."""
        self._write_lock(payload={"pid": self._dead_pid(), "time": time.time()})
        with self.store.acquire_writer("proj", timeout=5.0) as state:
            state.lifecycle = "READY"  # prove we hold the writer lock
            self.store.save(state)
        loaded = self.store.load("proj")
        self.assertEqual(loaded.lifecycle, "READY")

    def test_live_pid_lock_never_reaped(self):
        """A lock whose owner pid is ALIVE must never be unlinked."""
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            lock = self._write_lock(payload={"pid": sleeper.pid, "time": time.time()})
            with self.assertRaises(TimeoutError):
                with self.store.acquire_writer("proj", timeout=0.6):
                    pass
            self.assertTrue(lock.exists())  # not reaped
        finally:
            sleeper.kill()
            sleeper.wait(timeout=10)

    def test_own_pid_lock_not_reaped(self):
        """A lock owned by THIS process pid is never reaped (no self-reap)."""
        lock = self._write_lock(payload={"pid": os.getpid(), "time": time.time()})
        with self.assertRaises(TimeoutError):
            with self.store.acquire_writer("proj", timeout=0.6):
                pass
        self.assertTrue(lock.exists())

    def test_garbage_lock_content_not_reaped(self):
        """Unparseable lock content is never deleted (fail closed)."""
        lock = self._write_lock(raw="{not json")
        with self.assertRaises(TimeoutError):
            with self.store.acquire_writer("proj", timeout=0.4):
                pass
        self.assertTrue(lock.exists())

    def test_empty_stale_lock_reaped_by_mtime(self):
        """An EMPTY lock file older than the stale timeout is reaped."""
        lock = self._write_lock(raw="")
        old = time.time() - 120
        os.utime(lock, (old, old))
        with self.store.acquire_writer("proj", timeout=2.0) as state:
            state.lifecycle = "READY"
            self.store.save(state)
        self.assertEqual(self.store.load("proj").lifecycle, "READY")

    def test_is_pid_alive_helper(self):
        """_is_pid_alive is true for self, false for a verified-dead pid."""
        self.assertTrue(self.store._is_pid_alive(os.getpid()))
        self.assertFalse(self.store._is_pid_alive(self._dead_pid()))
        self.assertFalse(self.store._is_pid_alive(-1))


if __name__ == "__main__":
    unittest.main()
