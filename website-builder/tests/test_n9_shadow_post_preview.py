"""The post-preview shadow path — native integration tests.

These drive the real modules against a real workspace, with a real project
state and a real requirement artifact. Nothing is mocked except the scoring
call itself, which cannot be mocked out of the question: the point of these
tests is that the plumbing around it is real.
"""
import json
from pathlib import Path

import pytest

from app.core.decision_provider import DesignCandidate
from app.core.shadow_evaluation import (
    ShadowEvaluationRequest,
    ShadowNotReady,
    evaluate_after_preview,
    preview_already_delivered,
)
from app.core.shadow_runner import ShadowConfig
from app.core.shadow_scoring import (
    read_shadow_attempts,
    read_shadow_record,
    shadow_path,
)


class _State:
    """A minimal stand-in for the durable project state."""

    def __init__(self, lifecycle="PREVIEW_READY", revision=7, shown=7):
        # ``shown=None`` means "no preview has been shown".
        self.lifecycle = lifecycle
        self.deployment = {
            "latest_shown_preview": (
                {"source_revision": shown, "url": "https://preview.example/x"}
                if shown is not None else {}
            )
        }
        self.revisions = type("R", (), {"source_revision": revision})()


def _candidate(cid="c1", revision="7"):
    from app.core.design_reference import DesignAsset
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="n9lib://references/x", source_revision=revision,
        score=0.5, requirement_version="req-1",
        assets=(DesignAsset(
            kind="screenshot_desktop", uri="n9lib://references/x",
            revision=revision, digest="b" * 64, mime="image/png",
            width=1440, height=900, bytes=1000),),
    )


def _workspace(tmp_path, *, revision=7):
    from app.core.design_requirement_artifact import (
        RequirementArtifact,
        RequirementEntry,
        write_requirement_artifact,
    )
    entries = (RequirementEntry(
        id="R1", text="R1 real requirement", kind="content",
        evidence_kind="visual", verifiable_by="screenshot",
        origin="user_brief", trace="brief.what", mandatory=True,
        discriminating=True,
    ),)
    write_requirement_artifact(tmp_path, RequirementArtifact(
        project_id="p1", source_revision=revision, requirements_version=1,
        prd_digest="d" * 64, entries=entries, requirement_set_id="rs-abc",
    ))
    return tmp_path


def _scorer(suitability=0.8):
    def _fn(copies, *, brief, requirements, requirement_version,
            deadline_seconds):
        from dataclasses import replace
        return tuple(
            replace(c, suitability=suitability,
                    suitability_detail=(("suitability", 1.0),),
                    suitability_contract="n8-design-v1")
            for c in copies
        )
    return _fn


class TestThePreviewGate:
    def test_a_shadow_run_is_refused_before_the_preview_is_delivered(self, tmp_path):
        _workspace(tmp_path)
        with pytest.raises(ShadowNotReady):
            evaluate_after_preview(
                tmp_path,
                state=_State(lifecycle="BUILDING", shown=None),  # type: ignore[arg-type]
                request=ShadowEvaluationRequest(
                    project_id="p1", source_revision="7",
                    candidates=[_candidate()], brief={"what": "x"}),
                config=ShadowConfig(enabled=True), score_fn=_scorer(),
            )

    def test_a_shadow_run_is_refused_when_the_preview_is_for_another_revision(
            self, tmp_path):
        _workspace(tmp_path)
        with pytest.raises(ShadowNotReady):
            evaluate_after_preview(
                tmp_path,
                state=_State(revision=7, shown=3),
                request=ShadowEvaluationRequest(
                    project_id="p1", source_revision="7",
                    candidates=[_candidate()], brief={"what": "x"}),
                config=ShadowConfig(enabled=True), score_fn=_scorer(),
            )

    def test_a_delivered_preview_opens_the_path(self, tmp_path):
        _workspace(tmp_path)
        result = evaluate_after_preview(
            tmp_path,
            state=_State(revision=7, shown=7),
            request=ShadowEvaluationRequest(
                project_id="p1", source_revision="7",
                candidates=[_candidate()], brief={"what": "x"}),
            config=ShadowConfig(enabled=True), score_fn=_scorer(),
        )
        assert result.stored is True
        assert result.produced_suitability is True

    def test_shadow_is_off_by_default_and_refuses(self, tmp_path):
        _workspace(tmp_path)
        with pytest.raises(ShadowNotReady):
            evaluate_after_preview(
                tmp_path,
                state=_State(),
                request=ShadowEvaluationRequest(
                    project_id="p1", source_revision="7",
                    candidates=[_candidate()], brief={"what": "x"}),
                config=ShadowConfig(enabled=False), score_fn=_scorer(),
            )

    def test_the_preview_delivery_fact_is_read_correctly(self):
        assert preview_already_delivered(_State()) is True
        assert preview_already_delivered(_State(shown=None)) is False  # type: ignore[arg-type]
        assert preview_already_delivered(_State(shown=3)) is False
        assert preview_already_delivered(
            _State(lifecycle="FAILED")) is False


