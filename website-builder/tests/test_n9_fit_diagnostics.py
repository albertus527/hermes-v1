"""N9 Fit diagnostics + partial-mismatch fixtures — offline validation.

Covers the owner's required checks:

  * candidate and requirement revision binding
  * accurate requirement diagnostics
  * no contradictory aggregate-versus-diagnostic claims
  * controlled visual changes
  * no invented user requirements
  * existing approval/publication behaviour unchanged
"""

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.fit_diagnostics import (  # noqa: E402
    BASIS_DETERMINISTIC,
    FIT_REQUIRED_OPTIONS,
    BASIS_MODEL,
    BASIS_UNRESOLVED,
    DIAG_ABSENT,
    DIAG_PARTIAL,
    DIAG_SATISFIED,
    DIAG_UNVERIFIED,
    DIAGNOSTIC_STATUSES,
    FitDiagnosticsError,
    RequirementDiagnostic,
    assert_aggregate_intact,
    build_diagnostic_questions,
    deterministic_diagnostic,
    diagnostic_question_key,
    parse_diagnostic_answers,
)

FIX = Path("/home/albertus527/n8-benchmark/v3/v2-dataset/n9-fixtures")
CAPS = json.loads((FIX / "captures.json").read_text())
AUDIT = json.loads((FIX / "audit.json").read_text())
MATRIX = json.loads((FIX / "evidence-matrix.json").read_text())

F1_DIST = Path("/home/albertus527/website-workspaces/tg-6329821361-p26/dist")
F2_DIST = Path("/tmp/n9-fixtures/f2-partial/dist")
F3_DIST = Path("/tmp/n9-fixtures/f3-polished-wrong/dist")

REQS = [
    {"id": e["id"], "text": e["text"], "trace": e["trace"],
     "evidence_kind": e["evidence_kind"]}
    for e in AUDIT["requirements"]
]


class _Cand:
    def __init__(self, cid):
        self.candidate_id = cid


# ---------------------------------------------------------------------------
# diagnostics: accuracy and honesty
# ---------------------------------------------------------------------------


class TestDiagnosticQuestions:
    def test_one_question_per_requirement(self):
        cands = [_Cand("c1"), _Cand("c2")]
        q = build_diagnostic_questions(cands, REQS)
        assert len(q) == len(cands) * len(REQS)

    def test_questions_are_choice_type_with_four_options(self):
        q = build_diagnostic_questions([_Cand("c1")], REQS)
        for question in q.values():
            assert question["type"] == "choice"
            assert set(question["criteria"]) == {
                "satisfied", "partial", "absent", "unverified"}

    def test_candidate_id_never_enters_the_key(self):
        q = build_diagnostic_questions([_Cand("evil'; DROP TABLE")], REQS)
        for key in q:
            assert "evil" not in key

    def test_requirement_without_text_is_refused(self):
        with pytest.raises(FitDiagnosticsError):
            build_diagnostic_questions([_Cand("c1")],
                                       [{"id": "R1", "text": "", "trace": "x"}])

    def test_question_key_helper_rejects_unusable_ids(self):
        with pytest.raises(FitDiagnosticsError):
            diagnostic_question_key("c1", "///")


