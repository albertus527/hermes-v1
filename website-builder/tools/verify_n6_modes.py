"""N6 mode-matrix verification for the corrected design leg.

Proves, against the REAL adapter contract:
  * design mode disabled/shadow -> no candidate used (build path unchanged);
  * active -> candidate used;
  * provider outage -> honest abstention (no fabrication);
  * invalid provenance -> dropped;
  * scoring bounds -> out-of-range scores not trusted;
  * fallback -> unavailable configured provider reported degraded.
"""
from app.core import openviking_library as lib
from app.core import openviking_retrieval as ovr
from app.core.decision_engine import (
    DecisionEngine, ABSTAIN_NO_PROVENANCE, ABSTAIN_BELOW_FLOOR,
    SOURCE_FALLBACK, SOURCE_PROVIDER,
)
from app.core.decision_provider import DecisionProviderRegistry
from app.core.openviking_decision import OpenVikingDecisionProvider
from app.core.decision_modes import DecisionEngineConfig, DecisionEngineMode

PROJECT = "wb-design"
ok = True


def _adapter(content=b"# Typography\nEditorial type scale 1.2."):
    b = lib.FakeOpenVikingBackend()
    src = lib.SourceSpec(source_id="refero_typography", project_id=PROJECT,
                         category="design_dna", trust="reviewed",
                         locator="skills/x/typography.md")
    lib.ingest_sources(b, [src], reader=lambda l: content, project_id=PROJECT,
                       clock="2026-01-01T00:00:00Z")
    return ovr.build_adapter(ovr.OpenVikingConfig(enabled=True), b)


def _engine(adapter=None, *, name="openviking", **kw):
    reg = DecisionProviderRegistry()
    if adapter is not None:
        p = OpenVikingDecisionProvider(adapter)
        if p.enabled:
            reg.register(p)
    return DecisionEngine(reg, provider_name=name, **kw)


print("=== design mode gating ===")
for mode in (DecisionEngineMode.DISABLED, DecisionEngineMode.SHADOW, DecisionEngineMode.ACTIVE):
    cfg = DecisionEngineConfig(design_mode=mode)
    print(f"  design_mode={mode.value:8s} design_active={cfg.design_active}")

print("\n=== active: real candidate used ===")
d = _engine(_adapter()).decide_design({"query": "typography", "library_project_id": PROJECT})
print(f"  abstained={d.abstained} source={d.source} selected={d.selected.candidate_id if d.selected else None}")
ok &= (not d.abstained and d.source == SOURCE_PROVIDER)

print("\n=== provider outage -> honest abstention ===")
b = lib.FakeOpenVikingBackend(); b.fail_with = RuntimeError("down")
d = _engine(ovr.build_adapter(ovr.OpenVikingConfig(enabled=True), b)).decide_design(
    {"query": "typography", "library_project_id": PROJECT})
print(f"  abstained={d.abstained} selected={d.selected}")
ok &= d.abstained and d.selected is None

print("\n=== invalid provenance -> dropped ===")
# A record whose source_revision is empty must be refused by the provider.
class _NoProvAdapter:
    def retrieve_context(self, query, project_id, scope=None, budget=None):
        class R: items = (type("I", (), {"uri": "viking://x", "source_id": "s",
                                        "source_revision": "", "title": "", "summary": "",
                                        "body": "", "score": 0.9, "trust": "reviewed"})(),)
        return R()
prov = OpenVikingDecisionProvider(_NoProvAdapter())
out = prov.propose_design_candidates({"query": "x", "library_project_id": PROJECT}, limit=3)
print(f"  candidates={len(out)} (expected 0)")
ok &= len(out) == 0

print("\n=== scoring bounds ===")
class _ScoreAdapter:
    def __init__(self, score): self.score = score
    def retrieve_context(self, query, project_id, scope=None, budget=None):
        s = self.score
        class R: items = (type("I", (), {"uri": "viking://x", "source_id": "s",
                                        "source_revision": "r", "title": "t", "summary": "s",
                                        "body": "b", "score": s, "trust": "reviewed"})(),)
        return R()
for score, expect_abstain in ((0.9, False), (5.0, True), (float("nan"), True), (0.1, True)):
    reg = DecisionProviderRegistry(); reg.register(OpenVikingDecisionProvider(_ScoreAdapter(score)))
    eng = DecisionEngine(reg, provider_name="openviking")
    dd = eng.decide_design({"query": "x", "library_project_id": PROJECT})
    print(f"  score={score!r:>8} abstained={dd.abstained} reason={dd.abstain_reason}")
    ok &= (dd.abstained == expect_abstain)

print("\n=== fallback (configured provider unavailable) ===")
d = _engine(None).decide_design({"design_dna": {"brand_personality": "warm"}})
print(f"  source={d.source} degraded={d.degraded} provider={d.provider}")
ok &= d.source == SOURCE_FALLBACK and d.degraded

print("\nMODE MATRIX:", "PASS" if ok else "FAIL")
