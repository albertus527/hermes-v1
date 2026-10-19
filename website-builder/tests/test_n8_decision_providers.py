"""N8 Decision providers: intake six-intent contract, design provenance, abstain.

Covers owner corrections B (design candidates must be REAL, never fabricated) and
the provider half of T13-T17.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import decisions_client as dc  # noqa: E402
from app.core import decisions_intake_provider as _intake_mod  # noqa: E402
from app.core import decisions_design_provider as _design_mod  # noqa: E402
from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.decisions_design_provider import (  # noqa: E402
    ABSTAIN_OPTION,
    SCOPE_RULE,
    DecisionsDesignProvider,
    build_design_questions,
    build_design_state,
)
from app.core.decisions_intake_provider import (  # noqa: E402
    CRITERIA,
    ROUTING_INTENTS,
    DecisionsIntakeProvider,
    build_questions,
)


def _ok(payload):
    return dc.DecisionsResult(dc.OK, payload=payload, elapsed_ms=10, http_status=200)


def _fail(code="http_529"):
    return dc.DecisionsResult(dc.HTTP_ERROR, error_code=code, elapsed_ms=5,
                              http_status=529)


# ---------------------------------------------------------------------------
# Intake provider
# ---------------------------------------------------------------------------

class TestIntakeProvider:

    def _provider(self, **kw):
        return DecisionsIntakeProvider(
            model="typesafe/jev-1.14", api_key="k",
            base_url="https://example.invalid/x", **kw,
        )

    def test_six_routing_intents_match_benchmark(self):
        assert ROUTING_INTENTS == ("INTAKE", "REVISE", "APPROVE", "PUBLISH",
                                   "CLARIFY", "OUT_OF_SCOPE")

    def test_criteria_cover_every_intent(self):
        assert set(CRITERIA) == set(ROUTING_INTENTS)

    def test_offered_options_are_restricted_to_valid_intents(self):
        """Lifecycle legality is encoded in the option set, not hoped for."""
        q = build_questions(["INTAKE", "REVISE", "APPROVE", "PUBLISH"])
        assert set(q["intent"]["criteria"]) == {"INTAKE", "REVISE", "APPROVE",
                                                "PUBLISH"}

    def test_running_offers_only_intake(self):
        q = build_questions(["INTAKE"])
        assert list(q["intent"]["criteria"]) == ["INTAKE"]

    def test_successful_classification(self):
        provider = self._provider()
        payload = {
            "model": "typesafe/jev-1.14-20261014",
            "answers": {"intent": {"type": "choice", "choice": "REVISE",
                                   "confidence": 0.97}},
            "usage": {"cost": 0.000012},
            "provider": "TypeSafe",
        }
        with patch.object(_intake_mod, "post_decision", return_value=_ok(payload)):
            routing = provider.classify(
                text="ganti warna tombol", lifecycle="PREVIEW_READY",
                valid_intents=["INTAKE", "REVISE", "APPROVE", "PUBLISH"],
            )
        assert routing is not None
        assert routing.label == "REVISE"
        assert routing.confidence == pytest.approx(0.97)
        assert routing.resolved_model == "typesafe/jev-1.14-20261014"
        assert routing.cost_usd == pytest.approx(0.000012)

    def test_lifecycle_illegal_label_is_rejected(self):
        """Model says PUBLISH at RUNNING -> not in offered set -> None."""
        provider = self._provider()
        payload = {"answers": {"intent": {"choice": "PUBLISH"}}}
        with patch.object(_intake_mod, "post_decision", return_value=_ok(payload)):
            routing = provider.classify(
                text="publish sekarang", lifecycle="RUNNING",
                valid_intents=["INTAKE"],
            )
        assert routing is None

    def test_unknown_label_is_rejected(self):
        provider = self._provider()
        payload = {"answers": {"intent": {"choice": "DELETE_EVERYTHING"}}}
        with patch.object(_intake_mod, "post_decision", return_value=_ok(payload)):
            routing = provider.classify(
                text="x", lifecycle="PREVIEW_READY",
                valid_intents=["INTAKE", "REVISE"],
            )
        assert routing is None

    def test_transport_failure_returns_none(self):
        provider = self._provider()
        with patch.object(_intake_mod, "post_decision", return_value=_fail("http_529")):
            assert provider.classify(text="x", lifecycle="PREVIEW_READY",
                                     valid_intents=["INTAKE", "REVISE"]) is None

    def test_timeout_returns_none(self):
        provider = self._provider()
        with patch.object(_intake_mod, "post_decision",
                          return_value=dc.DecisionsResult(dc.TIMEOUT,
                                                          error_code="deadline_exceeded")):
            assert provider.classify(text="x", lifecycle="PREVIEW_READY",
                                     valid_intents=["INTAKE", "REVISE"]) is None

    def test_disabled_without_model(self):
        provider = DecisionsIntakeProvider(model="", api_key="k",
                                           base_url="https://x.invalid")
        assert provider.enabled is False
        assert provider.classify(text="x", lifecycle="PREVIEW_READY",
                                 valid_intents=["INTAKE"]) is None

    def test_disabled_without_key(self):
        provider = DecisionsIntakeProvider(model="a/b", api_key="",
                                           base_url="https://x.invalid")
        assert provider.enabled is False

    def test_no_retry_on_failure(self):
        """Exactly one transport call per classify()."""
        provider = self._provider()
        calls = []

        def _count(*a, **k):
            calls.append(1)
            return _fail()

        with patch.object(_intake_mod, "post_decision", _count):
            provider.classify(text="x", lifecycle="PREVIEW_READY",
                              valid_intents=["INTAKE"])
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Design provider -- owner correction B
# ---------------------------------------------------------------------------

def _candidate(cid="c1", uri="okv://lib/ref1", rev="r1"):
    return DesignCandidate(
        candidate_id=cid, label=f"Label {cid}", descriptor="editorial serif",
        palette=("#111", "#eee"), source_uri=uri, source_revision=rev,
    )


class TestDesignProvider:

    def _provider(self):
        return DecisionsDesignProvider(
            model="openai/gpt-6-luna-decisions", api_key="k",
            base_url="https://example.invalid/x",
        )

    def test_offered_options_are_only_real_candidate_ids(self):
        cands = [_candidate("c1"), _candidate("c2")]
        q = build_design_questions(cands)
        assert set(q["choice"]["criteria"]) == {"c1", "c2", ABSTAIN_OPTION}

    def test_scope_rule_is_injected_verbatim(self):
        """The D11 fix: the website-only constraint must be VISIBLE."""
        state = build_design_state(brief={"what": "logo and website"},
                                   candidates=[_candidate()])
        assert SCOPE_RULE in state

    def test_scope_rule_can_be_omitted(self):
        state = build_design_state(brief={}, candidates=[_candidate()],
                                   include_scope_rule=False)
        assert SCOPE_RULE not in state

    def test_selection_maps_back_to_original_candidate(self):
        """Provenance must come from the evidence pipeline, never the model."""
        provider = self._provider()
        cands = [_candidate("c1"), _candidate("c2", uri="okv://other", rev="r9")]
        payload = {"answers": {"choice": {"choice": "c2"}},
                   "model": "openai/gpt-6-luna-decisions-20261006"}
        with patch.object(_design_mod, "post_decision", return_value=_ok(payload)):
            out = provider.propose_design_candidates(
                {"candidates": cands, "brief": {}}, limit=4
            )
        assert out
        # N8 B6: the artificial score promotion is gone. The model's preference
        # is expressed as an ORDER (its pick first), and every candidate keeps
        # the retrieval score it was retrieved with. Provenance stays the
        # original object's.
        chosen = [c for c in out if c.candidate_id == "c2"][0]
        assert chosen.source_uri == "okv://other"
        assert chosen.source_revision == "r9"
        assert out[0].candidate_id == "c2"
        assert all(c.score != 1.0 for c in out)

    def test_abstain_returns_empty(self):
        provider = self._provider()
        payload = {"answers": {"choice": {"choice": ABSTAIN_OPTION}}}
        with patch.object(_design_mod, "post_decision", return_value=_ok(payload)):
            out = provider.propose_design_candidates(
                {"candidates": [_candidate()], "brief": {}}, limit=4
            )
        assert out == ()
        assert provider.last_error_code == "abstained"
        assert provider.last_degraded is False

    def test_unknown_candidate_id_is_rejected(self):
        """A model naming an unoffered id must not produce a selection."""
        provider = self._provider()
        payload = {"answers": {"choice": {"choice": "c9"}}}
        with patch.object(_design_mod, "post_decision", return_value=_ok(payload)):
            out = provider.propose_design_candidates(
                {"candidates": [_candidate("c1")], "brief": {}}, limit=4
            )
        assert out == ()
        assert provider.last_error_code == "unknown_candidate"

    def test_unprovenanced_candidates_never_reach_the_model(self):
        provider = self._provider()
        bare = DesignCandidate(candidate_id="c1", label="x", descriptor="d")
        assert bare.has_provenance is False
        sent = {}

        def _capture(*a, **k):
            sent["questions"] = k.get("questions") or a[2]
            return _ok({"answers": {"choice": {"choice": "c1"}}})

        with patch.object(_design_mod, "post_decision", _capture):
            out = provider.propose_design_candidates(
                {"candidates": [bare], "brief": {}}, limit=4
            )
        assert out == ()
        assert provider.last_error_code == "no_provenanced_candidates"

    def test_no_candidates_abstains_without_calling(self):
        provider = self._provider()
        calls = []
        with patch.object(_design_mod, "post_decision",
                          side_effect=lambda *a, **k: calls.append(1)):
            out = provider.propose_design_candidates({"brief": {}}, limit=4)
        assert out == ()
        assert calls == []
        assert provider.last_error_code == "no_candidates"

    def test_transport_failure_marks_degraded(self):
        provider = self._provider()
        with patch.object(_design_mod, "post_decision", return_value=_fail("http_529")):
            out = provider.propose_design_candidates(
                {"candidates": [_candidate()], "brief": {}}, limit=4
            )
        assert out == ()
        assert provider.last_degraded is True
        assert provider.last_error_code == "http_529"

    def test_intake_half_raises_not_implemented(self):
        provider = self._provider()
        from app.core.decision_provider import DecisionProviderError
        with pytest.raises(DecisionProviderError):
            provider.classify_intake("x", {})

    def test_disabled_provider_returns_empty(self):
        provider = DecisionsDesignProvider(model="", api_key="",
                                           base_url="https://x.invalid")
        assert provider.enabled is False
        assert provider.propose_design_candidates(
            {"candidates": [_candidate()]}, limit=4
        ) == ()
