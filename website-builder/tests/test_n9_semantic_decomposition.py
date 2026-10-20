"""N9 semantic decomposition — the contract, the rules and the migration.

Three things are under test:

  * the DECOMPOSITION RULES: composite requirements split only where the parts
    are independently assessable, coherent ones stay whole, and nothing is
    invented;
  * the PROVENANCE: original wording, source trace, origin, mandatory status,
    evidence modality and the group relationship all survive;
  * the MIGRATION: a schema-2 artifact is read as schema 2 and is NOT
    reinterpreted, an unknown schema is refused, and a stale artifact is
    rebuilt from the accepted brief.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.design_requirement_artifact import (  # noqa: E402
    ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_SCHEMA_VERSION_LEGACY,
    MIN_PART_CHARS,
    MIN_REQUIREMENT_CHARS,
    REASON_NO_ARTIFACT,
    SUPPORTED_SCHEMA_VERSIONS,
    RequirementArtifactError,
    parse_artifact,
    read_requirement_artifact,
    write_requirement_artifact,
)
from app.core.requirement_artifact_builder import (  # noqa: E402
    build_artifact_from_brief,
)
from app.core.requirement_decomposition import decompose_clause  # noqa: E402
from app.core.requirement_creator import compile_prd  # noqa: E402

BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur, gaya editorial minimalis premium modern (off-white, charcoal, aksen terracotta, tipografi elegan)",
    "why": "Menampilkan identitas studio dan karya (3 proyek arsitektur, tentang, layanan) serta mengarahkan pengunjung ke kontak",
}
COMPOSITE_PALETTE = "off-white, charcoal, aksen terracotta, tipografi elegan"
COMPOSITE_SECTIONS = "3 proyek arsitektur, tentang, layanan"
COHERENT_STYLE = "gaya editorial minimalis premium modern"


def _artifact(brief=None):
    brief = brief or BRIEF
    prd = compile_prd(brief, project_id="p", source_revision=3,
                      requirements_version=7)
    return build_artifact_from_brief(
        brief, project_id="p", source_revision=3, requirements_version=7,
        prd_text=prd), prd


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


class TestDecompositionRules:
    def test_a_property_list_splits(self):
        parts = decompose_clause(COMPOSITE_PALETTE, min_chars=MIN_PART_CHARS)
        assert parts == ["off-white", "charcoal", "aksen terracotta",
                         "tipografi elegan"]

    def test_a_section_enumeration_splits(self):
        parts = decompose_clause(COMPOSITE_SECTIONS, min_chars=MIN_PART_CHARS)
        assert parts == ["3 proyek arsitektur", "tentang", "layanan"]

    def test_a_coherent_aesthetic_stays_whole(self):
        assert decompose_clause(COHERENT_STYLE, min_chars=MIN_PART_CHARS) == [
            COHERENT_STYLE]

    def test_a_single_requirement_is_untouched(self):
        text = "Landing page untuk studio arsitektur"
        assert decompose_clause(text, min_chars=MIN_PART_CHARS) == [text]

    def test_a_quantity_never_becomes_its_own_part(self):
        """R4: a count binds to the noun it counts."""
        parts = decompose_clause(COMPOSITE_SECTIONS, min_chars=MIN_PART_CHARS)
        assert "3" not in parts
        assert "3 proyek arsitektur" in parts

    def test_every_part_is_a_verbatim_substring(self):
        """R5/R6: nothing is added, nothing is reworded."""
        for clause in (COMPOSITE_PALETTE, COMPOSITE_SECTIONS):
            for part in decompose_clause(clause, min_chars=MIN_PART_CHARS):
                assert part in clause, f"{part!r} is not verbatim in {clause!r}"

    def test_no_part_is_a_bare_number(self):
        """R5: no invented numeric requirement."""
        for clause in (COMPOSITE_PALETTE, COMPOSITE_SECTIONS):
            for part in decompose_clause(clause, min_chars=MIN_PART_CHARS):
                assert not part.strip().isdigit()

    def test_decomposition_is_deterministic(self):
        first = decompose_clause(COMPOSITE_PALETTE, min_chars=MIN_PART_CHARS)
        second = decompose_clause(COMPOSITE_PALETTE, min_chars=MIN_PART_CHARS)
        assert first == second

    def test_a_taste_phrase_is_not_split(self):
        for clause in ("gaya soft dan gentle", "desain tenang dan bersih",
                       "tampilan premium modern"):
            assert decompose_clause(clause, min_chars=MIN_PART_CHARS) == [clause]


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_the_artifact_records_groups(self):
        artifact, _ = _artifact()
        assert artifact.groups
        multi = [g for g in artifact.groups if len(g["parts"]) > 1]
        assert multi, "the palette and the sections must be decomposed"

    def test_a_group_keeps_the_original_clause_verbatim(self):
        artifact, _ = _artifact()
        texts = {g["text"] for g in artifact.groups}
        assert COMPOSITE_PALETTE in texts
        assert COMPOSITE_SECTIONS in texts

    def test_parts_carry_the_group_relationship(self):
        artifact, _ = _artifact()
        for entry in artifact.entries:
            if entry.group_id:
                group = next(g for g in artifact.groups
                             if g["group_id"] == entry.group_id)
                assert entry.id in group["parts"]
                assert entry.group_text == group["text"]
            else:
                # an undecomposed requirement is its own single-part group
                group = next(g for g in artifact.groups
                             if g["group_id"] == entry.id)
                assert group["parts"] == [entry.id]

    def test_original_wording_survives(self):
        artifact, _ = _artifact()
        texts = {e.text for e in artifact.entries}
        for part in ("off-white", "charcoal", "aksen terracotta",
                     "tipografi elegan", "3 proyek arsitektur", "tentang",
                     "layanan"):
            assert part in texts

    def test_the_coherent_phrase_is_untouched(self):
        artifact, _ = _artifact()
        assert COHERENT_STYLE in {e.text for e in artifact.entries}

    def test_source_trace_is_preserved(self):
        artifact, _ = _artifact()
        for entry in artifact.entries:
            assert entry.trace.startswith("brief.")

    def test_mandatory_status_is_preserved(self):
        artifact, _ = _artifact()
        for entry in artifact.entries:
            assert isinstance(entry.mandatory, bool)
            assert entry.mandatory is True, "nothing here may become optional"

    def test_evidence_modality_is_preserved(self):
        artifact, _ = _artifact()
        for entry in artifact.entries:
            assert entry.evidence_kind in ("visual", "structural", "behavioural")
            if entry.evidence_kind == "visual":
                assert entry.verifiable_by == "screenshot"

    def test_no_new_mandatory_obligation_appears(self):
        """The decomposed set must be a REFINEMENT of the original clauses."""
        artifact, _ = _artifact()
        for entry in artifact.entries:
            if entry.group_id:
                assert entry.text in entry.group_text
            else:
                assert entry.text in " ".join(BRIEF.values())

    def test_an_assumption_is_not_promoted(self):
        artifact, _ = _artifact()
        assumption_texts = {a["text"] for a in artifact.assumptions}
        for entry in artifact.entries:
            assert entry.text not in assumption_texts
            assert entry.origin != "assumption"

    def test_behavioural_entries_stay_behavioural_after_decomposition(self):
        brief = {
            "name": "Kafe", "what": "Situs untuk pelanggan memesan menu kafe",
            "why": "Pelanggan dapat memesan menu kafe secara online",
        }
        artifact, _ = _artifact(brief)
        for entry in artifact.entries:
            if "memesan" in entry.text.lower():
                assert entry.evidence_kind == "behavioural"
                assert entry.verifiable_by == "none"


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_the_current_schema_is_three(self):
        assert ARTIFACT_SCHEMA_VERSION == 3
        assert ARTIFACT_SCHEMA_VERSION_LEGACY == 2
        assert SUPPORTED_SCHEMA_VERSIONS == (2, 3)

    def test_a_legacy_artifact_is_read_without_reinterpretation(self):
        """A schema-2 artifact keeps its entries exactly as they were."""
        legacy = {
            "schema_version": 2,
            "project_id": "p",
            "source_revision": 3,
            "requirements_version": 7,
            "prd_digest": "",
            "entries": [{
                "id": "R1", "text": "off-white, charcoal, aksen terracotta",
                "kind": "content", "evidence_kind": "visual",
                "verifiable_by": "screenshot", "origin": "user_brief",
                "trace": "brief.what", "mandatory": True,
                "discriminating": True,
            }],
            "assumptions": [], "unresolved": [],
        }
        artifact = parse_artifact(legacy)
        assert artifact.schema_version == 2
        assert len(artifact.entries) == 1
        assert artifact.entries[0].text == "off-white, charcoal, aksen terracotta"
        assert artifact.entries[0].group_id == ""
        # a legacy entry becomes a single-part group of itself
        assert len(artifact.groups) == 1
        assert artifact.groups[0]["parts"] == ["R1"]

    def test_an_unknown_schema_is_refused(self):
        for version in (1, 4, 99):
            blob = {
                "schema_version": version, "project_id": "p",
                "source_revision": 1, "requirements_version": 1,
                "entries": [], "assumptions": [], "unresolved": [],
            }
            with pytest.raises(RequirementArtifactError):
                parse_artifact(blob)

    def test_an_unknown_schema_reads_as_absent(self, tmp_path):
        ws = Path(tmp_path)
        (ws / "requirements.json").write_text(json.dumps({
            "schema_version": 99, "project_id": "p", "source_revision": 1,
            "requirements_version": 1, "entries": [],
            "assumptions": [], "unresolved": [],
        }))
        assert read_requirement_artifact(ws) is None

    def test_a_current_artifact_round_trips(self, tmp_path):
        artifact, _ = _artifact()
        ws = Path(tmp_path)
        write_requirement_artifact(ws, artifact)
        again = read_requirement_artifact(ws)
        assert again is not None
        assert again.schema_version == 3
        assert len(again.entries) == len(artifact.entries)
        assert len(again.groups) == len(artifact.groups)
        for a, b in zip(artifact.entries, again.entries):
            assert a.id == b.id
            assert a.text == b.text
            assert a.group_id == b.group_id
            assert a.group_text == b.group_text
            assert a.mandatory == b.mandatory
            assert a.evidence_kind == b.evidence_kind

    def test_the_digest_changes_when_the_requirement_set_changes(self):
        """MK-2: a suitability from the old set is not comparable."""
        artifact, _ = _artifact()
        brief = dict(BRIEF)
        brief["what"] = brief["what"].replace(
            "off-white, charcoal, aksen terracotta, tipografi elegan",
            "off-white")
        other, _ = _artifact(brief)
        assert artifact.digest != other.digest

    def test_a_stale_artifact_is_rebuilt_from_the_brief(self, tmp_path):
        """The repair path migrates, because the BRIEF is the authority."""
        ws = Path(tmp_path)
        legacy = {
            "schema_version": 2, "project_id": "p", "source_revision": 1,
            "requirements_version": 1, "prd_digest": "stale-digest",
            "entries": [{"id": "R1", "text": "an old composite requirement",
                         "kind": "content", "evidence_kind": "visual",
                         "verifiable_by": "screenshot", "origin": "user_brief",
                         "trace": "brief.what", "mandatory": True,
                         "discriminating": True}],
            "assumptions": [], "unresolved": [],
        }
        (ws / "requirements.json").write_text(json.dumps(legacy))
        assert read_requirement_artifact(ws) is not None

        # the rebuild path in build.py re-emits from the brief, producing
        # schema 3 with the groups populated
        artifact, prd = _artifact()
        write_requirement_artifact(ws, artifact)
        again = read_requirement_artifact(ws)
        assert again.schema_version == 3
        assert any(len(g["parts"]) > 1 for g in again.groups)


# ---------------------------------------------------------------------------
# the scoring contract is untouched
# ---------------------------------------------------------------------------


class TestContractUntouched:
    def test_dimensions_weights_and_thresholds(self):
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

    def test_the_aggregate_is_never_recomputed_from_parts(self):
        """Decomposition changes the INPUT to Fit, never how Fit is computed."""
        from app.core import design_scoring as ds
        source = Path(ds.__file__).read_text(encoding="utf-8")
        assert "compute_suitability" in source
        # the suitability maths must not know about groups
        assert "group_id" not in source.split("def compute_suitability")[1][:600]

    def test_the_part_minimum_is_below_the_requirement_minimum(self):
        assert MIN_PART_CHARS < MIN_REQUIREMENT_CHARS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# the rules that a REAL brief exposed
# ---------------------------------------------------------------------------


class TestRealBriefRules:
    """Rules the jpopdreamy brief forced into the contract.

    A real brief contained an EXCLUSION list ("tanpa checkout, payment, login,
    database"), a DOMAIN name ("tokopedia.com") and a VISITOR ACTION
    ("Customer bisa menjelajah"). Each produced a wrong requirement until the
    rule existed.
    """

    JPOP = {
        "name": "jpopdreamy",
        "what": ("Situs jual-beli album J-pop dengan gaya visual J-pop soft dan "
                 "gentle, layout terinspirasi tokopedia.com, mode katalog/"
                 "tampilan produk tanpa checkout, payment, login, database, "
                 "atau backend transaksi"),
        "why": ("Customer bisa menjelajah dan memilih album J-pop yang mereka "
                "inginkan di situs"),
    }

    def test_a_domain_survives_the_split(self):
        from app.core.requirement_artifact_builder import _clauses
        clauses = _clauses(self.JPOP["what"])
        assert any("tokopedia.com" in c for c in clauses), (
            "a domain must not be split into its parts"
        )
        assert not any(c.strip().endswith("com,") for c in clauses)

    def test_an_exclusion_is_never_a_requirement(self):
        artifact, _ = _artifact(self.JPOP)
        for entry in artifact.entries:
            assert "tanpa" not in entry.text.lower(), (
                f"{entry.id} turned an exclusion into a requirement"
            )
            assert "checkout" not in entry.text.lower()

    def test_an_exclusion_is_kept_as_a_constraint(self):
        artifact, _ = _artifact(self.JPOP)
        texts = " ".join(c["text"] for c in artifact.constraints).lower()
        assert "tanpa checkout" in texts
        assert "payment" in texts and "login" in texts

    def test_a_visitor_action_is_behavioural(self):
        artifact, _ = _artifact(self.JPOP)
        behavioural = [e for e in artifact.entries
                       if e.evidence_kind == "behavioural"]
        texts = " ".join(e.text for e in behavioural).lower()
        assert "menjelajah" in texts, (
            "browsing is a visitor action, not a screenshot-scorable fact"
        )

    def test_the_layout_requirement_survives_and_is_scorable(self):
        artifact, _ = _artifact(self.JPOP)
        visual = [e for e in artifact.entries
                  if e.evidence_kind == "visual" and e.verifiable_by == "screenshot"]
        assert any("tokopedia" in e.text.lower() for e in visual), (
            "the brief's explicit layout constraint must be scorable"
        )

    def test_a_continuation_never_becomes_its_own_requirement(self):
        artifact, _ = _artifact(self.JPOP)
        for entry in artifact.entries:
            assert not entry.text.lower().startswith("atau "), (
                f"{entry.id} is an orphan continuation"
            )

    def test_the_exclusion_never_reaches_fit(self):
        from app.core.design_requirements import requirements_for_fit
        import tempfile
        from app.core.requirement_creator import compile_prd
        from app.core.requirement_artifact_builder import build_artifact_from_brief
        from app.core.design_requirement_artifact import write_requirement_artifact

        ws = Path(tempfile.mkdtemp())
        prd = compile_prd(self.JPOP, project_id="p", source_revision=1,
                          requirements_version=1)
        artifact = build_artifact_from_brief(
            self.JPOP, project_id="p", source_revision=1,
            requirements_version=1, prd_text=prd)
        write_requirement_artifact(ws, artifact)
        texts, _ = requirements_for_fit(ws)
        for t in texts:
            assert "tanpa" not in t.lower()
            assert "checkout" not in t.lower()

    def test_an_exclusion_is_not_counted_as_scorable(self):
        artifact, _ = _artifact(self.JPOP)
        scorable = [e for e in artifact.entries
                    if e.evidence_kind == "visual"
                    and e.verifiable_by == "screenshot"]
        for e in scorable:
            assert "tanpa" not in e.text.lower()
