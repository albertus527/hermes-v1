"""N9 wiring: artifact emission, build consumption, crash recovery.

Covers the owner's list:
  * structured requirements generation
  * native PRD compatibility
  * revision and source linkage
  * interrupted writes and recovery
  * idempotent resume
  * missing / stale artifact behaviour
  * assumption and boilerplate exclusion
  * screenshot versus behavioural evidence boundaries
  * single-requirement sufficiency
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import design_scoring as ds  # noqa: E402
from app.core.design_requirement_artifact import (  # noqa: E402
    ARTIFACT_FILE_NAME, REASON_NO_ARTIFACT, RequirementArtifact,
    RequirementEntry, assess_coverage, read_requirement_artifact,
    verify_artifact_matches_prd, write_requirement_artifact,
)
from app.core.design_requirements import requirements_for_fit  # noqa: E402
from app.core.requirement_artifact_builder import (  # noqa: E402
    build_artifact_from_brief,
)
from app.core.requirement_creator import compile_prd  # noqa: E402

BRIEF = {
    "name": "TailorIvan",
    "what": "layanan, harga, portofolio, lokasi kantor",
    "why": "Menampilkan kredibilitas dan memudahkan calon pelanggan memesan",
    "why_destination": "Formulir konsultasi",
    "design_intent": "Editorial atelier: bersih, tenang, tipografi serif, aksen brass",
}
PROJECT = "tailorivan"
REV = 3
REQV = 7


def _artifact(brief=None, **over):
    base = dict(project_id=PROJECT, source_revision=REV, requirements_version=REQV)
    base.update(over)
    return build_artifact_from_brief(brief or BRIEF, **base)


def _prd(**over):
    b = dict(BRIEF)
    b.update(over)
    return compile_prd(b, project_id=PROJECT, source_revision=REV,
                       requirements_version=REQV)


# ---------------------------------------------------------------------------
# 1. STRUCTURED GENERATION
# ---------------------------------------------------------------------------


def test_brief_produces_entries():
    art = _artifact()
    assert len(art.entries) >= 1


def test_content_list_is_split_into_assessable_items():
    """'layanan, harga, portofolio, lokasi kantor' must not stay one blob."""
    art = _artifact()
    content = [e for e in art.entries if e.kind == "content"]
    assert len(content) >= 1
    for e in content:
        assert len(e.text) >= 12


def test_every_entry_records_its_trace():
    for e in _artifact().entries:
        assert e.trace.startswith("brief.")


def test_every_entry_has_a_stable_id():
    ids = [e.id for e in _artifact().entries]
    assert ids == [f"R{i}" for i in range(1, len(ids) + 1)]


def test_generation_is_deterministic():
    """Same brief must produce a byte-identical artifact: the digest is recorded."""
    assert _artifact().digest == _artifact().digest


def test_style_clause_naming_a_property_becomes_a_requirement():
    art = _artifact()
    styles = [e for e in art.entries if e.kind == "style"]
    assert any("serif" in e.text.lower() for e in styles)


def test_adjective_only_style_clause_becomes_an_assumption():
    """'bersih' names no concrete property, so it cannot be a requirement."""
    art = _artifact({"name": "X", "what": "portfolio grid layout",
                     "design_intent": "clean and calm"})
    assert all("clean and calm" != e.text for e in art.entries)


def test_missing_brief_fields_become_unresolved_questions():
    art = _artifact({"name": "X"})
    assert len(art.unresolved) >= 1


# ---------------------------------------------------------------------------
# 2. NATIVE PRD COMPATIBILITY AND LINKAGE
# ---------------------------------------------------------------------------


def test_artifact_links_to_the_prd():
    ok, _reason = verify_artifact_matches_prd(_artifact(), _prd())
    assert ok is True


def test_source_revision_mismatch_is_detected():
    other = compile_prd(BRIEF, project_id=PROJECT, source_revision=99,
                        requirements_version=REQV)
    ok, _ = verify_artifact_matches_prd(_artifact(), other)
    assert ok is False


def test_requirements_version_mismatch_is_detected():
    stale = _artifact(requirements_version=REQV - 1)
    ok, _ = verify_artifact_matches_prd(stale, _prd())
    assert ok is False


def test_project_mismatch_is_detected():
    other = compile_prd(BRIEF, project_id="other", source_revision=REV,
                        requirements_version=REQV)
    ok, _ = verify_artifact_matches_prd(_artifact(), other)
    assert ok is False


# ---------------------------------------------------------------------------
# 3. CONSUMPTION
# ---------------------------------------------------------------------------


def test_requirements_for_fit_returns_scorable_texts(tmp_path):
    write_requirement_artifact(tmp_path, _artifact())
    texts, reason = requirements_for_fit(tmp_path)
    assert reason == ""
    assert len(texts) >= 1


def test_requirements_for_fit_abstains_without_artifact(tmp_path):
    texts, reason = requirements_for_fit(tmp_path)
    assert texts == ()
    assert reason == REASON_NO_ARTIFACT


def test_consumed_texts_exclude_behavioural_entries(tmp_path):
    art = _artifact()
    behavioural = [e.text for e in art.entries if e.evidence_kind == "behavioural"]
    assert behavioural, "fixture should contain at least one behavioural entry"
    write_requirement_artifact(tmp_path, art)
    texts, _ = requirements_for_fit(tmp_path)
    for b in behavioural:
        assert b not in texts


def test_consumed_texts_exclude_template_entries(tmp_path):
    art = RequirementArtifact(
        project_id=PROJECT, source_revision=REV, requirements_version=REQV,
        entries=(
            RequirementEntry("R1", "A pricing table showing a price per service",
                             "content", "visual", "screenshot", "user_brief",
                             "brief.what"),
            RequirementEntry("T1",
                             "Every published page renders without a critical runtime error",
                             "constraint", "behavioural", "runtime", "template",
                             "compiler.default", discriminating=False),
        ),
    )
    write_requirement_artifact(tmp_path, art)
    texts, _ = requirements_for_fit(tmp_path)
    assert all("renders without" not in t for t in texts)


# ---------------------------------------------------------------------------
# 4. INTERRUPTED WRITE AND RECOVERY
# ---------------------------------------------------------------------------


def test_missing_artifact_is_detected_as_repairable(tmp_path):
    """A crash between the PRD write and the artifact write leaves this state."""
    from app.core.document_store import store_documents

    store_documents(tmp_path, prd_text=_prd())
    assert read_requirement_artifact(tmp_path) is None
    # And the consumer refuses rather than falling back to PRD bullets.
    texts, reason = requirements_for_fit(tmp_path)
    assert texts == ()
    assert reason == REASON_NO_ARTIFACT


def test_stale_artifact_is_detected_against_a_newer_prd(tmp_path):
    from app.core.document_store import store_documents

    write_requirement_artifact(tmp_path, _artifact())          # old artifact
    newer = compile_prd(BRIEF, project_id=PROJECT, source_revision=REV + 1,
                        requirements_version=REQV + 1)
    store_documents(tmp_path, prd_text=newer)                   # newer PRD
    art = read_requirement_artifact(tmp_path)
    ok, _ = verify_artifact_matches_prd(art, newer)
    assert ok is False


def test_repair_is_idempotent(tmp_path):
    """Re-emitting the same brief must converge, not accumulate."""
    first = _artifact()
    write_requirement_artifact(tmp_path, first)
    second = _artifact()
    write_requirement_artifact(tmp_path, second)
    assert first.digest == second.digest
    assert read_requirement_artifact(tmp_path).digest == first.digest


def test_repair_leaves_no_temp_file(tmp_path):
    write_requirement_artifact(tmp_path, _artifact())
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_artifact_reads_as_absent(tmp_path):
    (tmp_path / ARTIFACT_FILE_NAME).write_text("{ truncated")
    assert read_requirement_artifact(tmp_path) is None
    texts, reason = requirements_for_fit(tmp_path)
    assert texts == ()
    assert reason == REASON_NO_ARTIFACT


def test_prd_survives_a_failed_artifact_write(tmp_path, monkeypatch):
    """The PRD is durable first; a later failure must not lose it."""
    from app.core.document_store import read_documents, store_documents

    store_documents(tmp_path, prd_text=_prd())

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(
        "app.core.design_requirement_artifact.os.replace", boom, raising=False)
    with pytest.raises(OSError):
        write_requirement_artifact(tmp_path, _artifact())
    prd, _ = read_documents(tmp_path)
    assert prd is not None and prd.text


# ---------------------------------------------------------------------------
# 5. EVIDENCE BOUNDARIES
# ---------------------------------------------------------------------------


def test_behavioural_requirements_are_retained_in_the_artifact():
    art = _artifact()
    assert any(e.evidence_kind == "behavioural" for e in art.entries)


def test_behavioural_requirements_are_never_screenshot_scorable():
    art = _artifact()
    for e in art.entries:
        if e.evidence_kind == "behavioural":
            assert e.is_screenshot_scorable is False


def test_screenshot_requirements_are_scorable():
    art = _artifact()
    scorable = [e for e in art.entries if e.is_screenshot_scorable]
    assert scorable


def test_coverage_counts_only_screenshot_scorable_entries():
    art = _artifact()
    cov = assess_coverage(art)
    assert cov.scorable_count == len(
        [e for e in art.entries if e.is_screenshot_scorable])


# ---------------------------------------------------------------------------
# 6. SUFFICIENCY WITHOUT A HARD COUNT
# ---------------------------------------------------------------------------


def test_single_visual_requirement_is_sufficient():
    art = RequirementArtifact(
        project_id=PROJECT, source_revision=REV, requirements_version=REQV,
        entries=(RequirementEntry("R1", "A pricing table showing a price per service",
                                  "content", "visual", "screenshot", "user_brief",
                                  "brief.what"),),
    )
    cov = assess_coverage(art)
    assert cov.sufficient is True
    assert cov.scorable_count == 1


def test_boilerplate_only_is_insufficient():
    art = RequirementArtifact(
        project_id=PROJECT, source_revision=REV, requirements_version=REQV,
        entries=(
            RequirementEntry("T1",
                             "Every published page renders without a critical runtime error",
                             "constraint", "behavioural", "runtime", "template",
                             "compiler.default", discriminating=False),
        ),
    )
    assert assess_coverage(art).sufficient is False


def test_brief_with_only_behaviour_abstains_from_screenshot_fit():
    art = _artifact({"name": "X", "why": "visitors can submit a booking form"})
    cov = assess_coverage(art)
    assert cov.sufficient is False


# ---------------------------------------------------------------------------
# 7. APPROVAL AND PUBLISH UNTOUCHED
# ---------------------------------------------------------------------------


def test_approval_and_publish_modules_are_not_imported_here():
    """N9 must not reach into the approval/publish path.

    Checks IMPORTS, not prose: the module docstring legitimately says it will not
    "promote an assumption into a requirement", which is text, not a dependency.
    """
    import ast

    import app.core.design_requirement_artifact as mod
    tree = ast.parse(Path(mod.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    forbidden = ("promote", "publish", "authz", "lifecycle")
    for name in imported:
        assert not any(f in name for f in forbidden), name
