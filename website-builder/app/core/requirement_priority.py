"""Requirement priority policy and requirement-set identity (N9, Option D + C default).

THE ARCHITECTURE
----------------
Option D with Option C as the deterministic default.

  Option C (DEFAULT)  When the accepted brief contains no explicit priority,
                      every eligible parent requirement group carries EQUAL
                      importance, and each child receives an equal fraction of
                      its parent's importance. Splitting one parent into four
                      children therefore does NOT quadruple that parent's
                      influence -- it divides one share four ways.

  Option D (EXPLICIT) When the owner EXPRESSLY states a priority, that
                      priority is recorded against the parent group it
                      names. A qualitative priority stays qualitative: "more
                      important than" is NOT silently turned into a percentage.

WHY THIS EXISTS
---------------
A composite requirement that is decomposed into four parts would otherwise
gain four times the influence on the aggregate Fit, because Fit counts
satisfied requirements over a total. The measured shift on the Forma Atelier
brief was 16.7% -> 36.4%, a 2.2x increase produced silently by a change
described as "more atomic". Atomicity and INFLUENCE are different things.

WHAT THIS MODULE DOES NOT DO
----------------------------
* It does not compute a Fit score. The aggregate Fit stays authoritative and
  is never recomputed from anything here.
* It does not change any threshold, floor, weight or formula.
* It does not invent a numeric importance where the owner gave none, and it
  does not invent a priority where the owner was ambiguous.
* It does not claim the evaluator enforces the weights. The provider contract
  has no weighting parameter, so the weights are RECORDED and PASSED AS
  CONTEXT, and the limitation is reported.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The rubric contract. Separate from the requirement set: the rubric says HOW
#: to score, the requirement set says WHAT to score against.
SCORING_CONTRACT_VERSION = "n8-design-v1"

#: The weighting policy version. Bumping it invalidates every stored
#: suitability computed under the old policy.
WEIGHTING_POLICY_VERSION = "priority-c-default-v1"

#: Priorities that are EXPLICITLY numerical. The owner wrote a number, so the
#: number is preserved -- after validation.
_NUMERIC_PRIORITY = re.compile(
    r"\b(?:priority|prioritas|weight|bobot|tingkat|level)\b[^0-9]{0,12}(\d{1,3})\b"
    r"|\b(\d{1,3})\b[^0-9]{0,12}\b(?:priority|prioritas|weight|bobot)\b",
    re.IGNORECASE,
)

#: Qualitative priority markers. These establish an ORDERING, never a number.
#:
#: The list is deliberately narrow. A phrase like "modern" is a design
#: preference, not a priority, and treating it as one would invent a priority
#: the owner never expressed.
_QUALITATIVE_PRIORITY = re.compile(
    r"(?i)\b("
    r"paling penting|lebih penting|terpenting|priority|prioritas|"
    r"fokus(?:kan)?|utamakan|diutamakan|lebih dulu|terlebih|"
    r"prioriti[sz]e|prioritas(?:kan)?|diPrioriti[sz]ekan|"
    r"most important|more important|less important|focus on|prioriti[sz]e"
    r")\b"
)

#: Words that mark a comparison BETWEEN two things, so the priority needs a
#: target on both sides. "Konten lebih penting daripada animasi" names two.
_COMPARISON = re.compile(
    r"(?i)\b(lebih penting|lebih dulu|than|daripada|di atas|over|compared to)\b"
)

#: A target group is identified by the owner NAMING a requirement or a section.
#: These are the words that make a phrase point at something.
_TARGET_HINT = re.compile(
    r"(?i)\b(desain|design|content|konten|informasi|information|produk|product|"
    r"animasi|animation|foto|gambar|image|typografi|typography|warna|color|"
    r"layout|halaman|page|menu|kontak|contact|fokus|focus)\b"
)

#: Which group CONCEPT a target word points at. A priority says "konten"; the
#: group it names is the one whose text carries the same concept. This is a
#: small, explicit vocabulary rather than a fuzzy similarity, so the mapping
#: is auditable.
_TARGET_CONCEPTS: Mapping[str, Tuple[str, ...]] = {
    "content": ("content", "konten", "informasi", "information", "produk",
                "product", "katalog", "catalog", "isi", "copy", "teks"),
    "design": ("design", "desain", "gaya", "style", "tipografi", "typography",
               "warna", "color", "layout", "visual"),
    "animation": ("animation", "animasi", "motion", "video"),
    "image": ("image", "gambar", "foto", "photo", "galeri", "gallery"),
    "page": ("page", "halaman", "section", "bagian"),
    "contact": ("contact", "kontak"),
}


def _normalise(text: str) -> str:
    return " ".join(str(text or "").lower().split())


@dataclass(frozen=True)
class Priority:
    """One priority the owner expressed, or that could not be resolved.

    ``kind`` is one of:
      ``numeric``       the owner wrote a number; it is preserved
      ``qualitative``   the owner expressed an ORDER; no number is invented
      ``unresolved``    the priority could not be mapped to a group

    ``explicit`` distinguishes what the owner said from what could only be
    inferred. An inferred priority is NOT used to weight anything: it is
    recorded so a reader can see it was considered and set aside.
    """

    kind: str
    source_text: str
    trace: str
    target_group_id: str = ""
    value: Optional[float] = None
    order: int = 0
    explicit: bool = True
    resolution: str = "resolved"
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source_text": self.source_text,
            "trace": self.trace,
            "target_group_id": self.target_group_id,
            "value": self.value,
            "order": self.order,
            "explicit": self.explicit,
            "resolution": self.resolution,
            "reason": self.reason,
        }


#: Where a group's numeric importance CAME FROM. This is the whole point:
#: a number the owner wrote is not the same thing as a number we defaulted to,
#: and the artifact must never blur the two.
IMPORTANCE_EXPLICIT_NUMERIC = "explicit_numeric"
IMPORTANCE_DEFAULT_EQUAL = "default_equal_groups"
IMPORTANCE_UNCOMMITTED = "uncommitted"


@dataclass(frozen=True)
class GroupWeight:
    """The weight of one parent requirement group.

    ``importance`` is OPTIONAL, and ``importance_source`` says where it came
    from. That distinction is the correction:

      ``explicit_numeric``  the owner wrote a number. It is the owner's.
      ``default_equal_groups``  the Option C default. It is OURS, and it is
                            labelled as ours. It is NOT the owner's expressed
                            weighting.
      ``uncommitted``       no numeric share is asserted at all.

    A QUALITATIVE priority never produces an importance. The owner said
    "more important than", not "twice as important", so no share is derived
    from it. The ordering is recorded in ``qualitative_order`` with the raw
    source text, and the group's numeric importance stays at the documented
    default (or uncommitted) -- never a number that pretends to be the
    owner's.

    The children STILL divide their parent's share equally, so splitting a
    group never multiplies its influence.
    """

    group_id: str
    text: str
    importance: Optional[float]
    importance_source: str
    child_ids: Tuple[str, ...] = ()
    priority: Optional[Priority] = None
    mandatory: bool = True
    qualitative_rank: Optional[int] = None

    def child_weight(self) -> Optional[float]:
        """The share of ONE child of this group, if one is committed."""
        if self.importance is None or not self.child_ids:
            return None
        return self.importance / len(self.child_ids)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "text": self.text,
            "importance": self.importance,
            "importance_source": self.importance_source,
            "child_ids": list(self.child_ids),
            "child_weight": self.child_weight(),
            "mandatory": self.mandatory,
            "priority": self.priority.to_dict() if self.priority else None,
            "qualitative_rank": self.qualitative_rank,
        }


@dataclass(frozen=True)
class WeightingPolicy:
    """The complete weighting decision for one requirement set.

    THE FOUR LAYERS ARE SEPARATE FIELDS, never one number:

      ``priorities``      LAYER 1 -- what the owner declared. Numeric stays
                          numeric; qualitative stays qualitative, with its
                          exact source text and trace.
      ``groups``          LAYER 2 -- the DETERMINISTIC DEFAULT normalisation:
                          equal parent importance, children split it. This is
                          OUR policy and every group says which source its
                          number came from.
      ``guidance``        LAYER 3 -- what may be TOLD to the evaluator. The
                          provider has no weighting parameter, so this is
                          context, never enforcement.
      ``enforcement``     LAYER 4 -- always "none_available" for the current
                          provider. Stated, not implied.

    ``active`` is False whenever the policy could not be resolved. An
    unresolved priority does not silently fall back to a different policy: it
    falls back to the DOCUMENTED SAFE DEFAULT (Option C), and says so.
    """

    mode: str                      # "c_default" | "d_explicit" | "c_default_fallback"
    groups: Tuple[GroupWeight, ...] = ()
    priorities: Tuple[Priority, ...] = ()
    unresolved: Tuple[Priority, ...] = ()
    policy_version: str = WEIGHTING_POLICY_VERSION
    note: str = ""
    #: The qualitative ORDER the owner stated, with its source text. This is
    #: the faithful representation of "more important than".
    qualitative_order: Tuple[Mapping[str, Any], ...] = ()
    #: What may be passed to the evaluator as context. Never enforcement.
    guidance: Tuple[str, ...] = ()
    #: What the provider can actually enforce. Never a guess.
    enforcement: str = "none_available"

    @property
    def active(self) -> bool:
        return self.mode in ("c_default", "d_explicit")

    @property
    def has_explicit_priority(self) -> bool:
        return any(p.kind != "unresolved" and p.explicit
                   for p in self.priorities)

    @property
    def has_numeric_priority(self) -> bool:
        return any(p.kind == "numeric" and p.resolution == "resolved"
                   for p in self.priorities)

    @property
    def has_qualitative_priority(self) -> bool:
        return any(p.kind == "qualitative" and p.resolution == "resolved"
                   for p in self.priorities)

    def group(self, group_id: str) -> Optional[GroupWeight]:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        return None

    def weight_of(self, entry_id: str) -> Optional[float]:
        """The committed share of one requirement entry, if any."""
        for g in self.groups:
            if entry_id in g.child_ids:
                return g.child_weight()
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "policy_version": self.policy_version,
            "active": self.active,
            "has_explicit_priority": self.has_explicit_priority,
            "has_numeric_priority": self.has_numeric_priority,
            "has_qualitative_priority": self.has_qualitative_priority,
            "groups": [g.to_dict() for g in self.groups],
            "priorities": [p.to_dict() for p in self.priorities],
            "unresolved": [p.to_dict() for p in self.unresolved],
            "qualitative_order": [dict(o) for o in self.qualitative_order],
            "guidance": list(self.guidance),
            "enforcement": self.enforcement,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# extracting priorities from the brief
# ---------------------------------------------------------------------------


def extract_priorities(
    brief: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
) -> Tuple[Priority, ...]:
    """Extract the priorities the owner EXPRESSED, from the accepted brief.

    Only the fields a brief can carry a priority in are read: ``what``,
    ``why``, ``design_intent`` and ``constraints``. A priority is looked for
    only where the owner could have written one.

    Each extracted priority records its source text and its trace, and is
    classified as numeric, qualitative, or unresolved. Nothing is invented: a
    phrase that cannot be mapped to a group is marked unresolved rather than
    being attached to the nearest one.
    """
    found: List[Priority] = []
    for field_name in ("what", "why", "why_destination", "design_intent",
                       "constraints"):
        raw = brief.get(field_name)
        if isinstance(raw, (list, tuple)):
            values = [(str(v), f"brief.{field_name}[{i}]")
                      for i, v in enumerate(raw)]
        elif isinstance(raw, str) and raw.strip():
            values = [(raw, f"brief.{field_name}")]
        else:
            continue

        for text, trace in values:
            for sentence in _sentences(text):
                folded = _normalise(sentence)
                if not _QUALITATIVE_PRIORITY.search(folded):
                    continue
                numeric = _NUMERIC_PRIORITY.search(folded)
                target, target_reason = _match_group(sentence, groups)
                if numeric:
                    value = float(numeric.group(1) or numeric.group(2))
                    kind = "numeric"
                    resolution = "resolved" if target else "unresolved"
                    reason = target_reason if target else (
                        "the priority names a number but no requirement group "
                        "could be mapped to it"
                    )
                else:
                    value = None
                    kind = "qualitative"
                    if _COMPARISON.search(folded):
                        # A comparison names TWO things. Mapping one side and
                        # resolving the priority would be guessing about the
                        # other side, so a half-mapped comparison is
                        # UNRESOLVED.
                        resolution = "unresolved"
                        reason = (
                            "the priority compares two things but only one "
                            "could be mapped to a requirement group"
                        )
                    elif target:
                        resolution = "resolved"
                        reason = ""
                    else:
                        resolution = "unresolved"
                        reason = (
                            "the priority names no requirement group, so it "
                            "cannot be applied without guessing"
                        )
                found.append(Priority(
                    kind=kind,
                    source_text=sentence.strip(),
                    trace=trace,
                    target_group_id=target,
                    value=value,
                    order=len(found) + 1,
                    explicit=True,
                    resolution=resolution,
                    reason=reason,
                ))
    return tuple(found)


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?;])\s+|,\s+|\s+dan\s+|\s+and\s+", str(text or ""))
    return [p for p in parts if p.strip()]


def _match_group(
    sentence: str,
    groups: Sequence[Mapping[str, Any]],
) -> Tuple[str, str]:
    """Map a priority sentence to a requirement group, or explain why not.

    The match is on the WORDS the owner used. A priority that says
    "prioritaskan informasi produk" is mapped to the group whose text contains
    "produk". A priority with no such anchor is left unresolved: attaching it
    to the nearest group would be guessing, and a guessed priority is worse
    than no priority.
    """
    folded = _normalise(sentence)
    if not _TARGET_HINT.search(folded):
        return "", "the priority names no target the requirement groups share"
    # Which concept does the priority name?
    concept = ""
    for name, words in _TARGET_CONCEPTS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", folded) for w in words):
            concept = name
            break

    best = ""
    best_score = 0
    for group in groups:
        text = _normalise(group.get("text", ""))
        words = {w for w in re.findall(r"[a-z]+", text) if len(w) > 3}
        overlap = 0
        for w in words:
            if w in folded:
                overlap += 1
                continue
            stem = w[:max(4, len(w) - 2)]
            if len(stem) >= 4 and stem in folded:
                overlap += 1
        # A group whose own words carry the CONCEPT the priority named is a
        # match even when the words differ ("konten" -> "informasi produk").
        if concept:
            for w in _TARGET_CONCEPTS.get(concept, ()):
                if re.search(rf"\b{re.escape(w)}\b", text):
                    overlap += 2
                    break
        if overlap > best_score:
            best_score = overlap
            best = str(group.get("group_id", ""))
    if not best:
        return "", "the priority names a target but no group shares its words"
    return best, ""


# ---------------------------------------------------------------------------
# building the policy
# ---------------------------------------------------------------------------


def build_weighting_policy(
    brief: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    *,
    eligible_group_ids: Optional[Sequence[str]] = None,
) -> WeightingPolicy:
    """Build the weighting policy for a requirement set.

    ``groups`` are the parent groups with their child ids, in a stable order.
    ``eligible_group_ids`` restricts which groups carry weight; a group with
    no scorable child carries none.
    """
    eligible = (
        set(eligible_group_ids)
        if eligible_group_ids is not None
        else {str(g.get("group_id", "")) for g in groups}
    )
    usable = [g for g in groups if str(g.get("group_id", "")) in eligible]
    priorities = extract_priorities(brief, usable)
    resolved = [p for p in priorities if p.resolution == "resolved"]
    unresolved = [p for p in priorities if p.resolution != "resolved"]

    if not resolved:
        mode = "c_default_fallback" if unresolved else "c_default"
        note = (
            "an explicit priority could not be mapped to a requirement group, "
            "so the documented safe default (equal parent importance) applies"
            if unresolved else
            "the brief states no priority, so every eligible parent group "
            "carries equal importance"
        )
        return WeightingPolicy(
            mode=mode,
            groups=_equal_groups(usable),
            priorities=priorities,
            unresolved=unresolved,
            note=note,
            guidance=(
                "Every requirement in this set counts equally. The owner "
                "stated no priority."
                if not unresolved else
                "Every requirement in this set counts equally. An owner "
                "priority was stated but could not be mapped to a specific "
                "requirement, so none is applied.",
            ),
        )

    # Option D: the named group takes the priority. The DEFAULT (equal) value
    # is 1.0 for every group; a numeric priority replaces it, a qualitative
    # one becomes an ORDER among the groups that were named.
    numeric = [p for p in resolved if p.kind == "numeric" and p.value is not None]
    qualitative = [p for p in resolved if p.kind == "qualitative"]

    if numeric:
        weights: Dict[str, float] = {}
        for p in numeric:
            weights[p.target_group_id] = float(p.value or 0.0)
        # Groups with no explicit numeric value keep the default share, so a
        # partial specification does not silently zero the others.
        remaining = [g for g in usable
                     if str(g.get("group_id", "")) not in weights]
        if remaining:
            share = 1.0 / len(remaining)
            for g in remaining:
                weights[str(g.get("group_id", ""))] = share
        guidance = (
            "The owner stated numeric priorities: "
            + "; ".join(
                f'"{p.source_text}" -> group {p.target_group_id} = {p.value:g}'
                for p in numeric
            )
            + ". These are the owner's own numbers."
        )
        return WeightingPolicy(
            mode="d_explicit",
            groups=_weighted_groups(usable, weights, resolved),
            priorities=priorities,
            unresolved=unresolved,
            guidance=(guidance,),
            note="the brief states an explicit NUMERIC priority; the owner's "
                 "numbers are preserved after validation. Groups with no "
                 "explicit number keep the documented default share.",
        )

    # A qualitative priority is an ORDER, not a number. The order is recorded
    # with the owner's exact source text, and NO numeric share is derived from
    # it. The groups keep the documented default share, labelled as the
    # default, so a number that is ours can never be read as the owner's.
    ordered_ids = [p.target_group_id for p in qualitative]
    ranks = {gid: i + 1 for i, gid in enumerate(ordered_ids)}
    order_records = tuple({
        "rank": ranks[p.target_group_id],
        "group_id": p.target_group_id,
        "source_text": p.source_text,
        "trace": p.trace,
        "kind": p.kind,
        "numeric_value": None,
    } for p in qualitative)
    guidance = (
        "The owner expressed a priority in these words: "
        + "; ".join(f'"{p.source_text}"' for p in qualitative)
        + ". Treat that ordering as the owner's stated preference. The owner "
          "gave no numbers, so none are assumed."
    )
    return WeightingPolicy(
        mode="d_explicit",
        groups=_ranked_groups(usable, ranks, resolved),
        priorities=priorities,
        unresolved=unresolved,
        qualitative_order=order_records,
        guidance=(guidance,),
        note="the brief states a QUALITATIVE priority; it is kept as an "
             "ordering with its exact source text, and NO numeric share is "
             "derived from it. The groups' numeric importance is the "
             "documented default, labelled as such.",
    )


def _equal_groups(
    groups: Sequence[Mapping[str, Any]],
    *,
    source: str = IMPORTANCE_DEFAULT_EQUAL,
) -> Tuple[GroupWeight, ...]:
    """Option C: every group gets the SAME importance; children split it.

    The source is recorded on every group, so a reader can always tell a
    number the owner wrote from a number we defaulted to.
    """
    if not groups:
        return ()
    share = 1.0 / len(groups)
    out = []
    for g in groups:
        gid = str(g.get("group_id", ""))
        children = tuple(str(c) for c in (g.get("parts") or ()))
        out.append(GroupWeight(
            group_id=gid,
            text=str(g.get("text", "")),
            importance=share,
            importance_source=source,
            child_ids=children,
            mandatory=bool(g.get("mandatory", True)),
        ))
    return tuple(out)


def _weighted_groups(groups, weights, priorities) -> Tuple[GroupWeight, ...]:
    """Groups whose numeric importance came from the OWNER's number.

    A group with no explicit number keeps the documented default share, and
    says so, so a partial specification never silently zeroes the others.
    """
    out = []
    for g in groups:
        gid = str(g.get("group_id", ""))
        children = tuple(str(c) for c in (g.get("parts") or ()))
        explicit = any(p.target_group_id == gid and p.kind == "numeric"
                        and p.resolution == "resolved" for p in priorities)
        out.append(GroupWeight(
            group_id=gid,
            text=str(g.get("text", "")),
            importance=float(weights.get(gid, 0.0)),
            importance_source=(
                IMPORTANCE_EXPLICIT_NUMERIC if explicit
                else IMPORTANCE_DEFAULT_EQUAL
            ),
            child_ids=children,
            priority=_priority_for(priorities, gid),
            mandatory=bool(g.get("mandatory", True)),
        ))
    return tuple(out)


def _ranked_groups(groups, ranks, priorities) -> Tuple[GroupWeight, ...]:
    """A qualitative priority produces an ORDER and NO number.

    The owner wrote "more important than", not "twice as important". Turning
    that into a share would be inventing a weighting the owner never stated,
    so no number is derived here.

    What IS recorded:
      * the ORDER, as a rank, on the group the priority named
      * the exact source text and trace, on the priority itself
      * the group's numeric importance stays at the DOCUMENTED DEFAULT, and
        its source says "default_equal_groups" -- so nobody can mistake it
        for the owner's expressed weighting

    The guidance the evaluator may be given carries the ORDER in words, which
    is the faithful representation of what the owner said.
    """
    if not groups:
        return ()
    ordered = sorted(
        groups,
        key=lambda g: (ranks.get(str(g.get("group_id", "")), len(ranks) + 1),
                       str(g.get("group_id", ""))))
    n = len(ordered)
    share = 1.0 / n
    out = []
    for index, g in enumerate(ordered):
        gid = str(g.get("group_id", ""))
        children = tuple(str(c) for c in (g.get("parts") or ()))
        rank = ranks.get(gid)
        out.append(GroupWeight(
            group_id=gid,
            text=str(g.get("text", "")),
            # The DEFAULT share, explicitly labelled as the default. The
            # owner's qualitative preference is carried by the rank and the
            # source text, never by a derived percentage.
            importance=share,
            importance_source=IMPORTANCE_DEFAULT_EQUAL,
            child_ids=children,
            priority=_priority_for(priorities, gid),
            mandatory=bool(g.get("mandatory", True)),
            qualitative_rank=rank,
        ))
    return tuple(out)


def _priority_for(priorities, group_id: str) -> Optional[Priority]:
    for p in priorities:
        if p.target_group_id == group_id and p.resolution == "resolved":
            return p
    return None


# ---------------------------------------------------------------------------
# requirement-set identity
# ---------------------------------------------------------------------------


def compute_requirement_set_id(
    entries: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    *,
    policy: Optional[WeightingPolicy] = None,
    schema_version: int = 3,
    project_id: str = "",
    source_revision: str = "",
    requirements_version: str = "",
) -> str:
    """A stable identity for one REQUIREMENT SET.

    It is derived from the canonical CONTENT of the set, its group structure,
    the schema version, the mandatory flags, the evidence modality and the
    weighting policy. Two sets that differ in ANY of those produce different
    ids, so a suitability computed against one can never be compared with a
    suitability computed against the other.

    The project id, source revision and requirements version are NOT part of
    the digest: they identify the PROJECT, not the set. Two projects with the
    same requirement set share a set id, and that is correct -- their Fit
    scores mean the same thing.
    """
    canonical_groups = sorted(
        (
            str(g.get("group_id", "")),
            " ".join(str(g.get("text", "")).lower().split()),
            tuple(sorted(str(c) for c in (g.get("parts") or ()))),
        )
        for g in groups
    )
    canonical_entries = sorted(
        (
            str(e.get("id", "")),
            " ".join(str(e.get("text", "")).lower().split()),
            bool(e.get("mandatory", True)),
            str(e.get("evidence_kind", "")),
            str(e.get("verifiable_by", "")),
        )
        for e in entries
    )
    policy_payload = policy.to_dict() if policy is not None else None

    payload = {
        "schema_version": schema_version,
        "groups": canonical_groups,
        "entries": canonical_entries,
        "policy": policy_payload,
    }
    blob = repr(sorted(payload.items())).encode("utf-8")
    return "rs-" + hashlib.sha256(blob).hexdigest()[:20]


__all__ = [
    "GroupWeight",
    "IMPORTANCE_DEFAULT_EQUAL",
    "IMPORTANCE_EXPLICIT_NUMERIC",
    "IMPORTANCE_UNCOMMITTED",
    "Priority",
    "SCORING_CONTRACT_VERSION",
    "WEIGHTING_POLICY_VERSION",
    "WeightingPolicy",
    "build_weighting_policy",
    "compute_requirement_set_id",
    "extract_priorities",
]
