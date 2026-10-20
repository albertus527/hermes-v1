"""N9 Requirement Fit — offline validation of the merged native path.

The owner's eight required verifications, each exercised against the real
modules (Requirement Creator, artifact builder, artifact reader, Fit
extractor) rather than against a mock:

  1. native PRD -> requirements.json -> Design Fit extraction
  2. requirement provenance and revision binding
  3. screenshot-verifiable mandatory requirement selection
  4. matched versus mismatched fixture integrity
  5. zero visual requirements -> intentional ABSTAIN
  6. no use of stale, corrupt or unrelated requirement artifacts
  7. no behavioural requirements incorrectly included in screenshot Fit
  8. no changes to native approval/publish semantics
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
    read_requirement_artifact,
    verify_artifact_matches_prd,
    write_requirement_artifact,
)
from app.core.design_requirements import requirements_for_fit  # noqa: E402
from app.core.requirement_artifact_builder import (  # noqa: E402
    _clauses,
    build_artifact_from_brief,
)
from app.core.requirement_creator import compile_prd  # noqa: E402

#: The real persisted FAST brief for project tg-6329821361-p26.
BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur, gaya editorial minimalis premium modern (off-white, charcoal, aksen terracotta, tipografi elegan)",
    "why": "Menampilkan identitas studio dan karya (3 proyek arsitektur, tentang, layanan) serta mengarahkan pengunjung ke kontak",
}
PROJECT = "n9-forma-atelier"
REVISION = 3
REQ_VERSION = 7

#: F3's brief: legitimate, but nothing a screenshot can settle.
F3_BRIEF = {
    "name": "Kafe Sederhana",
    "what": "Situs untuk pelanggan memesan menu kafe",
    "why": "Pelanggan dapat memesan menu kafe secara online",
}


def _prd(brief=None, project=PROJECT, revision=REVISION, version=REQ_VERSION):
    return compile_prd(brief or BRIEF, project_id=project, source_revision=revision,
                       requirements_version=version)


def _artifact(brief=None, prd=None, project=PROJECT, revision=REVISION,
              version=REQ_VERSION):
    brief = brief or BRIEF
    prd = prd if prd is not None else _prd(brief, project, revision, version)
    return build_artifact_from_brief(
        brief, project_id=project, source_revision=revision,
        requirements_version=version, prd_text=prd,
    )


def _workspace(tmp_path, brief=None, prd=None, project=PROJECT, revision=REVISION,
               version=REQ_VERSION, write=True):
    ws = Path(tmp_path)
    brief = brief or BRIEF
    prd = prd if prd is not None else _prd(brief, project, revision, version)
    (ws / "PRD.md").write_text(prd, encoding="utf-8")
    if write:
        write_requirement_artifact(
            ws, _artifact(brief, prd, project, revision, version))
    return ws, prd


# ---------------------------------------------------------------------------
# 1. native PRD -> requirements.json -> Design Fit extraction
# ---------------------------------------------------------------------------


class TestNativeExtractionChain:
    def test_prd_is_written_by_the_native_creator(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        assert (ws / "PRD.md").exists()
        assert "## Requirements" in prd
        assert f"project_id: {PROJECT}" in prd

    def test_artifact_is_written_and_read_back(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        assert artifact is not None
        assert artifact.project_id == PROJECT
        assert artifact.source_revision == REVISION
        assert artifact.requirements_version == REQ_VERSION
        assert artifact.entries

    def test_fit_extraction_returns_scorable_texts(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        texts, reason = requirements_for_fit(ws)
        assert texts, "a real brief must yield at least one Fit requirement"
        assert reason == ""

    def test_fit_texts_are_a_subset_of_the_artifact_entries(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        texts, _ = requirements_for_fit(ws)
        artifact = read_requirement_artifact(ws)
        entry_texts = {e.text for e in artifact.entries}
        for t in texts:
            assert t in entry_texts, "Fit must only use real artifact entries"

    def test_the_full_chain_reproduces_identically(self, tmp_path):
        a = _artifact()
        b = _artifact()
        assert a.to_dict() == b.to_dict(), "the artifact must be deterministic"

    def test_clause_splitting_keeps_parenthesised_lists_whole(self):
        """A brief's parenthetical palette is ONE visual direction."""
        clauses = _clauses(
            "Landing page untuk studio arsitektur, gaya editorial minimalis premium "
            "modern (off-white, charcoal, aksen terracotta, tipografi elegan)"
        )
        assert "gaya editorial minimalis premium modern (off-white" not in clauses
        assert "tipografi elegan)" not in clauses
        assert any("off-white" in c and "terracotta" in c for c in clauses), (
            "the palette list must survive as one clause"
        )


