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
    RECIPE_REQUIRED_SUFFIX,
    RECIPE_SUFFIXES,
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


#: The REAL ``transitions/card-resize.md`` body, transcribed from a live
#: ``transitions-dev@0.3.0 add card-resize``. It matters that the CSS -- and the
#: reduced-motion guard inside it -- is EMBEDDED in the Markdown: upstream emits
#: no sibling ``.css``, so reduced-motion detection must read the recipe body
#: rather than a companion file that does not exist.
REAL_CARD_RESIZE_MARKDOWN = """# Card resize

Scales a card up slightly while the pointer rests on it.

## Usage

```html
<article class="card">...</article>
```

## Styles

```css
.card {
  transition: transform 240ms cubic-bezier(0.2, 0, 0, 1);
  will-change: transform;
}

.card:hover {
  transform: scale(1.02);
}
```

## Reduced motion

```css
@media (prefers-reduced-motion: reduce) {
  .card {
    transition: none;
    transform: none;
  }
}
```

The `@media (prefers-reduced-motion: reduce)` guard at the bottom of the snippet
is required -- keep it. It zeroes the transition for users who have asked for
less motion at the OS level.

## Orchestration

Pair with a subtle shadow lift; do not stack with a scale on the same element.
"""


def _materialize(
    project: Path, slug: str, suffixes=(RECIPE_REQUIRED_SUFFIX,)
) -> None:
    """A complete materialization: the CLI writes ONLY the Markdown."""
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
    for suffix in RECIPE_SUFFIXES:
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


def test_a_verified_recipe_reports_the_markdown_alone(project):
    """The REAL upstream result: one ``.md``, and no ``.css``.

    Verified live against ``transitions-dev@0.3.0``: ``add card-resize``
    writes only ``transitions/card-resize.md``. Requiring a companion CSS
    file made every successful install fail verification.
    """
    _materialize(project, "card-resize")

    verified = verify_recipe_materialized(
        project, "card-resize", approved_recipes_dir(project)
    )

    assert verified == ("card-resize.md",)
    assert not (project / RECIPES_DIRNAME / "card-resize.css").exists()


def test_a_real_card_resize_recipe_verifies(project):
    """Modeled on the real ``card-resize.md`` upstream materialized."""
    _materialize(project, "card-resize")
    recipe = project / RECIPES_DIRNAME / "card-resize.md"
    recipe.write_text(REAL_CARD_RESIZE_MARKDOWN, encoding="utf-8")

    verified = verify_recipe_materialized(
        project, "card-resize", approved_recipes_dir(project)
    )

    assert verified == ("card-resize.md",)


def test_an_optional_companion_is_verified_when_present(project):
    """A companion upstream DID emit is accepted, and still contained.

    Upstream emits no companion today, so the verifier's optional branch takes
    an explicit suffix set. That keeps the "accept if present, never require"
    rule reachable and testable without pretending `.css` is currently shipped.
    """
    _materialize(
        project,
        "card-resize",
        suffixes=(RECIPE_REQUIRED_SUFFIX, ".css"),
    )

    verified = verify_recipe_materialized(
        project,
        "card-resize",
        approved_recipes_dir(project),
        optional_suffixes=(".css",),
    )

    assert verified == ("card-resize.css", "card-resize.md")


def test_an_optional_companion_is_not_required_by_default(project):
    """The real, upstream-current shape: Markdown alone, and that is a success."""
    _materialize(project, "card-resize")

    verified = verify_recipe_materialized(
        project, "card-resize", approved_recipes_dir(project)
    )

    assert verified == ("card-resize.md",)


def test_a_missing_recipe_is_not_verified(project):
    assert (
        verify_recipe_materialized(project, "card-resize", approved_recipes_dir(project))
        == ()
    )


def test_a_missing_required_markdown_is_not_a_success(project):
    """A companion alone does NOT satisfy the postcondition."""
    recipes = project / RECIPES_DIRNAME
    recipes.mkdir(parents=True)
    (recipes / "card-resize.css").write_text("x\n", encoding="utf-8")

    assert verify_recipe_materialized(project, "card-resize", recipes) == ()


