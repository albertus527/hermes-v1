"""Design autonomy restoration (C): application-owned CSS font audit.

The shipped Impeccable ``overused-font`` detector matches only LITERAL
``font-family: <name>`` declarations. A stylesheet that routes its families
through a CSS custom property — ``--font-display: "Fraunces"`` consumed as
``font-family: var(--font-display)`` — is invisible to it, so an overused font
can ship with a CLEAN critic. That is the confirmed regression this module
fixes: R2 shipped ``Fraunces`` + ``Inter`` (both on the engine's own overused
list) while the critic still reported CLEAN, whereas the production pipeline —
whose CSS declared the same class of face LITERALLY — was flagged and repaired.

This module resolves the indirection the engine misses. It is deliberately
conservative:

* it reports ONLY names on the engine's own overused list;
* it reports a name ONLY when a real element declaration actually USES it, so a
  custom property that is defined but never referenced can never create a
  finding (no false positive against unused variables or unrelated tokens);
* it never prescribes a replacement pairing, and never invents a font.

Pure, deterministic, stdlib-only. No model call, no network, no execution.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List

#: The engine's OWN overused list (``detector/shared/constants.mjs``), mirrored
#: verbatim so this audit never widens or narrows the engine's judgement. The
#: values are lower-cased for comparison.
OVERUSED_FONTS = frozenset(
    {
        # Older monoculture (still ubiquitous)
        "inter", "roboto", "open sans", "lato", "montserrat", "arial", "helvetica",
        # Newer monoculture (the Anthropic-skill / Vercel / GitHub default wave)
        "fraunces", "instrument sans", "instrument serif",
        "geist", "geist sans", "geist mono", "mona sans",
        "plus jakarta sans", "space grotesk", "recoleta",
    }
)

#: CSS generic / system keywords: never a real family choice, never flagged.
GENERIC_FONTS = frozenset(
    {
        "serif", "sans-serif", "monospace", "cursive", "fantasy",
        "system-ui", "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded",
        "-apple-system", "blinkmacsystemfont", "segoe ui",
        "inherit", "initial", "unset", "revert",
    }
)

#: The rule id the engine uses for this antipattern, so an audit finding is
#: indistinguishable (to the classifier) from an engine finding of the same kind.
RULE_ID = "overused-font"

#: Bounds. A project's CSS is small; these exist so a pathological tree can
#: never turn a scan into an unbounded walk.
MAX_FONT_FILES = 40
MAX_CSS_BYTES = 2_000_000
_MAX_VAR_DEPTH = 8

_SKIP_DIRS = frozenset({"node_modules", ".git", ".next", ".turbo", "coverage"})

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_FONTFACE_RE = re.compile(r"@font-face\s*\{([^{}]*)\}", re.I | re.S)
_CUSTOM_PROP_RE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+)")
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;{}]+)", re.I)
#: The ``font`` SHORTHAND only. The ``(?<![\w-])`` + ``\s*:`` means
#: ``font-family`` (where ``-`` follows ``font``) can never match here.
_FONT_SHORTHAND_RE = re.compile(r"(?<![\w-])font\s*:\s*([^;{}]+)", re.I)
_SIMPLE_VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^()]*))?\)")
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")
_SIZE_PREFIX_RE = re.compile(r"^(?:[\d.]+[a-z%]*)(?:/[\d.]+)?\s+", re.I)
_STYLE_WORDS = frozenset(
    {"normal", "italic", "oblique", "bold", "bolder", "lighter", "small-caps", "condensed"}
)


def _resolve_value(value: str, props: Dict[str, str], depth: int = 0) -> str:
    """Resolve ``var(--x[, fallback])`` references in ``value``.

    Bounded recursion: a variable may reference another variable. A reference to
    an UNDEFINED variable resolves to its fallback (or ``""``), which is exactly
    what the browser does — so an undefined variable can never fabricate a font.
    """
    if not value or depth > _MAX_VAR_DEPTH:
        return value or ""

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1).lower()
        fallback = match.group(2)
        if name in props:
            return props[name]
        return fallback if fallback is not None else ""

    resolved = _SIMPLE_VAR_RE.sub(_replace, value)
    if resolved == value:
        return resolved
    return _resolve_value(resolved, props, depth + 1)


def _primary_family_from(value: str) -> str:
    """The PRIMARY (first) family name of a ``font-family``/``font`` value.

    The engine matches the family that immediately follows the declaration, i.e.
    the one that actually renders; later entries in a stack are fallbacks that
    only apply if the primary fails to load. Reading only the primary keeps this
    audit aligned with the engine and avoids flagging a generic system fallback
    (``Roboto``/``Helvetica``/``Arial``) that merely sits behind a real choice.
    """
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        # A leftover ``var(...)`` (unresolved) is not a family name.
        if not chunk or "(" in chunk or ")" in chunk:
            continue
        quoted = _QUOTED_RE.search(chunk)
        if quoted:
            name = quoted.group(1).strip().lower()
            return "" if name in GENERIC_FONTS else name
        # Unquoted: strip a leading size (font shorthand) then style keywords.
        token = _SIZE_PREFIX_RE.sub("", chunk, count=1).strip()
        words = token.split()
        while words and words[0].lower() in _STYLE_WORDS:
            words = words[1:]
        name = " ".join(words).strip().lower()
        return "" if name in GENERIC_FONTS else name
    return ""


def audit_css(css_text: str, *, source: str = "") -> List[dict]:
    """Return the overused fonts ACTUALLY USED in one stylesheet.

    Only ``font-family`` and ``font`` declarations (i.e. real element styling)
    count as usage; a ``@font-face`` block merely *declares* a face and is
    excluded. A custom property is consulted ONLY when a usage references it, so
    an unused variable is never reported.
    """
    if not css_text:
        return []

    text = _COMMENT_RE.sub(" ", css_text)
    # Remove @font-face blocks so their own ``font-family`` (a declaration, not a
    # usage) is never read as an element that uses the face.
    usage_text = _FONTFACE_RE.sub(" ", text)

    props: Dict[str, str] = {}
    for match in _CUSTOM_PROP_RE.finditer(usage_text):
        props.setdefault(match.group(1).lower(), match.group(2).strip())

    findings: List[dict] = []
    seen: set = set()
    for regex in (_FONT_FAMILY_RE, _FONT_SHORTHAND_RE):
        for match in regex.finditer(usage_text):
            raw = match.group(1)
            resolved = _resolve_value(raw, props)
            family = _primary_family_from(resolved)
            if family in OVERUSED_FONTS and family not in seen:
                seen.add(family)
                findings.append(
                    {
                        "font": family,
                        "via_var": "var(" in raw.lower(),
                        "source": source,
                    }
                )
    return findings


def _dedupe(items: List[dict]) -> List[dict]:
    seen: set = set()
    out: List[dict] = []
    for item in items:
        font = item.get("font")
        if not font or font in seen:
            continue
        seen.add(font)
        out.append(item)
    return out


def audit_workspace(workspace) -> List[dict]:
    """Audit every CSS file under ``workspace`` (bounded, deterministic)."""
    root = Path(workspace)
    if not root.is_dir():
        return []

    out: List[dict] = []
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.lower().endswith(".css"):
                continue
            files += 1
            if files > MAX_FONT_FILES:
                return _dedupe(out)
            path = Path(dirpath) / filename
            try:
                if path.stat().st_size > MAX_CSS_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            try:
                source = str(path.relative_to(root))
            except ValueError:  # pragma: no cover - defensive
                source = str(path)
            out.extend(audit_css(text, source=source))
    return _dedupe(out)


__all__ = [
    "OVERUSED_FONTS",
    "GENERIC_FONTS",
    "RULE_ID",
    "MAX_FONT_FILES",
    "MAX_CSS_BYTES",
    "audit_css",
    "audit_workspace",
]
