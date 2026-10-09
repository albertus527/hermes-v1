#!/usr/bin/env python3
"""Measure live OpenViking relevance scores for relevant vs unrelated briefs.

Used to choose a principled Laya default relevance floor. Read-only; makes no
paid call (embeddings are metered $0.00 per the D4a.1 report).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.openviking_retrieval import OpenVikingConfig, build_adapter, RetrievalBudget

api_key = os.environ.get("OPENVIKING_API_KEY")
cfg = OpenVikingConfig(enabled=True, base_url="http://127.0.0.1:1933",
                       api_key=api_key, timeout_seconds=15.0)
adapter = build_adapter(cfg)

PROBES = {
    "relevant_design": "minimalist editorial landing page with botanical typography",
    "relevant_motion": "subtle page transition with reduced motion accessibility",
    "relevant_component": "accessible responsive card component design",
    "unrelated_qcd": "quantum chromodynamics lattice gauge theory renormalization",
    "unrelated_recipe": "how to bake sourdough bread with a rye starter",
    "unrelated_finance": "quarterly corporate tax filing deadlines for small business",
}
for name, q in PROBES.items():
    r = adapter.retrieve_context(q, "wb-design",
                                 scope=("design_dna", "components", "motion"),
                                 budget=RetrievalBudget(max_items=8))
    scores = sorted((round(i.score, 4) for i in r.items), reverse=True)
    print(f"{name:22s} status={r.status:8s} n={r.returned_items} scores={scores}")