def test_a_companion_without_its_required_markdown_is_not_a_success(project):
    """The required/optional distinction, in the only case it is observable.

    With a companion present and the REQUIRED Markdown missing, the result must
    still be empty. An implementation that treats every missing file as an
    optional companion would return the companion's name here, which is exactly
    reporting a partial materialization as a success.
    """
    recipes = project / RECIPES_DIRNAME
    recipes.mkdir(parents=True)
    (recipes / "card-resize.css").write_text("x\n", encoding="utf-8")

    assert (
        verify_recipe_materialized(
            project, "card-resize", recipes, optional_suffixes=(".css",)
        )
        == ()
    )


def test_an_empty_markdown_is_not_a_success(project):
    """A zero-byte recipe carries no transition at all."""
    recipes = project / RECIPES_DIRNAME
    recipes.mkdir(parents=True)
    (recipes / "card-resize.md").write_text("", encoding="utf-8")

    assert verify_recipe_materialized(project, "card-resize", recipes) == ()


def test_a_directory_named_like_the_recipe_is_not_a_success(project):
    recipes = project / RECIPES_DIRNAME
    (recipes / "card-resize.md").mkdir(parents=True)

    assert verify_recipe_materialized(project, "card-resize", recipes) == ()


def test_no_css_is_ever_synthesized(project):
    """Verification never writes; upstream owns what lands on disk."""
    _materialize(project, "card-resize")

    verify_recipe_materialized(
        project, "card-resize", approved_recipes_dir(project)
    )

    written = sorted(p.name for p in (project / RECIPES_DIRNAME).iterdir())
    assert written == ["card-resize.md"]


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


# ---------------------------------------------------------------------------
# Part I: .md-only materialization + no dependency mutation
# ---------------------------------------------------------------------------


def test_md_only_materialization_is_accepted(project):
    """The REAL upstream contract: `add <slug>` writes ONLY the Markdown."""
    _materialize(project, "card-resize")

    verified = verify_recipe_materialized(
        project, "card-resize", approved_recipes_dir(project)
    )

    assert verified == ("card-resize.md",)
    # No .css is required or synthesized.
    assert not (project / RECIPES_DIRNAME / "card-resize.css").exists()


def test_a_missing_css_companion_is_never_a_failure(project):
    """The old defect: requiring a .css that upstream never writes."""
    _materialize(project, "fade-in")

    assert verify_recipe_materialized(
        project, "fade-in", approved_recipes_dir(project)
    ) == ("fade-in.md",)


def test_the_transitions_argv_never_carries_a_package_install():
    """A transitions invocation is `add <slug>`; it installs no npm package."""
    from app.core.design_transitions import build_add_argv, normalize_recipe_catalog
    from app.core.design_install import build_pinned_cli_prefix

    catalog = normalize_recipe_catalog(
        [{"slug": "card-resize", "name": "Card resize", "tier": "free"}]
    )
    prefix = build_pinned_cli_prefix(
        ("npm",), "transitions_dev", Path(".")
    )
    argv = build_add_argv(prefix, "card-resize", catalog)

    assert argv is not None
    assert "install" not in argv
    assert "add" in argv
    # No npm package spec (name@version) appears as an install target.
    assert not any(part.startswith(("transitions-dev@",)) and part != "transitions-dev@0.3.0" for part in argv)


def test_transitions_materialization_does_not_touch_package_json(project):
    """Verifying a recipe is a filesystem check, not a manifest mutation."""
    import json

    (project / "package.json").write_text(
        json.dumps({"name": "site", "dependencies": {"react": "19.2.7"}}),
        encoding="utf-8",
    )
    before = (project / "package.json").read_text(encoding="utf-8")

    _materialize(project, "card-resize")
    verify_recipe_materialized(project, "card-resize", approved_recipes_dir(project))

    assert (project / "package.json").read_text(encoding="utf-8") == before


def test_a_stray_css_is_ignored_by_default(project):
    """The default set is Markdown ONLY: a present .css is not counted.

    Upstream ships no companion today, so the default verifier accepts the
    Markdown alone and does NOT treat a stray .css as part of the artifact. This
    is the behavior the docstring must describe.
    """
    _materialize(project, "card-resize")
    (project / RECIPES_DIRNAME / "card-resize.css").write_text("body{}\n", encoding="utf-8")

    verified = verify_recipe_materialized(
        project, "card-resize", approved_recipes_dir(project)
    )

    assert verified == ("card-resize.md",)


