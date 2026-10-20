"""N9: structured requirement artifact and evidence-based sufficiency.

Locks the four corrections the owner required:

1. evidence modality reflects what is actually verifiable — a screenshot cannot
   prove runtime behaviour, and source inspection cannot prove interaction
2. sufficiency is a coverage judgement, not a hard minimum count
3. template boilerplate is never project-specific Fit evidence
4. revision linkage between requirements.json and PRD.md is exact and detectable
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import design_scoring as ds  # noqa: E402
from app.core.design_requirement_artifact import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION, REASON_NO_ARTIFACT, REASON_NO_DISCRIMINATING,
    REASON_NO_SCORABLE, REASON_OK, REASON_REVISION_MISMATCH,
    RequirementArtifact, RequirementArtifactError, RequirementEntry,
    artifact_from_prd, assess_coverage, parse_artifact,
    read_requirement_artifact, verify_artifact_matches_prd,
    write_requirement_artifact,
)
from app.core.requirement_creator import compile_prd  # noqa: E402


def _entry(**over):
    base = dict(
        id="R1",
        text="A pricing table showing a price per service",
        kind="content",
        evidence_kind="visual",
        verifiable_by="screenshot",
        origin="user_brief",
        trace="brief.what",
        mandatory=True,
        discriminating=True,
    )
    base.update(over)
    return RequirementEntry(**base)


def _artifact(entries, **over):
    base = dict(project_id="tailorivan", source_revision=3,
                requirements_version=7, entries=tuple(entries))
    base.update(over)
    return RequirementArtifact(**base)


def _prd(**over):
    brief = {"name": "TailorIvan", "what": "layanan", "why": "kredibilitas",
             "why_destination": "form", "design_intent": "serif"}
    brief.update(over)
    return compile_prd(brief, project_id="tailorivan", source_revision=3,
                       requirements_version=7)


# ---------------------------------------------------------------------------
# 1. EVIDENCE MODALITY
# ---------------------------------------------------------------------------


def test_visual_screenshot_entry_is_scorable():
    assert _entry().is_screenshot_scorable is True


def test_behavioural_entry_is_never_screenshot_scorable():
    """A screenshot cannot prove a form submits, even with a plausible handler."""
    entry = _entry(id="R3", kind="behaviour", evidence_kind="behavioural",
                   verifiable_by="none", text="The visitor can submit a request")
    assert entry.is_screenshot_scorable is False


def test_behavioural_with_runtime_evidence_is_not_screenshot_scorable():
    entry = _entry(kind="behaviour", evidence_kind="behavioural",
                   verifiable_by="runtime", text="Form submission succeeds")
    assert entry.is_screenshot_scorable is False


def test_visual_but_source_only_is_not_screenshot_scorable():
    """Visual intent with no screenshot evidence available cannot be scored."""
    entry = _entry(evidence_kind="visual", verifiable_by="source")
    assert entry.is_screenshot_scorable is False


def test_assumption_is_never_scorable():
    entry = _entry(origin="assumption")
    assert entry.is_screenshot_scorable is False


def test_template_entry_is_never_scorable():
    entry = _entry(origin="template",
                   text="Every published page renders without a critical runtime error")
    assert entry.is_screenshot_scorable is False


def test_non_mandatory_entry_is_not_scorable():
    assert _entry(mandatory=False).is_screenshot_scorable is False


def test_behavioural_requirements_are_kept_in_the_artifact():
    """They must remain available for their own runtime QA path."""
    entry = _entry(id="R3", kind="behaviour", evidence_kind="behavioural",
                   verifiable_by="none", text="The visitor can submit a request")
    art = _artifact([_entry(), entry])
    assert len(art.entries) == 2
    assert any(e.evidence_kind == "behavioural" for e in art.entries)


# ---------------------------------------------------------------------------
# 2. SUFFICIENCY IS COVERAGE, NOT COUNT
# ---------------------------------------------------------------------------


def test_one_specific_requirement_is_sufficient():
    """The owner's correction: a single legitimate requirement must not abstain."""
    cov = assess_coverage(_artifact([_entry()]))
    assert cov.sufficient is True
    assert cov.reason == REASON_OK
    assert cov.scorable_count == 1


def test_two_specific_requirements_are_sufficient():
    cov = assess_coverage(_artifact([
        _entry(id="R1"),
        _entry(id="R2", text="Serif display typography for headings", kind="style"),
    ]))
    assert cov.sufficient is True
    assert cov.scorable_count == 2


