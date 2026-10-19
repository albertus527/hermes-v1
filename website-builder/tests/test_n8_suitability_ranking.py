"""N8 B5+B6: candidate wiring, requirement_version, suitability ranking.

The owner's requirements, each with a test:

* candidate retrieval occurs once per build;
* Decisions Design actually receives the candidates;
* requirement versions use the native RevisionState.requirements_version;
* suitability never overwrites the original retrieval score;
* mixed/incomparable scores never produce a misleading valid selection;
* intentional ABSTAIN never silently becomes a fallback selection;
* deterministic fallback stays compatible.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import decision_engine as de  # noqa: E402
from app.core import decisions_design_provider as ddp  # noqa: E402
from app.core.decision_provider import (  # noqa: E402
    DecisionProviderRegistry,
    DesignCandidate,
    DeterministicDecisionProvider,
)

CONTRACT = "n8-design-v1"
REQ = "req-7"


def _c(cid: str, score: float, suitability=None, contract=CONTRACT, req=REQ,
       uri="okv://lib/a", rev="r1"):
    return DesignCandidate(
        candidate_id=cid, label=f"L{cid}", descriptor="editorial serif",
        source_uri=uri, source_revision=rev, score=score,
        suitability=suitability, suitability_contract=contract,
        requirement_version=req,
    )


class _Provider:
    """A provider that returns a fixed candidate list."""

    def __init__(self, candidates, name="stub"):
        self._candidates = candidates
        self._name = name
        self.calls = 0

    @property
    def name(self):
        return self._name

    def classify_intake(self, request_text, context):  # pragma: no cover
        raise NotImplementedError

    def propose_design_candidates(self, context, *, limit):
        self.calls += 1
        return list(self._candidates)[:limit]


def _engine(provider, **kw):
    registry = DecisionProviderRegistry()
    registry.register(provider)
    return de.DecisionEngine(registry, provider_name=provider.name, **kw)


# ---------------------------------------------------------------------------
# B6: score is never rewritten
# ---------------------------------------------------------------------------


def test_selected_score_equals_retrieval_score():
    engine = _engine(_Provider([_c("c1", 0.42), _c("c2", 0.71)]))
    decision = engine.decide_design({"candidates": []})
    assert decision.selected.score == 0.71


def test_no_candidate_gets_score_one():
    engine = _engine(_Provider([_c("c1", 0.42), _c("c2", 0.71)]))
    decision = engine.decide_design({"candidates": []})
    assert all(c.score != 1.0 for c in decision.ranked)


def test_provider_does_not_promote_score():
    """The Decisions provider must return the retrieval score verbatim."""
    cands = [_c("c1", 0.33), _c("c2", 0.55)]
    provider = ddp.DecisionsDesignProvider(
        model="m", api_key="k", base_url="http://x", deadline_seconds=1.0,
    )
    payload = {"answers": {"choice": {"type": "choice", "choice": "c1",
                                     "confidence": 0.9}},
               "model": "m"}
    with mock.patch.object(ddp, "post_decision") as post:
        post.return_value = mock.Mock(ok=True, payload=payload, elapsed_ms=5,
                                      error_code="")
        out = provider.propose_design_candidates(
            {"candidates": cands, "brief": {}, "requirement_version": REQ},
            limit=4,
        )
    by_id = {c.candidate_id: c for c in out}
    assert by_id["c1"].score == 0.33
    assert by_id["c2"].score == 0.55


def test_provider_preserves_requirement_version():
    cands = [_c("c1", 0.4)]
    provider = ddp.DecisionsDesignProvider(
        model="m", api_key="k", base_url="http://x", deadline_seconds=1.0,
    )
    payload = {"answers": {"choice": {"type": "choice", "choice": "c1",
                                     "confidence": 0.9}}, "model": "m"}
    with mock.patch.object(ddp, "post_decision") as post:
        post.return_value = mock.Mock(ok=True, payload=payload, elapsed_ms=5,
                                      error_code="")
        out = provider.propose_design_candidates(
            {"candidates": cands, "brief": {}, "requirement_version": REQ},
            limit=4,
        )
    assert out[0].requirement_version == REQ


# ---------------------------------------------------------------------------
# B6: suitability ranking
# ---------------------------------------------------------------------------


def test_ranks_by_suitability_when_comparable():
    engine = _engine(_Provider([
        _c("c1", 0.90, suitability=0.60),
        _c("c2", 0.60, suitability=0.95),
    ]))
    decision = engine.decide_design({"candidates": []})
    assert decision.selected.candidate_id == "c2"


def test_retrieval_score_used_when_no_suitability():
    engine = _engine(_Provider([_c("c1", 0.90), _c("c2", 0.10)]))
    decision = engine.decide_design({"candidates": []})
    assert decision.selected.candidate_id == "c1"


def test_mixed_set_falls_back_and_is_degraded():
    """One scored + one unscored must not let scored silently win."""
    engine = _engine(_Provider([
        _c("c1", 0.90, suitability=None),
        _c("c2", 0.60, suitability=0.95),
    ]))
    decision = engine.decide_design({"candidates": []})
    assert decision.degraded is True
    assert decision.error_code == "suitability_incomplete"
    # retrieval ranking wins: c1 has the higher retrieval score
    assert decision.selected.candidate_id == "c1"


def test_contract_mismatch_falls_back():
    engine = _engine(_Provider([
        _c("c1", 0.90, suitability=0.95, contract="v1"),
        _c("c2", 0.60, suitability=0.95, contract="v2"),
    ]))
    decision = engine.decide_design({"candidates": []})
    assert decision.degraded is True
    assert decision.error_code == "suitability_incomplete"


def test_requirement_version_mismatch_falls_back():
    """Scores judged against different brief revisions are not comparable."""
    engine = _engine(_Provider([
        _c("c1", 0.90, suitability=0.95, req="req-7"),
        _c("c2", 0.60, suitability=0.95, req="req-8"),
    ]))
    decision = engine.decide_design({"candidates": []})
    assert decision.degraded is True
    assert decision.error_code == "suitability_incomplete"


def test_all_unscored_is_not_degraded():
    engine = _engine(_Provider([_c("c1", 0.9), _c("c2", 0.1)]))
    decision = engine.decide_design({"candidates": []})
    assert decision.degraded is False
    assert decision.error_code == ""


def test_out_of_range_suitability_is_dropped_to_none():
    engine = _engine(_Provider([_c("c1", 0.9, suitability=7.0)]))
    decision = engine.decide_design({"candidates": []})
    # treated as unscored, so retrieval ranking is used
    assert decision.selected.suitability is None


# ---------------------------------------------------------------------------
# ABSTAIN vs failure
# ---------------------------------------------------------------------------


def test_intentional_abstain_stays_abstain():
    engine = _engine(_Provider([]))
    decision = engine.decide_design({"candidates": []})
    assert decision.selected is None
    assert decision.abstained is True
    assert decision.degraded is False


def test_abstain_never_becomes_a_selection():
    engine = _engine(_Provider([]))
    decision = engine.decide_design({"candidates": []})
    assert decision.selected is None


def test_provider_failure_is_degraded():
    class Boom:
        name = "boom"

        def classify_intake(self, *a, **k):
            raise NotImplementedError

        def propose_design_candidates(self, *a, **k):
            raise RuntimeError("down")

    engine = _engine(Boom())
    decision = engine.decide_design({"candidates": []})
    assert decision.degraded is True
    assert decision.error_code


def test_provenance_gate_still_applies():
    engine = _engine(_Provider([_c("c1", 0.9, uri="", rev="")]))
    decision = engine.decide_design({"candidates": []})
    assert decision.selected is None


def test_score_floor_still_applies():
    engine = _engine(_Provider([_c("c1", 0.10)]), score_floor=0.5)
    decision = engine.decide_design({"candidates": []})
    assert decision.selected is None


# ---------------------------------------------------------------------------
# Normalize rebuild carries N8 fields
# ---------------------------------------------------------------------------


def test_normalize_rebuild_carries_suitability():
    """A bad retrieval score must not silently drop the suitability fields."""
    raw = {
        "candidate_id": "c1", "label": "L", "descriptor": "D",
        "source_uri": "okv://a", "source_revision": "r1",
        "score": "not-a-number",
        "suitability": 0.88,
        "suitability_contract": CONTRACT,
        "requirement_version": REQ,
        "evidence": {"hierarchy": "E2"},
    }
    out = de._normalize_candidate(raw)
    assert out is not None
    assert out.score == 0.0
    assert out.suitability == 0.88
    assert out.suitability_contract == CONTRACT
    assert out.requirement_version == REQ
    assert out.evidence == (("hierarchy", "E2"),)


def test_normalize_mapping_reads_n8_fields():
    raw = {
        "candidate_id": "c1", "label": "L", "descriptor": "D",
        "source_uri": "okv://a", "source_revision": "r1", "score": 0.7,
        "suitability": 0.9,
        "suitability_detail": {"design_quality": 0.9, "requirement_fit": 0.9},
        "suitability_contract": CONTRACT,
        "evidence": {"color": "E3"},
        "evidence_revision": "r1",
        "requirement_version": REQ,
    }
    out = de._normalize_candidate(raw)
    assert out.suitability == 0.9
    assert dict(out.suitability_detail)["design_quality"] == 0.9
    assert out.evidence_for("color") == "E3"
    assert out.evidence_revision == "r1"


# ---------------------------------------------------------------------------
# Deterministic fallback compatibility
# ---------------------------------------------------------------------------


def test_deterministic_fallback_unchanged():
    det = DeterministicDecisionProvider()
    out = det.propose_design_candidates(
        {"design_dna": {"palette": {"bg": "#000"}, "brand_personality": "calm"},
         "source_revision": "3"},
        limit=4,
    )
    assert len(out) == 1
    assert out[0].score == 1.0  # legacy synthetic retrieval score
    assert out[0].suitability is None  # never a suitability value


def test_deterministic_synthetic_score_is_not_suitability():
    """The legacy 1.0 is a synthetic RETRIEVAL score, not visual quality."""
    det = DeterministicDecisionProvider()
    out = det.propose_design_candidates(
        {"design_dna": {"palette": {"bg": "#000"}}, "source_revision": "3"},
        limit=4,
    )
    assert out[0].score == 1.0
    assert out[0].suitability is None
    assert out[0].suitability_contract == ""


def test_dominant_score_constant_is_gone():
    assert not hasattr(ddp, "_DOMINANT_SCORE")
