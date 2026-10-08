"""Transitions.dev recipes: bounded catalog and deterministic materialization
(Batch D3a.5 Part H).

Upstream is a pinned CLI (``transitions-dev@0.3.0``) with two verbs this
application uses:

    transitions-dev list             enumerate the catalog
    transitions-dev add <slug>       materialize ONE recipe

``add --pro`` and ``skill`` are deliberately NOT reachable from here. Both open a
browser device-flow sign-in and fetch from an authenticated service, and running
an open-ended "add everything" inside a build is exactly the autonomous loop this
batch refuses. The free tier needs no account, so the baseline works without
either.

**The slug is never taken from model or resource text.** It originates from a
normalized :class:`Recipe` -- i.e. from a real ``free-manifest.json`` entry -- and
:func:`resolve_recipe_slug` is the only function that can produce one. A caller
holding arbitrary text has no path to argv, exactly as for the package and CLI
allowlists.

**Reduced-motion metadata is preserved, not invented.** Upstream recipes ship a
``prefers-reduced-motion`` guard and the reader detects its presence, so a
consumer can tell a motion-safe recipe from one that is not. The reader reports
what the file contains and never adds a guard upstream did not ship -- silently
"improving" a third-party recipe would mean shipping code nobody reviewed.

**Deterministic materialization.** One slug produces one argv, writes are
verified against the project's own ``transitions/`` directory (upstream's
documented default), and containment is checked on the resolved path so a
symlinked destination cannot escape the project.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_install import is_contained

logger = logging.getLogger(__name__)


#: The pinned CLI id. Resolved through the closed ``PINNED_CLIS`` table by
#: ``build_pinned_cli_prefix``; named here only so the manifest-free tests can
#: assert the mapping is what actually gets used.
TRANSITIONS_CLI_ID = "transitions_dev"

#: Upstream writes recipes to ``./transitions/`` (its ``--help`` documents
#: ``--dir`` to override). The default is what this application relies on, so it
#: is recorded here rather than being inferred from a command's output.
RECIPES_DIRNAME = "transitions"

#: Recipe file extensions upstream ships, split by REQUIREMENT.
#:
#: Verified live against ``transitions-dev@0.3.0``: ``add card-resize`` reports
#: ``Added Card resize -> transitions\card-resize.md`` and writes ONLY the
#: Markdown. There is NO ``transitions/<slug>.css``. The recipe carries its CSS
#: inline inside fenced ```css blocks in the Markdown, alongside the HTML usage,
#: the transition rules, the orchestration notes, and the reduced-motion guard.
#:
#: The previous revision required BOTH ``.md`` and ``.css``, which meant a
#: perfectly successful install failed verification on every single recipe. That
#: is the fabrication this repair removes: the postcondition is now the real one.
RECIPE_REQUIRED_SUFFIX: str = ".md"

#: Companion files upstream MAY emit. Optional by construction: their absence is
#: not a failure, and this application never synthesizes them. Upstream has no
#: recipe that emits one, so the list is empty by default and exists so a future
#: genuinely-shipped companion can be added without reintroducing a requirement.
RECIPE_OPTIONAL_SUFFIXES: Tuple[str, ...] = ()

#: Every suffix a materialized recipe may legitimately contain, required first.
RECIPE_SUFFIXES: Tuple[str, ...] = (
    RECIPE_REQUIRED_SUFFIX,
) + RECIPE_OPTIONAL_SUFFIXES

#: Bound on catalog entries. Upstream's manifest lists a few dozen; this is a
#: ceiling against a payload that lists far more.
MAX_RECIPE_ENTRIES = 64

#: Bound on any single upstream text field.
MAX_FIELD_CHARS = 400

WARNING_RECIPE_CATALOG_EMPTY = "the recipe catalog contained no usable entries"
WARNING_RECIPE_CATALOG_MALFORMED = (
    "the recipe catalog could not be parsed; no entries were produced"
)
WARNING_RECIPE_TIER_UNKNOWN = "a catalog entry with an unrecognized tier was skipped"

#: Only the free tier is reachable. Pro requires a browser sign-in and an
#: authenticated download, neither of which belongs inside a build.
TIER_FREE = "free"
SUPPORTED_TIERS: Tuple[str, ...] = (TIER_FREE,)

#: Slugs are lowercase kebab, matching upstream's own manifest exactly.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Upstream's accessibility guard. Its presence is REPORTED, never synthesized.
_REDUCED_MOTION_RE = re.compile(
    r"prefers-reduced-motion\s*:\s*reduce", re.IGNORECASE
)


def recipe_slug_is_well_formed(slug: object) -> bool:
    """Whether ``slug`` is a bare kebab-case slug.

    Strict enough that a URL, a path, or a shell fragment fails: those contain
    ``:``/``/``/``;``/backticks and so cannot be substituted into an argv.
    """
    if not isinstance(slug, str) or not slug or len(slug) > 64:
        return False
    return bool(_SLUG_RE.match(slug))


@dataclass(frozen=True)
class Recipe:
    """One real, free-tier transition recipe.

    ``has_reduced_motion_guard`` reports what the recipe FILE contains. It is
    informational: this module neither requires it nor adds it, because doing
    either would mean shipping third-party motion code this application did not
    review.
    """

    slug: str
    name: str
    tier: str
    has_reduced_motion_guard: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "tier": self.tier,
            "has_reduced_motion_guard": self.has_reduced_motion_guard,
        }


@dataclass(frozen=True)
class RecipeCatalog:
    """A bounded, normalized set of recipes."""

    entries: Tuple[Recipe, ...]
    warnings: Tuple[str, ...] = ()
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.warnings

    def slugs(self) -> Tuple[str, ...]:
        return tuple(sorted(e.slug for e in self.entries))

    def get(self, slug: str) -> Optional[Recipe]:
        """Exact lookup only -- a prefix would match the wrong recipe."""
        if not isinstance(slug, str):
            return None
        for entry in self.entries:
            if entry.slug == slug:
                return entry
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "warnings": list(self.warnings),
            "truncated": self.truncated,
        }


def _bound_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_FIELD_CHARS]


def normalize_recipe(document: Any) -> Optional[Recipe]:
    """Normalize one ``free-manifest.json`` entry, or return ``None``.

    Returns ``None`` for an unsupported tier as well as for an invalid slug: the
    Pro tier is a paid, authenticated path this application does not reach, and
    surfacing it as an installable recipe would overstate the capability.
    """
    if not isinstance(document, Mapping):
        return None

    slug = document.get("slug")
    if not recipe_slug_is_well_formed(slug):
        return None

    tier = document.get("tier")
    if tier not in SUPPORTED_TIERS:
        return None

    return Recipe(
        slug=slug,
        name=_bound_text(document.get("name")) or slug,
        tier=tier,
    )


def normalize_recipe_catalog(
    payload: Any, *, limit: int = MAX_RECIPE_ENTRIES
) -> RecipeCatalog:
    """Normalize a ``free-manifest.json`` payload into a bounded catalog.

    No network access: the payload is supplied by the caller, already fetched
    and bounded. A malformed or empty payload yields zero entries plus a static
    warning -- never a synthesized recipe, because a plausible slug for a
    transition nobody fetched is exactly the fabrication this batch forbids.
    """
    documents: List[Mapping[str, Any]]
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return RecipeCatalog(entries=(), warnings=(WARNING_RECIPE_CATALOG_MALFORMED,))

    if isinstance(payload, Mapping):
        for key in ("transitions", "entries", "items", "results", "data"):
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                documents = [d for d in value if isinstance(d, Mapping)]
                break
        else:
            return RecipeCatalog(entries=(), warnings=(WARNING_RECIPE_CATALOG_MALFORMED,))
    elif isinstance(payload, (list, tuple)):
        documents = [d for d in payload if isinstance(d, Mapping)]
    else:
        return RecipeCatalog(entries=(), warnings=(WARNING_RECIPE_CATALOG_MALFORMED,))

    entries: List[Recipe] = []
    seen = set()
    for document in documents:
        recipe = normalize_recipe(document)
        if recipe is None or recipe.slug in seen:
            continue
        seen.add(recipe.slug)
        entries.append(recipe)
        if len(entries) >= limit:
            break

    if not entries:
        return RecipeCatalog(entries=(), warnings=(WARNING_RECIPE_CATALOG_EMPTY,))

    return RecipeCatalog(
        entries=tuple(entries),
        truncated=len(entries) < len(documents),
    )


def resolve_recipe_slug(slug: str, catalog: RecipeCatalog) -> Optional[str]:
    """The slug for ``slug``, but only if the catalog actually lists it.

    **This is the only function that can produce a value handed to the CLI.**
    Requiring membership in a real, normalized catalog is what stops arbitrary
    text -- from a model, a prompt, or a resource -- from becoming an argv
    element, and it also means a typo cannot be forwarded as if it were a recipe.
    """
    if not recipe_slug_is_well_formed(slug):
        return None
    recipe = catalog.get(slug)
    return recipe.slug if recipe is not None else None


def build_list_argv(cli_prefix: Sequence[str]) -> Tuple[str, ...]:
    """The argv for enumerating the catalog.

    No ``--json``: the free manifest is machine-readable upstream but the CLI's
    output format is not part of the verified contract, so the application reads
    the catalog through its own normalization instead of parsing CLI prose.
    """
    return tuple(cli_prefix) + ("list",)


def build_add_argv(
    cli_prefix: Sequence[str], slug: str, catalog: RecipeCatalog
) -> Optional[Tuple[str, ...]]:
    """The argv materializing ONE catalog-listed recipe, or ``None``.

    Membership in ``catalog`` is required HERE, not merely recommended upstream
    of this call. Validating shape alone would leave a gap: ``not-a-real-recipe``
    and ``all`` are both well-formed kebab slugs, so a shape-only check would
    happily forward them to the CLI and let arbitrary text -- or a request for
    every recipe at once -- reach argv. Requiring catalog membership makes the
    guard structural rather than dependent on every caller remembering to call
    :func:`resolve_recipe_slug` first.

    One slug, never ``--free`` (every free transition at once) and never
    ``--pro`` (browser sign-in plus an authenticated fetch). Both would turn a
    bounded selection into an open-ended action inside a build.
    """
    resolved = resolve_recipe_slug(slug, catalog)
    if resolved is None:
        return None
    return tuple(cli_prefix) + ("add", resolved)


def approved_recipes_dir(project_root: Path) -> Optional[Path]:
    """The recipes directory inside ``project_root``, or ``None`` if unsafe.

    Resolved and containment-checked, so a project whose ``transitions`` path is
    a symlink pointing outside the workspace yields ``None`` -- and the caller
    then runs NO command, because there would be nothing to verify against
    afterwards.
    """
    candidate = Path(project_root) / RECIPES_DIRNAME
    try:
        if not is_contained(project_root, candidate):
            logger.warning(
                "Refused a transitions directory that resolved outside the project."
            )
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def verify_recipe_materialized(
    project_root: Path,
    slug: str,
    recipes_dir: Optional[Path],
    *,
    optional_suffixes: Sequence[str] = RECIPE_OPTIONAL_SUFFIXES,
) -> Tuple[str, ...]:
    """Which files for ``slug`` are present as contained regular files.

    **The required artifact is ``transitions/<slug>.md`` and nothing more.**
    Verified live: ``transitions-dev@0.3.0 add card-resize`` writes exactly one
    file (``OUT_DIR = flags.dir || "transitions"``; the CLI writes
    ``join(OUT_DIR, slug + ".md")``). The app passes no ``--dir``, so the
    destination is that default.

    No optional companion is accepted TODAY: upstream ships no recipe that emits
    one, so :data:`RECIPE_OPTIONAL_SUFFIXES` is empty and a stray ``.css`` beside
    the Markdown is ignored rather than counted. The ``optional_suffixes``
    parameter exists so a future genuinely-shipped companion can be added
    WITHOUT reintroducing a requirement. Nothing is synthesized to make the set
    look complete.

    Returns ``()`` if the required Markdown is missing: a partial
    materialization is not a success, because the caller typechecks immediately
    afterwards and a missing recipe fails the build. Only files whose names
    derive from the slug are accepted, so a pre-existing unrelated file cannot
    satisfy the postcondition, and containment is checked on the resolved path so
    a symlinked file cannot escape the project.
    """
    if recipes_dir is None or not recipe_slug_is_well_formed(slug):
        return ()

    verified: List[str] = []
    for suffix in (RECIPE_REQUIRED_SUFFIX,) + tuple(optional_suffixes):
        candidate = Path(recipes_dir) / f"{slug}{suffix}"
        required = suffix == RECIPE_REQUIRED_SUFFIX
        if not candidate.exists():
            if required:
                return ()
            # An OPTIONAL companion upstream did not emit. Not a failure.
            continue
        try:
            if not candidate.is_file():
                return ()
            # A zero-byte artifact carries no recipe. Counting it as a success
            # would report an install that produced nothing, and the caller
            # typechecks immediately afterwards.
            if candidate.stat().st_size <= 0:
                return ()
            if not is_contained(project_root, candidate):
                return ()
        except OSError:
            return ()
        verified.append(candidate.name)

    # NOTE: no defensive re-check of the required name here. An earlier
    # unreachable guard made the loop's `required` branch look redundant, and a
    # guard nothing depends on is not a guard -- mutating it away changed
    # nothing. The loop above is the single, load-bearing decision.
    return tuple(sorted(verified))


def detect_reduced_motion_guard(recipe_text: str) -> bool:
    """Whether a recipe's text contains upstream's reduced-motion guard.

    Verified live against the real ``card-resize.md``: the guard appears at
    line 43 as a literal ``@media (prefers-reduced-motion: reduce) {`` INSIDE a
    fenced ```css block, and is explained in prose on line 48. Because the CSS
    is embedded in the Markdown rather than shipped beside it, detection runs
    over the whole recipe body and needs no separate ``.css`` file.

    Reporting only. This function never INSERTS the guard, and a recipe without
    one is still a valid recipe: adding accessibility code to a third-party
    transition after the fact would mean shipping motion behaviour upstream
    never reviewed, under the guise of a safety improvement.
    """
    if not isinstance(recipe_text, str):
        return False
    return bool(_REDUCED_MOTION_RE.search(recipe_text))


__all__ = [
    "MAX_RECIPE_ENTRIES",
    "RECIPES_DIRNAME",
    "RECIPE_OPTIONAL_SUFFIXES",
    "RECIPE_REQUIRED_SUFFIX",
    "RECIPE_SUFFIXES",
    "SUPPORTED_TIERS",
    "TIER_FREE",
    "TRANSITIONS_CLI_ID",
    "WARNING_RECIPE_CATALOG_EMPTY",
    "WARNING_RECIPE_CATALOG_MALFORMED",
    "Recipe",
    "RecipeCatalog",
    "approved_recipes_dir",
    "build_add_argv",
    "build_list_argv",
    "detect_reduced_motion_guard",
    "normalize_recipe",
    "normalize_recipe_catalog",
    "recipe_slug_is_well_formed",
    "resolve_recipe_slug",
    "verify_recipe_materialized",
]