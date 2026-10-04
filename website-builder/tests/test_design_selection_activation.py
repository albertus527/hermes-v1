"""Batch D3a.5 Part K: D2 selection consumes the activation capability.

The point of Part K is that a SINGLE boolean can no longer describe these
resources. The properties under test:

    * with an activation report, reachability follows the per-axis record
    * a resource usable for ONE axis is reachable (the 21st.dev case)
    * a resource with NO usable axis is not reachable, even if D0 said available
    * a resource missing from activation is NOT reachable -- never invented
    * omitting the activation report preserves the pre-D3a.5 behaviour exactly
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_activation import (
    ACTIVATION_REASONS,
    ResourceActivationCapability,
)
from app.core.design_capabilities import (
    STATUS_AVAILABLE,
    DesignCapability,
    DesignCapabilityReport,
)
from app.core.design_selection import _is_reachable


def _cap(**overrides):
    base = dict(
        resource_id="r",
        kind="reference",
        required=False,
        configured=True,
        available=True,
        status=STATUS_AVAILABLE,
        detail="",
    )
    base.update(overrides)
    return DesignCapability(**base)


def _d0_report(*capabilities):
    return DesignCapabilityReport(
        ok=True,
        manifest_version=1,
        profile_skills_dir="/tmp/skills",
        resources={c.resource_id: c for c in capabilities},
        failures=[],
        degraded=[],
    )


def _activation(*capabilities):
    class Report:
        def __init__(self, caps):
            self.resources = {c.resource_id: c for c in caps}

    return Report(capabilities)


def _act(resource_id="r", **overrides):
    base = dict(resource_id=resource_id, reasons=())
    base.update(overrides)
    return ResourceActivationCapability(**base)


# ---------------------------------------------------------------------------
# With no activation report, behaviour is unchanged
# ---------------------------------------------------------------------------


def test_no_reports_is_never_reachable():
    """We cannot prove reachability, so we never invent it."""
    assert _is_reachable(None, "r") is False


def test_a_d0_available_resource_is_reachable_without_activation():
    assert _is_reachable(_d0_report(_cap()), "r", None) is True


def test_a_d0_unavailable_resource_is_not_reachable_without_activation():
    report = _d0_report(_cap(available=False, status="unavailable_optional"))

    assert _is_reachable(report, "r") is False


def test_a_resource_missing_from_d0_is_not_reachable():
    assert _is_reachable(_d0_report(), "r") is False


# ---------------------------------------------------------------------------
# With an activation report it is authoritative
# ---------------------------------------------------------------------------


def test_an_activation_usable_resource_is_reachable():
    report = _activation(_act(retrieval_available=True))

    assert _is_reachable(None, "r", report) is True


def test_discovery_alone_makes_a_resource_reachable():
    """21st.dev: free search works with no credential, retrieval does not.

    The single D0 boolean forced a choice between discarding a true capability
    and claiming an unauthenticated one. Discovery-only must count as reachable.
    """
    report = _activation(_act(discovery_available=True, retrieval_available=False))

    assert _is_reachable(None, "r", report) is True


def test_a_resource_with_no_usable_axis_is_not_reachable():
    """Every axis false: the resource genuinely contributes nothing."""
    report = _activation(_act())

    assert _is_reachable(None, "r", report) is False


def test_a_critic_axis_alone_is_a_present_capability():
    report = _activation(_act(critic_available=True))

    assert _is_reachable(None, "r", report) is True


def test_activation_overrules_a_d0_available_flag():
    """Activation is authoritative when both are supplied."""
    d0 = _d0_report(_cap(available=True))
    report = _activation(_act(retrieval_available=False, discovery_available=False))

    assert _is_reachable(d0, "r", report) is False


def test_activation_confirms_what_d0_said():
    d0 = _d0_report(_cap(available=True))
    report = _activation(_act(retrieval_available=True))

    assert _is_reachable(d0, "r", report) is True


def test_a_resource_absent_from_activation_is_never_invented():
    """Declared in D0, missing from activation: unproven, so unreachable."""
    d0 = _d0_report(_cap())
    report = _activation()  # empty

    assert _is_reachable(d0, "r", report) is False


def test_a_degraded_but_usable_resource_is_still_reachable():
    """Degraded is a diagnostic, not a gate."""
    report = _activation(_act(retrieval_available=True, degraded=True))

    assert _is_reachable(None, "r", report) is True


def test_a_credential_present_but_unusable_resource_is_not_reachable():
    """Presence is not usability: the axis is what decides."""
    report = _activation(
        _act(
            authentication_required=True,
            authentication_present=True,
            retrieval_available=False,
        )
    )

    assert _is_reachable(None, "r", report) is False


def test_install_on_demand_counts_as_a_present_capability():
    """`install_available` is deliberately one of the axes `.usable` sums.

    An on-demand resource that CAN be installed is genuinely present as a
    capability -- that is exactly why its D0 resting state is `not_installed`
    rather than a permanent degradation. Excluding it here would reintroduce
    the "unactionable unavailable" problem the manifest comments call out.
    """
    report = _activation(_act(install_available=True))

    assert _is_reachable(None, "r", report) is True


def test_one_of_several_resources_is_reachable_without_affecting_others():
    report = _activation(
        _act(resource_id="a", retrieval_available=True),
        _act(resource_id="b"),
    )

    assert _is_reachable(None, "a", report) is True
    assert _is_reachable(None, "b", report) is False


# ---------------------------------------------------------------------------
# The reason vocabulary the capability carries
# ---------------------------------------------------------------------------


def test_the_reason_vocabulary_is_closed():
    assert isinstance(ACTIVATION_REASONS, frozenset)
    assert ACTIVATION_REASONS


def test_an_unknown_reason_cannot_be_constructed():
    with pytest.raises(ValueError):
        ResourceActivationCapability(resource_id="r", reasons=("not_a_reason",))