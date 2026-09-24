"""SCRATCH: verify the wedge is fixed and the repair path is reachable.

Turn 1 (greeting, INTAKE): smoke fails (artifact_defect) -> ONE bounded status,
turn still completes; project falls through.
Turn 2 (revision request, REVISE): NO repeated reconcile (smoke_blocked),
revision actually runs (new source revision, frontend called).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.r1_harness import LocalR1Scenario, make_preview_ready  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as d:
        h = LocalR1Scenario(Path(d))
        pid = h.seed_project("kitsunereading")
        ws = h.runner.create_workspace(pid)
        make_preview_ready(h.store, pid, ws)
        h.set_smoke_failure(classification="artifact_defect")
        h.set_router_decision("PROJECT_TURN", target_project_name="kitsunereading")

        # Turn 1: greeting -> INTAKE
        h.set_intent_response("INTAKE")
        h.send_user_message("halo, gimana websitenya?")
        print("after turn1: lifecycle=%s text=%s blocked=%s attempts=%s" % (
            h.current_lifecycle, len(h.telegram.text_calls),
            h.preview_intent().get("smoke_blocked"),
            h.preview_intent().get("pre_turn_reconcile_attempts")))
        print("  last user text:", h.telegram.text_calls[-1][1][:90])

        # Turn 2: revision request -> REVISE, no repeat reconcile
        h.set_intent_response("REVISE")
        recon_before = sum(1 for v in h.dispatch_events().values()
                           if v.get("action") == "reconcile_preview")
        h.send_user_message("tolong ubah warnanya jadi hijau")
        recon_after = sum(1 for v in h.dispatch_events().values()
                          if v.get("action") == "reconcile_preview")
        print("after turn2: lifecycle=%s frontend_calls=%s text=%s" % (
            h.current_lifecycle, h.hermes.frontend_calls, len(h.telegram.text_calls)))
        print("  reconcile_preview claims: before=%s after=%s" % (recon_before, recon_after))
        print("  revision counters:", h.revision_counters())


if __name__ == "__main__":
    main()
