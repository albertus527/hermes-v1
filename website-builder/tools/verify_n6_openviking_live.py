"""LIVE verification for the corrected N6 OpenViking design leg.

Runs against the REAL OpenViking server using read-only credentials. Proves:
  1. real retrieval returns provenance-bearing candidates;
  2. a candidate reaches DecisionEngine.decide_design (not abstained);
  3. the selected candidate reaches DesignGuidance.md (the reference line);
  4. the selected candidate reaches the pre-build FRONTEND context pack.
"""
import json
import os
from pathlib import Path

from app.core.openviking_retrieval import OpenVikingConfig, build_adapter
from app.core.openviking_decision import OpenVikingDecisionProvider, DESIGN_RETRIEVAL_CATEGORIES
from app.core.decision_engine import DecisionEngine
from app.core.decision_provider import DecisionProviderRegistry
from app.core.design_guidance import (
    compile_design_guidance,
    guidance_evidence,
    guidance_selection_input,
)

PROJECT = "wb-design"
BASE_URL = "http://127.0.0.1:1933"
KEY = os.environ["OV_KEY"]

cfg = OpenVikingConfig(enabled=True, base_url=BASE_URL, api_key=KEY)
adapter = build_adapter(cfg)
print("adapter enabled:", adapter.enabled, "| backend:", type(adapter._backend).__name__)

# --- 1 + 2: real retrieval -> provider -> engine ---------------------------
provider = OpenVikingDecisionProvider(adapter)
reg = DecisionProviderRegistry()
reg.register(provider)
engine = DecisionEngine(reg, provider_name="openviking")

brief = {
    "name": "Forma Atelier",
    "what": "a portfolio site for an architecture studio",
    "why": "show editorial typography and refined spacing",
}
query = " ".join(str(brief.get(k) or "") for k in ("name", "what", "why"))
print("query:", query)

candidates = provider.propose_design_candidates(
    {"query": query, "library_project_id": PROJECT}, limit=6
)
print(f"\n[1] real candidates: {len(candidates)}")
for c in candidates:
    print(f"    - {c.candidate_id} score={c.score:.4f} uri={c.source_uri} rev={c.source_revision}")

decision = engine.decide_design({"query": query, "library_project_id": PROJECT})
print(f"\n[2] decide_design abstained={decision.abstained} source={decision.source} provider={decision.provider}")
if decision.selected:
    s = decision.selected
    print(f"    SELECTED: {s.candidate_id} score={s.score:.4f}")
    print(f"    source_uri      = {s.source_uri}")
    print(f"    source_revision = {s.source_revision}")
    print(f"    has_provenance  = {s.has_provenance}")

# --- 3: DesignGuidance.md ------------------------------------------------
text = compile_design_guidance(
    brief, decision, project_id="live-verify", source_revision=1,
    design_guidance_version=1, generator_version=1,
)
print("\n[3] DesignGuidance.md reference line:")
for line in text.splitlines():
    if "reference" in line.lower() or "viking://" in line:
        print("    " + line.strip())
assert "viking://" in text, "no provenance reference reached DesignGuidance.md"
print("    -> DesignGuidance carries the candidate provenance: OK")

# --- 4: FRONTEND context pack -------------------------------------------
evidence = guidance_evidence(brief, decision)
selection_input = guidance_selection_input(evidence)
print("\n[4] FRONTEND pre-build selection input keys:", sorted(selection_input.keys()))
assert evidence.get("has_candidate") and evidence.get("source_uri")
print("    evidence.source_uri =", evidence.get("source_uri"))
print("    evidence.source_revision =", evidence.get("source_revision"))
print("    -> FRONTEND pack input carries the selected candidate: OK")

print("\nLIVE VERIFICATION: PASS")
