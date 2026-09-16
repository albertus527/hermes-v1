"""Phase 2 tests: lifecycle, state persistence, locking, deduplication."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
