"""V3-B: an honest asset strategy for website generation.

The owner rarely supplies every image or piece of business content up front. The
pipeline must still be able to produce a credible, visually intentional website
**without fabricating** client projects, portfolio claims, credentials, or
ownership of third-party photographs.

This module classifies every visual slot into one of four explicit states and
produces actionable, non-fabricating instructions for FRONTEND:

* ``OWNER_PROVIDED``      — the owner explicitly supplied the asset. Highest
  authority; FRONTEND must use it and confirm the owner owns or is licensed to
  use it.
* ``LICENSED_EDITORIAL``  — a third-party asset whose source, licence, and
  attribution are VERIFIED. Usable only as editorial illustration, never as the
  studio's own work. Reached only from supplied, validated metadata; the
  application never *assumes* a licence.
* ``CONCEPTUAL``          — no real asset exists. Use visually intentional,
  clearly-labelled conceptual treatments (varied per item), never identical
  repeated placeholder cards, and never imply a real completed project.
* ``MISSING``             — the content is genuinely required but absent; it is
  surfaced honestly (a concise request for owner-supplied material) rather than
  invented.

Pure, deterministic, stdlib-only. No model call, no network, no image download,
no new infrastructure.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence, Tuple

#: The four explicit asset states (A1).
ASSET_OWNER_PROVIDED = "OWNER_PROVIDED"
ASSET_LICENSED_EDITORIAL = "LICENSED_EDITORIAL"
ASSET_CONCEPTUAL = "CONCEPTUAL"
ASSET_MISSING = "MISSING"
ASSET_STATES: Tuple[str, ...] = (
    ASSET_OWNER_PROVIDED,
    ASSET_LICENSED_EDITORIAL,
    ASSET_CONCEPTUAL,
    ASSET_MISSING,
)

#: Missing-content classes (B1): launch-critical vs optional.
CONTENT_LAUNCH_CRITICAL = "launch-critical"
CONTENT_OPTIONAL = "optional"

#: Every field a third-party asset must carry before it may be treated as
#: LICENSED_EDITORIAL. Absence of any one means the asset is NOT verified, so it
#: is never assumed licensed (C6).
LICENSED_ASSET_REQUIRED_FIELDS: Tuple[str, ...] = (
    "source_uri",
    "license",
    "attribution",
)

#: Licence values that mean "not actually verified".
_UNVERIFIED_LICENSES: Tuple[str, ...] = (
    "", "unknown", "unverified", "unspecified", "none", "n/a", "na", "tbd", "?",
)

_URL_RE = re.compile(r"https?://[^\s,;)\]>\"']+", re.IGNORECASE)
#: A URL only counts as an owner asset when it looks like an actual asset
#: reference — never merely because it is a URL (the CTA destination is not an
#: asset).
_ASSET_URL_HINT = re.compile(
    r"(\.(?:png|jpe?g|webp|gif|avif|svg|mp4|mov|pdf)(?:$|[?#])"
    r"|image|photo|foto|gambar|asset|cdn|/img)",
    re.IGNORECASE,
)
#: "showcase 3 proyek arsitektur" → 3. Bounded 1..12.
_ITEM_COUNT_RE = re.compile(
    r"(\d{1,2})\s*(?:proyek|project|karya|work|case\s*stud\w*|item)",
    re.IGNORECASE,
)

#: Terms that mean the site will SHOW visual content.
_VISUAL_TERMS: Tuple[str, ...] = (
    "showcase", "portfolio", "gallery", "galeri", "proyek", "project",
    "karya", "work", "case study", "lookbook", "catalog", "katalog",
    "gambar", "foto", "photo", "image", "imagery", "visual", "render",
)
_SERVICE_TERMS: Tuple[str, ...] = ("layanan", "service", "services")
_ABOUT_TERMS: Tuple[str, ...] = ("tentang", "about")


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _brief_text(brief: Mapping[str, Any]) -> str:
    """The brief's own words, joined (excluding the CTA destination)."""
    if not isinstance(brief, Mapping):
        return ""
    return " ".join(
        _clean(brief.get(k))
        for k in ("name", "what", "why", "design_intent")
        if _clean(brief.get(k))
    )


def has_visual_content(brief: Mapping[str, Any]) -> bool:
    """Whether the site will actually SHOW visual content.

    Grounded in the brief's own words (and the owner's design intent). A site
    with no such content is NOT forced to use imagery (A3/B/C).
    """
    text = _brief_text(brief).lower()
    return any(term in text for term in _VISUAL_TERMS)


def owner_assets(brief: Mapping[str, Any]) -> Tuple[str, ...]:
    """Asset references the owner explicitly supplied, in order, de-duplicated.

    Only URLs that actually look like an asset reference count; the CTA
    destination is never mistaken for an asset. Nothing is inferred.
    """
    text = _brief_text(brief)
    destination = _clean((brief or {}).get("why_destination"))
    found: list = []
    for url in _URL_RE.findall(text):
        if destination and url == destination:
            continue
        if _ASSET_URL_HINT.search(url):
            found.append(url)
    seen: set = set()
    out: list = []
    for url in found:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return tuple(out)


