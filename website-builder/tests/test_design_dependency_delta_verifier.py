"""Focused tests for the reusable bounded direct-dependency snapshot/diff verifier.

The verifier composes the primitives the registry install paths already use
(``dependency_delta``, ``removed_direct_dependencies``, ``changed_manifest_sections``)
into ONE reusable check an operation can call around any mutation. Its invariants
are properties of its TYPES, not caller discipline:

* ``DirectDependencyDeltaVerdict`` binds ``ok`` to its payload at construction;
* the DECISION is computed from the full delta; only the REPORTED names are bounded;
* a verdict names only names (sections/packages), never a manifest value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_install import (
    DirectDependencyDeltaVerdict,
    DirectDependencySnapshot,
    REASON_MANIFEST_SECTION_CHANGED,
    REASON_REGISTRY_DEPENDENCY_DRIFT,
    _MAX_DELTA_NAMES,
    snapshot_direct_dependency_state,
    verify_direct_dependency_delta,
)

REVIEWED = ("cn", "radix-ui", "lucide-react")


def _project(tmp_path: Path, document: dict) -> Path:
    (tmp_path / "package.json").write_text(json.dumps(document), encoding="utf-8")
    return tmp_path


def _verdict(tmp_path: Path, before: dict, after: dict, allowed=REVIEWED):
    root = _project(tmp_path, before)
    snapshot_before = snapshot_direct_dependency_state(root)
    _project(tmp_path, after)
    snapshot_after = snapshot_direct_dependency_state(root)
    return verify_direct_dependency_delta(
        snapshot_before, snapshot_after, allowed_additions=allowed
    )


# ---------------------------------------------------------------------------
# The snapshot pairs both views in one capture
# ---------------------------------------------------------------------------


def test_the_snapshot_carries_both_views(tmp_path):
    root = _project(tmp_path, {
        "name": "p",
        "dependencies": {"react": "19.3.0"},
        "overrides": {"x": "1.0.0"},
    })

    state = snapshot_direct_dependency_state(root)

    assert state.direct["dependencies"] == {"react": "19.3.0"}
    assert "overrides" in state.sections


def test_a_missing_manifest_snapshots_empty(tmp_path):
    from app.core.design_install import SNAPSHOT_SECTIONS

    state = snapshot_direct_dependency_state(tmp_path)

    assert state.direct == {section: {} for section in SNAPSHOT_SECTIONS}
    assert state.sections == {}


# ---------------------------------------------------------------------------
# The decision matrix
# ---------------------------------------------------------------------------


def test_a_reviewed_package_added_to_dependencies_is_acceptable(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0"}},
        {"name": "p", "dependencies": {"react": "19.3.0", "cn": "0.4.0"}},
    )

    assert v.ok is True
    assert v.reason is None
    assert v.to_dict()["added"] == []


def test_an_unreviewed_package_added_is_refused(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0"}},
        {"name": "p", "dependencies": {"react": "19.3.0", "evil": "1.0.0"}},
    )

    assert v.ok is False
    assert v.reason == REASON_REGISTRY_DEPENDENCY_DRIFT
    assert ("dependencies", "evil") in v.added


@pytest.mark.parametrize("section", ["devDependencies", "optionalDependencies", "peerDependencies"])
def test_a_reviewed_package_in_a_non_runtime_section_is_refused(tmp_path, section):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {}, section: {"cn": "0.4.0"}},
    )

    assert v.ok is False
    assert (section, "cn") in v.added


def test_a_removed_project_dependency_is_refused(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0", "cn": "0.4.0"}},
        {"name": "p", "dependencies": {"react": "19.3.0"}},
    )

    assert v.ok is False
    assert ("dependencies", "cn") in v.removed


@pytest.mark.parametrize("section", ["overrides", "resolutions", "packageManager"])
def test_an_unexpected_section_change_is_refused(tmp_path, section):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {}, section: {"x": "1.0.0"}},
    )

    assert v.ok is False
    assert v.reason == REASON_MANIFEST_SECTION_CHANGED
    assert section in v.sections_changed


def test_no_change_is_acceptable(tmp_path):
    doc = {"name": "p", "dependencies": {"react": "19.3.0"}, "devDependencies": {"vite": "7.0.0"}}
    v = _verdict(tmp_path, doc, dict(doc))

    assert v.ok is True


def test_an_empty_allowed_set_accepts_no_addition(tmp_path):
    """The conservative default: a caller that forgets the reviewed set adds nothing."""
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {"cn": "0.4.0"}},
        allowed=(),
    )

    assert v.ok is False
    assert ("dependencies", "cn") in v.added


# ---------------------------------------------------------------------------
# The verdict's invariants are properties of the TYPE
# ---------------------------------------------------------------------------


def test_an_ok_verdict_may_not_carry_an_offending_delta():
    with pytest.raises(ValueError):
        DirectDependencyDeltaVerdict(ok=True, added=(("dependencies", "evil"),))


def test_a_refusal_must_name_an_offending_delta():
    with pytest.raises(ValueError):
        DirectDependencyDeltaVerdict(ok=False, reason="r")


def test_a_refusal_must_carry_a_reason():
    with pytest.raises(ValueError):
        DirectDependencyDeltaVerdict(ok=False, added=(("dependencies", "evil"),))


def test_the_valid_verdicts_construct():
    assert DirectDependencyDeltaVerdict(ok=True).ok is True
    assert DirectDependencyDeltaVerdict(
        ok=False, added=(("dependencies", "evil"),), reason="r"
    ).ok is False


# ---------------------------------------------------------------------------
# Bounded output, unaffected decision
# ---------------------------------------------------------------------------


def test_the_decision_uses_the_full_delta_but_the_report_is_bounded():
    huge = {f"pkg{i}": "1.0.0" for i in range(_MAX_DELTA_NAMES + 50)}
    before = DirectDependencySnapshot(direct={"dependencies": {}}, sections={})
    after = DirectDependencySnapshot(direct={"dependencies": huge}, sections={})

    v = verify_direct_dependency_delta(before, after, allowed_additions=())

    assert v.ok is False, "the decision is computed from the FULL delta"
    assert len(v.added) == _MAX_DELTA_NAMES, "the report is bounded"
    assert v.truncated is True


def test_a_small_delta_is_not_reported_as_truncated(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {"evil": "1.0.0"}},
    )

    assert v.truncated is False


# ---------------------------------------------------------------------------
# A verdict names NAMES only, never a value
# ---------------------------------------------------------------------------


def test_the_verdict_never_echoes_a_manifest_value(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {"evil": "https://evil.example/x.tgz"}},
    )
    rendered = json.dumps(v.to_dict())

    assert "evil" in rendered, "the offending NAME is reported"
    assert "https://evil.example" not in rendered, "the VALUE is never echoed"
    assert "1.0.0" not in rendered


def test_the_serialization_is_deterministic(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {"b": "1.0.0", "a": "1.0.0"}},
    )

    assert v.to_dict() == v.to_dict()
    assert v.to_dict()["added"] == [["dependencies", "a"], ["dependencies", "b"]]


# ---------------------------------------------------------------------------
# The reusable verifier AGREES with the primitive the install paths call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "before, after",
    [
        ({"dependencies": {}}, {"dependencies": {"cn": "0.4.0"}}),
        ({"dependencies": {}}, {"dependencies": {"evil": "1.0.0"}}),
        ({"dependencies": {}}, {"devDependencies": {"cn": "0.4.0"}}),
        ({"dependencies": {"cn": "0.4.0"}}, {"dependencies": {}}),
        ({"dependencies": {"react": "19.3.0"}}, {"dependencies": {"react": "19.3.0"}}),
    ],
)
def test_the_verifier_agrees_with_registry_dependency_delta_is_acceptable(before, after):
    """The additions/removals decision must match the existing primitive.

    The install paths call ``registry_dependency_delta_is_acceptable``; this
    reusable verifier must not disagree with it, or the two would drift.
    """
    from app.core.design_install import registry_dependency_delta_is_acceptable

    ok, offending = registry_dependency_delta_is_acceptable(
        before, after, allowed_packages=list(REVIEWED)
    )
    snapshot_before = DirectDependencySnapshot(direct=before, sections={})
    snapshot_after = DirectDependencySnapshot(direct=after, sections={})
    verdict = verify_direct_dependency_delta(
        snapshot_before, snapshot_after, allowed_additions=REVIEWED
    )

    # The verdict also checks sections; with empty section snapshots that is a
    # no-op, so the two must agree exactly on the dependency decision.
    assert verdict.ok is ok, (before, after, verdict.to_dict(), offending)
    assert {name for _section, name in verdict.added} | {
        name for _section, name in verdict.removed
    } == set(offending)


# ---------------------------------------------------------------------------
# "At minimum reason over these sections: dependencies"
# ---------------------------------------------------------------------------
# The reviewed surface is a MINIMUM, not a maximum: `dependencies` must always be
# snapshotted and reasoned over, whatever else the surface grows or shrinks to.
# A future edit that quietly drops it from SNAPSHOT_SECTIONS must fail here.


def test_dependencies_is_always_a_reviewed_section():
    from app.core.design_install import SNAPSHOT_SECTIONS, SECTION_DEPENDENCIES

    assert SECTION_DEPENDENCIES == "dependencies"
    assert SECTION_DEPENDENCIES in SNAPSHOT_SECTIONS


def test_a_dependencies_addition_is_reasoned_over_even_with_an_empty_allowed_set(tmp_path):
    """The section is part of the snapshot UNCONDITIONALLY -- not opted into."""
    before = snapshot_direct_dependency_state(
        _project(tmp_path, {"name": "p", "dependencies": {}})
    )
    after = snapshot_direct_dependency_state(
        _project(tmp_path, {"name": "p", "dependencies": {"anything": "1.0.0"}})
    )

    verdict = verify_direct_dependency_delta(before, after)  # no allowed set

    assert verdict.ok is False
    assert ("dependencies", "anything") in verdict.added


def test_a_removal_from_dependencies_is_always_reasoned_over(tmp_path):
    before = snapshot_direct_dependency_state(
        _project(tmp_path, {"name": "p", "dependencies": {"react": "19.3.0"}})
    )
    after = snapshot_direct_dependency_state(
        _project(tmp_path, {"name": "p", "dependencies": {}})
    )

    verdict = verify_direct_dependency_delta(before, after)

    assert verdict.ok is False
    assert ("dependencies", "react") in verdict.removed


def test_the_whole_reviewed_surface_is_snapshotted(tmp_path):
    """Every reviewed section is captured, so no section is silently skipped."""
    from app.core.design_install import SNAPSHOT_SECTIONS

    document = {"name": "p"}
    for index, section in enumerate(SNAPSHOT_SECTIONS):
        document[section] = {f"pkg-{index}": "1.0.0"}

    state = snapshot_direct_dependency_state(_project(tmp_path, document))

    for index, section in enumerate(SNAPSHOT_SECTIONS):
        assert state.direct[section] == {f"pkg-{index}": "1.0.0"}


# ---------------------------------------------------------------------------
# "At minimum reason over these sections: devDependencies"
# ---------------------------------------------------------------------------
# Like `dependencies`, `devDependencies` is part of the reviewed surface and is
# snapshotted unconditionally. It differs in one way worth pinning: a reviewed
# package is LEGITIMATELY added there (``@types/three``), so its allowed SECTION
# is devDependencies, not the runtime default -- the mapping form exists for
# exactly this. The verifier checks MEMBERSHIP + SECTION; the VERSION dimension
# is the separate exactness check, verified here to backstop it.


def test_devdependencies_is_always_a_reviewed_section():
    from app.core.design_install import SNAPSHOT_SECTIONS

    assert "devDependencies" in SNAPSHOT_SECTIONS


def test_a_reviewed_package_may_be_added_to_devdependencies_when_the_mapping_says_so(tmp_path):
    """The legitimate shape: @types/three belongs in devDependencies."""
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {}, "devDependencies": {"@types/three": "0.186.0"}},
        allowed={"@types/three": "devDependencies"},
    )

    assert v.ok is True


def test_a_devdependency_addition_is_refused_by_the_default_runtime_section(tmp_path):
    """The registry shape maps everything to `dependencies`, so a dev addition fails."""
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {}, "devDependencies": {"cn": "0.4.0"}},
        allowed=["cn"],
    )

    assert v.ok is False
    assert ("devDependencies", "cn") in v.added


def test_a_devdependency_removal_is_always_reasoned_over(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "devDependencies": {"vite": "7.0.0"}},
        {"name": "p", "devDependencies": {}},
    )

    assert v.ok is False
    assert ("devDependencies", "vite") in v.removed


def test_a_devdependency_version_change_is_backstopped_by_the_exactness_dimension(tmp_path):
    """Membership accepts an allowed package's version change; exactness must refuse it.

    This pins the SPLIT: the verifier reasons over WHICH packages + their SECTION,
    and a version change is the SEPARATE exactness dimension's job. Verified here
    that the exactness postcondition refuses a devDependency that is not at its
    exact pin -- so the version dimension is never left unguarded.
    """
    from app.core.design_install import project_satisfies_dependency

    # membership alone: an allowed package's version change passes
    v = _verdict(
        tmp_path,
        {"name": "p", "devDependencies": {"@types/three": "0.186.0"}},
        {"name": "p", "devDependencies": {"@types/three": "0.100.0"}},
        allowed={"@types/three": "devDependencies"},
    )
    assert v.ok is True

    # exactness alone: the same change is refused at the postcondition
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "p",
        "dependencies": {"three": "0.186.1"},
        "devDependencies": {"@types/three": "0.100.0"},
    }))
    assert project_satisfies_dependency(tmp_path, "three") is False

    (tmp_path / "package.json").write_text(json.dumps({
        "name": "p",
        "dependencies": {"three": "0.186.1"},
        "devDependencies": {"@types/three": "0.186.0"},
    }))
    assert project_satisfies_dependency(tmp_path, "three") is True


# ---------------------------------------------------------------------------
# "At minimum reason over these sections: optionalDependencies"
# ---------------------------------------------------------------------------
# `optionalDependencies` is part of the reviewed surface and is snapshotted
# unconditionally. Unlike `dependencies`/`devDependencies`, no application-owned
# spec targets it today, so it is included DEFENSIVELY -- an unreviewed package
# smuggled there must still be refused. It is caught by TWO layers: the
# delta/membership check (because it is in SNAPSHOT_SECTIONS) and, as a backstop,
# the section-freeze check (which catches ANY change outside the reviewed
# surface). Both are pinned here.


def test_optionaldependencies_is_always_a_reviewed_section():
    from app.core.design_install import SNAPSHOT_SECTIONS

    assert "optionalDependencies" in SNAPSHOT_SECTIONS


def test_an_unreviewed_optional_dependency_is_refused(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "optionalDependencies": {}},
        {"name": "p", "optionalDependencies": {"evil": "1.0.0"}},
        allowed={"fsevents": "optionalDependencies"},
    )

    assert v.ok is False
    assert ("optionalDependencies", "evil") in v.added


def test_a_reviewed_package_may_be_added_to_optionaldependencies_via_the_mapping(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "optionalDependencies": {}},
        {"name": "p", "optionalDependencies": {"fsevents": "2.3.3"}},
        allowed={"fsevents": "optionalDependencies"},
    )

    assert v.ok is True


def test_a_removal_from_optionaldependencies_is_always_reasoned_over(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "optionalDependencies": {"fsevents": "2.3.3"}},
        {"name": "p", "optionalDependencies": {}},
    )

    assert v.ok is False
    assert ("optionalDependencies", "fsevents") in v.removed


def test_optionaldependencies_is_caught_by_the_section_freeze_layer_too(tmp_path):
    """Defense in depth: even if it left the reviewed surface, the freeze catches it.

    Passing it as an UNREVIEWED section to `changed_manifest_sections` must still
    refuse ANY change -- so the property does not depend on one layer alone.
    """
    from app.core.design_install import changed_manifest_sections

    before = {"name": '"p"', "dependencies": "{}"}
    after = {"name": '"p"', "dependencies": "{}", "optionalDependencies": '{"evil": "1.0.0"}'}

    changed = changed_manifest_sections(
        before, after, reviewed_sections=("dependencies",)
    )

    assert "optionalDependencies" in changed


# ---------------------------------------------------------------------------
# "At minimum reason over these sections: peerDependencies"
# ---------------------------------------------------------------------------
# `peerDependencies` is the last of the four reviewed sections and is snapshotted
# unconditionally. Like `optionalDependencies`, no application-owned spec targets
# it today, so it is included DEFENSIVELY. It is caught by the same two layers:
# the delta/membership check (it is in SNAPSHOT_SECTIONS) and the section-freeze
# backstop. Its sibling `peerDependenciesMeta` is a SEPARATE top-level section
# OUTSIDE the surface, so a change there is frozen -- pinned below too.


def test_peerdependencies_is_always_a_reviewed_section():
    from app.core.design_install import SNAPSHOT_SECTIONS

    assert "peerDependencies" in SNAPSHOT_SECTIONS


def test_an_unreviewed_peer_dependency_is_refused(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "peerDependencies": {}},
        {"name": "p", "peerDependencies": {"evil": "1.0.0"}},
        allowed={"react": "peerDependencies"},
    )

    assert v.ok is False
    assert ("peerDependencies", "evil") in v.added


def test_a_reviewed_package_may_be_added_to_peerdependencies_via_the_mapping(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "peerDependencies": {}},
        {"name": "p", "peerDependencies": {"react": "19.3.0"}},
        allowed={"react": "peerDependencies"},
    )

    assert v.ok is True


def test_a_removal_from_peerdependencies_is_always_reasoned_over(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "peerDependencies": {"react": "19.3.0"}},
        {"name": "p", "peerDependencies": {}},
    )

    assert v.ok is False
    assert ("peerDependencies", "react") in v.removed


def test_peerdependenciesmeta_is_frozen_outside_the_surface(tmp_path):
    """A peer's modifier section is a SEPARATE top-level key, outside the surface."""
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {}, "peerDependenciesMeta": {"react": {"optional": True}}},
        allowed=(),
    )

    assert v.ok is False
    assert "peerDependenciesMeta" in v.sections_changed


