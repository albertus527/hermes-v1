"""Semantic decomposition of composite N9 requirements.

WHY
---
A live three-candidate run showed that three of the six requirements in one
brief are COMPOSITE: "off-white, charcoal, aksen terracotta, tipografi elegan"
bundles four separately visible facts. A composite requirement can only ever
be answered "partial", which is a weaker and less actionable signal than four
independent requirements would give.

The fix belongs in the REQUIREMENT, not in the scorer. But naive comma
splitting is wrong, because it destroys coherent phrases: "gaya editorial
minimalis premium modern" would become the unassessable fragments "minimalis"
and "premium modern".

THE RULE
--------
A requirement is decomposed only where the parts are INDEPENDENTLY
ASSESSABLE -- a reader looking at one screenshot can decide each part on its
own, without weighing it against the others.

That is a semantic property, so it is expressed as lexical rules over the
parts of a clause. The vocabulary is small, inspectable and testable, and no
model call is involved: the same brief always produces the same artifact.

THE RULES
---------
R1  Split only on an independently assessable boundary.
R2  A part naming a countable, checkable thing is a boundary.
R3  An adjective cluster is NOT a boundary.
R4  A quantity binds to the noun it counts and never becomes its own part.
R5  Never create a requirement the owner did not write.
R6  Every part is a verbatim substring of the original clause.
R7  A part below the minimum length is merged back, never emitted as a stub.
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Sequence, Tuple

#: A part is INDEPENDENT when it names something concrete. These are the
#: nouns that denote a page element, a visual property or a document item.
CONCRETE_NOUNS: Tuple[str, ...] = (
    # page sections and document items
    "proyek", "project", "projects", "tentang", "about", "layanan", "services",
    "service", "kontak", "contact", "halaman", "page", "pages", "bagian",
    "section", "menu", "katalog", "catalog", "produk", "product", "produk",
    "galeri", "gallery", "portfolio", "portofolio", "karya", "testimoni",
    "testimonial", "faq", "tim", "team", "blog", "artikel", "article",
    "harga", "price", "prices", "lokasi", "location", "alamat", "address",
    "jam", "hours", "fasilitas", "facility", "promo", "diskon", "blog",
    # visual properties
    "warna", "color", "colour", "palet", "palette", "tipografi", "typography",
    "font", "typeface", "huruf", "latar", "background", "gradasi", "gradient",
    "layout", "tata", "letak", "grid", "ikon", "icon", "gambar", "image",
    "foto", "photo", "video", "border", "bingkai", "radius", "spasi", "spacing",
    # named palette colours. A colour word is a checkable thing: it is visible.
    "off-white", "offwhite", "charcoal", "terracotta", "brass", "amber",
    "ivory", "cream", "beige", "sand", "olive", "sage", "navy", "teal",
    "coral", "rust", "mustard", "slate", "marble", "sage", "olive",
    "monokrom", "monochrome", "grayscale", "greyscale", "pastel", "neon",
    # named type treatments
    "serif", "sans", "sans-serif", "monospace", "display", "heading",
    "headingfont", "bodyfont",
    # style words that ARE concrete when they name a treatment
    "gaya", "style", "estetika", "aesthetic", "visual", "tampilan",
)

#: Words that describe a QUALITY rather than naming a checkable thing. A part
#: built only from these is coherent and stays with its group.
TASTE_WORDS: Tuple[str, ...] = (
    "minimalis", "minimal", "premium", "modern", "elegan", "elegant", "editorial",
    "tenang", "calm", "bersih", "clean", "soft", "gentle", "playful", "warm",
    "hangat", "cerah", "bright", "dark", "gelap", "terang", "cozy", "nyaman",
    "luxury", "mewah", "simple", "sederhana", "professional", "profesional",
    "trust", "terpercaya", "friendly", "ramah", "bold", "tegas", "muted",
    "pastel", "vibrant", "cerah", "monokrom", "monochrome", "rustic", "retro",
    "futuristik", "futuristic", "luxurious", "coherent", "konsisten",
    "harmonis", "harmonious", "monokromatik", "stylish", " stylish",
)

#: A leading count. It binds to the noun it modifies and is never its own part.
_COUNT_WORDS: Tuple[str, ...] = (
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan",
    "sembilan", "sepuluh", "beberapa", "several", "some", "multiple",
)

#: A clause that EXCLUDES something is not an obligation. "tanpa checkout,
#: payment, login" tells the builder what the site must NOT have; turning
#: that into a requirement would assert that the page MUST show a checkout
#: flow. An exclusion is a constraint on the design, not a requirement to
#: satisfy, so it never becomes an entry.
NEGATION_MARKERS: Tuple[str, ...] = (
    "tanpa", "tidak ada", "tidak", "bukan", "non-", "no ", "without",
    "tanpa adanya", "free dari", "kecuali",
)

#: Separators between the PARTS of a clause. A comma is a hint that a split
#: point may exist; the decision is made by the classification rules.
_PART_SEPARATORS = re.compile(
    r",\s*|\s+dan\s+|\s+and\s+", re.IGNORECASE
)

#: A part that is only a count, with nothing else. Such a part would be a
#: bare number, which is exactly the kind of invented numeric requirement the
#: contract forbids.
_BARE_COUNT = re.compile(
    r"^(?:" + "|".join(re.escape(w) for w in _COUNT_WORDS) + r")\s*[\w\s]{0,20}$",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def is_negated(text: str) -> bool:
    """True when the clause EXCLUDES something rather than requiring it."""
    folded = _normalise(text)
    return any(marker in folded for marker in NEGATION_MARKERS)


def _mentions_concrete(text: str) -> bool:
    """True when the part names a checkable thing."""
    folded = _normalise(text)
    if not folded:
        return False
    for noun in CONCRETE_NOUNS:
        # A hyphenated word is one token: \b fails between "off" and "-white",
        # so a hyphen is treated as a word character here.
        pattern = r"(?<![a-z0-9])" + re.escape(noun).replace(r"\-", r"[\s-]") + r"(?![a-z0-9])"
        if re.search(pattern, folded):
            return True
    return False


def _is_only_taste(text: str) -> bool:
    """True when the part is adjectives and qualities only."""
    folded = _normalise(text)
    if not folded:
        return True
    if _mentions_concrete(text):
        return False
    # A part with a concrete-ish noun that is not in the vocabulary is not
    # confidently taste-only either, so it is treated as concrete.
    words = re.findall(r"[a-z]+", folded)
    if not words:
        return True
    unknown = [w for w in words if w not in TASTE_WORDS and len(w) > 3]
    return not unknown


#: A part that continues the previous one rather than standing alone.
_CONTINUATION_PREFIXES: Tuple[str, ...] = (
    "atau", "and", "atau pun", "dan lain",
)


def _is_continuation(text: str) -> bool:
    """True when the part continues the previous one instead of standing alone.

    "checkout, payment, login, database, atau backend transaksi" is ONE
    exclusion list. Splitting it at "atau" would leave "atau backend transaksi"
    as a requirement in its own right, which says nothing.
    """
    folded = _normalise(text)
    return any(folded.startswith(prefix + " ") for prefix in _CONTINUATION_PREFIXES)


def _has_leading_count(text: str) -> bool:
    folded = _normalise(text)
    for word in _COUNT_WORDS:
        if folded.startswith(word + " ") or folded == word:
            return True
    return False


def decompose_clause(
    clause: str,
    *,
    min_chars: int,
) -> List[str]:
    """Split *clause* into independently assessable parts, or keep it whole.

    Returns a list whose every element is a verbatim substring of the
    original clause (R5, R6). The original clause is returned as a single
    element when it is not decomposable.
    """
    text = str(clause or "").strip()
    if not text:
        return []

    parts = [p.strip(" ,:") for p in _PART_SEPARATORS.split(text)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return [text]

    # A clause that EXCLUDES something is a constraint, not an obligation. It
    # stays whole and is never emitted as a requirement.
    if is_negated(text):
        return [text]

    # R3: a part that names no checkable thing is COHERENT and joins its
    # neighbour. Consecutive coherent parts form one group.
    #
    # A part is INDEPENDENT when it names a concrete thing. An independent
    # part ALWAYS starts its own group -- it is never merged with a
    # neighbour, because the neighbour is a different obligation.
    groups: List[List[str]] = []
    for part in parts:
        # A CONTINUATION never stands alone. "atau backend transaksi" is the
        # tail of the exclusion list before it, and "dan lain-lain" is the tail
        # of the item before it. Emitting either as a requirement would assert
        # something the owner never wrote.
        if _is_continuation(part):
            if groups:
                groups[-1].append(part)
            else:
                groups.append([part])
            continue
        if is_negated(part):
            # An exclusion inside a list stays attached to what precedes it.
            if groups:
                groups[-1].append(part)
            else:
                groups.append([part])
            continue
        independent = (
            _mentions_concrete(part)
            and not _is_only_taste(part)
        )
        if independent or not groups:
            groups.append([part])
        else:
            groups[-1].append(part)

    # R4: a leading count binds to the noun it modifies. If a group is ONLY a
    # count it is merged forward, because a bare number is a numeric
    # requirement the owner never wrote.
    merged: List[List[str]] = []
    pending_count: List[str] = []
    for group in groups:
        if len(group) == 1 and _has_leading_count(group[0]) and not _mentions_concrete(group[0]):
            pending_count.append(group[0])
            continue
        if pending_count:
            group = pending_count + group
            pending_count = []
        merged.append(group)
    if pending_count and merged:
        merged[-1] = merged[-1] + pending_count
    elif pending_count:
        merged.append(pending_count)

    # R7: a group below the minimum is merged back into its neighbour rather
    # than emitted as an unassessable stub.
    #
    # The check is on the JOINED group, not on the individual part. A single
    # colour name like "off-white" is short but perfectly assessable -- what
    # matters is that the requirement as a whole says something a reader can
    # act on. Applying the minimum to each part instead would merge every
    # colour back together and defeat the whole point.
    out: List[str] = []
    for group in merged:
        joined = ", ".join(group)
        # R7 applies to a COHERENT group only. A group that names a concrete
        # thing is assessable even when it is short: "off-white" is nine
        # characters and is exactly as checkable as "aksen terracotta". Only a
        # fragment that says nothing at all gets merged back.
        names_something = any(_mentions_concrete(p) for p in group)
        if len(joined) < min_chars and not names_something:
            if out:
                out[-1] = f"{out[-1]}, {joined}"
            elif len(merged) > 1:
                return [text]
            else:
                out.append(joined)
        else:
            out.append(joined)

    if len(out) < 2:
        return [text]

    # R5/R6: every part must be a verbatim substring of the original clause.
    for part in out:
        if part not in text:
            return [text]

    return out


def group_of(part: str, clause: str) -> str:
    """The stable group id for a part of *clause*."""
    import hashlib
    return "G" + hashlib.sha256(clause.encode("utf-8")).hexdigest()[:8]


__all__ = [
    "CONCRETE_NOUNS",
    "NEGATION_MARKERS",
    "is_negated",
    "TASTE_WORDS",
    "decompose_clause",
    "group_of",
]
