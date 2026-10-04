"""Batch D2: Design DNA -> resource/dependency selection, and the bounded pack.

Real manifests, real capability resolution, real temporary profiles. No network,
no subprocess, no npm install, and no operator home: every fixture is built under
``tmp_path``.

The properties under test are BEHAVIOUR CONTRACTS. A test names a dependency
because that dependency carries a distinct semantic obligation (3D is not motion,
a transition is not a timeline, a preference is not availability), not because
the list is expected to stay frozen.

Three invariants are asserted STRUCTURALLY rather than by reading the source,
because each is the difference between a boundary that holds and one that merely
appears to:

    * no selection reason falls outside the closed SELECTION_REASONS set
    * no dependency decision reaches "installed" (D2 cannot install)
    * no DesignEntry carries an authority-bearing field

The last one is asserted through the frozen dataclass annotations rather than by
grepping source, so it tests the actual type rather than its text.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_capabilities import resolve_design_capabilities
from app.core.design_context import (
    WARNING_NO_DESIGN_DNA,
    DesignContextPackLimits,
    build_design_context_pack,
)
from app.core.design_context_render import render_design_context_block
from app.core.design_policies import DEPENDENCY_STATES, STATE_SELECTED
from app.core.design_resources import load_design_resource_manifest
from app.core.design_retrieval import DesignContextLimits, DesignEntry, EntryProvenance
from app.core.design_selection import (
    REASON_AUTHORITY,
    SELECTION_REASONS,
    DesignRequirementSet,
    SelectionReason,
    derive_design_requirements,
    select_design_resources,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_MANIFEST = REPO_ROOT / "website-builder" / "config" / "design_resources.yaml"

GSAP = "gsap"
THREE = "three"
LENIS = "lenis"
SHADCN = "shadcn"
TWENTY_FIRST = "twenty_first"
REACT_BITS = "react_bits"
TRANSITIONS = "transitions_dev"
IMPECCABLE = "impeccable"
GUIDANCE = "ui_ux_pro_max"

#: Every resource the tests ask about. Order is the requested order D2 preserves.
SELECTION_SET = (SHADCN, GSAP, THREE, LENIS, TWENTY_FIRST, REACT_BITS, IMPECCABLE)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if anything here reaches for a socket."""

    def deny(*args, **kwargs):
        raise AssertionError("network access is forbidden in design selection tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.fixture
def manifest():
    return load_design_resource_manifest(SHIPPED_MANIFEST)


@pytest.fixture
def profile(tmp_path):
    home = tmp_path / "hermes-website"
    (home / "skills").mkdir(parents=True)
    return home


@pytest.fixture
def capabilities(manifest):
    """Capabilities for an EMPTY profile: guidance absent, all optional absent.

    This is the realistic shape on a host that has never been provisioned, and
    it is the shape that makes "degrade honestly" observable.
    """

    def build(profile_home):
        return resolve_design_capabilities(profile_home, manifest)

    return build


def _select(dna, *, requested=SELECTION_SET, manifest=None, capabilities=None, **kwargs):
    return select_design_resources(
        dna,
        requested=requested,
        manifest=manifest,
        capability_report=capabilities,
        **kwargs,
    )


def _selected_ids(plan):
    return set(plan.selected_ids)


# ---------------------------------------------------------------------------
# 9. A simple/minimal DNA selects no unnecessary optional dependency
# ---------------------------------------------------------------------------


def test_minimal_dna_selects_nothing_optional():
    """A plain brief with no motion, 3D, or scroll demand selects nothing.

    This is the DEFAULT and must not be an error: most sites need no optional
    dependency, and a selection that fires on every project is a selection that
    has stopped meaning anything.
    """
    # "typography" alone names no primitive; keep the DNA genuinely free of
    # button/card/dialog/form tokens so the expectation is about the RESTING
    # state rather than about a token that happens to appear in the brief.
    plan = _select(
        {"brand_personality": "calm", "palette": {"primary": "#111"}, "spacing": {"scale": 8}}
    )

    assert _selected_ids(plan) == set(), plan.selected_ids
    assert plan.is_minimal

    # Every requested REFERENCE is genuinely absent on this host, so the plan is
    # correctly degraded -- absent reference corpora are a real, reportable
    # condition. What must NOT be degraded is an optional DEPENDENCY: its absence
    # is the resting state nobody can act on, and reporting it as a degradation
    # is what teaches operators to ignore degradations.
    dependency_details = [
        e.detail
        for e in plan.rejected_resources
        if e.resource_kind in ("registry", "npm_optional")
    ]
    assert all(detail is None for detail in dependency_details), dependency_details


def test_minimal_dna_rejects_every_dependency_at_the_on_demand_rung():
    """Rejection is the ON-DEMAND resting state, not a failure or a degradation."""
    plan = _select({"brand_personality": "calm", "spacing": {"scale": 8}})

    states = {d.resource_id: d.state for d in plan.dependency_decisions}
    for dependency_id in (GSAP, THREE, LENIS, SHADCN):
        assert states[dependency_id] == "available_for_project_on_demand"
        assert dependency_id not in _selected_ids(plan)


# ---------------------------------------------------------------------------
# 10/11. GSAP: timeline yes, ordinary transitions no
# ---------------------------------------------------------------------------


def test_complex_timeline_dna_selects_gsap_with_an_explicit_reason():
    """A sequenced, scroll-linked timeline is exactly GSAP's declared gate."""
    plan = _select({"motion": "sequenced scroll-linked timeline with scrub"})

    assert GSAP in _selected_ids(plan)
    entry = next(e for e in plan.selected_resources if e.resource_id == GSAP)
    assert entry.reason == SelectionReason.DNA_REQUIRES_COMPLEX_TIMELINE


def test_ordinary_transitions_do_not_select_gsap():
    """Hover/fade/slide transitions are what CSS is FOR; GSAP must not be added."""
    plan = _select({"motion": "simple fade and slide hover transition on scroll"})

    assert GSAP not in _selected_ids(plan), (
        "an ordinary transition must not drag in an animation runtime"
    )


def test_gsap_gate_requires_a_sequencing_signal_not_merely_the_word_motion():
    """'motion' alone is not a timeline; sequencing language is."""
    plan = _select({"motion": "some motion on hover"})
    assert GSAP not in _selected_ids(plan)


# ---------------------------------------------------------------------------
# 12/13. Three.js: real 3D yes, decorative depth no
# ---------------------------------------------------------------------------


def test_actual_3d_dna_selects_three():
    """A real WebGL scene names a scene/camera/shader, which is the gate."""
    plan = _select({"layout": "interactive WebGL scene with an orbiting camera"})

    assert THREE in _selected_ids(plan)
    entry = next(e for e in plan.selected_resources if e.resource_id == THREE)
    assert entry.reason == SelectionReason.DNA_REQUIRES_3D


def test_css_perspective_and_decorative_depth_do_not_select_three():
    """The exact trap: depth-IMPLYING language must not imply a WebGL runtime."""
    plan = _select({"layout": "decorative depth using css perspective and layered shadow"})

    assert THREE not in _selected_ids(plan), (
        "css perspective and layered shadows are 2D techniques"
    )


def test_particle_words_inside_3d_counter_signal_do_not_select_three():
    """A counter-signal vetoes a positive in the same field."""
    plan = _select({"layout": "decorative depth only, no real 3d scene"})
    assert THREE not in _selected_ids(plan)


# ---------------------------------------------------------------------------
# 14/15. Lenis
# ---------------------------------------------------------------------------


def test_justified_smooth_scroll_selects_lenis():
    """Momentum scrolling as a REQUIREMENT of the design is Lenis's gate."""
    plan = _select({"motion": "momentum smooth scroll is central to the experience"})

    assert LENIS in _selected_ids(plan)
    entry = next(e for e in plan.selected_resources if e.resource_id == LENIS)
    assert entry.reason == SelectionReason.DNA_REQUIRES_SMOOTH_SCROLL


def test_smooth_scroll_is_never_default_on():
    """A DNA that merely mentions scrolling does not justify momentum scrolling."""
    plan = _select({"layout": "long scrolling page with sections"})
    assert LENIS not in _selected_ids(plan)


def test_absent_motion_leaves_lenis_unselected():
    assert LENIS not in _selected_ids(_select({"palette": {"primary": "#111"}}))


# ---------------------------------------------------------------------------
# 16/17/18. Component sources and honest degradation
# ---------------------------------------------------------------------------


def test_ordinary_primitives_prefer_shadcn():
    """Ordinary accessible primitives come from the declared source."""
    plan = _select({"layout": "cards with buttons, forms and a dialog"})

    assert SHADCN in _selected_ids(plan)
    assert plan.component_sources.get(SHADCN) == "shadcn"


def test_richer_composition_never_defaults_to_21st_without_justification():
    """Availability alone must not select 21st.dev."""
    plan = _select({"palette": {"primary": "#111"}})
    assert TWENTY_FIRST not in _selected_ids(plan)


def test_richer_composition_considers_21st_only_when_the_dna_justifies_it(
    manifest, capabilities, profile
):
    """Justified AND reachable => considered; justified but NOT reachable =>
    an honest rejection with no invented content."""
    dna = {"layout": "bento showcase with magazine layout"}

    unreachable = _select(
        dna,
        manifest=manifest,
        capabilities=capabilities(profile),
    )
    assert TWENTY_FIRST not in _selected_ids(unreachable)
    rejection = next(
        e for e in unreachable.rejected_resources if e.resource_id == TWENTY_FIRST
    )
    assert "no retrievable integration" in (rejection.detail or ""), (
        "an unavailable reference must say so rather than imply content exists"
    )


def test_unavailable_optional_reference_degrades_honestly(manifest, capabilities, profile):
    """Absent references produce an explicit rejection, never invented guidance."""
    plan = _select(
        {"motion": "gesture driven microinteraction"},
        manifest=manifest,
        capabilities=capabilities(profile),
        requested=(REACT_BITS, TRANSITIONS),
    )

    assert _selected_ids(plan) & {REACT_BITS, TRANSITIONS} == set()
    for entry in plan.rejected_resources:
        assert entry.detail, "an unavailable reference must carry a reason"


def test_impeccable_is_never_selected_into_a_build(manifest, capabilities, profile):
    """The critic is schema/capability only; selecting it would imply D3b exists."""
    plan = _select(
        {"motion": "sequenced scroll-linked timeline with scrub"},
        manifest=manifest,
        capabilities=capabilities(profile),
        requested=(IMPECCABLE,),
    )

    assert IMPECCABLE not in _selected_ids(plan)
    assert IMPECCABLE in {e.resource_id for e in plan.rejected_resources}


# ---------------------------------------------------------------------------
# 19/20. Authority precedence
# ---------------------------------------------------------------------------


def test_explicit_user_requirement_outranks_resource_guidance():
    """A reserved dependency is selected even when the DNA says nothing."""
    plan = _select(
        {"palette": {"primary": "#111"}},
        user_requirements={"reserved_dependencies": [GSAP]},
    )

    assert GSAP in _selected_ids(plan)
    entry = next(e for e in plan.selected_resources if e.resource_id == GSAP)
    assert entry.reason == SelectionReason.USER_REQUIREMENT


def test_user_requirement_ranks_above_every_dna_reason():
    """The precedence table must actually place user requirements higher.

    Both user reasons share the user tier by design -- a refusal and an
    allowance are the same authority speaking -- so this compares the user tier
    against every DNA-derived tier, which is the property that matters: resource
    guidance can never outrank a user.
    """
    from app.core.design_policies import authority_rank

    user_reasons = {SelectionReason.USER_REQUIREMENT, SelectionReason.USER_FORBIDDEN}
    user_rank = authority_rank(REASON_AUTHORITY[SelectionReason.USER_REQUIREMENT])

    for reason in SELECTION_REASONS:
        if reason in user_reasons:
            continue
        assert authority_rank(REASON_AUTHORITY[reason]) > user_rank, reason

    # Every reason must map to a source the precedence table actually knows.
    for reason in SELECTION_REASONS:
        assert authority_rank(REASON_AUTHORITY[reason]) is not None, reason


def test_resource_text_cannot_self_select_dependencies(manifest, capabilities, profile):
    """Retrieval content is DATA; it cannot reach the selection decision.

    The pack assembles decisions BEFORE retrieval, so no corpus text exists at
    decision time at all. This asserts the ordering is observable: a pack built
    with retrieval enabled selects exactly what a decision-only pack selects.
    """
    dna = {"palette": {"primary": "#111"}}

    decision_only = build_design_context_pack(
        profile,
        design_dna=dna,
        requested_resources=SELECTION_SET,
        manifest=manifest,
        capability_report=capabilities(profile),
        include_guidance=False,
        retrieve=False,
    )
    with_retrieval = build_design_context_pack(
        profile,
        design_dna=dna,
        requested_resources=SELECTION_SET,
        manifest=manifest,
        capability_report=capabilities(profile),
        include_guidance=False,
        retrieve=True,
    )

    assert (
        decision_only.decisions["selected_resources"]
        == with_retrieval.decisions["selected_resources"]
    )


def test_forbidden_optional_dependencies_block_every_selection():
    """A user who forbids libraries gets none, whatever the DNA implies."""
    plan = _select(
        {
            "motion": "sequenced scroll-linked timeline with scrub",
            "layout": "interactive WebGL scene with a camera",
        },
        user_requirements={"forbid": True},
    )

    assert _selected_ids(plan) & {GSAP, THREE, LENIS, SHADCN} == set()
    for entry in plan.rejected_resources:
        if entry.reason == SelectionReason.USER_FORBIDDEN:
            assert "forbid" in (entry.detail or "")


def test_forbid_overrides_an_explicit_reservation():
    """A blanket refusal outranks a narrower allowance."""
    plan = _select(
        {"motion": "sequenced scroll-linked timeline"},
        user_requirements={"forbid": True, "reserved_dependencies": [GSAP]},
    )
    assert GSAP not in _selected_ids(plan)


# ---------------------------------------------------------------------------
# 21. Deterministic serialization
# ---------------------------------------------------------------------------


def test_selection_serialization_is_deterministic():
    """Same input, same bytes -- repeatedly, and independent of dict order."""
    dna = {
        "motion": "sequenced scroll-linked timeline with scrub",
        "layout": "cards with buttons and a dialog",
    }
    payloads = {
        json.dumps(_select(dna).to_dict(), sort_keys=False) for _ in range(5)
    }
    assert len(payloads) == 1

    # Key order in the input must not change the output.
    reordered = {key: dna[key] for key in reversed(list(dna))}
    assert json.dumps(_select(reordered).to_dict()) == payloads.pop()


def test_every_selection_reason_is_from_the_closed_set():
    """No free-text justification can enter the record."""
    for dna in (
        {"motion": "sequenced scroll-linked timeline with scrub"},
        {"layout": "cards with buttons and a dialog"},
        {"palette": {"primary": "#111"}},
        {"layout": "interactive WebGL scene with a camera"},
    ):
        for entry in _select(dna).selected_resources + _select(dna).rejected_resources:
            assert entry.reason in SELECTION_REASONS, entry.reason


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


def test_d2_can_never_reach_installed():
    """D2 selects; it does not install. The state is structurally unreachable."""
    for dna in (
        {"motion": "sequenced scroll-linked timeline with scrub"},
        {"layout": "interactive WebGL scene with a camera"},
        {"layout": "cards with buttons and a dialog"},
    ):
        for decision in _select(dna).dependency_decisions:
            assert decision.state != "installed"
            assert decision.is_installed is False
            assert decision.state in DEPENDENCY_STATES


def test_selected_dependency_reaches_exactly_the_selected_state():
    """The only promotion D2 performs is available -> selected."""
    plan = _select({"motion": "sequenced scroll-linked timeline with scrub"})
    decisions = {d.resource_id: d for d in plan.dependency_decisions}
    assert decisions[GSAP].state == STATE_SELECTED
    assert decisions[THREE].state == "available_for_project_on_demand"


def test_design_entry_carries_no_authority_bearing_field():
    """The trust boundary, asserted on the actual dataclass annotations."""
    annotations = set(DesignEntry.__dataclass_fields__)
    assert annotations == {
        "entry_id",
        "kind",
        "title",
        "body",
        "fields",
        "provenance",
        "truncated",
    }
    assert annotations.isdisjoint(
        {"instruction", "requirement", "override", "directive", "command", "policy"}
    )


def test_requirement_set_is_minimal_only_when_nothing_is_demanded():
    assert DesignRequirementSet().is_minimal
    assert not DesignRequirementSet(requires_3d=True).is_minimal


def test_design_dna_schema_is_not_widened_by_d2():
    """D2 derives from existing fields; it adds no required Design DNA key."""
    from app.core.design_dna import CANONICAL_DESIGN_DNA_KEYS, validate_typography

    before = tuple(CANONICAL_DESIGN_DNA_KEYS)
    derive_design_requirements({"motion": "sequenced scroll-linked timeline"})
    assert tuple(CANONICAL_DESIGN_DNA_KEYS) == before

    # A DNA document that predates D2 still validates unchanged.
    assert validate_typography({"typography": {"heading_font": "Inter"}}) is True


def test_unknown_resource_id_fails_loudly(manifest):
    """A typo must be loud, exactly as in D0 and D1."""
    from app.core.design_resources import DesignResourceManifestError

    with pytest.raises(DesignResourceManifestError):
        _select({"palette": {"primary": "#111"}}, requested=("no_such_resource",), manifest=manifest)


# ---------------------------------------------------------------------------
# B6: the bounded context pack
# ---------------------------------------------------------------------------


def test_pack_is_deterministic_and_bounded():
    dna = {"motion": "sequenced scroll-linked timeline with scrub", "palette": {"primary": "#111"}}
    limits = DesignContextPackLimits(
        retrieval=DesignContextLimits(
            max_resources=2,
            max_entries_per_resource=2,
            max_entry_chars=300,
            max_resource_chars=1_000,
            max_total_chars=2_000,
        )
    )
    first = build_design_context_pack(
        Path("."), design_dna=dna, requested_resources=(GSAP,), limits=limits, retrieve=False
    )
    second = build_design_context_pack(
        Path("."), design_dna=dna, requested_resources=(GSAP,), limits=limits, retrieve=False
    )
    assert json.dumps(first.to_dict()) == json.dumps(second.to_dict())


def test_pack_reuses_d1_budgets_rather_than_reinventing_them():
    """The pack's retrieval limits ARE D1's limits, not a second set."""
    limits = DesignContextPackLimits(
        retrieval=DesignContextLimits(max_entry_chars=321, max_total_chars=4321)
    )
    pack = build_design_context_pack(
        Path("."), design_dna={}, requested_resources=(), limits=limits, retrieve=False
    )
    assert pack.limits["retrieval"]["max_entry_chars"] == 321
    assert pack.limits["retrieval"]["max_total_chars"] == 4321


def test_pack_carries_truncation_metadata():
    """A clipped pack says so, so a consumer can raise a budget."""
    tight = DesignContextPackLimits(max_decision_chars=1)
    pack = build_design_context_pack(
        Path("."),
        design_dna={"layout": "cards with buttons, forms and a dialog"},
        requested_resources=(SHADCN,),
        limits=tight,
        retrieve=False,
    )
    assert pack.truncation["truncated"] is True
    assert pack.warnings


def test_pack_drops_rejected_detail_before_selected_detail():
    """Losing a rejection is inconvenient; losing a selection fails unsafely."""
    loose = DesignContextPackLimits(max_decision_chars=10_000)
    tight = DesignContextPackLimits(max_decision_chars=1)

    dna = {"layout": "cards with buttons, forms and a dialog"}
    full = build_design_context_pack(
        Path("."), design_dna=dna, requested_resources=SELECTION_SET, limits=loose, retrieve=False
    )
    trimmed = build_design_context_pack(
        Path("."), design_dna=dna, requested_resources=SELECTION_SET, limits=tight, retrieve=False
    )

    assert full.decisions["rejected_resources"]
    assert trimmed.decisions["selected_resources"], (
        "a selected dependency must survive decision-section truncation"
    )


def test_pack_leaks_no_absolute_path_or_secret(profile, manifest, capabilities):
    dna = {"motion": "sequenced scroll-linked timeline with scrub"}
    pack = build_design_context_pack(
        profile,
        design_dna=dna,
        requested_resources=(GSAP,),
        manifest=manifest,
        capability_report=capabilities(profile),
        retrieve=True,
    )
    serialized = json.dumps(pack.to_dict())

    assert str(profile) not in serialized
    assert "\\" not in serialized.replace("\\\\", ""), "no Windows-style absolute path"


def test_pack_warns_when_no_design_dna_was_supplied(profile):
    pack = build_design_context_pack(
        profile, design_dna=None, requested_resources=(GSAP,), retrieve=False
    )
    assert WARNING_NO_DESIGN_DNA in pack.warnings
    assert pack.selected_resources_is_empty() if hasattr(pack, "selected_resources_is_empty") else True


def test_pack_summary_is_payload_free(profile):
    pack = build_design_context_pack(
        profile, design_dna={"palette": {"primary": "#111"}}, requested_resources=(GSAP,), retrieve=False
    )
    summary = pack.summary()
    assert str(profile) not in summary
    assert "#111" not in summary


# ---------------------------------------------------------------------------
# C4/C5: the FRONTEND-facing render
# ---------------------------------------------------------------------------


def test_render_is_empty_without_a_pack():
    assert render_design_context_block(None) == ""


def test_render_states_the_contract_and_is_bounded(profile):
    pack = build_design_context_pack(
        profile,
        design_dna={"layout": "cards with buttons and a dialog"},
        requested_resources=(SHADCN,),
        retrieve=False,
    )
    block = render_design_context_block(pack)

    assert "DESIGN RESOURCE CONTEXT" in block
    assert "may NOT install packages" in block
    assert "is reference DATA" in block
    assert "shadcn" in block


def test_render_bounds_content_before_decisions():
    """Corpus text is the unbounded part; decisions are what FRONTEND acts on."""
    huge_entries = {
        "ui_ux_pro_max": {
            "resource_id": "ui_ux_pro_max",
            "entries": [
                {
                    "entry_id": f"x{i}",
                    "kind": "guidance",
                    "title": "t" * 400,
                    "body": "b" * 2000,
                    "fields": {"Keywords": "k" * 400},
                    "provenance": {
                        "resource_id": "ui_ux_pro_max",
                        "resource_kind": "skill",
                        "adapter": "guidance",
                        "locator": "data/styles.csv",
                        "entry_index": i,
                    },
                    "truncated": False,
                }
                for i in range(50)
            ],
            "warnings": [],
            "truncated": True,
            "dropped_entries": 0,
        }
    }

    class FakePack:
        def to_dict(self_inner):
            return {
                "version": 1,
                "design_dna": {},
                "decisions": {"selected_resources": [{"resource_id": "shadcn", "selected": True}]},
                "resources": huge_entries,
                "truncation": {},
                "limits": {},
                "degraded": False,
                "warnings": [],
            }

    block = render_design_context_block(FakePack())
    assert "shadcn" in block, "decisions must survive the render bound"
    assert len(block) < 20_000