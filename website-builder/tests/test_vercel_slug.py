"""Friendly Vercel project slug: derivation + stable persistence.

Covers the user-facing naming requirement (R1 addendum):
  * slugify_display_name() derives a conservative, non-truncated slug
  * ConversationRegistryStore.set_vercel_slug_once() binds it exactly once
  * a later display_name rename never re-triggers the binding
  * ownership/identity still derives only from the immutable project_id
    (WEBSITE_BUILDER_OWNER marker), never from the slug -- see
    tests/test_preview_adapters.py for the adapter-level collision tests.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.registry import ConversationRegistryStore, slugify_display_name


@pytest.mark.parametrize("display_name,expected", [
    ("Dapur Kedaton", "dapur-kedaton"),
    ("The Daily Bake", "the-daily-bake"),
    ("Kopi & Roti", "kopi-roti"),
])
def test_slugify_matches_required_examples(display_name, expected):
    assert slugify_display_name(display_name) == expected


def test_slugify_never_truncates_or_invents():
    # No word-count limit: every meaningful word survives.
    assert slugify_display_name("Warung Makan Bahagia Sejahtera") == \
        "warung-makan-bahagia-sejahtera"
    # No business-name invention when input is already empty/unusable.
    assert slugify_display_name("   ") is None
    assert slugify_display_name(None) is None


def test_slug_bound_once_and_never_overwritten(tmp_path):
    store = ConversationRegistryStore(tmp_path / "conversations")
    entry = store.allocate_project("555", "Dapur Kedaton")

    first = store.set_vercel_slug_once("555", entry.project_id, "dapur-kedaton")
    assert first == "dapur-kedaton"

    # A second bind attempt (e.g. re-resolved after a display_name rename)
    # must return the ORIGINAL slug unchanged -- never re-bind/rename.
    second = store.set_vercel_slug_once("555", entry.project_id, "dapur-kedaton-renamed")
    assert second == "dapur-kedaton"

    reloaded = store.load("555")
    assert reloaded.find_by_id(entry.project_id).vercel_slug == "dapur-kedaton"


def test_display_name_rename_does_not_touch_bound_slug(tmp_path):
    store = ConversationRegistryStore(tmp_path / "conversations")
    entry = store.allocate_project("555", "Dapur Kedaton")
    store.set_vercel_slug_once("555", entry.project_id, "dapur-kedaton")

    # Renaming via a new alias / display name change should never mutate
    # the already-bound Vercel slug (R1: no migration complexity).
    store.add_alias("555", entry.project_id, "Kedaton Baru")

    reloaded = store.load("555")
    bound = reloaded.find_by_id(entry.project_id)
    assert bound.vercel_slug == "dapur-kedaton"
    assert bound.display_name == "Dapur Kedaton"


def test_two_projects_same_human_slug_both_get_independent_bindings(tmp_path):
    """Registry-level: two DIFFERENT projects may each bind their own slug
    candidate independently -- the registry itself does not enforce slug
    uniqueness (that collision detection lives in the Vercel adapter, see
    test_preview_adapters.py::test_slug_collision_with_foreign_project_never_adopted).
    This test only proves the persistence layer keeps bindings independent
    and never silently merges/derives an app-added suffix.
    """
    store = ConversationRegistryStore(tmp_path / "conversations")
    entry_a = store.allocate_project("555", "Dapur Kedaton")
    entry_b = store.allocate_project("666", "Dapur Kedaton")

    store.set_vercel_slug_once("555", entry_a.project_id, "dapur-kedaton")
    store.set_vercel_slug_once("666", entry_b.project_id, "dapur-kedaton")

    a = store.load("555").find_by_id(entry_a.project_id)
    b = store.load("666").find_by_id(entry_b.project_id)
    assert a.vercel_slug == "dapur-kedaton"
    assert b.vercel_slug == "dapur-kedaton"
    # No app-added suffix was invented for either binding.
    assert "-" not in a.vercel_slug.replace("dapur-kedaton", "")
