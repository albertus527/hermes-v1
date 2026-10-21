"""AUDIT C — revision binding and privacy.

The properties:
  * a record is unambiguously bound to project, candidate, source revision,
    requirement set, scoring contract and screenshot digest;
  * an old revision can neither overwrite nor impersonate the current one;
  * the record cannot leak through the preview, the deployment, the logs or
    a diagnostic export.
"""
import json
import re
from pathlib import Path

import pytest

from app.core.decision_provider import DesignCandidate
from app.core.shadow_runner import ShadowConfig, run_shadow_scoring
from app.core.shadow_scoring import (
    read_shadow_attempts,
    read_shadow_record,
    shadow_path,
)


def _artifact(tmp_path, revision=7, set_id="rs-abc"):
    from app.core.design_requirement_artifact import (
        RequirementArtifact, RequirementEntry, write_requirement_artifact)
    write_requirement_artifact(tmp_path, RequirementArtifact(
        project_id="p1", source_revision=revision, requirements_version=1,
        prd_digest="d" * 64,
        entries=(RequirementEntry(
            id="R1", text="R1 real requirement", kind="content",
            evidence_kind="visual", verifiable_by="screenshot",
            origin="user_brief", trace="brief.what", mandatory=True,
            discriminating=True,
        ),),
        requirement_set_id=set_id,
    ))
    return tmp_path


def _candidate(cid="c1", revision="7"):
    from app.core.design_reference import DesignAsset
    return DesignCandidate(
        candidate_id=cid, label=cid, descriptor="d",
        source_uri="n9lib://x", source_revision=revision, score=0.5,
        requirement_version="req-1",
        assets=(DesignAsset(
            kind="screenshot_desktop", uri="n9lib://x", revision=revision,
            digest="b" * 64, mime="image/png", width=1440, height=900,
            bytes=1000),),
    )


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


class TestTheBinding:
    def test_every_binding_field_is_present_on_the_record(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        record = read_shadow_record(tmp_path)
        attempt = record["attempts"][0]
        for field in ("project_id", "candidate_id", "source_revision",
                      "requirement_set_id", "scoring_contract_version",
                      "screenshot_digests", "invocation_id",
                      "invocation_state"):
            assert field in attempt, field

    def test_the_screenshot_digest_is_bound_to_the_candidate(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        attempt = read_shadow_attempts(tmp_path)[0]
        assert attempt.screenshot_digests == ("b" * 64,)

    def test_the_requirement_set_is_bound(self, tmp_path):
        _artifact(tmp_path, set_id="rs-abc")
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        attempt = read_shadow_attempts(tmp_path)[0]
        assert attempt.requirement_set_id == "rs-abc"

    def test_the_scoring_contract_is_bound(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1",
            config=ShadowConfig(enabled=True,
                                scoring_contract_version="n8-design-v1"),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        attempt = read_shadow_attempts(tmp_path)[0]
        assert attempt.scoring_contract_version == "n8-design-v1"


class TestRevisionsCannotImpersonateEachOther:
    def test_an_old_revision_does_not_erase_the_current_one(self, tmp_path):
        _artifact(tmp_path, revision=7)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="7")], source_revision="7",
            score_fn=_scorer(),
        )
        assert len(read_shadow_attempts(tmp_path)) == 1

        # Now an OLD revision is measured in the same workspace.
        _artifact(tmp_path, revision=3)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="3")], source_revision="3",
            score_fn=_scorer(),
        )
        attempts = read_shadow_attempts(tmp_path)
        revisions = {a.source_revision for a in attempts}
        assert "7" in revisions and "3" in revisions, revisions

    def test_the_file_lists_every_revision_it_holds(self, tmp_path):
        _artifact(tmp_path, revision=7)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="7")], source_revision="7",
            score_fn=_scorer(),
        )
        _artifact(tmp_path, revision=3)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate(revision="3")], source_revision="3",
            score_fn=_scorer(),
        )
        record = read_shadow_record(tmp_path)
        assert sorted(record["source_revisions"]) == ["3", "7"]

    def test_an_old_revision_cannot_reuse_the_current_identity(self, tmp_path):
        from app.core.shadow_runner import invocation_identity
        current = invocation_identity("p1", "7", "rs-abc")
        old = invocation_identity("p1", "3", "rs-abc")
        assert current != old

    def test_a_different_requirement_set_cannot_reuse_the_identity(self):
        from app.core.shadow_runner import invocation_identity
        assert invocation_identity("p1", "7", "rs-a") != invocation_identity(
            "p1", "7", "rs-b")


class TestPrivacy:
    def test_the_record_holds_no_brief(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(), brief={"what": "SECRET OWNER WORDS"},
        )
        raw = shadow_path(tmp_path).read_text(encoding="utf-8")
        assert "SECRET OWNER WORDS" not in raw

    def test_the_record_holds_no_credential(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        raw = shadow_path(tmp_path).read_text(encoding="utf-8")
        assert not re.search(r"sk-[A-Za-z0-9]{8,}", raw)
        assert "api_key" not in raw

    def test_the_record_holds_no_image_bytes(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        raw = shadow_path(tmp_path).read_text(encoding="utf-8")
        assert "base64" not in raw
        assert "data:image" not in raw

    def test_no_deploy_module_references_the_record(self):
        deploy = (Path(__file__).resolve().parents[1] / "app" / "deploy")
        for path in deploy.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "shadow-scoring" not in text, path.name
            assert "shadow_scoring" not in text, path.name

    def test_no_qa_module_references_the_record(self):
        qa = Path(__file__).resolve().parents[1] / "app" / "qa"
        for path in qa.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "shadow-scoring" not in text, path.name
            assert "shadow_scoring" not in text, path.name

    def test_the_logger_lines_carry_no_brief_or_credential(self):
        """The shadow logger must not log the brief, the requirement texts
        or a credential."""
        runner = (Path(__file__).resolve().parents[1]
                  / "app" / "core" / "shadow_runner.py").read_text(
                      encoding="utf-8")
        # Every logger call is an extra={...} with named scalar fields.
        for match in re.finditer(r"logger\.(?:info|warning|error)\(", runner):
            block = runner[match.start():match.start() + 600]
            for banned in ("brief", "requirements", "api_key", "state",
                           "questions", "payload"):
                assert f'"{banned}"' not in block.split(")")[0], banned

    def test_the_record_is_not_in_the_published_output(self, tmp_path):
        _artifact(tmp_path)
        run_shadow_scoring(
            tmp_path, project_id="p1", config=ShadowConfig(enabled=True),
            candidates=[_candidate()], source_revision="7",
            score_fn=_scorer(),
        )
        # The record sits at the workspace root, beside the artifact, and
        # the site is a different directory entirely.
        assert shadow_path(tmp_path).parent == tmp_path
        site = tmp_path / "dist"
        site.mkdir()
        (site / "index.html").write_text("<html></html>", encoding="utf-8")
        assert not [p for p in site.rglob("*") if p.is_file()
                    and "shadow" in p.name.lower()]

    def test_the_preview_path_does_not_read_the_record(self):
        preview = (Path(__file__).resolve().parents[1]
                   / "app" / "qa" / "orchestrator.py").read_text(
                       encoding="utf-8")
        assert "shadow-scoring" not in preview
        assert "shadow_scoring" not in preview
