"""Phase 6 tests: workspace containment, traversal rejection, isolation, MAX_WORKERS=1."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.core.state import ProjectStateStore
from app.sandbox.runner import (
    MAX_WORKERS,
    PortAllocator,
    ProcessTracker,
    ProjectRunner,
    WorkspaceError,
    project_workspace_path,
    validate_project_id,
    validate_workspace_root,
)


class TestProjectIdValidation(unittest.TestCase):
    def test_valid_ids(self):
        self.assertEqual(validate_project_id("northcut"), "northcut")
        self.assertEqual(validate_project_id("my-project_123"), "my-project_123")
        self.assertEqual(validate_project_id("a" * 64), "a" * 64)

    def test_empty_id_rejected(self):
        with self.assertRaises(WorkspaceError):
            validate_project_id("")

    def test_too_long_rejected(self):
        with self.assertRaises(WorkspaceError):
            validate_project_id("a" * 65)

    def test_path_traversal_rejected(self):
        with self.assertRaises(WorkspaceError):
            validate_project_id("../evil")
        with self.assertRaises(WorkspaceError):
            validate_project_id("project/../other")
        with self.assertRaises(WorkspaceError):
            validate_project_id("project\\other")

    def test_invalid_chars_rejected(self):
        with self.assertRaises(WorkspaceError):
            validate_project_id("project.name")
        with self.assertRaises(WorkspaceError):
            validate_project_id("-leading-hyphen")
        with self.assertRaises(WorkspaceError):
            validate_project_id("trailing-hyphen-")


class TestWorkspaceContainment(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = Path(self.tmpdir) / "workspaces"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_workspace_path_contained(self):
        path = project_workspace_path(self.root, "proj-1")
        self.assertTrue(str(path).startswith(str(self.root.resolve())))

    def test_traversal_rejected(self):
        with self.assertRaises(WorkspaceError):
            project_workspace_path(self.root, "../escape")

    def test_workspace_created(self):
        path = project_workspace_path(self.root, "proj-2")
        path.mkdir(parents=True, exist_ok=True)
        self.assertTrue(path.exists())


class TestPortAllocator(unittest.TestCase):
    def test_deterministic_allocation(self):
        allocator = PortAllocator(base=5100, range_size=10)
        p1 = allocator.allocate()
        p2 = allocator.allocate()
        p3 = allocator.allocate()
        self.assertEqual(p1, 5100)
        self.assertEqual(p2, 5101)
        self.assertEqual(p3, 5102)

    def test_release_and_reallocate(self):
        allocator = PortAllocator(base=5100, range_size=5)
        p1 = allocator.allocate()
        allocator.release(p1)
        p2 = allocator.allocate()
        self.assertEqual(p1, p2)

    def test_exhaustion(self):
        allocator = PortAllocator(base=5100, range_size=2)
        allocator.allocate()
        allocator.allocate()
        with self.assertRaises(RuntimeError):
            allocator.allocate()


class TestProcessTracker(unittest.TestCase):
    def test_register_unregister(self):
        tracker = ProcessTracker()
        tracker.register(12345, ["npm", "run", "dev"], port=5100)
        self.assertIn(5100, tracker.active_ports())
        tracker.unregister(12345)
        self.assertNotIn(5100, tracker.active_ports())

    def test_cleanup_all(self):
        tracker = ProcessTracker()
        tracker.register(12345, ["sleep", "10"])
        tracker.register(12346, ["sleep", "10"])
        count = tracker.cleanup_all()
        self.assertGreaterEqual(count, 0)


class TestProjectRunner(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_max_workers_is_one(self):
        self.assertEqual(MAX_WORKERS, 1)

    def test_acquire_release_project(self):
        self.assertTrue(self.runner.acquire_project("proj-1"))
        self.assertFalse(self.runner.acquire_project("proj-2"))
        self.runner.release_project("proj-1")
        self.assertTrue(self.runner.acquire_project("proj-2"))

    def test_create_workspace(self):
        path = self.runner.create_workspace("proj-ws")
        self.assertTrue(path.exists())
        self.assertTrue((path / ".hermes").exists())
        self.assertTrue((path / ".browser").exists())
        self.assertTrue((path / ".runtime").exists())

    def test_workspace_isolation(self):
        path1 = self.runner.create_workspace("proj-a")
        path2 = self.runner.create_workspace("proj-b")
        self.assertNotEqual(path1, path2)
        self.assertTrue(str(path1).startswith(str(self.workspace_root.resolve())))
        self.assertTrue(str(path2).startswith(str(self.workspace_root.resolve())))

    def test_run_command_containment(self):
        workspace = self.runner.create_workspace("proj-cmd")
        with self.assertRaises(WorkspaceError):
            self.runner.run_command(
                "proj-cmd",
                ["echo", "hello"],
                cwd=Path(self.tmpdir),
            )

    def test_credentials_stripped_from_project_env(self):
        """Generated-project environment must not receive platform secrets."""
        os.environ["TELEGRAM_BOT_TOKEN"] = "secret-token-123"
        os.environ["OPENROUTER_API_KEY"] = "or-key-456"
        os.environ["VERCEL_TOKEN"] = "vercel-789"
        os.environ["NINEROUTER_API_KEY"] = "9router-key-000"
        try:
            env = self.runner._build_project_env("proj-secret", Path("/tmp/test"))
            self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
            self.assertNotIn("OPENROUTER_API_KEY", env)
            self.assertNotIn("VERCEL_TOKEN", env)
            self.assertNotIn("NINEROUTER_API_KEY", env)
            self.assertIn("HERMES_HOME", env)
            self.assertIn("PROJECT_ID", env)
            self.assertIn("WORKSPACE_ROOT", env)
        finally:
            del os.environ["TELEGRAM_BOT_TOKEN"]
            del os.environ["OPENROUTER_API_KEY"]
            del os.environ["VERCEL_TOKEN"]
            del os.environ["NINEROUTER_API_KEY"]

    def test_hermes_env_preserves_credentials(self):
        """Hermes platform environment MAY receive credentials."""
        os.environ["TELEGRAM_BOT_TOKEN"] = "secret-token-123"
        os.environ["NINEROUTER_API_KEY"] = "9router-key-000"
        try:
            env = self.runner.build_hermes_env("proj-hermes")
            self.assertIn("TELEGRAM_BOT_TOKEN", env)
            self.assertIn("NINEROUTER_API_KEY", env)
            self.assertIn("HERMES_HOME", env)
        finally:
            del os.environ["TELEGRAM_BOT_TOKEN"]
            del os.environ["NINEROUTER_API_KEY"]

    def test_source_repo_clean_check(self):
        """Source repo protection uses git status --porcelain."""
        # Use a real existing directory so the existence guard passes and the
        # (mocked) git status logic is actually exercised.
        repo = Path(self.tmpdir)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            self.assertTrue(self.runner.is_source_repo_clean(repo))

            mock_run.return_value = MagicMock(returncode=0, stdout=" M file.py")
            self.assertFalse(self.runner.is_source_repo_clean(repo))


# ---------------------------------------------------------------------------
# MEDIUM-2: OS-level port occupancy
# ---------------------------------------------------------------------------


class TestOsLevelPortOccupancy(unittest.TestCase):
    """PortAllocator must skip ports occupied at OS level while staying
    within its bounded deterministic range."""

    def test_occupied_port_skipped(self):
        allocator = PortAllocator(base=6100, range_size=5, is_free_fn=lambda p: p != 6101)
        self.assertEqual(allocator.allocate(), 6100)
        self.assertEqual(allocator.allocate(), 6102)  # 6101 occupied at OS level

    def test_all_occupied_exhausts_range(self):
        allocator = PortAllocator(base=6200, range_size=3, is_free_fn=lambda p: False)
        with self.assertRaises(RuntimeError):
            allocator.allocate()

    def test_os_level_probe_detects_real_listener(self):
        from app.sandbox.runner import _port_is_free

        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertFalse(_port_is_free(port))
        # After the listener closes the probe reports free again.
        self.assertTrue(_port_is_free(port))

    def test_default_allocator_uses_os_probe(self):
        """Constructed without an override, the allocator consults the OS probe."""
        with patch("app.sandbox.runner._port_is_free", return_value=False):
            allocator = PortAllocator(base=6300, range_size=3)
            with self.assertRaises(RuntimeError):
                allocator.allocate()


# ---------------------------------------------------------------------------
# MEDIUM-4: generated-project HERMES_HOME isolation
# ---------------------------------------------------------------------------


class TestProjectEnvIsolation(unittest.TestCase):
    """Generated-project processes must get HERMES_HOME=<workspace>/.hermes
    and must not inherit the platform Hermes profile or platform credentials.
    Hermes FRONTEND execution keeps the platform profile."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.platform_home = Path(self.tmpdir) / "platform-hermes"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(
            self.workspace_root, self.store, hermes_home=self.platform_home
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_project_env_uses_workspace_hermes_home(self):
        ws = self.runner.create_workspace("proj")
        env = self.runner._build_project_env("proj", ws)
        self.assertEqual(env["HERMES_HOME"], str(ws / ".hermes"))
        self.assertNotEqual(env["HERMES_HOME"], str(self.runner.hermes_home))
        self.assertEqual(env["PROJECT_ID"], "proj")
        self.assertEqual(env["WORKSPACE_ROOT"], str(ws))

    def test_platform_hermes_env_still_uses_platform_profile(self):
        env = self.runner.build_hermes_env("proj")
        self.assertEqual(env["HERMES_HOME"], str(self.platform_home))

    def test_platform_credentials_stripped_from_project_env(self):
        ws = self.runner.create_workspace("proj")
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "t",
                "WHATSAPP_TOKEN": "w",
                "VERCEL_TOKEN": "v",
                "NINEROUTER_API_KEY": "n",
                "OPENROUTER_API_KEY": "o",
            },
        ):
            env = self.runner._build_project_env("proj", ws)
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "WHATSAPP_TOKEN",
            "VERCEL_TOKEN",
            "NINEROUTER_API_KEY",
            "OPENROUTER_API_KEY",
        ):
            self.assertNotIn(key, env)

    def test_unlisted_credentials_never_leak_into_project_env(self):
        """M-4: a prefix denylist cannot enumerate every credential an
        operator's Hermes profile may define. The project env must be an
        ALLOWLIST, so provider keys outside the old denylist (GEMINI_,
        DEEPSEEK_, MOONSHOT_, GMI_, STRIPE_, AWS_, DATABASE_URL, ...) never
        flow into generated-project subprocesses.
        """
        ws = self.runner.create_workspace("proj")
        leaked = [
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
            "MOONSHOT_API_KEY", "GMI_API_KEY", "NVIDIA_API_KEY",
            "XAI_API_KEY", "MISTRAL_API_KEY", "STRIPE_SECRET_KEY",
            "AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "NPM_TOKEN",
            "SENTRY_DSN", "MY_SECRET_KEY", "WB_VERCEL_TOKEN",
        ]
        with patch.dict(os.environ, {k: "x" for k in leaked}):
            env = self.runner._build_project_env("proj", ws)
        for key in leaked:
            self.assertNotIn(key, env, key)
        # Benign toolchain vars still flow through.
        self.assertIn("PATH", env)
        self.assertIn("HERMES_HOME", env)
        self.assertIn("PROJECT_ID", env)
        self.assertIn("WORKSPACE_ROOT", env)


if __name__ == "__main__":
    unittest.main()