class TestDiagnosticParsing:
    def test_a_model_answer_becomes_a_model_diagnostic(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "satisfied"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.91)
        row = reports[0].by_id(REQS[0]["id"])
        assert row.status == DIAG_SATISFIED
        assert row.basis == BASIS_MODEL

    def test_a_missing_answer_is_unverified_not_guessed(self):
        reports = parse_diagnostic_answers(
            {}, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.5)
        for row in reports[0].diagnostics:
            assert row.status == DIAG_UNVERIFIED
            assert row.basis == BASIS_UNRESOLVED

    def test_an_unknown_option_is_unverified(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "excellent"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.5)
        for row in reports[0].diagnostics:
            assert row.status == DIAG_UNVERIFIED, (
                "an option the model did not choose must not become a status"
            )

    def test_each_candidate_gets_its_own_answers(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "satisfied"} for r in REQS}
        answers.update({f"fitdiag1_{r['id']}": {"choice": "absent"} for r in REQS})
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1"), _Cand("c2")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.5)
        assert reports[0].by_id(REQS[0]["id"]).status == DIAG_SATISFIED
        assert reports[1].by_id(REQS[0]["id"]).status == DIAG_ABSENT

    def test_aggregate_is_carried_through_unchanged(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "satisfied"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.910)
        assert reports[0].aggregate_fit == 0.910

    def test_report_marks_the_aggregate_authoritative(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "satisfied"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.91)
        assert reports[0].to_dict()["aggregate_is_authoritative"] is True

    def test_counts_are_reported(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "satisfied"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.91)
        counts = reports[0].counts()
        assert counts[DIAG_SATISFIED] == len(REQS)
        assert set(counts) == set(DIAGNOSTIC_STATUSES)


class TestNoContradictoryClaims:
    def test_aggregate_guard_rejects_a_changed_aggregate(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "satisfied"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.91)
        with pytest.raises(FitDiagnosticsError):
            assert_aggregate_intact(reports[0], 0.42)

    def test_aggregate_guard_accepts_the_same_aggregate(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "satisfied"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.91)
        assert_aggregate_intact(reports[0], 0.91)

    def test_diagnostics_never_recompute_the_aggregate(self):
        """A set of statuses cannot be turned back into an F score here."""
        answers = {}
        for r in REQS:
            answers[f"fitdiag0_{r['id']}"] = {"choice": "satisfied"}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.91)
        source = Path(__file__).parent.parent / "app" / "core" / "fit_diagnostics.py"
        text = source.read_text(encoding="utf-8")
        assert "compute_suitability" not in text
        assert "0.45" not in text and "0.55" not in text

    def test_a_deterministic_diagnostic_needs_its_measurement(self):
        with pytest.raises(FitDiagnosticsError):
            deterministic_diagnostic(
                requirement_id="R5", text="t", trace="brief.why",
                requirement_version="7", evidence_modality="visual",
                status=DIAG_PARTIAL, evidence="")

    def test_a_deterministic_diagnostic_is_labelled_as_such(self):
        row = deterministic_diagnostic(
            requirement_id="R5", text="t", trace="brief.why",
            requirement_version="7", evidence_modality="visual",
            status=DIAG_PARTIAL,
            evidence="the rendered DOM contains 1 project article, not 3")
        assert row.basis == BASIS_DETERMINISTIC
        assert row.is_resolved is True

    def test_diagnostic_row_carries_its_provenance(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "partial"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.5)
        row = reports[0].by_id(REQS[0]["id"])
        assert row.requirement_id == REQS[0]["id"]
        assert row.text == REQS[0]["text"]
        assert row.trace == REQS[0]["trace"]
        assert row.requirement_version == "7"
        assert row.evidence_modality == REQS[0]["evidence_kind"]
        assert row.candidate_id == "c1"

    def test_unverified_is_distinguishable_from_a_negative_verdict(self):
        answers = {f"fitdiag0_{r['id']}": {"choice": "unverified"} for r in REQS}
        reports = parse_diagnostic_answers(
            answers, candidates=[_Cand("c1")], requirements=REQS,
            requirement_version="7", aggregate_fit=0.5)
        row = reports[0].by_id(REQS[0]["id"])
        assert row.status == DIAG_UNVERIFIED
        assert row.is_resolved is False
        assert row.status != DIAG_ABSENT


# ---------------------------------------------------------------------------
# binding
# ---------------------------------------------------------------------------


