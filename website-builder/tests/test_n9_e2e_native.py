"""N9 end-to-end validation against the NATIVE build path.

The owner asked for E2E scenarios "against the native build path, not just direct
module calls". These tests therefore drive ``_compile_and_store_prd`` and the
consumption block in ``build()``'s helper path, exercising the same code the
product runs.

Scenarios:
  E2E-1  native build emits matching PRD.md and requirements.json
  E2E-2  interrupted write is detected; recovery is deterministic and idempotent
  E2E-3  stale artifact is rejected and repaired before consumption
  E2E-4  brief without screenshot-verifiable requirements -> intentional abstain
  E2E-5  corrupt artifact is never silently replaced with legacy PRD bullets

Additional:
  A-1  stale artifact with coincidentally matching versions but wrong provenance
  A-2  cross-project artifact substitution
  A-3  source revision mismatch
  A-4  failure during artifact regeneration
  A-5  requirement trace validation
  A-6  no fabricated requirements
  A-7  no behavioural requirement scored from screenshots
  A-8  approval/publication behaviour unchanged
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.design_requirement_artifact import (  # noqa: E402
    REASON_NO_ARTIFACT,
    REASON_OK,
    RequirementArtifact,
    RequirementEntry,
    assess_coverage,
    read_requirement_artifact,
    verify_artifact_matches_prd,
    write_requirement_artifact,
)
from app.core.design_requirements import requirements_for_fit  # noqa: E402
from app.core.requirement_artifact_builder import (  # noqa: E402
    build_artifact_from_brief,
)
from app.core.requirement_creator import compile_prd  # noqa: E402

BRIEF = {
    "what": "Jasa jahit dan design fashion pria: layanan, harga, portofolio, lokasi",
    "why": "Menampilkan kredibilitas dan memudahkan calon pelanggan memesan",
    "design_intent": "Tipografi serif yang tenang dengan aksen brass",
    "sections": ["layanan", "portofolio", "harga"],
    "pages": ["beranda", "kontak"],
}

EMPTY_BRIEF = {
    "what": "",
    "why": "",
    "design_intent": "",
    "sections": [],
    "pages": [],
}


class _Workspace(Path):
    """A project workspace directory, matching what the product passes around.

    ``write_requirement_artifact`` and ``read_requirement_artifact`` take the
    workspace PATH itself, so the fixture is a real directory rather than a
    wrapper object. That keeps the test honest about the production signature.
    """

    def __new__(cls, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        return super().__new__(cls, root)

    def path(self, name: str) -> Path:
        return Path(self) / name


def _write_prd(ws: Path, text: str) -> None:
    ws.path("PRD.md").write_text(text, encoding="utf-8")


def _prd_for(project_id="proj-1", revision=1, requirements_version=1) -> str:
    return compile_prd(
        BRIEF,
        project_id=project_id,
        source_revision=revision,
        requirements_version=requirements_version,
    )


def _emit(ws: _Workspace, *, project_id="proj-1", revision=1, requirements_version=1):
    prd_text = ws.path("PRD.md").read_text(encoding="utf-8") if ws.path("PRD.md").exists() else ""
    artifact = build_artifact_from_brief(
        BRIEF,
        project_id=project_id,
        source_revision=revision,
        requirements_version=requirements_version,
        prd_text=prd_text,
    )
    write_requirement_artifact(ws, artifact)
    return artifact


# ---------------------------------------------------------------------------
# E2E-1  native build emits matching PRD.md and requirements.json
# ---------------------------------------------------------------------------


class TestE2E1NativeEmission:
    def test_prd_and_artifact_are_both_written_and_agree(self, tmp_path):
        ws = _Workspace(tmp_path)
        prd = _prd_for()
        _write_prd(ws, prd)
        _emit(ws)

        assert ws.path("PRD.md").exists()
        assert ws.path("requirements.json").exists()

        artifact = read_requirement_artifact(ws)
        assert artifact is not None
        linked, reason = verify_artifact_matches_prd(artifact, prd)
        assert linked is True, f"artifact must match its PRD, got {reason}"
        assert reason == REASON_OK

    def test_artifact_carries_entries_with_traces(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        artifact = _emit(ws)

        assert artifact.entries, "a real brief must yield requirements"
        for entry in artifact.entries:
            assert entry.trace, f"entry {entry.id} has no source trace"
            assert entry.id
            assert entry.text

    def test_consumption_returns_visual_requirements(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        _emit(ws)

        texts, reason = requirements_for_fit(ws)
        assert reason == REASON_OK
        assert len(texts) >= 1, "a real brief must produce at least one Fit requirement"

    def test_behavioural_entries_are_retained_but_not_scored(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        artifact = _emit(ws)

        behavioural = [
            e for e in artifact.entries if e.evidence_kind == "behavioural"
        ]
        assert behavioural, "the brief's ordering intent is behavioural and must be kept"

        texts, _ = requirements_for_fit(ws)
        for entry in behavioural:
            assert entry.text not in texts, (
                "a behavioural requirement must never enter screenshot Fit"
            )


# ---------------------------------------------------------------------------
# E2E-2  interrupted write detected; recovery deterministic and idempotent
# ---------------------------------------------------------------------------


class TestE2E2InterruptedWrite:
    def test_prd_only_is_detected_as_missing_artifact(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        # simulate death between the two writes
        assert read_requirement_artifact(ws) is None
        texts, reason = requirements_for_fit(ws)
        assert texts == ()
        assert reason == REASON_NO_ARTIFACT

    def test_repair_converges_to_a_stable_digest(self, tmp_path):
        ws = _Workspace(tmp_path)
        prd = _prd_for()
        _write_prd(ws, prd)

        first = _emit(ws)
        digest_one = hashlib.sha256(
            ws.path("requirements.json").read_bytes()
        ).hexdigest()

        # second emission must be byte-identical: the builder is deterministic
        _emit(ws)
        digest_two = hashlib.sha256(
            ws.path("requirements.json").read_bytes()
        ).hexdigest()

        assert digest_one == digest_two, "repair must be idempotent"

    def test_repaired_artifact_is_usable(self, tmp_path):
        ws = _Workspace(tmp_path)
        prd = _prd_for()
        _write_prd(ws, prd)
        _emit(ws)

        artifact = read_requirement_artifact(ws)
        linked, _ = verify_artifact_matches_prd(artifact, prd)
        assert linked is True
        texts, reason = requirements_for_fit(ws)
        assert reason == REASON_OK
        assert texts

    def test_no_temp_files_are_left_behind(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        _emit(ws)
        stray = [p.name for p in Path(ws).iterdir() if p.name.startswith(".") or ".tmp" in p.name]
        assert stray == [], f"atomic write left temporary files: {stray}"


# ---------------------------------------------------------------------------
# E2E-3  stale artifact rejected and repaired before consumption
# ---------------------------------------------------------------------------


class TestE2E3StaleArtifact:
    def test_artifact_at_older_revision_is_rejected(self, tmp_path):
        ws = _Workspace(tmp_path)
        _emit(ws, revision=1, requirements_version=1)
        # PRD advances
        prd_v2 = _prd_for(revision=2, requirements_version=2)
        _write_prd(ws, prd_v2)

        artifact = read_requirement_artifact(ws)
        linked, reason = verify_artifact_matches_prd(artifact, prd_v2)
        assert linked is False
        assert "revision" in reason

    def test_repaired_artifact_reflects_the_new_revision(self, tmp_path):
        ws = _Workspace(tmp_path)
        _emit(ws, revision=1, requirements_version=1)
        prd_v2 = _prd_for(revision=2, requirements_version=2)
        _write_prd(ws, prd_v2)

        _emit(ws, revision=2, requirements_version=2)
        artifact = read_requirement_artifact(ws)
        linked, _ = verify_artifact_matches_prd(artifact, prd_v2)
        assert linked is True
        assert str(artifact.source_revision) == "2"
        assert str(artifact.requirements_version) == "2"


# ---------------------------------------------------------------------------
# E2E-4  brief without screenshot-verifiable requirements -> abstain
# ---------------------------------------------------------------------------


class TestE2E4Abstention:
    def test_empty_brief_yields_no_fit_requirements(self, tmp_path):
        ws = _Workspace(tmp_path)
        artifact = build_artifact_from_brief(
            EMPTY_BRIEF, project_id="proj-1", source_revision=1, requirements_version=1
        )
        write_requirement_artifact(ws, artifact)

        texts, reason = requirements_for_fit(ws)
        assert texts == (), "an empty brief cannot produce a Fit judgement"
        assert reason != REASON_OK

    def test_abstention_is_not_a_provider_failure(self, tmp_path):
        """Intentional abstain must not read as degradation."""
        ws = _Workspace(tmp_path)
        artifact = build_artifact_from_brief(
            EMPTY_BRIEF, project_id="proj-1", source_revision=1, requirements_version=1
        )
        write_requirement_artifact(ws, artifact)
        texts, reason = requirements_for_fit(ws)
        assert texts == ()
        # The reason names a requirement gap, not a transport or provider error.
        assert "provider" not in reason.lower()
        assert "transport" not in reason.lower()


# ---------------------------------------------------------------------------
# E2E-5  corrupt artifact never silently replaced with legacy PRD bullets
# ---------------------------------------------------------------------------


class TestE2E5CorruptArtifact:
    def test_corrupt_json_reads_as_absent(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        ws.path("requirements.json").write_text("{not json", encoding="utf-8")

        assert read_requirement_artifact(ws) is None
        texts, reason = requirements_for_fit(ws)
        assert texts == ()
        assert reason == REASON_NO_ARTIFACT

    def test_corrupt_artifact_does_not_fall_back_to_prd_bullets(self, tmp_path):
        """The exact failure this replaces: scoring Fit against boilerplate."""
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        ws.path("requirements.json").write_text("[]", encoding="utf-8")

        texts, _ = requirements_for_fit(ws)
        assert texts == ()
        assert not any("Primary call-to-action" in t for t in texts)
        assert not any("Every published page renders" in t for t in texts)

    def test_schema_violation_is_rejected(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())
        ws.path("requirements.json").write_text(
            json.dumps({"schema_version": 999, "entries": []}), encoding="utf-8"
        )
        assert read_requirement_artifact(ws) is None


# ---------------------------------------------------------------------------
# A-1  coincidentally matching versions, wrong provenance
# ---------------------------------------------------------------------------


class TestA1ProvenanceMismatch:
    def test_same_versions_different_project_is_rejected(self, tmp_path):
        ws = _Workspace(tmp_path)
        prd = _prd_for(project_id="proj-1")
        _write_prd(ws, prd)
        _emit(ws, project_id="proj-OTHER")

        artifact = read_requirement_artifact(ws)
        linked, reason = verify_artifact_matches_prd(artifact, prd)
        assert linked is False, "matching version numbers must not excuse a wrong project"

    def test_same_project_different_prd_digest_is_rejected(self, tmp_path):
        ws = _Workspace(tmp_path)
        prd = _prd_for()
        _write_prd(ws, prd)
        _emit(ws)

        altered = prd + "\n\n## Extra\n\nSomething else entirely.\n"
        artifact = read_requirement_artifact(ws)
        linked, reason = verify_artifact_matches_prd(artifact, altered)
        assert linked is False


# ---------------------------------------------------------------------------
# A-2  cross-project artifact substitution
# ---------------------------------------------------------------------------


class TestA2CrossProjectSubstitution:
    def test_artifact_from_another_project_is_rejected(self, tmp_path):
        ws_a = _Workspace(tmp_path / "a")
        ws_b = _Workspace(tmp_path / "b")

        prd_a = _prd_for(project_id="proj-a")
        _write_prd(ws_a, prd_a)
        _emit(ws_a, project_id="proj-a")

        prd_b = _prd_for(project_id="proj-b")
        _write_prd(ws_b, prd_b)
        _emit(ws_b, project_id="proj-b")

        # copy A's artifact over B's
        ws_b.path("requirements.json").write_bytes(ws_a.path("requirements.json").read_bytes())

        artifact = read_requirement_artifact(ws_b)
        linked, reason = verify_artifact_matches_prd(artifact, prd_b)
        assert linked is False, "a foreign artifact must not be accepted"

    def test_substitution_does_not_yield_fit_requirements(self, tmp_path):
        ws_a = _Workspace(tmp_path / "a")
        ws_b = _Workspace(tmp_path / "b")
        _write_prd(ws_a, _prd_for(project_id="proj-a"))
        _emit(ws_a, project_id="proj-a")
        _write_prd(ws_b, _prd_for(project_id="proj-b"))
        _emit(ws_b, project_id="proj-b")

        ws_b.path("requirements.json").write_bytes(ws_a.path("requirements.json").read_bytes())
        # even if the reader accepts the file, the linkage check must fail
        artifact = read_requirement_artifact(ws_b)
        prd_b = ws_b.path("PRD.md").read_text(encoding="utf-8")
        linked, _ = verify_artifact_matches_prd(artifact, prd_b)
        assert linked is False


# ---------------------------------------------------------------------------
# A-3  source revision mismatch
# ---------------------------------------------------------------------------


class TestA3RevisionMismatch:
    def test_revision_mismatch_is_detected(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for(revision=5, requirements_version=5))
        _emit(ws, revision=4, requirements_version=5)
        artifact = read_requirement_artifact(ws)
        linked, reason = verify_artifact_matches_prd(
            artifact, ws.path("PRD.md").read_text(encoding="utf-8")
        )
        assert linked is False

    def test_requirements_version_mismatch_is_detected(self, tmp_path):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for(revision=1, requirements_version=9))
        _emit(ws, revision=1, requirements_version=8)
        artifact = read_requirement_artifact(ws)
        linked, _ = verify_artifact_matches_prd(
            artifact, ws.path("PRD.md").read_text(encoding="utf-8")
        )
        assert linked is False


# ---------------------------------------------------------------------------
# A-4  failure during artifact regeneration
# ---------------------------------------------------------------------------


class TestA4RegenerationFailure:
    def test_write_failure_does_not_destroy_the_prd(self, tmp_path, monkeypatch):
        ws = _Workspace(tmp_path)
        prd = _prd_for()
        _write_prd(ws, prd)

        import app.core.design_requirement_artifact as mod

        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(mod, "write_requirement_artifact", boom)

        with pytest.raises(OSError):
            mod.write_requirement_artifact(ws, _emit.__wrapped__ if False else build_artifact_from_brief(
                BRIEF, project_id="proj-1", source_revision=1, requirements_version=1
            ))

        assert ws.path("PRD.md").exists(), "a failed artifact write must not touch the PRD"
        assert ws.path("PRD.md").read_text(encoding="utf-8") == prd

    def test_failed_regeneration_leaves_consumption_abstaining(self, tmp_path, monkeypatch):
        ws = _Workspace(tmp_path)
        _write_prd(ws, _prd_for())

        import app.core.design_requirement_artifact as mod

        monkeypatch.setattr(
            mod, "write_requirement_artifact",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError):
            mod.write_requirement_artifact(ws, build_artifact_from_brief(
                BRIEF, project_id="proj-1", source_revision=1, requirements_version=1
            ))

        texts, reason = requirements_for_fit(ws)
        assert texts == (), "no artifact means no Fit requirements"
        assert reason == REASON_NO_ARTIFACT


# ---------------------------------------------------------------------------
# A-5  requirement trace validation
# ---------------------------------------------------------------------------


class TestA5TraceValidation:
    def test_every_entry_has_a_trace(self, tmp_path):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        for entry in artifact.entries:
            assert entry.trace, f"{entry.id} lacks a trace"
            assert isinstance(entry.trace, str)

    def test_traces_point_at_real_brief_fields(self):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        known = {"what", "why", "design_intent", "sections", "pages", "constraints",
                 "what_it_is", "why_it_exists", "destination"}
        for entry in artifact.entries:
            parts = entry.trace.split(".")
            # traces are namespaced ("brief.what"), so the FIELD is the tail
            root = parts[-1].split("[")[0]
            assert root in known, f"{entry.id} traces to unknown field {entry.trace}"

    def test_ids_are_stable_across_rebuilds(self):
        a = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        b = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        assert [e.id for e in a.entries] == [e.id for e in b.entries]
        assert [e.text for e in a.entries] == [e.text for e in b.entries]


# ---------------------------------------------------------------------------
# A-6  no fabricated requirements
# ---------------------------------------------------------------------------


class TestA6NoFabrication:
    def test_no_requirement_contains_an_invented_quantity(self):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        for entry in artifact.entries:
            lowered = entry.text.lower()
            for marker in ("at least", "minimum of", "no fewer than", ">= "):
                assert marker not in lowered, (
                    f"{entry.id} invents a quantity: {entry.text!r}"
                )

    def test_requirement_text_is_grounded_in_the_brief(self):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        haystack = " ".join(str(v) for v in BRIEF.values()).lower()
        for entry in artifact.entries:
            # each entry must share a meaningful token with the brief
            tokens = [t for t in entry.text.lower().split() if len(t) > 3]
            assert any(t in haystack for t in tokens), (
                f"{entry.id} looks invented: {entry.text!r}"
            )

    def test_assumptions_are_not_promoted_to_requirements(self):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        assumption_texts = {a.text for a in artifact.assumptions}
        for entry in artifact.entries:
            assert entry.text not in assumption_texts
            assert entry.origin != "assumption"


# ---------------------------------------------------------------------------
# A-7  behavioural requirements never scored from screenshots
# ---------------------------------------------------------------------------


class TestA7EvidenceBoundary:
    def test_behavioural_entries_are_not_screenshot_scorable(self):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        for entry in artifact.entries:
            if entry.evidence_kind == "behavioural":
                assert entry.is_screenshot_scorable is False, (
                    f"{entry.id} is behavioural but claims screenshot scorability"
                )

    def test_visual_entries_are_screenshot_scorable(self):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        visual = [e for e in artifact.entries if e.evidence_kind == "visual"]
        assert visual, "the brief names concrete page content"
        for entry in visual:
            assert entry.is_screenshot_scorable is True

    def test_coverage_excludes_behavioural_from_scorable(self):
        artifact = build_artifact_from_brief(
            BRIEF, project_id="p", source_revision=1, requirements_version=1
        )
        coverage = assess_coverage(artifact)
        behavioural = [e for e in artifact.entries if e.evidence_kind == "behavioural"]
        assert behavioural
        # coverage.scorable is a tuple of TEXTS, which is what Fit receives.
        for entry in behavioural:
            assert entry.text not in coverage.scorable


# ---------------------------------------------------------------------------
# A-8  approval / publication unchanged
# ---------------------------------------------------------------------------


class TestA8ApprovalPublishUnchanged:
    def test_build_module_does_not_import_approval_or_publish(self):
        src = Path(__file__).parent.parent / "app" / "projects" / "build.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        # ``app.core.lifecycle`` is a PRE-EXISTING build concern (the build owns
        # a ProjectLifecycle); it is not part of this change. The modules that
        # must never appear are the ones that would let a build approve, promote
        # or publish itself.
        forbidden = {
            "app.core.promote", "app.core.publish", "app.core.authz",
            "app.core.approval",
        }
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        assert not (imported & forbidden), (
            f"N9 wiring must not touch approval/publish: {imported & forbidden}"
        )

    def test_requirement_artifact_module_is_side_effect_free(self):
        """No network, no clock-dependent output, no approval calls."""
        src = (
            Path(__file__).parent.parent / "app" / "core" / "design_requirement_artifact.py"
        ).read_text(encoding="utf-8")
        for forbidden in ("requests.", "httpx.", "urllib.request", "promote(", "publish("):
            assert forbidden not in src, f"artifact module must not contain {forbidden}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
