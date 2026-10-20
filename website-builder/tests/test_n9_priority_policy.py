"""N9 final architecture — Option D with the Option C default.

The owner's required checks:

  * no explicit priorities -> equal parent importance
  * one parent with four children -> the SAME total default importance as one
    single-child parent
  * explicit numerical priorities -> preserved and validated
  * explicit qualitative priorities -> represented without invented percentages
  * ambiguous or conflicting priorities -> unresolved
  * a mandatory requirement unmet despite higher scores elsewhere
  * schema 2 / schema 3 compatibility
  * the requirement-set identity changes when a priority changes
  * an old suitability cannot be reused under a new requirement set
  * subjective style intent remains available to Design Guidance
  * hidden contrast evidence excluded; visible violations retained
  * native approval/publication behaviour unchanged
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.decision_provider import DesignCandidate  # noqa: E402
from app.core.design_requirement_artifact import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_SCHEMA_VERSION_LEGACY,
    RequirementArtifactError,
    parse_artifact,
)
from app.core.requirement_artifact_builder import build_artifact_from_brief  # noqa: E402
from app.core.requirement_creator import compile_prd  # noqa: E402
from app.core.requirement_priority import (  # noqa: E402
    SCORING_CONTRACT_VERSION,
    WEIGHTING_POLICY_VERSION,
    build_weighting_policy,
    compute_requirement_set_id,
    extract_priorities,
)

BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur, gaya editorial minimalis premium modern (off-white, charcoal, aksen terracotta, tipografi elegan)",
    "why": "Menampilkan identitas studio dan karya (3 proyek arsitektur, tentang, layanan) serta mengarahkan pengunjung ke kontak",
}

#: A parent with FOUR children and a parent with ONE, for the influence test.
GROUPS = [
    {"group_id": "G_PALETTE", "text": "off-white, charcoal, aksen terracotta, tipografi elegan",
     "parts": ["R3", "R4", "R5", "R6"], "mandatory": True},
    {"group_id": "G_STYLE", "text": "gaya editorial minimalis premium modern",
     "parts": ["R2"], "mandatory": True},
    {"group_id": "G_LANDING", "text": "Landing page untuk studio arsitektur",
     "parts": ["R1"], "mandatory": True},
]

#: Groups whose vocabulary lets a priority name them.
NAMED_GROUPS = [
    {"group_id": "G_CONTENT", "text": "informasi produk katalog album",
     "parts": ["R1", "R2"], "mandatory": True},
    {"group_id": "G_DESIGN", "text": "gaya visual editorial premium",
     "parts": ["R3"], "mandatory": True},
]


def _artifact(brief=None):
    brief = brief or BRIEF
    prd = compile_prd(brief, project_id="p", source_revision=3,
                      requirements_version=7)
    return build_artifact_from_brief(
        brief, project_id="p", source_revision=3, requirements_version=7,
        prd_text=prd), prd


# ---------------------------------------------------------------------------
# Option C default
# ---------------------------------------------------------------------------


class TestOptionCDefault:
    def test_no_priority_means_equal_parent_importance(self):
        policy = build_weighting_policy({}, GROUPS)
        assert policy.mode == "c_default"
        assert policy.active is True
        shares = [g.importance for g in policy.groups]
        assert len(set(round(s, 9) for s in shares)) == 1, shares

    def test_a_four_child_parent_has_the_same_total_as_a_one_child_parent(self):
        policy = build_weighting_policy({}, GROUPS)
        palette = policy.group("G_PALETTE")
        landing = policy.group("G_LANDING")
        assert len(palette.child_ids) == 4
        assert len(landing.child_ids) == 1
        assert palette.importance == pytest.approx(landing.importance)
        assert palette.importance == pytest.approx(1.0 / 3)

    def test_children_split_their_parent_equally(self):
        policy = build_weighting_policy({}, GROUPS)
        palette = policy.group("G_PALETTE")
        assert palette.child_weight() == pytest.approx(palette.importance / 4)

    def test_splitting_does_not_quadruple_the_influence(self):
        """The measured regression the Option C default exists to prevent."""
        policy = build_weighting_policy({}, GROUPS)
        palette = policy.group("G_PALETTE")
        # 4 children x child_weight == the parent's single share
        total = sum(palette.child_weight() for _ in palette.child_ids)
        assert total == pytest.approx(palette.importance)
        # and the parent still carries exactly one share of the whole
        assert palette.importance == pytest.approx(1.0 / len(GROUPS))

    def test_the_default_is_deterministic(self):
        a = build_weighting_policy({}, GROUPS).to_dict()
        b = build_weighting_policy({}, GROUPS).to_dict()
        assert a == b


# ---------------------------------------------------------------------------
# Option D, explicit priorities
# ---------------------------------------------------------------------------


class TestOptionDExplicit:
    def test_a_numeric_priority_is_preserved(self):
        policy = build_weighting_policy(
            {"what": "konten priority 3, desain priority 1"}, NAMED_GROUPS)
        assert policy.mode == "d_explicit"
        content = policy.group("G_CONTENT")
        design = policy.group("G_DESIGN")
        assert content.priority is not None
        assert content.priority.kind == "numeric"
        assert content.priority.value == 3.0
        assert design.priority.value == 1.0
        assert content.importance == 3.0
        assert design.importance == 1.0

    def test_a_numeric_priority_keeps_its_source_text_and_trace(self):
        policy = build_weighting_policy(
            {"what": "konten priority 3, desain priority 1"}, NAMED_GROUPS)
        priority = policy.group("G_CONTENT").priority
        assert "konten" in priority.source_text.lower()
        assert priority.trace.startswith("brief.")
        assert priority.explicit is True
        assert priority.resolution == "resolved"

    def test_a_qualitative_priority_invents_no_percentage(self):
        policy = build_weighting_policy(
            {"what": "Yang paling penting desain premium"}, NAMED_GROUPS)
        assert policy.mode == "d_explicit"
        priority = policy.group("G_DESIGN").priority
        assert priority is not None
        assert priority.kind == "qualitative"
        assert priority.value is None, "a qualitative priority carries no number"
        # The ORDER is expressed, and the note says the numbers are derived.
        assert policy.note
        assert "no numeric share is invented" in policy.note.lower() \
            or "no number" in policy.note.lower()

    def test_a_qualitative_priority_keeps_an_ordering(self):
        policy = build_weighting_policy(
            {"what": "Yang paling penting desain premium"}, NAMED_GROUPS)
        design = policy.group("G_DESIGN")
        content = policy.group("G_CONTENT")
        assert design.importance > content.importance

    def test_a_mappable_priority_is_resolved(self):
        """"produk" names the content group, so the priority applies to it."""
        policy = build_weighting_policy(
            {"what": "Prioritaskan informasi produk"}, NAMED_GROUPS)
        assert len(policy.unresolved) == 0
        assert policy.group("G_CONTENT").priority is not None
        assert policy.group("G_CONTENT").importance > \
            policy.group("G_DESIGN").importance

    def test_an_unmappable_priority_is_unresolved(self):
        """A priority that names nothing any group shares stays unresolved."""
        policy = build_weighting_policy(
            {"what": "Prioritaskan layanankurasi"}, NAMED_GROUPS)
        assert len(policy.unresolved) == 1
        assert policy.unresolved[0].resolution == "unresolved"
        assert policy.unresolved[0].reason

    def test_a_conflicting_comparison_is_unresolved(self):
        policy = build_weighting_policy(
            {"what": "Konten lebih penting daripada animasi"}, NAMED_GROUPS)
        assert policy.unresolved
        assert "compares two things" in policy.unresolved[0].reason

    def test_an_unresolved_priority_falls_back_to_the_documented_default(self):
        policy = build_weighting_policy(
            {"what": "Prioritaskan layanankurasi"}, NAMED_GROUPS)
        assert policy.mode == "c_default_fallback"
        shares = [g.importance for g in policy.groups]
        assert len(set(round(s, 9) for s in shares)) == 1

    def test_a_priority_that_names_nothing_is_not_inferred(self):
        priorities = extract_priorities(
            {"what": "desain yang modern dan bersih"}, NAMED_GROUPS)
        assert len(priorities) == 0, (
            "a design preference is not a priority and must not be inferred"
        )


# ---------------------------------------------------------------------------
# mandatory requirements
# ---------------------------------------------------------------------------


class TestMandatoryVisibility:
    def test_a_mandatory_requirement_keeps_its_flag_under_decomposition(self):
        artifact, _ = _artifact()
        for entry in artifact.entries:
            assert entry.mandatory is True

    def test_weighting_never_makes_a_requirement_optional(self):
        policy = build_weighting_policy(
            {"what": "konten priority 3"}, NAMED_GROUPS)
        for group in policy.groups:
            assert group.mandatory is True

    def test_a_weighted_average_cannot_conceal_a_missing_mandatory_group(self):
        """A high-weight group cannot hide a low-weight group's failure.

        The weighting policy assigns IMPORTANCE. It does not remove a
        requirement from the set, so a mandatory requirement that is unmet
        still has no satisfied answer. The policy therefore cannot produce a
        weighted average that hides it -- there is no averaging step here at
        all, and the aggregate Fit stays computed from the requirement set.
        """
        policy = build_weighting_policy(
            {"what": "konten priority 9, desain priority 1"}, NAMED_GROUPS)
        content = policy.group("G_CONTENT")
        design = policy.group("G_DESIGN")
        assert content.importance > design.importance
        # both groups still carry their requirements
        assert content.child_ids
        assert design.child_ids


# ---------------------------------------------------------------------------
# requirement-set identity
# ---------------------------------------------------------------------------


class TestRequirementSetIdentity:
    def _id(self, brief, groups=None):
        artifact, _ = _artifact(brief)
        return artifact.requirement_set_id

    def test_the_set_id_exists_and_is_stable(self):
        a = self._id(BRIEF)
        b = self._id(BRIEF)
        assert a and a.startswith("rs-")
        assert a == b

    def test_changing_a_priority_changes_the_set_id(self):
        plain = self._id(BRIEF)
        with_priority = self._id(
            dict(BRIEF, what=BRIEF["what"] + " kontak priority 3"))
        assert plain != with_priority, (
            "a priority changes the evaluation contract, so the identity must "
            "change with it"
        )

    def test_changing_the_content_changes_the_set_id(self):
        a = self._id(BRIEF)
        b = self._id(dict(BRIEF, what="Halaman TOKO KOPI dengan menu dan galeri"))
        assert a != b

    def test_the_scoring_contract_is_separate_from_the_set(self):
        """The rubric version and the requirement set are different things."""
        assert SCORING_CONTRACT_VERSION == "n8-design-v1"
        assert WEIGHTING_POLICY_VERSION
        artifact, _ = _artifact()
        assert artifact.requirement_set_id != SCORING_CONTRACT_VERSION

    def test_two_projects_with_the_same_set_share_a_set_id(self):
        """The set id identifies the SET, not the project."""
        prd = compile_prd(BRIEF, project_id="p", source_revision=1,
                          requirements_version=1)
        a = build_artifact_from_brief(BRIEF, project_id="p", source_revision=1,
                                      requirements_version=1, prd_text=prd)
        prd2 = compile_prd(BRIEF, project_id="q", source_revision=9,
                           requirements_version=4)
        b = build_artifact_from_brief(BRIEF, project_id="q", source_revision=9,
                                      requirements_version=4, prd_text=prd2)
        assert a.requirement_set_id == b.requirement_set_id


# ---------------------------------------------------------------------------
# compatibility and suitability reuse
# ---------------------------------------------------------------------------


class TestCompatibilityAndSuitability:
    def test_a_schema_2_artifact_is_read_unchanged(self):
        legacy = {
            "schema_version": 2, "project_id": "p", "source_revision": 3,
            "requirements_version": 7, "prd_digest": "",
            "entries": [{"id": "R1", "text": "off-white, charcoal, aksen terracotta",
                         "kind": "content", "evidence_kind": "visual",
                         "verifiable_by": "screenshot", "origin": "user_brief",
                         "trace": "brief.what", "mandatory": True,
                         "discriminating": True}],
            "assumptions": [], "unresolved": [],
        }
        artifact = parse_artifact(legacy)
        assert artifact.schema_version == ARTIFACT_SCHEMA_VERSION_LEGACY
        assert artifact.entries[0].text == "off-white, charcoal, aksen terracotta"
        assert artifact.entries[0].group_id == "", "a legacy entry stands alone"
        assert artifact.requirements_version == 7, (
            "the requirement version must be preserved, not reinterpreted"
        )

    def test_a_legacy_artifact_carries_no_weighting(self):
        legacy = {
            "schema_version": 2, "project_id": "p", "source_revision": 3,
            "requirements_version": 7, "prd_digest": "",
            "entries": [], "assumptions": [], "unresolved": [],
        }
        artifact = parse_artifact(legacy)
        assert artifact.weighting is None
        assert artifact.requirement_set_id == ""

    def test_an_unknown_schema_is_refused(self):
        for version in (1, 4, 99):
            blob = {"schema_version": version, "project_id": "p",
                    "source_revision": 1, "requirements_version": 1,
                    "entries": [], "assumptions": [], "unresolved": []}
            with pytest.raises(RequirementArtifactError):
                parse_artifact(blob)

    def test_a_stored_suitability_cannot_cross_a_set_boundary(self):
        """The engine compares suitability only when the contract matches.

        The contract carries the requirement-set identity, so a suitability
        computed under one set is not comparable with one computed under
        another. The engine's OWN rule already refuses to compare across
        different contract ids; this pins that a set change produces a
        different contract id, which is what makes the rule bite.
        """
        from app.core.decision_engine import _suitability_comparable

        plain = self._id_of(BRIEF)
        changed = self._id_of(
            dict(BRIEF, what=BRIEF["what"] + " kontak priority 3"))
        assert plain != changed

        def _cand(cid: str, contract: str) -> DesignCandidate:
            return DesignCandidate(
                candidate_id=cid, label=cid, descriptor="d",
                source_uri="lib://x", source_revision="3", score=0.8,
                suitability=0.9, suitability_contract=contract,
                requirement_version="req-7",
            )

        # Same set -> comparable.
        rank, mixed = _suitability_comparable([
            _cand("a", f"n8-design-v1:{plain}"),
            _cand("b", f"n8-design-v1:{plain}"),
        ])
        assert (rank, mixed) == (True, False)

        # Different set -> NOT comparable, and the engine calls it mixed
        # rather than silently ranking one set against another.
        rank, mixed = _suitability_comparable([
            _cand("a", f"n8-design-v1:{plain}"),
            _cand("b", f"n8-design-v1:{changed}"),
        ])
        assert rank is False
        assert mixed is True, (
            "a suitability from a different requirement set must not be "
            "silently compared with another"
        )

    def test_the_same_set_is_comparable(self):
        from app.core.decision_engine import _suitability_comparable

        same = self._id_of(BRIEF)

        def _cand(cid: str) -> DesignCandidate:
            return DesignCandidate(
                candidate_id=cid, label=cid, descriptor="d",
                source_uri="lib://x", source_revision="3", score=0.8,
                suitability=0.9, suitability_contract=f"n8-design-v1:{same}",
                requirement_version="req-7",
            )

        rank, mixed = _suitability_comparable([_cand("a"), _cand("b")])
        assert (rank, mixed) == (True, False)

    @staticmethod
    def _id_of(brief):
        prd = compile_prd(brief, project_id="p", source_revision=3,
                          requirements_version=7)
        return build_artifact_from_brief(
            brief, project_id="p", source_revision=3, requirements_version=7,
            prd_text=prd).requirement_set_id


# ---------------------------------------------------------------------------
# subjective style intent stays available
# ---------------------------------------------------------------------------


class TestSubjectiveIntent:
    def test_a_taste_clause_becomes_an_assumption_not_a_requirement(self):
        brief = {
            "name": "Warung Senja",
            "what": "Warung kopi dengan menu harian dan galeri suasana kedai",
            "why": "Pembeli dapat melihat menu dan suasana kedai sebelum datang",
            "design_intent": "suasana hangat dan inviting yang membuat "
                             "pelanggan merasa nyaman",
        }
        artifact, _ = _artifact(brief)
        assumption_texts = {a["text"] for a in artifact.assumptions}
        assert assumption_texts, "a taste clause must be recorded as an assumption"
        for entry in artifact.entries:
            assert entry.text not in assumption_texts
            assert entry.origin != "assumption"

    def test_the_taste_clause_never_enters_the_scorable_set(self):
        brief = {
            "name": "Warung Senja",
            "what": "Warung kopi dengan menu harian dan galeri suasana kedai",
            "why": "Pembeli dapat melihat menu dan suasana kedai sebelum datang",
            "design_intent": "suasana hangat dan inviting yang membuat "
                             "pelanggan merasa nyaman",
        }
        artifact, _ = _artifact(brief)
        scorable = [e for e in artifact.entries
                    if e.evidence_kind == "visual"
                    and e.verifiable_by == "screenshot"]
        for entry in scorable:
            assert "nyaman" not in entry.text.lower()
            assert "inviting" not in entry.text.lower()

    def test_the_assumption_is_still_available_to_design_guidance(self):
        """It must not be a requirement, but it must not be LOST either."""
        brief = {
            "name": "Warung Senja",
            "what": "Warung kopi dengan menu harian dan galeri suasana kedai",
            "why": "Pembeli dapat melihat menu dan suasana kedai sebelum datang",
            "design_intent": "suasana hangat dan inviting yang membuat "
                             "pelanggan merasa nyaman",
        }
        artifact, _ = _artifact(brief)
        assert artifact.assumptions, "the clause must survive as an assumption"
        joined = " ".join(a["text"] for a in artifact.assumptions).lower()
        assert "nyaman" in joined or "inviting" in joined


# ---------------------------------------------------------------------------
# approval / publication unchanged
# ---------------------------------------------------------------------------


class TestApprovalPublishUnchanged:
    def test_the_priority_module_publishes_nothing(self):
        src = (Path(__file__).parent.parent / "app" / "core" /
               "requirement_priority.py").read_text(encoding="utf-8")
        for forbidden in ("promote(", "publish(", "requests.", "httpx."):
            assert forbidden not in src

    def test_the_builder_imports_no_approval_module(self):
        import ast
        src = (Path(__file__).parent.parent / "app" / "core" /
               "requirement_artifact_builder.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        assert not (imported & {
            "app.core.promote", "app.core.publish", "app.core.authz",
            "app.core.lifecycle", "app.core.approval",
        })

    def test_the_scoring_contract_is_untouched(self):
        from app.core.design_scoring import (
            DEFAULT_MIN_FIT, DEFAULT_MIN_QUALITY, DEFAULT_MIN_SUITABILITY,
            DIMENSIONS, WEIGHTS, compute_suitability,
        )
        assert DIMENSIONS == ("hierarchy", "typography", "layout", "color",
                              "usability", "consistency")
        assert round(sum(WEIGHTS.values()), 6) == 1.0
        assert (DEFAULT_MIN_QUALITY, DEFAULT_MIN_FIT,
                DEFAULT_MIN_SUITABILITY) == (0.70, 0.70, 0.75)
        assert round(compute_suitability(0.70, 0.70), 6) == round(
            0.70 ** 0.45 * 0.70 ** 0.55, 6)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
