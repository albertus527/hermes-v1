"""N8 Decision Design consumption: T10-T12, T17.

Proves the Design provider plugs into the EXISTING DecisionEngine and that
``compile_design_guidance`` receives a genuine ``DesignDecision`` -- no parallel
pipeline, no fabricated provenance.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import decisions_client as dc  # noqa: E402
from app.core import decisions_design_provider as ddp  # noqa: E402
from app.core.decision_engine import DecisionEngine  # noqa: E402
from app.core.decision_provider import (  # noqa: E402
    DecisionProviderRegistry,
    DesignCandidate,
)
from app.core.decisions_design_provider import (  # noqa: E402
    ABSTAIN_OPTION,
    DECISIONS_DESIGN_PROVIDER_NAME,
    SCOPE_RULE,
    DecisionsDesignProvider,
)


def _cand(cid, uri="okv://lib/a", rev="r1", score=0.9):
    """A candidate as the evidence pipeline would produce it.

    ``score`` matters: the existing engine's ``_rank`` drops anything below
    ``score_floor`` (default 0.5) with reason ``below_score_floor``. A real
    retrieval layer supplies a relevance score, so a 0.0 placeholder would be
    correctly rejected and would prove nothing about N8.
    """
    return DesignCandidate(
        candidate_id=cid, label=f"Label {cid}", descriptor="editorial serif",
        palette=("#111",), source_uri=uri, source_revision=rev, score=score,
    )


def _ok(payload):
    return dc.DecisionsResult(dc.OK, payload=payload, elapsed_ms=12,
                              http_status=200)


def _engine_with(provider):
    registry = DecisionProviderRegistry()
    registry.register(provider)
    return DecisionEngine(registry, provider_name=DECISIONS_DESIGN_PROVIDER_NAME)


# ---------------------------------------------------------------------------
# T10 -- scope rule reaches the model and shapes the selection
# ---------------------------------------------------------------------------

class TestT10Scope:

    def test_scope_rule_appears_in_the_prompt_sent(self):
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        captured = {}

        def _capture(**kw):
            captured.update(kw)
            return _ok({"answers": {"choice": {"choice": "c2"}}})

        with patch.object(ddp, "post_decision", _capture):
            provider.propose_design_candidates(
                {"candidates": [_cand("c1"), _cand("c2")],
                 "brief": {"what": "logo and website"}}, limit=4)

        assert SCOPE_RULE in captured["state"]
        assert "c2" in captured["questions"]["choice"]["criteria"]

    def test_website_only_option_is_selected(self):
        """The brand-work candidate is forbidden; the website-only one is chosen."""
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        brand = DesignCandidate(candidate_id="c1", label="Brand identity system",
                                descriptor="logo + mascot", source_uri="okv://a",
                                source_revision="r1")
        website = DesignCandidate(candidate_id="c2", label="Website-only scope",
                                  descriptor="website surfaces only",
                                  source_uri="okv://b", source_revision="r1")
        with patch.object(ddp, "post_decision",
                          return_value=_ok({"answers": {"choice": {"choice": "c2"}}})):
            out = provider.propose_design_candidates(
                {"candidates": [brand, website], "brief": {}}, limit=4)
        # The chosen candidate must carry the dominant score so the engine's
        # (-score, id) ranking selects it.
        chosen = [c for c in out if c.candidate_id == "c2"][0]
        assert chosen.score > max(c.score for c in out if c.candidate_id != "c2")
        # And its provenance is still the original one, not invented.
        assert chosen.source_uri == "okv://b"


# ---------------------------------------------------------------------------
# T11 -- missing provenance is dropped before ranking
# ---------------------------------------------------------------------------

class TestT11Provenance:

    def test_unprovenanced_candidate_is_never_offered(self):
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        bare = DesignCandidate(candidate_id="c1", label="x", descriptor="d")
        assert bare.has_provenance is False
        with patch.object(ddp, "post_decision") as mock_post:
            out = provider.propose_design_candidates(
                {"candidates": [bare], "brief": {}}, limit=4)
        assert out == ()
        mock_post.assert_not_called()

    def test_engine_drops_unprovenanced_candidates_from_ranking(self):
        """The EXISTING engine's provenance gate still applies."""
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        engine = _engine_with(provider)
        bare = DesignCandidate(candidate_id="c9", label="no provenance",
                               descriptor="d")
        with patch.object(ddp, "post_decision",
                          return_value=_ok({"answers": {"choice": {"choice": "c9"}}})):
            decision = engine.decide_design(
                {"candidates": [bare], "brief": {}}, limit=4)
        # Nothing provenanced was available, so the provider abstained.
        assert decision.selected is None