def is_verified_licensed_asset(asset: Any) -> bool:
    """Whether ``asset`` carries VERIFIED licence + source + attribution (A4).

    A missing or explicitly-unverified licence is NOT verified, so the asset is
    never treated as licensed. This is the whole licence boundary: nothing is
    assumed.
    """
    if not isinstance(asset, Mapping):
        return False
    for field in LICENSED_ASSET_REQUIRED_FIELDS:
        if not _clean(asset.get(field)):
            return False
    if _clean(asset.get("license")).lower() in _UNVERIFIED_LICENSES:
        return False
    return True


def showcase_item_count(brief: Mapping[str, Any]) -> Optional[int]:
    """The item count the brief states for a visual showcase, or ``None``."""
    match = _ITEM_COUNT_RE.search(_brief_text(brief))
    if not match:
        return None
    count = int(match.group(1))
    return count if 1 <= count <= 12 else None


def plan_assets(
    brief: Mapping[str, Any],
    *,
    licensed_assets: Sequence[Any] = (),
) -> dict:
    """Return the deterministic asset plan for ``brief``.

    The plan names the state of the visual showcase, the actionable
    instructions FRONTEND should follow, and the launch-critical vs optional
    content that is still missing. It NEVER invents a project, client, service,
    location, award, or testimonial (A5/B5).
    """
    brief = brief if isinstance(brief, Mapping) else {}
    visual = has_visual_content(brief)
    owner = owner_assets(brief)
    verified = tuple(a for a in (licensed_assets or ()) if is_verified_licensed_asset(a))
    count = showcase_item_count(brief)

    states: dict = {}
    instructions: list = []
    missing_launch: list = []
    missing_optional: list = []

    if not visual:
        # Not a portfolio/showcase site: imagery is simply not required.
        instructions.append(
            "This site does not require imagery; do not add decorative stock "
            "photos or fabricated project images."
        )
    elif owner:
        states["showcase"] = ASSET_OWNER_PROVIDED
        instructions.append(
            "Owner-supplied assets take priority for the showcase: use them, "
            "and confirm the owner owns or is licensed to use them."
        )
        instructions.append(
            "Do not substitute a conceptual treatment where an owner asset exists."
        )
    elif verified:
        states["showcase"] = ASSET_LICENSED_EDITORIAL
        instructions.append(
            "Use the verified licensed asset(s) as editorial illustration only; "
            "satisfy the stated attribution and never present them as the "
            "studio's own work."
        )
    else:
        states["showcase"] = ASSET_CONCEPTUAL
        qualifier = f" {count} distinct" if count else " distinct"
        instructions.append(
            f"No owner-supplied project photography exists: create{qualifier} "
            "visually intentional, clearly-labelled conceptual treatments "
            "(vary motif, composition, and framing per item) instead of "
            "repeating identical placeholder cards."
        )
        instructions.append(
            "Disclose the conceptual nature of these visuals truthfully, but keep "
            "it proportionate: a concise disclosure at the section level (one "
            "short note per section) is sufficient to distinguish conceptual "
            "studies from real completed work. Do NOT stamp a repeated, visually "
            "dominant PLACEHOLDER badge on every decorative element — that makes "
            "a public-facing preview read like an internal requirements document. "
            "Never imply any visual depicts a real completed project."
        )
        missing_launch.append(
            "real showcase content — project names, descriptions, and "
            "photography, or explicit permission to publish them"
        )

    text = _brief_text(brief).lower()
    if any(term in text for term in _SERVICE_TERMS):
        missing_optional.append(
            "concrete service names and descriptions (use honest, general "
            "wording meanwhile)"
        )
    if any(term in text for term in _ABOUT_TERMS):
        missing_optional.append(
            "about-studio prose (use honest, general wording meanwhile)"
        )

    return {
        "visual_content": visual,
        "showcase_item_count": count,
        "owner_assets": owner,
        "licensed_assets": verified,
        "states": states,
        "instructions": tuple(instructions),
        "missing_launch_critical": tuple(missing_launch),
        "missing_optional": tuple(missing_optional),
    }


__all__ = [
    "ASSET_OWNER_PROVIDED",
    "ASSET_LICENSED_EDITORIAL",
    "ASSET_CONCEPTUAL",
    "ASSET_MISSING",
    "ASSET_STATES",
    "CONTENT_LAUNCH_CRITICAL",
    "CONTENT_OPTIONAL",
    "LICENSED_ASSET_REQUIRED_FIELDS",
    "has_visual_content",
    "owner_assets",
    "is_verified_licensed_asset",
    "showcase_item_count",
    "plan_assets",
]