def test_zero_requirements_is_not_sufficient():
    cov = assess_coverage(_artifact([]))
    assert cov.sufficient is False
    assert cov.reason == REASON_NO_SCORABLE


def test_template_only_is_not_sufficient():
    """Six boilerplate entries must not pass where one real one would."""
    entries = [
        _entry(id="T1", origin="template", discriminating=False,
               text="Every published page renders without a critical runtime error"),
        _entry(id="T2", origin="template", discriminating=False,
               text="No unresolved launch-critical placeholder remains"),
    ]
    cov = assess_coverage(_artifact(entries))
    assert cov.sufficient is False
    assert cov.reason == REASON_NO_SCORABLE
    assert len(cov.excluded_template) == 2


def test_legacy_count_gate_would_have_failed_this():
    """The regression the owner asked to fix: evidence quality, not entry count.

    The old gate counted usable strings. That number says nothing about whether
    any of them can be judged from a screenshot, so it produced both false
    negatives (one real requirement) and false positives (compiler defaults).
    """
    one_specific = ["A pricing table showing a price per service"]
    template_heavy = [
        "Every published page renders without a critical runtime error",
        "Primary call-to-action is reachable from the landing page",
    ]

    # OLD: a count test. One real requirement is rejected.
    assert ds.requirements_are_sufficient(one_specific) is False

    # NEW: evidence-based. The same single requirement is accepted...
    ok_one, reason_one, scorable_one, _ = ds.assess_requirement_sufficiency(one_specific)
    assert ok_one is True
    assert reason_one == ""
    assert scorable_one == ("A pricing table showing a price per service",)

    # ...while compiler defaults are rejected no matter how many there are.
    ok_t, reason_t, scorable_t, excluded_t = ds.assess_requirement_sufficiency(template_heavy)
    assert ok_t is False
    assert reason_t == "no_project_specific_requirement"
    assert scorable_t == ()
    assert len(excluded_t) == len(template_heavy)


def test_sufficiency_drops_template_but_keeps_the_real_one():
    mixed = ["A pricing table showing a price per service",
             "Every published page renders without a critical runtime error"]
    ok, reason, scorable, excluded = ds.assess_requirement_sufficiency(mixed)
    assert ok is True
    assert reason == ""
    assert scorable == ("A pricing table showing a price per service",)
    assert len(excluded) == 1


def test_undiscriminating_only_set_is_not_sufficient():
    cov = assess_coverage(_artifact([_entry(discriminating=False)]))
    assert cov.sufficient is False
    assert cov.reason == REASON_NO_DISCRIMINATING


def test_min_fit_constant_is_no_longer_the_gate():
    """The constant survives for import compatibility but must not decide."""
    assert ds.MIN_FIT_REQUIREMENTS == 3
    # A single specific requirement passes despite being below the constant.
    assert ds.assess_requirement_sufficiency([_entry().text])[0] is True


# ---------------------------------------------------------------------------
# 3. TEMPLATE BOILERPLATE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Every published page renders without a critical runtime error",
    "No unresolved launch-critical placeholder remains",
    "Primary call-to-action is reachable from the landing page",
])
def test_compiler_defaults_are_recognised_as_template(text):
    assert ds.is_template_requirement(text) is True


def test_a_project_specific_criterion_is_not_template():
    assert ds.is_template_requirement(
        "A pricing table showing a price per service") is False


def test_legacy_artifact_marks_prd_bullets_as_template():
    """A pre-N9 PRD must produce ZERO scorable entries, not a false pass."""
    art = artifact_from_prd(_prd())
    assert art is not None
    cov = assess_coverage(art)
    assert cov.sufficient is False


# ---------------------------------------------------------------------------
# 4. REVISION LINKAGE AND RECOVERY
# ---------------------------------------------------------------------------


def test_artifact_matches_its_prd():
    ok, reason = verify_artifact_matches_prd(_artifact([_entry()]), _prd())
    assert ok is True
    assert reason == REASON_OK


def test_project_mismatch_is_detected():
    """An interrupted write can leave a new PRD beside an old artifact."""
    other = compile_prd({"name": "X", "what": "y", "why": "z"},
                        project_id="other-project", source_revision=3,
                        requirements_version=7)
    ok, reason = verify_artifact_matches_prd(_artifact([_entry()]), other)
    assert ok is False
    assert reason == REASON_REVISION_MISMATCH