class TestBinding:
    def test_all_three_candidates_share_one_requirement_version(self):
        assert str(AUDIT["requirements_version"]) == "7"
        assert AUDIT["project_id"] == "n9-forma-atelier"
        assert str(AUDIT["source_revision"]) == "3"

    def test_every_candidate_capture_records_the_same_revision(self):
        revisions = {k: v["revision"] for k, v in CAPS.items()}
        assert len(set(revisions.values())) == 1, revisions

    def test_every_candidate_has_both_viewports(self):
        for label, cap in CAPS.items():
            assert cap["desktop_digest"], label
            assert cap["mobile_digest"], label

    def test_the_evidence_matrix_covers_every_requirement(self):
        assert set(MATRIX["matrix"]) == {r["id"] for r in AUDIT["requirements"]}

    def test_the_evidence_matrix_covers_every_candidate(self):
        for row in MATRIX["matrix"].values():
            for key in ("F1", "F2", "F3"):
                assert key in row
                assert row[key]["status"] in DIAGNOSTIC_STATUSES


# ---------------------------------------------------------------------------
# controlled visual changes
# ---------------------------------------------------------------------------


def _css(dist: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in dist.rglob("*.css")).lower()


def _js(dist: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in dist.rglob("*.js")).lower()


class TestControlledChanges:
    def test_f2_changes_only_the_project_count(self):
        """F2 is a PARTIAL mismatch: one requirement, nothing else."""
        assert CAPS["F2"]["dom"]["project_articles"] == 1
        assert CAPS["F1"]["dom"]["project_articles"] == 3
        for token in ("#faf8f5", "#b4552d", "--font-display:\"playfair"):
            assert token in _css(F2_DIST), f"F2 must preserve {token}"
        for section in ("karya", "tentang", "layanan", "kontak"):
            assert section in CAPS["F2"]["dom"]["section_ids"], (
                f"F2 must keep the {section} section"
            )

    def test_f2_introduces_no_unrelated_visual_defect(self):
        """F2's palette and type are F1's, so its contrast must match F1's."""
        f1 = min(c["ratio"] for c in CAPS["F1"]["desktop"]["contrast"]
                 if c["state"] == "measured" and c["ratio"])
        f2 = min(c["ratio"] for c in CAPS["F2"]["desktop"]["contrast"]
                 if c["state"] == "measured" and c["ratio"])
        assert f1 == f2, f"F2's contrast should equal F1's: {f1} vs {f2}"

    def test_f3_preserves_the_visual_craft(self):
        """F3 must be visually strong: same type, same section count, good contrast."""
        assert "--font-display:\"playfair" in _css(F3_DIST)
        assert len(CAPS["F3"]["dom"]["section_ids"]) == len(CAPS["F1"]["dom"]["section_ids"])
        f3 = min(c["ratio"] for c in CAPS["F3"]["desktop"]["contrast"]
                 if c["state"] == "measured" and c["ratio"])
        f1 = min(c["ratio"] for c in CAPS["F1"]["desktop"]["contrast"]
                 if c["state"] == "measured" and c["ratio"])
        assert f3 >= f1, f"F3 should be at least as legible as F1: {f3} vs {f1}"

    def test_f3_violates_the_content_requirements(self):
        assert CAPS["F3"]["dom"]["mentions"]["cafe"] is True
        assert CAPS["F3"]["dom"]["mentions"]["architecture"] is False
        assert CAPS["F3"]["dom"]["mentions"]["contact"] is False
        assert CAPS["F3"]["dom"]["mentions"]["reservation"] is True

    def test_f3_still_renders_three_entries(self):
        """F3 violates the CONTENT of the projects section, not its count."""
        assert CAPS["F3"]["dom"]["project_articles"] == 3
        assert all("Nasi" in t or "Kopi" in t or "Roti" in t
                   for t in CAPS["F3"]["dom"]["project_titles"])


# ---------------------------------------------------------------------------
# no invented user requirements
# ---------------------------------------------------------------------------