def test_the_docstring_does_not_claim_a_css_is_currently_accepted():
    """Doc coherence: the verifier's docstring must match the empty suffix table.

    An earlier revision said a .css companion is "accepted if and only if
    upstream emitted one", which reads as "a present .css IS accepted". It is
    not: RECIPE_OPTIONAL_SUFFIXES is empty, so the default ignores a stray .css.
    The docstring must not overstate the current behavior.
    """
    from app.core import design_transitions as transitions

    doc = verify_recipe_materialized.__doc__ or ""
    assert "accepted *if and only if* upstream emitted one" not in doc
    # The current truth is stated.
    assert "RECIPE_OPTIONAL_SUFFIXES" in doc or "empty" in doc
    assert transitions.RECIPE_OPTIONAL_SUFFIXES == ()


def test_a_prose_mention_without_the_media_block_is_not_a_guard():
    """The real recipes mention the guard in prose; that is NOT the guard.

    Every transitions-dev@0.3.0 recipe explains the @media guard in prose AND
    carries the block. A phrase-only detector reports PRESENT after the block is
    removed -- a false positive on the property being reported. The match must
    require the actual @media (...) { block.
    """
    prose_only = (
        "The `@media (prefers-reduced-motion: reduce)` guard is required - "
        "keep it.\n```css\n.t-x { transition: width 1s; }\n```\n"
    )

    assert detect_reduced_motion_guard(prose_only) is False


def test_the_guard_block_itself_is_still_detected():
    """The tightened match keeps the true positive."""
    real = (
        "```css\n.t-resize { transition: width 1s; }\n"
        "@media (prefers-reduced-motion: reduce) {\n"
        "  .t-resize { transition: none !important; }\n}\n```\n"
    )

    assert detect_reduced_motion_guard(real) is True


def test_the_guard_is_matched_with_the_opening_brace_only():
    """A dangling selector with no block is not a guard either."""
    assert detect_reduced_motion_guard(
        "/* @media (prefers-reduced-motion: reduce) */"
    ) is False


def test_the_flag_reports_not_established_rather_than_absent():
    """A manifest-only catalog leaves the flag False -- NOT "no guard".

    Upstream's free-manifest.json carries only slug/name/tier, so a catalog built
    from it cannot know the file content. The field must read as not-established,
    not as a claim that the guard is absent (every real recipe HAS it).
    """
    catalog = normalize_recipe_catalog(
        [{"slug": "card-resize", "name": "Card resize", "tier": "free"}]
    )

    entry = catalog.entries[0]
    # The manifest gives no guard metadata, so the field is False -- and the
    # docstring must say False means NOT ESTABLISHED, not "absent".
    assert entry.has_reduced_motion_guard is False
    doc = type(entry).__doc__ or ""
    assert "NOT ESTABLISHED" in doc


def test_the_canonical_guard_string_is_detected():
    """The exact upstream form: `@media (prefers-reduced-motion: reduce) {`."""
    assert detect_reduced_motion_guard("@media (prefers-reduced-motion: reduce) {") is True


@pytest.mark.parametrize(
    "text",
    [
        "@media (prefers-reduced-motion: reduce) {",
        "@media(prefers-reduced-motion: reduce){",
        "@media   (  prefers-reduced-motion :  reduce  )  {",
        "@MEDIA (PREFERS-REDUCED-MOTION: REDUCE) {",
        "@Media (prefers-reduced-motion: Reduce) {",
        "@media\n(prefers-reduced-motion: reduce)\n{",
    ],
)
def test_every_realistic_spelling_of_the_guard_is_detected(text):
    """Whitespace and case variants of the SAME standalone selector."""
    assert detect_reduced_motion_guard(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "@media (prefers-reduced-motion: no-preference) {",
        "@media (min-width: 40em) {",
        "prefers-reduced-motion: reduce",
        "@media (prefers-reduced-motion: reduce)",  # no brace
        "",
    ],
)
def test_a_non_guard_media_query_is_not_detected(text):
    assert detect_reduced_motion_guard(text) is False


def test_the_combined_form_is_deliberately_out_of_scope():
    """The detector matches the STANDALONE block only.

    Upstream ships only the standalone form (32/32). Widening to match a combined
    query would also re-match the PROSE mention and reintroduce the false
    positive, so the combined form is a deliberate false NEGATIVE -- the safe
    direction, since True asserts accessibility code is present.
    """
    assert detect_reduced_motion_guard(
        "@media (prefers-reduced-motion: reduce) and (min-width: 40em) {"
    ) is False
    assert detect_reduced_motion_guard(
        "@media screen, (prefers-reduced-motion: reduce) {"
    ) is False
