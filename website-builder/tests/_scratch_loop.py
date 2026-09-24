"""SCRATCH: reproduce the recovery loop wedge after smoke failure."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.r1_harness import LocalR1Scenario, patch_qa_boundaries, make_preview_ready  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        h = LocalR1Scenario(tmp)
        pid = h.seed_project("kitsunereading")
        ws = h.runner.create_workspace(pid)
        make_preview_ready(h.store, pid, ws)
        h.set_smoke_failure(classification="artifact_defect")
        h.hermes.set_router_decision("PROJECT_TURN", target_project_name="kitsunereading")
        h.set_intent_response("REVISE")

        # Turn 1: any message -> pre-turn reconcile_preview -> smoke fails
        h.send_user_message("halo, gimana websitenya?")
        h.restart()
        print("after turn1:")
        print("  lifecycle:", h.current_lifecycle)
        print("  intent stage:", h.preview_intent().get("stage"))
        print("  failures:", h.preview_intent().get("smoke", {}).get("failures"))
        print("  latest_shown:", bool(h.latest_shown_preview()))
        print("  telegram text:", len(h.telegram.text_calls))
        print("  dispatch events:", list(h.dispatch_events().keys()))
        for k, v in h.dispatch_events().items():
            print("    ", v)

        # Turn 2
        st = h.project_state()
        print("classify(REVISE prompt):", h._loop._classify_intent(
            "tolong ubah warnanya jadi hijau", "PREVIEW_READY", pid))
        print("fast calls:", h.hermes.fast_calls)
        h.send_user_message("tolong ubah warnanya jadi hijau")
        print("after turn2:")
        print("  lifecycle:", h.current_lifecycle)
        print("  telegram text:", len(h.telegram.text_calls))
        print("  frontend_calls:", h.hermes.frontend_calls)
        print("  revision counters:", h.revision_counters())
        for k, v in h.dispatch_events().items():
            print("    ", v)


if __name__ == "__main__":
    main()