# ---------------------------------------------------------------------------
# T12 / T17 -- abstention
# ---------------------------------------------------------------------------

class TestT12T17Abstain:

    def test_model_abstain_produces_abstained_decision(self):
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        engine = _engine_with(provider)
        with patch.object(ddp, "post_decision",
                          return_value=_ok({"answers":
                                            {"choice": {"choice": ABSTAIN_OPTION}}})):
            decision = engine.decide_design(
                {"candidates": [_cand("c1"), _cand("c2")], "brief": {}}, limit=4)
        assert decision.abstained is True
        assert decision.selected is None

    def test_abstention_is_distinguishable_from_failure(self):
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        with patch.object(ddp, "post_decision",
                          return_value=_ok({"answers":
                                            {"choice": {"choice": ABSTAIN_OPTION}}})):
            provider.propose_design_candidates(
                {"candidates": [_cand("c1")], "brief": {}}, limit=4)
        assert provider.last_error_code == "abstained"
        assert provider.last_degraded is False

    def test_transport_failure_is_degraded_not_abstained(self):
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        with patch.object(ddp, "post_decision",
                          return_value=dc.DecisionsResult(
                              dc.HTTP_ERROR, error_code="http_529", http_status=529)):
            provider.propose_design_candidates(
                {"candidates": [_cand("c1")], "brief": {}}, limit=4)
        assert provider.last_degraded is True
        assert provider.last_error_code == "http_529"


# ---------------------------------------------------------------------------
# Genuine candidates reach guidance
# ---------------------------------------------------------------------------

class TestGenuineCandidatesReachGuidance:

    def test_selected_candidate_carries_original_provenance(self):
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        engine = _engine_with(provider)
        # Both provenanced and above the floor; equal scores so the model's
        # choice, not the ranking score, decides which one is selected.
        real = _cand("c2", uri="okv://library/ref-42", rev="rev-9", score=0.9)
        with patch.object(ddp, "post_decision",
                          return_value=_ok({"answers": {"choice": {"choice": "c2"}}})):
            decision = engine.decide_design(
                {"candidates": [_cand("c1", score=0.9), real], "brief": {}}, limit=4)
        assert decision.selected is not None
        assert decision.selected.candidate_id == "c2"
        # Provenance came from the evidence pipeline, not the model.
        assert decision.selected.source_uri == "okv://library/ref-42"
        assert decision.selected.source_revision == "rev-9"

    def test_guidance_evidence_reads_the_selection(self):
        from app.core.design_guidance import guidance_evidence
        provider = DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://x.invalid")
        engine = _engine_with(provider)
        real = _cand("c2", uri="okv://library/ref-42", rev="rev-9")
        with patch.object(ddp, "post_decision",
                          return_value=_ok({"answers": {"choice": {"choice": "c2"}}})):
            decision = engine.decide_design(
                {"candidates": [_cand("c1"), real], "brief": {}}, limit=4)
        evidence = guidance_evidence({"what": "studio"}, decision)
        assert evidence["has_candidate"] is True
        assert evidence["source_uri"] == "okv://library/ref-42"
        assert evidence["source_revision"] == "rev-9"

    def test_no_candidate_means_brief_only_evidence(self):
        from app.core.design_guidance import guidance_evidence
        evidence = guidance_evidence({"what": "studio"}, None)
        assert evidence["has_candidate"] is False
        assert evidence["source_uri"] == ""

    def test_deterministic_fallback_preserved_when_provider_absent(self):
        """Owner decision #2: the existing deterministic fallback stays."""
        registry = DecisionProviderRegistry()
        engine = DecisionEngine(registry, provider_name="nonexistent")
        decision = engine.decide_design({"candidates": [], "brief": {}}, limit=4)
        # Falls back deterministically rather than raising.
        assert decision is not None
        assert decision.provider != DECISIONS_DESIGN_PROVIDER_NAME
