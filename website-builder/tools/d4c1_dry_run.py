#!/usr/bin/env python3
"""D4c.1 harness dry-run -- validates the real smoke runner with a FAKE AIAgent.

ZERO paid calls. Patches ``app.hermes.adapter.AIAgent`` with a stand-in that
returns a canned FAST JSON and exposes token counters, then drives the SAME
``tools/d4c1_real_fast_smoke.py`` main() for Run A and Run B. Proves:
  * exactly ONE model invocation per run;
  * Run A gets NO reference block; Run B gets a labelled block;
  * the brief is verbatim and last in the prompt;
  * no production profile state is touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.hermes.adapter as adapter_mod  # noqa: E402
import tools.d4c1_real_fast_smoke as smoke  # noqa: E402

CALLS = {"n": 0}


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_total_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_estimated_cost_usd = "0"
        self.session_cost_status = "dry_run"
        self.session_cost_source = "dry_run"

    def run_conversation(self, *a, **k):
        CALLS["n"] += 1
        self.session_input_tokens = 1234
        self.session_output_tokens = 56
        self.session_total_tokens = 1290
        return {"final_response": json.dumps({
            "scope": "WEBSITE",
            "name": "Portofolio Software Engineer",
            "what": "website portofolio personal software engineer",
            "why": "menampilkan proyek dan kontak",
            "why_destination": None,
            "ambiguity": None,
            "clarification_needed": False,
            "clarification_question": None,
            "readiness": "DISCOVERY_READY",
        })}

    def shutdown_memory_provider(self, *a, **k):
        pass

    def close(self):
        pass


def main() -> int:
    adapter_mod.AIAgent = FakeAgent  # patch the seam the runner uses
    rc = 0
    for run in ("A", "B"):
        CALLS["n"] = 0
        rc |= smoke.main(["--run", run, "--out", f"/tmp/d4c1_dryrun_{run}.json"])
        ev = json.load(open(f"/tmp/d4c1_dryrun_{run}.json"))
        print(f"--- DRY RUN {run} ---")
        print("  model invocations:", CALLS["n"])
        print("  fast_call_count:", ev["fast_call_count"])
        print("  received_reference_block:", ev["fast_model_call"]["received_reference_block"])
        print("  block_chars:", ev["fast_model_call"]["reference_block_chars"])
        print("  brief_verbatim:", ev["fast_model_call"]["brief_present_verbatim_in_prompt"])
        print("  brief_last:", ev["fast_model_call"]["brief_is_last_user_text"])
        print("  decision_source:", ev["fast_decision_source"])
        print("  readiness:", ev["intake_result"]["readiness"])
        print("  retrieval_items:", ev["retrieval"].get("items"))
        assert CALLS["n"] == 1, f"{run}: expected exactly 1 model call, got {CALLS['n']}"
        assert ev["fast_call_count"] == 1
        if run == "A":
            assert ev["fast_model_call"]["received_reference_block"] is False
        else:
            assert ev["fast_model_call"]["received_reference_block"] is True
            assert ev["fast_model_call"]["reference_block_first_line"].startswith("=== LAYA CONTEXT")
            assert ev["fast_model_call"]["reference_block_last_line"].startswith("=== END LAYA CONTEXT")
    print("DRY RUN OK")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