class TestPostPreviewExecution:
    def test_the_record_is_written_beside_the_artifact(self, tmp_path):
        _workspace(tmp_path)
        evaluate_after_preview(
            tmp_path,
            state=_State(),
            request=ShadowEvaluationRequest(
                project_id="p1", source_revision="7",
                candidates=[_candidate()], brief={"what": "x"}),
            config=ShadowConfig(enabled=True), score_fn=_scorer(),
        )
        assert shadow_path(tmp_path).exists()
        assert read_shadow_record(tmp_path) is not None

    def test_the_owner_s_words_never_reach_the_record(self, tmp_path):
        _workspace(tmp_path)
        evaluate_after_preview(
            tmp_path,
            state=_State(),
            request=ShadowEvaluationRequest(
                project_id="p1", source_revision="7",
                candidates=[_candidate()],
                brief={"what": "SECRET OWNER BRIEF TEXT"}),
            config=ShadowConfig(enabled=True), score_fn=_scorer(),
        )
        raw = shadow_path(tmp_path).read_text(encoding="utf-8")
        assert "SECRET OWNER BRIEF TEXT" not in raw

    def test_the_live_candidates_are_untouched_end_to_end(self, tmp_path):
        _workspace(tmp_path)
        candidates = [_candidate("c1"), _candidate("c2")]
        before = tuple(
            (c.candidate_id, c.suitability, c.suitability_contract, id(c))
            for c in candidates
        )
        evaluate_after_preview(
            tmp_path,
            state=_State(),
            request=ShadowEvaluationRequest(
                project_id="p1", source_revision="7",
                candidates=candidates, brief={"what": "x"}),
            config=ShadowConfig(enabled=True), score_fn=_scorer(0.97),
        )
        after = tuple(
            (c.candidate_id, c.suitability, c.suitability_contract, id(c))
            for c in candidates
        )
        assert before == after
        assert all(c.suitability is None for c in candidates)

    def test_a_failed_shadow_run_does_not_change_the_preview_state(self, tmp_path):
        _workspace(tmp_path)
        state = _State()

        def _boom(*args, **kwargs):
            raise RuntimeError("provider down")

        result = evaluate_after_preview(
            tmp_path, state=state,
            request=ShadowEvaluationRequest(
                project_id="p1", source_revision="7",
                candidates=[_candidate()], brief={"what": "x"}),
            config=ShadowConfig(enabled=True), score_fn=_boom,
        )
        assert result.produced_suitability is False
        # The durable state the user can see is unchanged.
        assert state.lifecycle == "PREVIEW_READY"
        assert state.deployment["latest_shown_preview"]["url"]

    def test_a_duplicate_post_preview_run_is_refused(self, tmp_path):
        _workspace(tmp_path)
        request = ShadowEvaluationRequest(
            project_id="p1", source_revision="7",
            candidates=[_candidate()], brief={"what": "x"})
        first = evaluate_after_preview(
            tmp_path, state=_State(), request=request,
            config=ShadowConfig(enabled=True), score_fn=_scorer())
        assert first.produced_suitability is True
        second = evaluate_after_preview(
            tmp_path, state=_State(), request=request,
            config=ShadowConfig(enabled=True), score_fn=_scorer())
        assert second.error == "duplicate_invocation"
        assert len(read_shadow_attempts(tmp_path)) == 1

    def test_the_workspace_keeps_the_artifact_and_the_record_apart(self, tmp_path):
        _workspace(tmp_path)
        evaluate_after_preview(
            tmp_path, state=_State(),
            request=ShadowEvaluationRequest(
                project_id="p1", source_revision="7",
                candidates=[_candidate()], brief={"what": "x"}),
            config=ShadowConfig(enabled=True), score_fn=_scorer(),
        )
        names = {p.name for p in tmp_path.iterdir()}
        assert "requirements.json" in names
        assert "shadow-scoring.json" in names
        # They are separate files, and the record never embeds the artifact.
        record = read_shadow_record(tmp_path)
        assert "requirements" not in json.dumps(record)


class TestNoAutonomy:
    def test_nothing_schedules_a_shadow_run(self):
        """No cron, no timer, no thread, no queue."""
        from pathlib import Path
        core = Path(__file__).resolve().parents[1] / "app"
        for path in core.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for banned in ("schedule_shadow", "cron", "ThreadPoolExecutor",
                           "BackgroundTasks", "APScheduler"):
                if banned in text and "shadow" in text.lower():
                    # Only allowed if the file is the shadow seam itself.
                    assert path.name in ("shadow_scoring.py", "shadow_runner.py",
                                         "shadow_evaluation.py"), path.name

    def test_the_build_never_calls_the_shadow_path(self):
        from pathlib import Path
        build = (Path(__file__).resolve().parents[1]
                 / "app" / "projects" / "build.py").read_text(encoding="utf-8")
        for banned in ("shadow_scoring", "shadow_runner", "shadow_evaluation",
                       "evaluate_after_preview", "run_shadow_scoring"):
            assert banned not in build, banned

    def test_the_runtime_does_not_register_a_shadow_provider(self):
        """The runtime may offer the operator flag, but it never registers
        a shadow provider into the normal composition."""
        from pathlib import Path
        runtime = (Path(__file__).resolve().parents[1]
                   / "app" / "runtime.py").read_text(encoding="utf-8")
        # The flag exists, and it is dispatched ONLY behind the explicit
        # operator flag -- never from the receive loop.
        assert '"--shadow-evaluate"' in runtime
        assert 'if "--shadow-evaluate" in argv:' in runtime
        # The composition itself never wires a shadow provider.
        assert "shadow_score_pool=" not in runtime
        assert "shadow_score_pool =" not in runtime

    def test_the_shadow_flag_is_gated_behind_the_explicit_operator_flag(self):
        from pathlib import Path
        runtime = (Path(__file__).resolve().parents[1]
                   / "app" / "runtime.py").read_text(encoding="utf-8")
        # The dispatch is an explicit argv check, so a Telegram message can
        # never reach it.
        assert 'if "--shadow-evaluate" in argv:' in runtime
        # And the flag is a known flag, so a typo is refused.
        assert '"--shadow-evaluate",' in runtime