def test_the_reviewed_surface_is_exactly_the_four_dependency_sections():
    """The surface is a closed, known set -- no section is silently added or lost."""
    from app.core.design_install import SNAPSHOT_SECTIONS

    assert SNAPSHOT_SECTIONS == (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
    )


# ---------------------------------------------------------------------------
# "Before/after comparison must identify: added direct package"
# ---------------------------------------------------------------------------
# The verdict must identify an ADDED direct package AS an addition -- not conflate
# it with a version change of an existing package. `added` holds only genuinely
# NEW packages; an already-present package whose version moved is `changed`. Both
# are still offending (ok=False), so the split is about IDENTITY, not policy.


def test_an_added_package_is_identified_as_added_not_changed(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0"}},
        {"name": "p", "dependencies": {"react": "19.3.0", "evil": "1.0.0"}},
    )

    assert v.added == (("dependencies", "evil"),)
    assert v.changed == ()


def test_a_version_change_is_identified_as_changed_not_added(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0"}},
        {"name": "p", "dependencies": {"react": "19.4.0"}},
    )

    assert v.added == ()
    assert v.changed == (("dependencies", "react"),)


def test_an_addition_and_a_change_are_kept_in_separate_buckets(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0"}},
        {"name": "p", "dependencies": {"react": "19.4.0", "evil": "1.0.0"}},
    )

    assert v.added == (("dependencies", "evil"),)
    assert v.changed == (("dependencies", "react"),)
    assert v.ok is False