def test_source_revision_mismatch_is_detected():
    newer = compile_prd({"name": "TailorIvan", "what": "y", "why": "z"},
                        project_id="tailorivan", source_revision=9,
                        requirements_version=7)
    ok, reason = verify_artifact_matches_prd(_artifact([_entry()]), newer)
    assert ok is False
    assert reason == REASON_REVISION_MISMATCH


def test_requirements_version_mismatch_is_detected():
    stale = _artifact([_entry()], requirements_version=6)
    ok, reason = verify_artifact_matches_prd(stale, _prd())
    assert ok is False
    assert reason == REASON_REVISION_MISMATCH


def test_missing_prd_is_detected():
    ok, reason = verify_artifact_matches_prd(_artifact([_entry()]), "")
    assert ok is False


def test_artifact_round_trips_through_disk(tmp_path):
    art = _artifact([_entry()])
    digest = write_requirement_artifact(tmp_path, art)
    assert len(digest) == 64
    back = read_requirement_artifact(tmp_path)
    assert back is not None
    assert back.project_id == art.project_id
    assert back.requirements_version == art.requirements_version
    assert len(back.entries) == 1


def test_absent_artifact_reads_as_none(tmp_path):
    assert read_requirement_artifact(tmp_path) is None


def test_corrupt_artifact_reads_as_none_rather_than_raising(tmp_path):
    (tmp_path / "requirements.json").write_text("{not json")
    assert read_requirement_artifact(tmp_path) is None


def test_artifact_write_leaves_no_temp_file(tmp_path):
    write_requirement_artifact(tmp_path, _artifact([_entry()]))
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 5. SCHEMA VALIDATION
# ---------------------------------------------------------------------------


def test_schema_version_is_enforced():
    bad = _artifact([_entry()]).to_dict()
    bad["schema_version"] = 99
    with pytest.raises(RequirementArtifactError):
        parse_artifact(bad)


def test_unknown_evidence_kind_is_rejected():
    with pytest.raises(RequirementArtifactError):
        _entry(evidence_kind="vibes")


def test_unknown_origin_is_rejected():
    with pytest.raises(RequirementArtifactError):
        _entry(origin="guesswork")


def test_duplicate_ids_are_rejected():
    data = _artifact([_entry(), _entry()]).to_dict()
    with pytest.raises(RequirementArtifactError):
        parse_artifact(data)


def test_too_short_requirement_is_rejected():
    with pytest.raises(RequirementArtifactError):
        _entry(text="pricing")


def test_artifact_digest_is_stable():
    a = _artifact([_entry()])
    b = _artifact([_entry()])
    assert a.digest == b.digest


def test_assumptions_and_unresolved_are_separate_from_entries():
    art = RequirementArtifact(
        project_id="p", source_revision=1, requirements_version=1,
        entries=(_entry(),),
        assumptions=({"id": "A1", "text": "audience is local"},),
        unresolved=({"id": "U1", "question": "languages?"},),
    )
    cov = assess_coverage(art)
    assert cov.scorable_count == 1
    assert len(art.assumptions) == 1
    assert len(art.unresolved) == 1
    assert "audience is local" not in cov.scorable


def test_assumption_text_never_enters_scorable_set():
    art = _artifact([
        _entry(),
        _entry(id="A1", origin="assumption",
               text="The audience is local retail customers"),
    ])
    cov = assess_coverage(art)
    assert all("audience" not in s.lower() for s in cov.scorable)


# ---------------------------------------------------------------------------
# 6. CONSUMER WIRING
# ---------------------------------------------------------------------------


def test_requirements_for_fit_reads_the_artifact(tmp_path):
    from app.core.design_requirements import requirements_for_fit

    write_requirement_artifact(tmp_path, _artifact([_entry()]))
    texts, reason = requirements_for_fit(tmp_path)
    assert reason == ""
    assert texts == ("A pricing table showing a price per service",)


def test_requirements_for_fit_abstains_without_an_artifact(tmp_path):
    from app.core.design_requirements import requirements_for_fit

    texts, reason = requirements_for_fit(tmp_path)
    assert texts == ()
    assert reason == REASON_NO_ARTIFACT


def test_requirements_for_fit_excludes_behavioural(tmp_path):
    from app.core.design_requirements import requirements_for_fit

    art = _artifact([
        _entry(),
        _entry(id="R3", kind="behaviour", evidence_kind="behavioural",
               verifiable_by="none", text="The visitor can submit a request"),
    ])
    write_requirement_artifact(tmp_path, art)
    texts, _ = requirements_for_fit(tmp_path)
    assert all("submit" not in t.lower() for t in texts)
    assert len(texts) == 1