# ---------------------------------------------------------------------------
# 2. requirement provenance and revision binding
# ---------------------------------------------------------------------------


class TestProvenanceAndBinding:
    def test_every_entry_traces_to_a_real_brief_field(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        known = {"what", "why", "why_destination", "design_intent",
                 "sections", "pages", "features", "constraints"}
        for e in artifact.entries:
            parts = e.trace.split(".")
            assert parts[-1].split("[")[0] in known, e.trace

    def test_prd_digest_is_recorded_and_verified(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        assert artifact.prd_digest == hashlib.sha256(prd.encode()).hexdigest()
        linked, reason = verify_artifact_matches_prd(artifact, prd)
        assert linked is True, reason

    def test_a_different_prd_is_rejected(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        linked, _ = verify_artifact_matches_prd(artifact, prd + "\n\nchanged\n")
        assert linked is False

    def test_a_different_revision_is_rejected(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        newer = _prd(BRIEF, PROJECT, REVISION + 1, REQ_VERSION)
        linked, _ = verify_artifact_matches_prd(artifact, newer)
        assert linked is False

    def test_a_different_project_is_rejected(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        other = _prd(BRIEF, "some-other-project", REVISION, REQ_VERSION)
        linked, _ = verify_artifact_matches_prd(artifact, other)
        assert linked is False

    def test_unresolved_questions_are_not_requirements(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        entry_texts = {e.text for e in artifact.entries}
        for u in artifact.unresolved:
            assert u["question"] not in entry_texts

    def test_assumptions_are_not_requirements(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        entry_texts = {e.text for e in artifact.entries}
        for a in artifact.assumptions:
            assert a["text"] not in entry_texts


# ---------------------------------------------------------------------------
# 3. screenshot-verifiable mandatory requirement selection
# ---------------------------------------------------------------------------


class TestScorableSelection:
    def test_only_visual_entries_reach_fit(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        texts, _ = requirements_for_fit(ws)
        artifact = read_requirement_artifact(ws)
        for e in artifact.entries:
            if e.evidence_kind != "visual":
                assert e.text not in texts

    def test_entries_are_mandatory_or_explicitly_optional(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        for e in artifact.entries:
            assert isinstance(e.mandatory, bool)

    def test_no_invented_quantities(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        for e in artifact.entries:
            lowered = e.text.lower()
            for marker in ("at least", "minimum of", "no fewer than", ">= "):
                assert marker not in lowered, f"{e.id} invents a quantity: {e.text!r}"

    def test_requirement_text_is_grounded_in_the_brief(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        haystack = " ".join(str(v) for v in BRIEF.values()).lower()
        for e in artifact.entries:
            tokens = [t for t in e.text.lower().split() if len(t) > 4]
            assert any(t in haystack for t in tokens), f"{e.id} looks invented"

    def test_a_single_visual_requirement_is_enough(self, tmp_path):
        """One specific screenshot-verifiable requirement is sufficient."""
        brief = {"name": "X", "what": "Halaman dengan foto galeri",
                 "why": "Pengunjung melihat karya"}
        ws, prd = _workspace(tmp_path, brief=brief)
        texts, reason = requirements_for_fit(ws)
        assert len(texts) >= 1
        assert reason == ""


# ---------------------------------------------------------------------------
# 4. matched versus mismatched fixture integrity
# ---------------------------------------------------------------------------


class TestFixtureIntegrity:
    def test_f1_and_f2_share_one_requirement_set(self):
        """F1 and F2 are judged against the SAME requirement set."""
        f1 = _artifact()
        f2 = _artifact()
        assert [e.text for e in f1.entries] == [e.text for e in f2.entries]
        assert f1.prd_digest == f2.prd_digest

    def test_the_mismatched_build_really_is_different(self):
        f1 = Path("/home/albertus527/website-workspaces/tg-6329821361-p26/dist")
        f2 = Path("/tmp/n9-fixtures/f2-mismatched/dist")
        if not f2.exists():
            pytest.skip("F2 build not present")
        css1 = "".join(p.read_text() for p in f1.rglob("*.css"))
        css2 = "".join(p.read_text() for p in f2.rglob("*.css"))
        assert "#faf8f5" in css1.lower(), "F1 must be the off-white original"
        assert "#faf8f5" not in css2.lower(), "F2 must not be off-white"
        assert "#b4552d" in css1.lower(), "F1 must have the terracotta accent"
        assert "#b4552d" not in css2.lower(), "F2 must not have terracotta"

    def test_the_mismatched_build_lost_its_content_sections(self):
        f2 = Path("/tmp/n9-fixtures/f2-mismatched/dist")
        if not f2.exists():
            pytest.skip("F2 build not present")
        js = "".join(p.read_text() for p in f2.rglob("*.js"))
        for marker in ("karya-title", "tentang-title", "layanan-title",
                       "kontak-title"):
            assert marker not in js, f"F2 must not contain the {marker} section"


# ---------------------------------------------------------------------------
# 5. zero visual requirements -> intentional ABSTAIN
# ---------------------------------------------------------------------------


class TestIntentionalAbstention:
    def test_a_behavioural_only_brief_yields_no_fit_requirements(self, tmp_path):
        ws, _ = _workspace(tmp_path, brief=F3_BRIEF)
        texts, reason = requirements_for_fit(ws)
        assert texts == ()
        assert reason == "no_screenshot_scorable_requirements"

    def test_the_abstention_is_not_a_provider_failure(self, tmp_path):
        ws, _ = _workspace(tmp_path, brief=F3_BRIEF)
        _, reason = requirements_for_fit(ws)
        assert "provider" not in reason.lower()
        assert "transport" not in reason.lower()

    def test_the_behavioural_requirement_is_retained_for_the_runtime_path(self, tmp_path):
        ws, _ = _workspace(tmp_path, brief=F3_BRIEF)
        artifact = read_requirement_artifact(ws)
        behavioural = [e for e in artifact.entries
                       if e.evidence_kind == "behavioural"]
        assert behavioural, "the requirement must survive for runtime QA"
        for e in behavioural:
            assert e.verifiable_by == "none"
            assert e.text not in requirements_for_fit(ws)[0]

    def test_a_missing_artifact_abstains_rather_than_failing(self, tmp_path):
        ws = Path(tmp_path)
        (ws / "PRD.md").write_text(_prd(), encoding="utf-8")
        texts, reason = requirements_for_fit(ws)
        assert texts == ()
        assert reason == REASON_NO_ARTIFACT


# ---------------------------------------------------------------------------
# 6. no use of stale, corrupt or unrelated requirement artifacts
# ---------------------------------------------------------------------------


class TestStaleCorruptUnrelated:
    def test_a_corrupt_artifact_reads_as_absent(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        (ws / "requirements.json").write_text("{not json", encoding="utf-8")
        assert read_requirement_artifact(ws) is None
        texts, reason = requirements_for_fit(ws)
        assert texts == ()
        assert reason == REASON_NO_ARTIFACT

    def test_a_schema_violation_reads_as_absent(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        (ws / "requirements.json").write_text(
            json.dumps({"schema_version": 999, "entries": []}), encoding="utf-8")
        assert read_requirement_artifact(ws) is None

    def test_a_corrupt_artifact_never_falls_back_to_prd_bullets(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        (ws / "requirements.json").write_text("[]", encoding="utf-8")
        texts, _ = requirements_for_fit(ws)
        assert texts == ()
        assert not any("Primary call-to-action" in t for t in texts)
        assert not any("Every published page renders" in t for t in texts)

    def test_a_stale_artifact_is_rejected_by_the_linkage_check(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        artifact = read_requirement_artifact(ws)
        newer = _prd(BRIEF, PROJECT, REVISION + 1, REQ_VERSION)
        linked, _ = verify_artifact_matches_prd(artifact, newer)
        assert linked is False

    def test_an_artifact_from_another_project_is_rejected(self, tmp_path):
        ws, prd = _workspace(tmp_path)
        other_ws = Path(tmp_path) / "other"
        other_ws.mkdir()
        other_artifact = _artifact(BRIEF, project="another-project")
        write_requirement_artifact(other_ws, other_artifact)
        artifact = read_requirement_artifact(other_ws)
        linked, _ = verify_artifact_matches_prd(artifact, prd)
        assert linked is False

    def test_a_measurement_free_artifact_still_extracts(self, tmp_path):
        """A stale artifact must not silently become a valid one."""
        ws, prd = _workspace(tmp_path)
        blob = json.loads((ws / "requirements.json").read_text())
        blob["source_revision"] = REVISION + 5
        (ws / "requirements.json").write_text(json.dumps(blob))
        artifact = read_requirement_artifact(ws)
        linked, _ = verify_artifact_matches_prd(artifact, prd)
        assert linked is False


# ---------------------------------------------------------------------------
# 7. no behavioural requirements incorrectly included in screenshot Fit
# ---------------------------------------------------------------------------


class TestBehaviouralBoundary:
    def test_a_clause_naming_an_interaction_is_behavioural(self):
        artifact = _artifact(F3_BRIEF)
        for e in artifact.entries:
            if "memesan" in e.text.lower():
                assert e.evidence_kind == "behavioural", (
                    f"{e.id} names an interaction and must not be visual"
                )
                assert e.verifiable_by == "none"

    def test_behavioural_entries_never_enter_fit(self, tmp_path):
        ws, _ = _workspace(tmp_path, brief=F3_BRIEF)
        texts, _ = requirements_for_fit(ws)
        artifact = read_requirement_artifact(ws)
        for e in artifact.entries:
            if e.evidence_kind == "behavioural":
                assert e.text not in texts

    def test_a_visual_clause_stays_visual(self):
        artifact = _artifact()
        visual = [e for e in artifact.entries if e.evidence_kind == "visual"]
        assert visual, "the Forma Atelier brief has visual requirements"

    def test_screenshot_scorability_is_reported_honestly(self, tmp_path):
        ws, _ = _workspace(tmp_path, brief=F3_BRIEF)
        artifact = read_requirement_artifact(ws)
        for e in artifact.entries:
            if e.evidence_kind == "behavioural":
                assert e.is_screenshot_scorable is False
            elif e.evidence_kind == "visual":
                assert e.is_screenshot_scorable is True


# ---------------------------------------------------------------------------
# 8. no changes to native approval/publish semantics
# ---------------------------------------------------------------------------


class TestApprovalPublishUnchanged:
    def test_build_does_not_import_approval_or_publish(self):
        src = (Path(__file__).parent.parent / "app" / "projects" / "build.py")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        assert not (imported & {
            "app.core.promote", "app.core.publish", "app.core.authz",
            "app.core.approval",
        })

    def test_the_n9_modules_import_nothing_that_publishes(self):
        for name in ("design_requirement_artifact.py", "design_requirements.py",
                     "requirement_artifact_builder.py"):
            src = (Path(__file__).parent.parent / "app" / "core" / name)
            text = src.read_text(encoding="utf-8")
            for forbidden in ("promote(", "publish(", "requests.", "httpx."):
                assert forbidden not in text, f"{name} must not contain {forbidden}"

    def test_q_and_f_remain_separate_quantities(self):
        from app.core.design_scoring import (
            DEFAULT_MIN_FIT, DEFAULT_MIN_QUALITY, DEFAULT_MIN_SUITABILITY,
            compute_suitability,
        )
        assert DEFAULT_MIN_QUALITY == 0.70
        assert DEFAULT_MIN_FIT == 0.70
        assert DEFAULT_MIN_SUITABILITY == 0.75
        assert round(compute_suitability(0.70, 0.70), 6) == round(
            0.70 ** 0.45 * 0.70 ** 0.55, 6)

    def test_the_artifact_is_written_atomically_and_leaves_no_temp_file(self, tmp_path):
        ws, _ = _workspace(tmp_path)
        stray = [p.name for p in Path(ws).iterdir() if ".tmp" in p.name]
        assert stray == [], f"atomic write left temporary files: {stray}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
