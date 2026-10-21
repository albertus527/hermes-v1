"""The operator surface — the config gate and the report shape."""
import json
from pathlib import Path

import pytest

from app.core.shadow_cli import (
    ShadowEvaluationError,
    load_shadow_config,
    run_shadow_evaluation_command,
    shadow_config_help,
)
from app.core.shadow_evaluation import ShadowEvaluationRequest
from app.core.shadow_runner import ShadowConfig


class _State:
    def __init__(self, revision=7, shown=7):
        # ``shown=None`` means "no preview has been shown".
        self.lifecycle = "PREVIEW_READY"
        self.deployment = {"latest_shown_preview": (
            {"source_revision": shown, "url": "https://preview.example/x"}
            if shown is not None else {}
        )}
        self.revisions = type("R", (), {"source_revision": revision})()


def _candidate(cid="c1"):
    from app.core.decision_provider import DesignCandidate
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="n9lib://references/x", source_revision="7",
        score=0.5, requirement_version="req-1",
    )


def _workspace(tmp_path):
    from app.core.design_requirement_artifact import (
        RequirementArtifact, RequirementEntry, write_requirement_artifact)
    write_requirement_artifact(tmp_path, RequirementArtifact(
        project_id="p1", source_revision=7, requirements_version=1,
        prd_digest="d" * 64,
        entries=(RequirementEntry(
            id="R1", text="R1 real requirement", kind="content",
            evidence_kind="visual", verifiable_by="screenshot",
            origin="user_brief", trace="brief.what", mandatory=True,
            discriminating=True,
        ),),
        requirement_set_id="rs-abc",
    ))
    return tmp_path


def _scorer(suitability=0.8):
    def _fn(copies, **kwargs):
        from dataclasses import replace
        return tuple(
            replace(c, suitability=suitability,
                    suitability_detail=(("suitability", 1.0),),
                    suitability_contract="n8-design-v1")
            for c in copies
        )
    return _fn


class TestTheConfigGate:
    def test_a_config_without_enabled_is_refused(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"deadline_seconds": 20}))
        with pytest.raises(ShadowEvaluationError) as exc:
            load_shadow_config(path)
        assert "enabled" in str(exc.value)

    def test_a_disabled_config_is_refused(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"enabled": False}))
        with pytest.raises(ShadowEvaluationError):
            load_shadow_config(path)

    def test_an_unknown_key_is_refused(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"enabled": True, "bogus": 1}))
        with pytest.raises(ShadowEvaluationError) as exc:
            load_shadow_config(path)
        assert "bogus" in str(exc.value)

    def test_a_missing_config_is_refused(self, tmp_path):
        with pytest.raises(ShadowEvaluationError):
            load_shadow_config(tmp_path / "nope.json")

    def test_a_malformed_config_is_refused(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not json")
        with pytest.raises(ShadowEvaluationError):
            load_shadow_config(path)

    def test_a_valid_config_is_accepted(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({
            "enabled": True, "deadline_seconds": 15,
            "max_candidates": 4, "cost_ceiling": 0.01,
        }))
        config = load_shadow_config(path)
        assert config.enabled is True
        assert config.deadline_seconds == 15
        assert config.max_candidates == 4
        assert config.cost_ceiling == 0.01

    def test_the_help_example_enables_shadow(self):
        payload = json.loads(shadow_config_help())
        assert payload["enabled"] is True


class TestTheReport:
    def test_the_report_carries_the_attempt_status(self, tmp_path):
        _workspace(tmp_path)
        report = run_shadow_evaluation_command(
            project_id="p1", workspace=tmp_path, state=_State(),
            source_revision="7", candidates=[_candidate()],
            brief={"what": "SECRET"}, config=ShadowConfig(enabled=True),
            score_fn=_scorer(),
        )
        assert report["stored"] is True
        assert report["produced_suitability"] is True
        assert len(report["attempts"]) == 1
        attempt = report["attempts"][0]
        assert attempt["status"] == "ok"
        assert attempt["candidate_id"] == "c1"
        assert attempt["requirement_set_id"] == "rs-abc"

    def test_the_report_never_carries_the_brief_or_the_requirement_texts(
            self, tmp_path):
        _workspace(tmp_path)
        report = run_shadow_evaluation_command(
            project_id="p1", workspace=tmp_path, state=_State(),
            source_revision="7", candidates=[_candidate()],
            brief={"what": "SECRET OWNER WORDS"},
            config=ShadowConfig(enabled=True), score_fn=_scorer(),
        )
        text = json.dumps(report)
        assert "SECRET OWNER WORDS" not in text
        assert "R1 real requirement" not in text

    def test_a_refused_run_raises_and_writes_nothing(self, tmp_path):
        _workspace(tmp_path)
        with pytest.raises(ShadowEvaluationError):
            run_shadow_evaluation_command(
                project_id="p1", workspace=tmp_path,
                state=_State(shown=None),  # type: ignore[arg-type]
                source_revision="7", candidates=[_candidate()],
                brief={"what": "x"}, config=ShadowConfig(enabled=True),
                score_fn=_scorer(),
            )
        from app.core.shadow_scoring import read_shadow_record
        assert read_shadow_record(tmp_path) is None


class TestTheOperatorFlag:
    def test_the_flag_is_known_and_documented(self):
        runtime = (Path(__file__).resolve().parents[1]
                   / "app" / "runtime.py").read_text(encoding="utf-8")
        assert '"--shadow-evaluate",' in runtime
        assert '"--shadow-config",' in runtime
        assert "python -m app --shadow-evaluate" in runtime

    def test_an_unknown_flag_is_still_refused(self):
        """The shadow flag must not weaken the typo gate."""
        runtime = (Path(__file__).resolve().parents[1]
                   / "app" / "runtime.py").read_text(encoding="utf-8")
        assert "unknown = [a for a in argv if a.startswith(\"--\")" in runtime
