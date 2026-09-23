"""PHASE C — canonical registry / brief identity convergence.

A first-contact/bootstrap fallback path may allocate a project under a
PROVISIONAL or derived display name, while later intake discovers the true
human name (``brief['name']``). Because the Vercel slug derives from
``registry.display_name``, a stale/provisional registry name would leak into
the remote Vercel project identity.

These tests pin the convergence contract on the SAME internal project_id:

  A. provisional fallback identity -> explicit confirmed name -> same
     project_id, registry.display_name updated, brief.name matches, slug
     candidate matches the confirmed name.
  B. confirmed name collides with another existing project -> no overwrite,
     no duplicate -> clarification/failure.
  C. vercel_slug already bound -> no implicit rename that would fork remote
     identity.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.registry import (  # noqa: E402
    ConversationRegistryStore,
    slugify_display_name,
)


def _store(tmp_path) -> ConversationRegistryStore:
    return ConversationRegistryStore(Path(tmp_path) / "conversations")


CONV = "555"


def test_a_provisional_then_confirmed_converges_same_project_id(tmp_path):
    store = _store(tmp_path)
    # Provisional/derived name allocated by the first-contact fallback.
    entry = store.allocate_project(CONV, "membaca-soft-tone")
    pid = entry.project_id
    assert entry.display_name == "membaca-soft-tone"

    resolution = store.converge_display_name(CONV, pid, "cozyreadingspace")

    assert resolution.status == "ok"
    assert resolution.entry is not None
    # SAME immutable internal id, never a second project.
    assert resolution.entry.project_id == pid
    assert resolution.entry.display_name == "cozyreadingspace"
    # Old name retained as an alias so earlier references still resolve.
    assert "membaca-soft-tone" in resolution.entry.aliases
    # Slug candidate now derives from the confirmed name.
    assert slugify_display_name(resolution.entry.display_name) == "cozyreadingspace"
    # Exactly one project; identity unchanged.
    reg = store.load_or_create(CONV)
    assert [p.project_id for p in reg.projects] == [pid]


def test_a_convergence_is_noop_when_already_canonical(tmp_path):
    store = _store(tmp_path)
    entry = store.allocate_project(CONV, "cozyreadingspace")
    resolution = store.converge_display_name(CONV, entry.project_id, "cozyreadingspace")
    assert resolution.status == "noop"
    assert resolution.entry.project_id == entry.project_id
    assert resolution.entry.display_name == "cozyreadingspace"


def test_b_collision_with_other_project_fails_closed_no_overwrite(tmp_path):
    store = _store(tmp_path)
    other = store.allocate_project(CONV, "kitsunereading")
    provisional = store.allocate_project(CONV, "membaca-soft-tone")

    resolution = store.converge_display_name(
        CONV, provisional.project_id, "kitsunereading"
    )

    assert resolution.status == "ambiguous"
    assert [c.project_id for c in resolution.candidates] == [other.project_id]
    reg = store.load_or_create(CONV)
    # NO overwrite, NO duplicate.
    assert len(reg.projects) == 2
    assert reg.find_by_id(provisional.project_id).display_name == "membaca-soft-tone"
    assert reg.find_by_id(other.project_id).display_name == "kitsunereading"


def test_c_bound_slug_is_never_renamed(tmp_path):
    store = _store(tmp_path)
    entry = store.allocate_project(CONV, "membaca-soft-tone")
    pid = entry.project_id
    # The remote identity is already bound.
    store.set_vercel_slug_once(CONV, pid, "membaca-soft-tone")

    resolution = store.converge_display_name(CONV, pid, "cozyreadingspace")

    assert resolution.status == "noop"
    reg = store.load_or_create(CONV)
    assert reg.find_by_id(pid).display_name == "membaca-soft-tone"
    assert reg.find_by_id(pid).vercel_slug == "membaca-soft-tone"


def test_c_unknown_project_is_none(tmp_path):
    store = _store(tmp_path)
    store.allocate_project(CONV, "alpha")
    resolution = store.converge_display_name(CONV, "tg-555-p9", "beta")
    assert resolution.status == "none"