def test_a_reviewed_addition_is_in_neither_bucket(tmp_path):
    """A correctly-placed reviewed package is not an offending addition."""
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {}},
        {"name": "p", "dependencies": {"cn": "0.4.0"}},
        allowed=("cn",),
    )

    assert v.ok is True
    assert v.added == ()
    assert v.changed == ()


def test_a_changed_only_verdict_serializes_both_buckets(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0"}},
        {"name": "p", "dependencies": {"react": "19.4.0"}},
    )

    document = v.to_dict()
    assert document["added"] == []
    assert document["changed"] == [["dependencies", "react"]]


def test_the_ok_binding_covers_the_changed_bucket():
    """An ok verdict may not carry a `changed` entry either."""
    with pytest.raises(ValueError):
        DirectDependencyDeltaVerdict(ok=True, changed=(("dependencies", "x"),))


def test_a_refusal_may_name_only_a_changed_entry():
    verdict = DirectDependencyDeltaVerdict(
        ok=False, changed=(("dependencies", "x"),), reason="r"
    )

    assert verdict.ok is False
    assert verdict.changed == (("dependencies", "x"),)


# ---------------------------------------------------------------------------
# "Before/after comparison must identify: removed direct package"
# ---------------------------------------------------------------------------
# The verdict must identify a REMOVED direct package AS a removal -- distinct
# from a version change (which is `changed`) and from an addition (which is
# `added`). A package moved between sections is BOTH a removal (from its original
# section) and an addition (to the new one).


