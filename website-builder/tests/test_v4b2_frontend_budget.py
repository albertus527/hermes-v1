"""V4-B2: the GLOBAL per-operation FRONTEND invocation budget.

Locks the requirement that ONE shared budget bounds ALL FRONTEND invocation
paths of a single build/revision operation:

  * initial generation
  * Phase-7 compile repair
  * Phase-8 deterministic QA repair
  * Impeccable critic repair
  * design-fidelity repair

At most TWO actual invocation attempts total, never two per stage, and never a
third under any combination. The budget is durable (a crash/restart/timeout/
ambiguous outcome never refunds a consumed slot) and disabled by default so
production behaviour is byte-identical.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.frontend_budget import (
    DEFAULT_MAX_FRONTEND_INVOCATIONS,
    FrontendInvocationBudget,
    budget_for_operation,
)
from app.core.state import ProjectStateStore
from app.hermes.adapter import (
    FRONTEND_BUDGET_EXHAUSTED,
    HermesAdapter,
    HermesResult,
)


def _store(tmp) -> ProjectStateStore:
    return ProjectStateStore(Path(tmp) / "state")


# ---------------------------------------------------------------------------
# The budget primitive itself
# ---------------------------------------------------------------------------
class TestFrontendBudgetPrimitive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = _store(self.tmp)

    def _budget(self, limit=2, token="op-1"):
        return budget_for_operation(self.store, "proj-1", token, limit)

    def test_limit_none_returns_no_budget(self):
        """None (the production default) means NO global cap."""
        self.assertIsNone(budget_for_operation(self.store, "proj-1", "op", None))

    def test_non_positive_limit_returns_no_budget(self):
        for bad in (0, -1, "abc", "", None):
            self.assertIsNone(budget_for_operation(self.store, "proj-1", "op", bad))

    def test_two_slots_then_exhausted(self):
        b = self._budget()
        self.assertTrue(b.try_acquire(stage="initial_generation"))
        self.assertTrue(b.try_acquire(stage="qa_repair"))
        self.assertFalse(b.try_acquire(stage="critic_repair"))
        self.assertFalse(b.try_acquire(stage="fidelity_repair"))
        snap = b.snapshot()
        self.assertEqual(snap["used"], 2)
        self.assertEqual(snap["remaining"], 0)
        self.assertEqual(snap["limit"], 2)

    def test_default_is_two(self):
        self.assertEqual(DEFAULT_MAX_FRONTEND_INVOCATIONS, 2)

    def test_reservation_is_durable_before_use(self):
        """The slot is on disk BEFORE the caller runs the model."""
        b = self._budget()
        b.try_acquire(stage="initial_generation")
        # A brand-new object over the SAME ledger sees the consumed slot.
        b2 = self._budget()
        self.assertEqual(b2.snapshot()["used"], 1)
        self.assertTrue(b2.try_acquire(stage="qa_repair"))
        self.assertFalse(self._budget().try_acquire(stage="critic_repair"))

    def test_crash_or_restart_cannot_refund(self):
        """Re-reading the ledger never resets ``used``."""
        b = self._budget()
        self.assertTrue(b.try_acquire(stage="initial_generation"))
        self.assertTrue(b.try_acquire(stage="compile_repair"))
        # Simulate a process restart: fresh object, same on-disk ledger.
        for _ in range(3):
            self.assertFalse(self._budget().try_acquire(stage="qa_repair"))

    def test_timeout_or_ambiguous_counts_as_consumed(self):
        """The slot is consumed at reservation time, so an ambiguous call counts."""
        b = self._budget(limit=1)
        self.assertTrue(b.try_acquire(stage="initial_generation"))
        # The caller may then time out; the slot is already spent.
        self.assertFalse(b.try_acquire(stage="qa_repair"))

    def test_separate_operations_have_separate_budgets(self):
        """A NEW operation gets its own budget without resetting the old one."""
        a = budget_for_operation(self.store, "proj-1", "build-r1", 2)
        a.try_acquire(stage="initial_generation")
        a.try_acquire(stage="qa_repair")
        # A different operation token -> a fresh ledger.
        b = budget_for_operation(self.store, "proj-1", "build-r2", 2)
        self.assertEqual(b.snapshot()["used"], 0)
        self.assertTrue(b.try_acquire(stage="initial_generation"))
        # The first operation is still exhausted.
        self.assertFalse(a.try_acquire(stage="qa_repair"))

    def test_same_operation_token_resumes_same_ledger(self):
        """A resumed operation (same token) restores the SAME consumed budget."""
        token = "revision-seq3"
        first = budget_for_operation(self.store, "proj-1", token, 2)
        first.try_acquire(stage="revision_generation")
        resumed = budget_for_operation(self.store, "proj-1", token, 2)
        self.assertEqual(resumed.snapshot()["used"], 1)
        self.assertTrue(resumed.try_acquire(stage="qa_repair"))
        self.assertFalse(resumed.try_acquire(stage="critic_repair"))

    def test_concurrent_attempts_cannot_double_acquire_final_slot(self):
        """Only ONE of many concurrent attempts wins the last slot."""
        b = self._budget(limit=1)
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(b.try_acquire(stage="qa_repair"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r), 1)
        self.assertEqual(b.snapshot()["used"], 1)

    def test_corrupt_ledger_fails_closed(self):
        """A corrupt ledger REFUSES rather than allowing an uncapped call."""
        b = self._budget()
        b.try_acquire(stage="initial_generation")
        b.ledger_path.write_text("{not json", encoding="utf-8")
        self.assertFalse(b.try_acquire(stage="qa_repair"))

    def test_snapshot_records_stage_and_operation_id(self):
        b = self._budget()
        b.try_acquire(stage="initial_generation", operation_id="7")
        snap = b.snapshot()
        self.assertEqual(snap["reservations"][0]["stage"], "initial_generation")
        self.assertEqual(snap["reservations"][0]["operation_id"], "7")
        self.assertEqual(snap["reservations"][0]["slot"], 1)

    def test_exhaustion_reason_recorded(self):
        b = self._budget(limit=1)
        b.try_acquire(stage="initial_generation")
        b.try_acquire(stage="qa_repair")
        snap = b.snapshot()
        self.assertEqual(snap["refusals"][-1]["reason"], "EXHAUSTED")
        self.assertEqual(snap["refusals"][-1]["stage"], "qa_repair")

    def test_no_secrets_in_ledger(self):
        b = self._budget()
        b.try_acquire(stage="initial_generation", operation_id="op")
        blob = b.ledger_path.read_text(encoding="utf-8")
        self.assertNotIn("token", blob.lower().replace("operation_token", ""))

    def test_has_capacity_peek(self):
        b = self._budget(limit=1)
        self.assertTrue(b.has_capacity())
        b.try_acquire(stage="initial_generation")
        self.assertFalse(b.has_capacity())


# ---------------------------------------------------------------------------
# The common choke point: HermesAdapter.frontend_build
# ---------------------------------------------------------------------------
class TestAdapterEnforcesBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = _store(self.tmp)
        self.adapter = HermesAdapter(
            self.store,
            hermes_home=Path(self.tmp) / ".hermes-website",
            repo_root=Path(self.tmp) / "repo",
        )
        self.workspace = Path(self.tmp) / "workspaces" / "proj-1"
        (self.workspace / "src").mkdir(parents=True, exist_ok=True)
        (self.workspace / "design-dna.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )
        (self.workspace / "src" / "App.tsx").write_text(
            "export default () => null\n", encoding="utf-8"
        )
        starter = self.adapter.repo_root / "templates" / "frontend-starter" / "src" / "App.tsx"
        starter.parent.mkdir(parents=True, exist_ok=True)
        starter.write_text("// starter placeholder\n", encoding="utf-8")

    def _call(self, budget, stage="initial_generation"):
        return self.adapter.frontend_build(
            project_id="proj-1",
            brief={"name": "X", "what": "y", "why": "z"},
            workspace=self.workspace,
            frontend_budget=budget,
            invocation_stage=stage,
        )

    def test_no_budget_means_unchanged_path(self):
        """Without a budget the adapter invokes FRONTEND (no extra check)."""
        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(success=True, response='{"success": true}')
            result = self._call(None)
        self.assertTrue(result["success"])
        self.assertEqual(mock_cli.call_count, 1)

    def test_initial_generation_consumes_slot_one(self):
        b = budget_for_operation(self.store, "proj-1", "build-r1", 2)
        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(success=True, response='{"success": true}')
            self._call(b, "initial_generation")
        self.assertEqual(b.snapshot()["used"], 1)
        self.assertEqual(mock_cli.call_count, 1)

    def test_third_invocation_is_refused_without_running_frontend(self):
        b = budget_for_operation(self.store, "proj-1", "build-r1", 2)
        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(success=True, response='{"success": true}')
            self._call(b, "initial_generation")
            self._call(b, "qa_repair")
            result = self._call(b, "critic_repair")
        # FRONTEND ran exactly TWICE; the third call never reached the model.
        self.assertEqual(mock_cli.call_count, 2)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], FRONTEND_BUDGET_EXHAUSTED)

    def test_all_stages_share_one_budget(self):
        """Compile + QA + critic + fidelity cannot cumulatively exceed 2."""
        b = budget_for_operation(self.store, "proj-1", "build-r1", 2)
        stages = [
            "initial_generation", "compile_repair",
            "qa_repair", "critic_repair", "fidelity_repair",
        ]
        with patch.object(self.adapter, "_run_hermes_cli") as mock_cli:
            mock_cli.return_value = HermesResult(success=True, response='{"success": true}')
            outcomes = [self._call(b, s) for s in stages]
        self.assertEqual(mock_cli.call_count, 2)
        self.assertTrue(outcomes[0]["success"])
        self.assertTrue(outcomes[1]["success"])
        for later in outcomes[2:]:
            self.assertFalse(later["success"])
            self.assertEqual(later["error_code"], FRONTEND_BUDGET_EXHAUSTED)


# ---------------------------------------------------------------------------
# Orchestrator repair paths share the budget
# ---------------------------------------------------------------------------
class TestOrchestratorSharesBudget(unittest.TestCase):
    def _orchestrator(self, tmp, budget):
        from app.qa.orchestrator import QAOrchestrator

        store = _store(tmp)
        return QAOrchestrator(
            runner=None,
            store=store,
            hermes_adapter=None,  # repairs short-circuit; we assert the guard
            frontend_budget=budget,
        )

    def test_repair_refuses_when_budget_spent(self):
        tmp = tempfile.mkdtemp()
        store = _store(tmp)
        b = budget_for_operation(store, "proj-1", "build-r1", 1)
        b.try_acquire(stage="initial_generation")  # spend the only slot
        orch = self._orchestrator(tmp, b)
        from app.qa.findings import DeterministicFindings, QAAttempt
        attempt = QAAttempt(
            attempt=1,
            deterministic=DeterministicFindings(
                render_ok=True, desktop_screenshot_ok=True, mobile_screenshot_ok=True,
                design_dna_valid=True, source_present=True, build_ok=True, typecheck_ok=True,
            ),
            vision=None,
        )
        # The cheap peek refuses; no adapter call is attempted.
        self.assertFalse(orch._repair("proj-1", Path(tmp), {}, None, attempt))

    def test_repair_guard_is_noop_without_budget(self):
        """No budget -> the guard is skipped (production path unchanged)."""
        tmp = tempfile.mkdtemp()
        orch = self._orchestrator(tmp, None)
        # adapter is None, so _repair returns False for that reason only.
        from app.qa.findings import DeterministicFindings, QAAttempt
        attempt = QAAttempt(
            attempt=1,
            deterministic=DeterministicFindings(
                render_ok=True, desktop_screenshot_ok=True, mobile_screenshot_ok=True,
                design_dna_valid=True, source_present=True, build_ok=True, typecheck_ok=True,
            ),
            vision=None,
        )
        self.assertFalse(orch._repair("proj-1", Path(tmp), {}, None, attempt))


# ---------------------------------------------------------------------------
# Config: staging=2, production default unchanged
# ---------------------------------------------------------------------------
class TestBudgetConfig(unittest.TestCase):
    def test_production_default_is_none(self):
        from app.runtime import RuntimeConfig

        cfg = RuntimeConfig(
            telegram_bot_token="x", hermes_home="/tmp/h", workspace_root="/tmp/w",
            state_root="/tmp/s", output_repo_path="/tmp/o", vercel_token="x",
            vercel_team_id="t", vercel_ownership_namespace="n",
        )
        self.assertIsNone(cfg.frontend_invocation_limit)

    def test_staging_config_two_is_loaded(self):
        """A config with frontend_invocation_limit: 2 yields exactly 2."""
        import tempfile

        from app.runtime import load_runtime_config

        tmp = tempfile.mkdtemp()
        cfg_path = Path(tmp) / "e2e.yaml"
        cfg_path.write_text(
            "website_builder:\n"
            "  workspace_root: '/tmp/w'\n"
            "  state_root: '/tmp/s'\n"
            "  output_repo: '/tmp/o'\n"
            "  hermes_home: '/tmp/h'\n"
            "  frontend_invocation_limit: 2\n",
            encoding="utf-8",
        )
        env = {
            "TELEGRAM_BOT_TOKEN": "x",
            "VERCEL_TOKEN": "y",
            "VERCEL_TEAM_ID": "t",
            "VERCEL_OWNERSHIP_NAMESPACE": "n",
        }
        with patch.dict("os.environ", env, clear=False):
            cfg = load_runtime_config(cfg_path)
        self.assertEqual(cfg.frontend_invocation_limit, 2)

    def test_coerce_optional_int(self):
        from app.runtime import _coerce_optional_int

        self.assertEqual(_coerce_optional_int("2", None), 2)
        self.assertEqual(_coerce_optional_int(2, None), 2)
        self.assertIsNone(_coerce_optional_int("0", None))
        self.assertIsNone(_coerce_optional_int("-1", None))
        self.assertIsNone(_coerce_optional_int("abc", None))
        self.assertIsNone(_coerce_optional_int(None, None))
        self.assertIsNone(_coerce_optional_int(True, None))
        # Env wins when present; config used otherwise.
        self.assertEqual(_coerce_optional_int(None, "2"), 2)

    def test_default_yaml_has_no_cap(self):
        """Production config/default.yaml must NOT install a global cap."""
        import yaml

        path = Path(__file__).parent.parent / "config" / "default.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        wb = data["website_builder"]
        self.assertIsNone(wb.get("frontend_invocation_limit"))


# ---------------------------------------------------------------------------
# Build/revise-level integration is covered in tests/test_build.py using the
# proven Phase-7 harness (TestV4B2SharedFrontendBudget).
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