class TestNoInventedRequirements:
    def test_the_requirement_set_is_the_native_artifact(self):
        assert len(AUDIT["scorable"]) == len(AUDIT["requirements"])
        for entry in AUDIT["requirements"]:
            assert entry["text"] in AUDIT["scorable"]

    def test_every_requirement_traces_to_the_brief(self):
        for entry in AUDIT["requirements"]:
            assert entry["trace"].startswith("brief.")

    def test_no_requirement_invents_a_quantity(self):
        for entry in AUDIT["requirements"]:
            lowered = entry["text"].lower()
            for marker in ("at least", "minimum of", "no fewer than", ">= "):
                assert marker not in lowered, entry["text"]

    def test_the_matrix_did_not_add_requirements(self):
        assert set(MATRIX["matrix"]) == {r["id"] for r in AUDIT["requirements"]}

    def test_no_assumption_or_unresolved_became_a_requirement(self):
        for entry in AUDIT["requirements"]:
            assert entry["origin"] == "user_brief"
            assert entry["evidence_kind"] == "visual"


# ---------------------------------------------------------------------------
# approval / publication unchanged
# ---------------------------------------------------------------------------


class TestApprovalPublishUnchanged:
    def test_the_new_module_imports_nothing_that_publishes(self):
        src = (Path(__file__).parent.parent / "app" / "core" / "fit_diagnostics.py")
        text = src.read_text(encoding="utf-8")
        for forbidden in ("promote(", "publish(", "requests.", "httpx.",
                          "app.core.promote", "app.core.publish"):
            assert forbidden not in text

    def test_the_new_module_makes_no_network_calls(self):
        src = (Path(__file__).parent.parent / "app" / "core" / "fit_diagnostics.py")
        tree = ast.parse(src.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        assert not (imported & {
            "requests", "httpx", "urllib.request", "app.core.decisions_client",
            "app.core.promote", "app.core.publish",
        })

    def test_scoring_thresholds_are_untouched(self):
        from app.core.design_scoring import (
            DEFAULT_MIN_FIT, DEFAULT_MIN_QUALITY, DEFAULT_MIN_SUITABILITY,
            DIMENSIONS, WEIGHTS, compute_suitability,
        )
        assert DEFAULT_MIN_QUALITY == 0.70
        assert DEFAULT_MIN_FIT == 0.70
        assert DEFAULT_MIN_SUITABILITY == 0.75
        assert len(DIMENSIONS) == 6
        assert round(sum(WEIGHTS.values()), 6) == 1.0
        assert round(compute_suitability(0.70, 0.70), 6) == round(
            0.70 ** 0.45 * 0.70 ** 0.55, 6)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


class TestClosedOptionSet:
    def test_the_option_set_is_closed(self):
        assert FIT_REQUIRED_OPTIONS == (
            "satisfied", "partial", "absent", "unverified")

    def test_questions_offer_exactly_the_closed_set(self):
        q = build_diagnostic_questions([_Cand("c1")], REQS)
        for question in q.values():
            assert set(question["criteria"]) == set(FIT_REQUIRED_OPTIONS)

    def test_the_option_set_is_exported(self):
        import app.core.fit_diagnostics as mod
        assert "FIT_REQUIRED_OPTIONS" in mod.__all__


class TestSharedRequirementRevision:
    """The candidates must be bound to ONE requirement revision.

    An earlier build used each candidate's own asset digest as its
    ``source_revision``. That made every candidate carry a different revision,
    so the claim "one identical requirement set" was not actually bound to
    them -- the requirement artifact's revision and the candidates' revision
    were different things wearing the same name.
    """

    def test_the_artifact_revision_is_shared(self):
        assert str(AUDIT["source_revision"]) == "3"

    def test_candidates_share_the_artifact_revision(self):
        revs = {c["revision"] for c in CAPS.values()}
        assert revs == {str(AUDIT["source_revision"])}, revs

    def test_each_candidate_still_has_its_own_image_digests(self):
        desks = {c["desktop_digest"] for c in CAPS.values()}
        assert len(desks) == 3, "each candidate must carry its own image"