def test_a_removed_package_is_identified_as_removed(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0", "cn": "0.4.0"}},
        {"name": "p", "dependencies": {"react": "19.3.0"}},
    )

    assert v.removed == (("dependencies", "cn"),)
    assert v.added == ()
    assert v.changed == ()


def test_a_version_change_is_not_a_removal(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"react": "19.3.0"}},
        {"name": "p", "dependencies": {"react": "19.4.0"}},
    )

    assert v.removed == ()
    assert v.changed == (("dependencies", "react"),)


@pytest.mark.parametrize("section", [
    "dependencies", "devDependencies", "optionalDependencies", "peerDependencies"
])
def test_a_removal_is_identified_in_every_reviewed_section(tmp_path, section):
    v = _verdict(
        tmp_path,
        {"name": "p", section: {"x": "1.0.0"}},
        {"name": "p", section: {}},
    )

    assert v.removed == ((section, "x"),)
    assert v.ok is False


def test_a_moved_package_is_a_removal_and_an_addition(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"cn": "0.4.0"}},
        {"name": "p", "devDependencies": {"cn": "0.4.0"}},
    )

    assert v.removed == (("dependencies", "cn"),)
    assert v.added == (("devDependencies", "cn"),)


def test_removing_a_reviewed_package_is_still_a_removal(tmp_path):
    """Allowed to ADD a reviewed package is not allowed to REMOVE one."""
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"cn": "0.4.0"}},
        {"name": "p", "dependencies": {}},
        allowed=("cn",),
    )

    assert v.removed == (("dependencies", "cn"),)
    assert v.ok is False


def test_a_removed_only_verdict_serializes_the_removed_bucket(tmp_path):
    v = _verdict(
        tmp_path,
        {"name": "p", "dependencies": {"cn": "0.4.0"}},
        {"name": "p", "dependencies": {}},
    )

    document = v.to_dict()
    assert document["removed"] == [["dependencies", "cn"]]
    assert document["added"] == []
    assert document["changed"] == []
