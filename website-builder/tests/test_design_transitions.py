"""Batch D3a.5 Part H: Transitions.dev recipes.

Pure normalization and argv tests over supplied payloads. **No network, no
subprocess, no install.**

The properties under test are BEHAVIOUR CONTRACTS:

    * a slug must originate from a real, normalized catalog entry -- arbitrary
      text has no path to argv
    * only the FREE tier is reachable; Pro requires a browser sign-in and is
      never surfaced as installable
    * ``add --free`` / ``add --pro`` (open-ended, or authenticated) are not
      constructible
    * materialization is verified under a CONTAINED project directory
    * reduced-motion metadata is reported, never synthesized
    * malformed/empty payloads invent nothing
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_install import PINNED_CLIS, build_pinned_cli_prefix, is_contained
from app.core.design_transitions import (
    RECIPES_DIRNAME,
    TRANSITIONS_CLI_ID,
    WARNING_RECIPE_CATALOG_EMPTY,
    WARNING_RECIPE_CATALOG_MALFORMED,
    approved_recipes_dir,
    build_add_argv,
    build_list_argv,
    detect_reduced_motion_guard,
    normalize_recipe,
    normalize_recipe_catalog,
    recipe_slug_is_well_formed,
    resolve_recipe_slug,
    verify_recipe_materialized,
)

#: The real upstream manifest shape, verified against cli/free-manifest.json.
FREE_MANIFEST = [
    {"slug": "card-resize", "name": "Card resize", "tier": "free"},
    {"slug": "modal", "name": "Modal open / close", "tier": "free"},
    {"slug": "tooltip", "name": "Tooltip open/close", "tier": "free"},
    {"slug": "shimmer-text", "name": "Shimmer text", "tier": "free"},
]

PREFIX = ("npm", "exec", "--yes", "--package=transitions-dev@0.3.0", "--", "transitions-dev")


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("this layer decides intent; it never acts on it")

    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.fixture
def catalog():
    return normalize_recipe_catalog(FREE_MANIFEST)


# ---------------------------------------------------------------------------
# The CLI is pinned and comes from the closed table
# ---------------------------------------------------------------------------


def test_the_transitions_cli_is_pinned_exactly():
    pinned = PINNED_CLIS[TRANSITIONS_CLI_ID]

    assert pinned.version == "0.3.0"
    assert pinned.spec == "transitions-dev@0.3.0"
    assert pinned.binary == "transitions-dev"
    assert pinned.is_exact()


def test_the_prefix_comes_from_the_generic_pinned_primitive(tmp_path):
    assert build_pinned_cli_prefix(("npm",), TRANSITIONS_CLI_ID, tmp_path) == PREFIX


# ---------------------------------------------------------------------------
# Slugs originate from a real catalog
# ---------------------------------------------------------------------------


def test_a_real_manifest_normalizes(catalog):
    assert catalog.ok is True
    assert catalog.slugs() == ("card-resize", "modal", "shimmer-text", "tooltip")


def test_a_slug_in_the_catalog_resolves(catalog):
    assert resolve_recipe_slug("card-resize", catalog) == "card-resize"


@pytest.mark.parametrize(
    "candidate",
    [
        "not-a-real-recipe",
        "../../etc/passwd",
        "--pro",
        "; rm -rf /",
        "`id`",
        "card-resize; extra",
        "",
    ],
)
def test_an_unknown_or_dangerous_slug_resolves_to_nothing(candidate, catalog):
    """Arbitrary text must have no path to argv."""
    assert resolve_recipe_slug(candidate, catalog) is None
    assert build_add_argv(PREFIX, candidate, catalog) is None


def test_a_slug_is_matched_exactly_not_by_prefix(catalog):
    """`card` must not materialize `card-resize`."""
    assert resolve_recipe_slug("card", catalog) is None
    assert catalog.get("card") is None


def test_slug_shaping_rejects_urls_and_paths():
    for candidate in (
        "https://evil.example/r/x",
        "x/y",
        "x\\y",
        "X-UPPER",
        "x" * 100,
        7,
        None,
    ):
        assert recipe_slug_is_well_formed(candidate) is False, candidate


# ---------------------------------------------------------------------------
# The open-ended and authenticated paths are unreachable
# ---------------------------------------------------------------------------


def test_the_free_tier_needs_no_account():
    catalog = normalize_recipe_catalog(FREE_MANIFEST)

    assert all(e.tier == "free" for e in catalog.entries)


def test_a_pro_entry_is_skipped_not_surfaced():
    """Pro requires a browser sign-in; it is not an installable recipe."""
    catalog = normalize_recipe_catalog(
        [*FREE_MANIFEST, {"slug": "premium-wipe", "name": "Premium", "tier": "pro"}]
    )

    assert "premium-wipe" not in catalog.slugs()
    assert resolve_recipe_slug("premium-wipe", catalog) is None


def test_no_argv_can_request_every_recipe_at_once(catalog):
    """`add --free` would turn a bounded selection into an open-ended action."""
    for slug in ("--free", "--pro", "all", "free"):
        assert build_add_argv(PREFIX, slug, catalog) is None, slug


def test_a_valid_add_argv_names_exactly_one_recipe(catalog):
    argv = build_add_argv(PREFIX, "card-resize", catalog)

    assert argv == PREFIX + ("add", "card-resize")
    assert argv[argv.index("add") + 1] == "card-resize"


def test_the_list_argv_is_the_pinned_prefix_plus_list():
    assert build_list_argv(PREFIX) == PREFIX + ("list",)


# ---------------------------------------------------------------------------
# Nothing is invented
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [None, 42, {"nope": []}, "not json", 3.5])
def test_a_malformed_manifest_yields_nothing(payload):
    catalog = normalize_recipe_catalog(payload)

    assert catalog.entries == ()
    assert catalog.warnings == (WARNING_RECIPE_CATALOG_MALFORMED,)


def test_an_empty_manifest_yields_nothing():
    catalog = normalize_recipe_catalog([])

    assert catalog.entries == ()
    assert catalog.warnings == (WARNING_RECIPE_CATALOG_EMPTY,)


def test_entries_without_a_real_slug_are_dropped():
    catalog = normalize_recipe_catalog(
        [
            {"name": "no slug"},
            {"slug": "NOT-A-SLUG", "tier": "free"},
            {"slug": "real-one", "tier": "free"},
        ]
    )

    assert catalog.slugs() == ("real-one",)


def test_a_json_string_manifest_is_parsed():
    import json

    catalog = normalize_recipe_catalog(json.dumps(FREE_MANIFEST))

    assert catalog.ok is True


def test_duplicate_slugs_collapse():
    catalog = normalize_recipe_catalog([*FREE_MANIFEST, FREE_MANIFEST[0]])

    assert len(catalog.entries) == 4


def test_the_entry_limit_is_enforced_and_reported():
    payload = [{"slug": f"recipe-{i}", "tier": "free"} for i in range(200)]

    catalog = normalize_recipe_catalog(payload, limit=5)

    assert len(catalog.entries) == 5
    assert catalog.truncated is True


def test_normalization_is_deterministic():
    first = normalize_recipe_catalog(FREE_MANIFEST)
    second = normalize_recipe_catalog(FREE_MANIFEST)

    assert first.slugs() == second.slugs()


# ---------------------------------------------------------------------------
# Deterministic materialization
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    return root


def _materialize(project: Path, slug: str, suffixes=(".md", ".css")) -> None:
    """A complete materialization: the CLI writes BOTH files per recipe."""
    target = project / RECIPES_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    for suffix in suffixes:
        (target / f"{slug}{suffix}").write_text("recipe\n", encoding="utf-8")


def test_the_recipes_dir_is_inside_the_project(project):
    resolved = approved_recipes_dir(project)

    assert resolved is not None
    assert resolved.name == RECIPES_DIRNAME
    assert is_contained(project, resolved)


def test_a_destination_outside_the_project_verifies_nothing(project, tmp_path):
    """Containment, not mere existence, is the postcondition.

    The planted files are REAL and correctly named for the slug -- so only the
    containment check can refuse them. Pointing at a missing or differently
    named file would let the ``is_file`` / slug-derivation guards pass and would
    prove nothing about containment. This is the symlink-escape case, proven
    without needing a link.
    """
    outside = tmp_path / "outside" / RECIPES_DIRNAME
    outside.mkdir(parents=True)
    for suffix in (".md", ".css"):
        (outside / f"card-resize{suffix}").write_text("x\n", encoding="utf-8")

    assert verify_recipe_materialized(project, "card-resize", outside) == ()


def _symlinks_available() -> bool:
    """Whether this host can create a directory symlink at all.

    Windows needs Developer Mode or SeCreateSymbolicLinkPrivilege, so the
    escape case cannot be exercised there. Rather than silently reporting green
    over zero coverage, the symlink test skips and the CONTAINMENT property is
    proven portably by ``test_a_destination_outside_the_project_verifies_nothing``
    and by the ``is_contained`` assertion on ``approved_recipes_dir``.
    """
    probe = Path(tempfile.mkdtemp()) / "probe_target"
    probe.mkdir()
    try:
        (probe.parent / "probe_link").symlink_to(probe, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        return False
    return True


_SYMLINKS_AVAILABLE = _symlinks_available()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="a symlink escape is exercised on the POSIX lane; the containment "
           "property itself is proven portably above",
)
def test_a_symlinked_recipes_dir_escaping_the_project_is_refused(project, tmp_path):
    if not _symlinks_available(tmp_path):
        pytest.skip("symlink creation unavailable on this host")

    outside = tmp_path / "outside"
    outside.mkdir()
    (project / RECIPES_DIRNAME).symlink_to(outside, target_is_directory=True)

    assert approved_recipes_dir(project) is None


def test_a_verified_recipe_reports_both_files(project):
    _materialize(project, "card-resize")

    verified = verify_recipe_materialized(
        project, "card-resize", approved_recipes_dir(project)
    )

    assert verified == ("card-resize.css", "card-resize.md")


def test_a_missing_recipe_is_not_verified(project):
    assert (
        verify_recipe_materialized(project, "card-resize", approved_recipes_dir(project))
        == ()
    )


def test_a_partial_materialization_is_not_a_success(project):
    """All-or-nothing: the caller builds immediately afterwards."""
    recipes = project / RECIPES_DIRNAME
    recipes.mkdir(parents=True)
    (recipes / "card-resize.md").write_text("x\n", encoding="utf-8")

    assert verify_recipe_materialized(project, "card-resize", recipes) == ()


def test_an_unrelated_file_cannot_satisfy_the_postcondition(project):
    """The file must derive from the slug, or a stale file would pass."""
    _materialize(project, "other-recipe")

    assert (
        verify_recipe_materialized(
            project, "card-resize", approved_recipes_dir(project)
        )
        == ()
    )


@pytest.mark.skipif(not _SYMLINKS_AVAILABLE, reason="symlink creation needs privilege here")
def test_a_symlinked_recipes_dir_escaping_the_project_is_refused(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / RECIPES_DIRNAME).symlink_to(outside, target_is_directory=True)

    assert approved_recipes_dir(project) is None


# ---------------------------------------------------------------------------
# Reduced-motion metadata is reported, never invented
# ---------------------------------------------------------------------------


def test_no_recipes_dir_means_nothing_is_verified(project):
    assert (
        verify_recipe_materialized(project, "card-resize", approved_recipes_dir(project))
        == ()
    )


def test_a_none_directory_verifies_nothing(project):
    """The fail-closed answer when the destination is unknown."""
    assert verify_recipe_materialized(project, "card-resize", None) == ()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege on Windows")
def test_a_symlinked_recipes_dir_escaping_the_project_is_refused(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project / RECIPES_DIRNAME).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    assert approved_recipes_dir(project) is None


# ---------------------------------------------------------------------------
# Reduced-motion metadata is reported, never invented
# ---------------------------------------------------------------------------


def test_the_reduced_motion_guard_is_detected_when_present():
    """Real upstream text, verified from cli/free/card-resize.md."""
    text = (
        "@media (prefers-reduced-motion: reduce) {\n"
        "  .t-resize { transition: none !important; }\n"
        "}"
    )

    assert detect_reduced_motion_guard(text) is True


def test_a_recipe_without_the_guard_reports_false():
    assert detect_reduced_motion_guard(".t-resize { transition: width 1s; }") is False


def test_the_guard_is_never_synthesized_into_a_recipe():
    """Reporting only: normalization must not add motion code upstream lacks."""
    catalog = normalize_recipe_catalog(
        [{"slug": "no-guard", "name": "No guard", "tier": "free"}]
    )

    entry = catalog.entries[0]
    assert entry.has_reduced_motion_guard is False
    assert "prefers-reduced-motion" not in entry.to_dict()["name"]


def test_detect_handles_non_string_input():
    assert detect_reduced_motion_guard(None) is False
    assert detect_reduced_motion_guard(7) is False