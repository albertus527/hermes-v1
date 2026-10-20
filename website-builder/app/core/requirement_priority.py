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


@dataclass(frozen=True)
class GroupWeight:
    """The weight of one parent requirement group.

    Under the Option C default every group carries the SAME importance and
    its children divide it equally. Under Option D, a group named by an
    explicit priority carries that priority instead -- and the children STILL
    divide it equally, so a group never gains influence merely by being
    split.
    """

    group_id: str
    text: str
    importance: float
    child_ids: Tuple[str, ...] = ()
    priority: Optional[Priority] = None
    mandatory: bool = True

    def child_weight(self) -> float:
        """The weight of ONE child of this group."""
        if not self.child_ids:
            return 0.0
        return self.importance / len(self.child_ids)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "text": self.text,
            "importance": self.importance,
            "child_ids": list(self.child_ids),
            "child_weight": self.child_weight(),
            "mandatory": self.mandatory,
            "priority": self.priority.to_dict() if self.priority else None,
        }


@dataclass(frozen=True)
class WeightingPolicy:
    """The complete weighting decision for one requirement set.

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

    @property
    def active(self) -> bool:
        return self.mode in ("c_default", "d_explicit")

    @property
    def has_explicit_priority(self) -> bool:
        return any(p.kind != "unresolved" and p.explicit
                   for p in self.priorities)

    def group(self, group_id: str) -> Optional[GroupWeight]:
        for g in self.groups:
            if g.group_id == group_id:
                return g
        return None

    def weight_of(self, entry_id: str) -> float:
        """The weight of one requirement entry, by its id."""
        for g in self.groups:
            if entry_id in g.child_ids:
                return g.child_weight()
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "policy_version": self.policy_version,
            "active": self.active,
            "has_explicit_priority": self.has_explicit_priority,
            "groups": [g.to_dict() for g in self.groups],
            "priorities": [p.to_dict() for p in self.priorities],
            "unresolved": [p.to_dict() for p in self.unresolved],
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
        return WeightingPolicy(
            mode="d_explicit",
            groups=_weighted_groups(usable, weights, resolved),
            priorities=priorities,
            unresolved=unresolved,
            note="the brief states an explicit numeric priority; the numbers "
                 "are preserved after validation",
        )

    # A qualitative priority is an ORDER, not a number. It is represented as a
    # rank: the named group comes first, the rest keep the default order. NO
    # percentage is invented -- the ordering is the whole content.
    ordered_ids = [p.target_group_id for p in qualitative]
    ranks = {gid: i + 1 for i, gid in enumerate(ordered_ids)}
    return WeightingPolicy(
        mode="d_explicit",
        groups=_ranked_groups(usable, ranks, resolved),
        priorities=priorities,
        unresolved=unresolved,
        note="the brief states a qualitative priority; it is kept as an "
             "ordering and NO numeric share is invented",
    )


def _equal_groups(groups: Sequence[Mapping[str, Any]]) -> Tuple[GroupWeight, ...]:
    """Option C: every group gets the SAME importance; children split it."""
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
            child_ids=children,
            mandatory=bool(g.get("mandatory", True)),
        ))
    return tuple(out)


def _weighted_groups(groups, weights, priorities) -> Tuple[GroupWeight, ...]:
    out = []
    for g in groups:
        gid = str(g.get("group_id", ""))
        children = tuple(str(c) for c in (g.get("parts") or ()))
        out.append(GroupWeight(
            group_id=gid,
            text=str(g.get("text", "")),
            importance=float(weights.get(gid, 0.0)),
            child_ids=children,
            priority=_priority_for(priorities, gid),
            mandatory=bool(g.get("mandatory", True)),
        ))
    return tuple(out)


def _ranked_groups(groups, ranks, priorities) -> Tuple[GroupWeight, ...]:
    """A qualitative priority is a RANK, expressed as a decreasing share.

    The share is a monotone function of the rank so that the ordering is
    honoured, and the shares still sum to 1 so the scale is unchanged. The
    NUMBERS are derived from the ORDER the owner stated; they are not claims
    about how much more the owner meant. The policy's note says so, and the
    raw source text is kept on every priority.
    """
    if not groups:
        return ()
    ordered = sorted(
        groups,
        key=lambda g: (ranks.get(str(g.get("group_id", "")), len(ranks) + 1),
                       str(g.get("group_id", ""))))
    n = len(ordered)
    # A qualitative priority is an ORDER. It is expressed as a strictly
    # decreasing share that still sums to 1, so the scale is unchanged and the
    # ordering is honoured.
    #
    # The named groups take the front of the order, and the share falls off
    # geometrically so the first named group is unambiguously the most
    # important and the rest follow. The NUMBERS are derived from the ORDER
    # the owner stated; they are not claims about how much more the owner
    # meant, and the priority record keeps the raw source text.
    weights: Dict[str, float] = {}
    if n == 1:
        weights[str(ordered[0].get("group_id", ""))] = 1.0
    else:
        raw = [0.6**i for i in range(n)]
        total = sum(raw)
        for g, value in zip(ordered, raw):
            weights[str(g.get("group_id", ""))] = value / total
    out = []
    for g in ordered:
        gid = str(g.get("group_id", ""))
        children = tuple(str(c) for c in (g.get("parts") or ()))
        out.append(GroupWeight(
            group_id=gid,
            text=str(g.get("text", "")),
            importance=weights.get(gid, 0.0),
            child_ids=children,
            priority=_priority_for(priorities, gid),
            mandatory=bool(g.get("mandatory", True)),
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
    "Priority",
    "SCORING_CONTRACT_VERSION",
    "WEIGHTING_POLICY_VERSION",
    "WeightingPolicy",
    "build_weighting_policy",
    "compute_requirement_set_id",
    "extract_priorities",
]
