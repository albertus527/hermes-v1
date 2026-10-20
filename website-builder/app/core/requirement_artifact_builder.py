"""Derive a structured requirement artifact from FAST's accepted brief (N9).

WHY THIS EXISTS
---------------
``compile_prd`` re-expresses the brief as prose bullets. That is fine for a human
reader and useless for Fit scoring: the bullets mix project requirements with
compiler acceptance-criteria boilerplate, and nothing says which of them a
screenshot could actually settle.

This module reads the SAME brief and produces the structured entries that
:mod:`app.core.design_requirement_artifact` validates and B7 consumes. It is not a
second Requirement Creator: it invents nothing. Every entry is derived from a
field the owner or FAST actually supplied, and every entry records the field it
came from in ``trace``.

WHAT IT REFUSES TO DO
---------------------
* invent a quantitative obligation the brief does not state;
* promote an assumption into a mandatory requirement;
* emit compiler boilerplate as a project requirement.

When the brief supplies too little, the artifact simply has few or no entries and
Fit abstains. That is the correct outcome.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, List, Mapping, Sequence, Tuple

from app.core.design_reference_safety import one_line
from app.core.design_requirement_artifact import (
    EVIDENCE_BEHAVIOURAL,
    EVIDENCE_STRUCTURAL,
    EVIDENCE_VISUAL,
    KIND_BEHAVIOUR,
    KIND_CONSTRAINT,
    KIND_CONTENT,
    KIND_STYLE,
    ORIGIN_ACCEPTED_CONTEXT,
    ORIGIN_TEMPLATE,
    ORIGIN_USER_BRIEF,
    VERIFIABLE_NONE,
    VERIFIABLE_RUNTIME,
    VERIFIABLE_SCREENSHOT,
    VERIFIABLE_SOURCE,
    RequirementArtifact,
    RequirementEntry,
)

#: Section names a brief may use for content the site must carry. Each becomes a
#: separate CONTENT requirement so Fit can judge them one by one.
CONTENT_KEYS: Tuple[str, ...] = ("what", "sections", "pages", "features")

#: Style intent is carried in ``design_intent``. Only phrases that name a concrete
#: visual property become requirements; adjectives do not.
STYLE_PROPERTY_WORDS: Tuple[str, ...] = (
    "serif", "sans", "monospace", "display", "typeface", "typography",
    "palette", "accent", "brass", "amber", "colour", "color", "monochrome",
    "minimal", "editorial", "grid", "layout", "spacing", "dark", "light",
)

#: A phrase that names an interaction the visitor performs. These are BEHAVIOURAL
#: and therefore never screenshot-scorable; they stay in the artifact for the
#: runtime/functional QA path.
BEHAVIOUR_WORDS: Tuple[str, ...] = (
    "submit", "book", "booking", "purchase", "buy", "checkout", "sign up",
    "signup", "register", "log in", "login", "contact form", "consultation",
    "request", "reserve", "download", "subscribe", "upload", "search",
    # Indonesian, because a brief is written in the owner's language.
    "memesan", "pesan", "membeli", "beli", "mendaftar", "daftar", "konsultasi",
    "menghubungi", "hubungi", "mengirim", "kirim", "formulir", "mencari",
)

#: Clause separators inside a free-text field. Comma is included because a brief
#: routinely lists deliverables as "layanan, harga, portofolio" and each item is
#: separately assessable; leaving them fused makes one requirement out of four.
_CLAUSE_SPLIT = re.compile(
    r"[.;\n]+|,\s*|\s+-\s+|\s+dan\s+|\s+and\s+", re.IGNORECASE
)

#: A clause shorter than this is a fragment, not an obligation. Matched to
#: ``RequirementEntry``'s own floor rather than chosen independently: a bare noun
#: like "harga" cannot be judged from a screenshot on its own, so it must not
#: become an entry. Splitting on commas still helps, because "layanan, harga,
#: portofolio, lokasi kantor" yields "portofolio" and "lokasi kantor", which are
#: independently assessable.
_MIN_CLAUSE_CHARS = 12

#: Boilerplate the compiler emits for every project. Never a project requirement.
BOILERPLATE_MARKERS: Tuple[str, ...] = (
    "every published page renders",
    "no unresolved launch-critical placeholder",
    "primary call-to-action is reachable from the landing page",
)


def _clauses(text: str) -> List[str]:
    """Split a free-text field into individually assessable clauses."""
    out: List[str] = []
    for piece in _CLAUSE_SPLIT.split(one_line(text, 800)):
        cleaned = piece.strip(" ,:")
        if len(cleaned) < _MIN_CLAUSE_CHARS:
            continue
        if cleaned.upper().startswith("UNRESOLVED"):
            continue
        out.append(cleaned)
    return out


def _is_boilerplate(text: str) -> bool:
    folded = " ".join(text.lower().split())
    return any(marker in folded for marker in BOILERPLATE_MARKERS)


def _names_behaviour(text: str) -> bool:
    folded = " ".join(text.lower().split())
    return any(word in folded for word in BEHAVIOUR_WORDS)


def _names_style_property(text: str) -> bool:
    folded = " ".join(text.lower().split())
    return any(word in folded for word in STYLE_PROPERTY_WORDS)


def build_artifact_from_brief(
    brief: Mapping[str, Any],
    *,
    project_id: str,
    source_revision: int,
    requirements_version: int,
    prd_text: str = "",
) -> RequirementArtifact:
    """Derive the structured artifact from FAST's accepted brief.

    Determinism matters: the same brief must produce a byte-identical artifact,
    because the artifact digest is what a build records. Entries are therefore
    emitted in a fixed order (content, style, behaviour) with ids assigned by
    position, and no timestamps or counters are used.

    ``prd_text`` is the compiled PRD this artifact belongs to. Its digest is
    recorded so a later consumer can tell "same revision" from "same content" --
    the three linkage fields alone cannot distinguish those.
    """
    entries: List[RequirementEntry] = []
    assumptions: List[Mapping[str, Any]] = []
    unresolved: List[Mapping[str, Any]] = []
    counter = 0

    def _add(
        text: str,
        *,
        kind: str,
        evidence_kind: str,
        verifiable_by: str,
        origin: str,
        trace: str,
        discriminating: bool = True,
        mandatory: bool = True,
    ) -> None:
        nonlocal counter
        if len(text) < _MIN_CLAUSE_CHARS or _is_boilerplate(text):
            return
        counter += 1
        entries.append(RequirementEntry(
            id=f"R{counter}",
            text=one_line(text, 300),
            kind=kind,
            evidence_kind=evidence_kind,
            verifiable_by=verifiable_by,
            origin=origin,
            trace=trace,
            mandatory=mandatory,
            discriminating=discriminating,
        ))

    # --- content: what the site must carry ---------------------------------
    for key in CONTENT_KEYS:
        raw = brief.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for i, item in enumerate(raw):
                for clause in _clauses(str(item)):
                    _add(clause, kind=KIND_CONTENT, evidence_kind=EVIDENCE_VISUAL,
                         verifiable_by=VERIFIABLE_SCREENSHOT,
                         origin=ORIGIN_USER_BRIEF, trace=f"brief.{key}[{i}]")
        elif isinstance(raw, str) and raw.strip():
            for clause in _clauses(raw):
                _add(clause, kind=KIND_CONTENT, evidence_kind=EVIDENCE_VISUAL,
                     verifiable_by=VERIFIABLE_SCREENSHOT,
                     origin=ORIGIN_USER_BRIEF, trace=f"brief.{key}")

    # --- style: concrete visual properties named in design_intent ----------
    intent = brief.get("design_intent")
    if isinstance(intent, str) and intent.strip():
        for clause in _clauses(intent):
            if not _names_style_property(clause):
                # An adjective-only clause ("calm", "clean") is not assessable.
                assumptions.append({
                    "id": f"A{len(assumptions) + 1}",
                    "text": one_line(clause, 200),
                    "trace": "brief.design_intent",
                    "confirmed": False,
                    "note": "names no concrete visual property; not a requirement",
                })
                continue
            _add(clause, kind=KIND_STYLE, evidence_kind=EVIDENCE_VISUAL,
                 verifiable_by=VERIFIABLE_SCREENSHOT,
                 origin=ORIGIN_USER_BRIEF, trace="brief.design_intent")

    # --- behaviour: what the visitor must be able to do -------------------
    why = brief.get("why")
    destination = brief.get("why_destination")
    for field_name, value in (("why", why), ("why_destination", destination)):
        if not isinstance(value, str) or not value.strip():
            continue
        for clause in _clauses(value):
            if not _names_behaviour(clause):
                continue
            _add(clause, kind=KIND_BEHAVIOUR,
                 evidence_kind=EVIDENCE_BEHAVIOURAL,
                 verifiable_by=VERIFIABLE_NONE,
                 origin=ORIGIN_ACCEPTED_CONTEXT,
                 trace=f"brief.{field_name}")

    # --- constraints: only if the brief states one explicitly -------------
    constraints = brief.get("constraints")
    if isinstance(constraints, Sequence) and not isinstance(constraints, (str, bytes)):
        for i, item in enumerate(constraints):
            for clause in _clauses(str(item)):
                _add(clause, kind=KIND_CONSTRAINT,
                     evidence_kind=EVIDENCE_STRUCTURAL,
                     verifiable_by=VERIFIABLE_SOURCE,
                     origin=ORIGIN_USER_BRIEF,
                     trace=f"brief.constraints[{i}]")

    # --- unresolved: fields the brief never supplied ----------------------
    for field_name, question in (
        ("name", "What is the project called?"),
        ("what", "What must the site contain?"),
        ("why", "What should the site achieve for the visitor?"),
        ("design_intent", "What visual direction does the owner want?"),
    ):
        if not str(brief.get(field_name) or "").strip():
            unresolved.append({
                "id": f"U{len(unresolved) + 1}",
                "question": question,
                "trace": f"brief.{field_name}",
            })

    return RequirementArtifact(
        project_id=project_id,
        source_revision=source_revision,
        requirements_version=requirements_version,
        prd_digest=(
            hashlib.sha256(prd_text.encode("utf-8")).hexdigest() if prd_text else ""
        ),
        entries=tuple(entries),
        assumptions=tuple(assumptions),
        unresolved=tuple(unresolved),
    )


__all__ = [
    "BEHAVIOUR_WORDS",
    "BOILERPLATE_MARKERS",
    "CONTENT_KEYS",
    "STYLE_PROPERTY_WORDS",
    "build_artifact_from_brief",
]
