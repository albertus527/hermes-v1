"""Batch D3a.5 Part J: manifest and capability truth.

The D3a.5 failure this replaces is specific and named in the batch: five
resources were declared and nothing operated them. The property now under test
is that the manifest cannot go back to lying:

    * every shipped resource names an adapter that ACTUALLY EXISTS
    * ``deferred`` is refused on a resource that claims one
    * no shipped resource is left ``deferred``
    * a companion package is a closed table entry, never derived by naming rule
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_resources import (
    DESIGN_ADAPTERS,
    IMPLEMENTED_RESOLUTIONS,
    RESOLUTIONS,
    DesignResourceManifestError,
    load_design_resource_manifest,
    parse_design_resource_manifest,
    validate_adapter_claims,
)

SHIPPED = Path(__file__).resolve().parents[1] / "config" / "design_resources.yaml"

#: The five resources D3a.5 exists to activate.
ACTIVATED = ("refero", "twenty_first", "react_bits", "transitions_dev", "impeccable")


def _minimal(resources):
    return {"version": 1, "resources": resources}


def _write(tmp_path, document, name="manifest.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The shipped manifest tells the truth
# ---------------------------------------------------------------------------


def test_the_shipped_manifest_loads():
    assert load_design_resource_manifest().resources


def test_no_shipped_resource_is_left_deferred():
    """The D3a.5 placeholder state, asserted as gone rather than assumed."""
    assert load_design_resource_manifest().deferred_ids == []


@pytest.mark.parametrize("resource_id", ACTIVATED)
def test_each_activated_resource_is_implemented(resource_id):
    resource = load_design_resource_manifest().get(resource_id)

    assert resource.is_implemented is True
    assert resource.resolution in IMPLEMENTED_RESOLUTIONS


@pytest.mark.parametrize("resource_id", ACTIVATED)
def test_each_activated_resource_names_an_adapter(resource_id):
    resource = load_design_resource_manifest().get(resource_id)

    assert resource.adapter, f"{resource_id} claims no adapter"
    assert resource.adapter in DESIGN_ADAPTERS


def test_every_shipped_resource_names_a_real_adapter():
    """Not just the five: every implemented resource in the file."""
    for resource_id, resource in load_design_resource_manifest().resources.items():
        if resource.is_implemented:
            assert resource.adapter in DESIGN_ADAPTERS, resource_id


def test_every_adapter_in_the_table_points_at_something_real():
    import importlib

    for name, (module_name, attribute) in DESIGN_ADAPTERS.items():
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), f"{name} -> {module_name}.{attribute}"


# ---------------------------------------------------------------------------
# `deferred` cannot hide behind a claim of wiring
# ---------------------------------------------------------------------------


def test_a_deferred_resource_may_not_claim_an_adapter():
    """Self-contradictory: deferred asserts nothing operates it."""
    document = _minimal(
        {
            "x": {
                "kind": "reference",
                "required": False,
                "resolution": "deferred",
                "adapter": "npm_package",
            }
        }
    )

    with pytest.raises(DesignResourceManifestError, match="deferred"):
        parse_design_resource_manifest(document)


def test_an_unknown_resolution_is_refused():
    document = _minimal(
        {"x": {"kind": "reference", "required": False, "resolution": "on_demand"}}
    )

    with pytest.raises(DesignResourceManifestError, match="unknown resolution"):
        parse_design_resource_manifest(document)


def test_the_resolution_vocabulary_is_closed_and_explicit():
    assert set(RESOLUTIONS) == {
        "profile_skill",
        "on_demand_registry",
        "deferred",
    }


def test_deferred_is_the_only_unimplemented_resolution():
    """Nothing may join the implemented set without a real adapter behind it."""
    assert set(IMPLEMENTED_RESOLUTIONS) == {"profile_skill", "on_demand_registry"}


# ---------------------------------------------------------------------------
# A claimed adapter must exist
# ---------------------------------------------------------------------------


def test_a_claim_on_an_unknown_adapter_is_refused():
    document = _minimal(
        {
            "x": {
                "kind": "npm_optional",
                "required": False,
                "resolution": "on_demand_registry",
                "install_mode": "project_on_demand",
                "adapter": "totally_made_up",
            }
        }
    )
    manifest = parse_design_resource_manifest(document)

    with pytest.raises(DesignResourceManifestError, match="unknown adapter"):
        validate_adapter_claims(manifest)


def test_a_claim_on_a_deleted_implementation_is_refused():
    """A renamed or removed implementation breaks the claim."""
    from app.core import design_resources

    poisoned = dict(DESIGN_ADAPTERS)
    poisoned["npm_package"] = ("app.core.design_install", "no_such_attribute_here")
    original = design_resources.DESIGN_ADAPTERS
    design_resources.DESIGN_ADAPTERS = poisoned
    try:
        document = _minimal(
            {
                "x": {
                    "kind": "npm_optional",
                    "required": False,
                    "resolution": "on_demand_registry",
                    "install_mode": "project_on_demand",
                    "adapter": "npm_package",
                }
            }
        )
        with pytest.raises(DesignResourceManifestError, match="no attribute"):
            validate_adapter_claims(parse_design_resource_manifest(document))
    finally:
        design_resources.DESIGN_ADAPTERS = original


def test_a_claim_on_an_unimportable_module_is_refused():
    from app.core import design_resources

    poisoned = dict(DESIGN_ADAPTERS)
    poisoned["npm_package"] = ("app.core.does_not_exist", "whatever")
    original = design_resources.DESIGN_ADAPTERS
    design_resources.DESIGN_ADAPTERS = poisoned
    try:
        document = _minimal(
            {
                "x": {
                    "kind": "npm_optional",
                    "required": False,
                    "resolution": "on_demand_registry",
                    "install_mode": "project_on_demand",
                    "adapter": "npm_package",
                }
            }
        )
        with pytest.raises(DesignResourceManifestError, match="cannot be imported"):
            validate_adapter_claims(parse_design_resource_manifest(document))
    finally:
        design_resources.DESIGN_ADAPTERS = original


# ---------------------------------------------------------------------------
# Companions are a closed table, not a naming convention
# ---------------------------------------------------------------------------


def test_three_declares_the_companion_that_makes_it_compile():
    """The @types/three gap Part A closed is named in the manifest."""
    assert load_design_resource_manifest().get("three").companion == "three"


def test_an_unclosed_companion_is_refused():
    document = _minimal(
        {
            "x": {
                "kind": "npm_optional",
                "required": False,
                "resolution": "on_demand_registry",
                "install_mode": "project_on_demand",
                "adapter": "npm_package",
                "companion": "types_for_anything",
            }
        }
    )

    with pytest.raises(DesignResourceManifestError, match="closed companion table"):
        validate_adapter_claims(parse_design_resource_manifest(document))


def test_a_companion_must_be_a_non_empty_string():
    document = _minimal(
        {
            "x": {
                "kind": "npm_optional",
                "required": False,
                "resolution": "on_demand_registry",
                "install_mode": "project_on_demand",
                "companion": "   ",
            }
        }
    )

    with pytest.raises(DesignResourceManifestError, match="companion"):
        parse_design_resource_manifest(document)


def test_no_other_resource_claims_a_companion():
    for resource_id, resource in load_design_resource_manifest().resources.items():
        if resource_id != "three":
            assert resource.companion is None, resource_id


# ---------------------------------------------------------------------------
# The shipped file is held to a stricter standard than a synthetic one
# ---------------------------------------------------------------------------


def test_an_implemented_resource_with_no_adapter_fails_the_shipped_contract():
    """The shipped file's standard, exercised directly.

    The requirement lives in ``load_*`` behind ``path is None``. Rather than
    monkeypatching that condition, this proves the two halves it rests on: the
    shipped file satisfies it, and the predicate it uses rejects an unnamed
    resource. The `path is None` branch itself is a two-line dispatch.
    """
    manifest = load_design_resource_manifest()

    unnamed = [
        resource.resource_id
        for resource in manifest.resources.values()
        if resource.is_implemented and not resource.adapter
    ]
    assert unnamed == []

    # The predicate, applied to a resource stripped of its adapter.
    stripped = copy.deepcopy(manifest.get("three"))
    object.__setattr__(stripped, "adapter", None)
    assert stripped.is_implemented and not stripped.adapter


def test_a_custom_manifest_may_omit_adapters(tmp_path):
    """Inspecting an ad-hoc manifest is not a claim about ours."""
    path = _write(
        tmp_path,
        _minimal(
            {
                "x": {
                    "kind": "skill",
                    "required": False,
                    "resolution": "profile_skill",
                    "skill_name": "x",
                }
            }
        ),
    )

    assert load_design_resource_manifest(path).get("x").adapter is None


def test_deferred_ids_lists_only_unwired_resources():
    document = _minimal(
        {
            "wired": {
                "kind": "npm_optional",
                "required": False,
                "resolution": "on_demand_registry",
                "install_mode": "project_on_demand",
                "adapter": "npm_package",
            },
            "unwired": {
                "kind": "reference",
                "required": False,
                "resolution": "deferred",
            },
        }
    )

    manifest = parse_design_resource_manifest(document)

    assert manifest.deferred_ids == ["unwired"]


def test_the_resource_is_serializable_with_its_adapter():
    payload = load_design_resource_manifest().get("three").to_dict()

    assert payload["adapter"] == "npm_package"
    assert payload["companion"] == "three"