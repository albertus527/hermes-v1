"""Design DNA -> design-resource selection (Batch D2).

D1 made design resources *retrievable*. That leaves the question a build
actually asks: **given the accepted Design DNA, which resources and project
dependencies are justified for THIS revision?**

D2 answers it, and answers it **declaratively and deterministically**. It
selects nothing, installs nothing, and runs no subprocess. That separation is
load-bearing: D3a is the first batch permitted to install, and it consumes
exactly what this module emits.

Four properties are load-bearing, each one a bug class being prevented:

**Every selection carries a closed-set reason class.** ``reason`` is one of the
:class:`SelectionReason` constants, each of which corresponds to a *known Design
DNA fact*. There is no free-text justification, so no "AI thinks GSAP would look
better" can ever enter the record. A reason that cannot be named from a DNA fact
is a reason the selection should not have happened.

**Authority precedence is enforced, not documented.** Resource guidance can
never outrank an explicit user requirement, an accepted reference, product
scope, or safety. A declared requirement RESERVES its dependency and every
other justification class for that dependency is suppressed. A critic or a
resource that could outrank the user would be rewriting the brief.

**Selection is a DECISION, never a heuristic.** Optional dependencies move
``available_for_project_on_demand`` -> ``selected`` and never further. D2 has
no mechanism to reach ``installed``; that transition requires D3a's verified
project-local install, and :meth:`DependencyDecision.selected` is the only
promotion this batch performs.

**Degrading, never inventing.** An unavailable optional reference is REJECTED
with an honest reason, never replaced by a synthesized summary of what such a
corpus "typically contains". An unavailable resource that the DNA does not need
is simply not selected -- absence of a dependency is the correct resting state,
not a degradation.

**What this batch deliberately does NOT do.** It does not install, run a
subprocess, make an LLM call, or wire resources into FRONTEND. It does not
rewrite the Design DNA schema: selection derives from the *existing* accepted
fields via :func:`derive_design_requirements`, and the one contract this module
adds (``DesignRequirementSet``) is a derived view, not a second design-state
document.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_capabilities import (
    STATUS_AVAILABLE,
    STATUS_NOT_INSTALLED,
    STATUS_UNAVAILABLE_OPTIONAL,
    STATUS_UNAVAILABLE_REQUIRED,
    DesignCapabilityReport,
)
from app.core.design_policies import (
    COMPONENT_SOURCE_PREFERENCE,
    DEPENDENCY_STATES,
    DESIGN_AUTHORITY_PRECEDENCE,
    STATE_AVAILABLE_ON_DEMAND,
    STATE_SELECTED,
    authority_rank,
    is_globally_installed,
    justification_gate,
    simpler_default,
)
from app.core.design_resources import (
    DesignResourceManifest,
    DesignResourceManifestError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selection reasons -- a CLOSED set
# ---------------------------------------------------------------------------

#: The only reasons a resource or dependency may be selected. Each corresponds
#: to a known, checkable Design DNA fact or an explicit declared requirement.
#:
#: A closed set is the point. An open "justification" string is where "the model
#: felt like it" enters a build record and becomes indistinguishable from a
#: requirement. Every value below is derived from a DNA field, so a reviewer can
#: re-derive it by hand and get the same answer.
class SelectionReason:
    """Closed vocabulary of selection reasons."""

    #: The Design DNA explicitly demands a real 3D/WebGL scene. Justifies
    #: ``three`` and nothing else.
    DNA_REQUIRES_3D = "dna_requires_3d"

    #: The Design DNA describes sequenced / scroll-linked / interruptible motion
    #: that CSS animation and the Web Animations API cannot express. Justifies
    #: ``gsap``.
    DNA_REQUIRES_COMPLEX_TIMELINE = "dna_requires_complex_timeline"

    #: The Design DNA describes momentum-based smooth scrolling as a requirement
    #: of the design rather than an effect. Justifies ``lenis``.
    DNA_REQUIRES_SMOOTH_SCROLL = "dna_requires_smooth_scroll"

    #: The Design DNA names ordinary, accessible UI primitives (button, dialog,
    #: tabs, card, form). Justifies ``shadcn``.
    DNA_REQUIRES_UI_PRIMITIVES = "dna_requires_ui_primitives"

    #: The Design DNA describes a composition richer than ordinary primitives.
    #: Only *considers* 21st.dev, and only when it is actually reachable --
    #: declared preference is not availability.
    DNA_REQUIRES_RICH_COMPOSITION = "dna_requires_rich_composition"

    #: The Design DNA calls for interactive / motion-heavy UI patterns. Justifies
    #: ``react_bits`` consideration when a real integration exists.
    DNA_REQUIRES_INTERACTIVE_PATTERNS = "dna_requires_interactive_patterns"

    #: The Design DNA calls for meaningful transition patterns. Justifies
    #: ``transitions_dev`` consideration when a real integration exists.
    DNA_REQUIRES_TRANSITION_PATTERNS = "dna_requires_transition_patterns"

    #: The Design DNA contains no motion/3D/scroll requirement. This is the
    #: DEFAULT, not a failure: most sites need no optional dependency at all.
    DNA_SIMPLE_MINIMAL = "dna_simple_minimal"

    #: An explicit user or reference requirement reserved this dependency. Ranks
    #: ABOVE every DNA-derived reason.
    USER_REQUIREMENT = "user_requirement"

    #: An explicit user requirement FORBADE optional dependencies.
    USER_FORBIDDEN = "user_forbidden"


#: Every reason a resource/dependency may carry. Exhaustive on purpose: a
#: selection carrying anything else is a bug, and tests assert membership.
SELECTION_REASONS: Tuple[str, ...] = (
    SelectionReason.DNA_REQUIRES_3D,
    SelectionReason.DNA_REQUIRES_COMPLEX_TIMELINE,
    SelectionReason.DNA_REQUIRES_SMOOTH_SCROLL,
    SelectionReason.DNA_REQUIRES_UI_PRIMITIVES,
    SelectionReason.DNA_REQUIRES_RICH_COMPOSITION,
    SelectionReason.DNA_REQUIRES_INTERACTIVE_PATTERNS,
    SelectionReason.DNA_REQUIRES_TRANSITION_PATTERNS,
    SelectionReason.DNA_SIMPLE_MINIMAL,
    SelectionReason.USER_REQUIREMENT,
    SelectionReason.USER_FORBIDDEN,
)

#: Which authority source each DNA-derived reason belongs to. Used to enforce
#: that resource guidance can never outrank an explicit user requirement.
#:
#: All DNA-derived reasons map to ``accepted_design_dna``; the user reasons map
#: to ``explicit_user_and_reference_requirements``, which ranks strictly higher.
REASON_AUTHORITY: Dict[str, str] = {
    SelectionReason.DNA_REQUIRES_3D: "accepted_design_dna",
    SelectionReason.DNA_REQUIRES_COMPLEX_TIMELINE: "accepted_design_dna",
    SelectionReason.DNA_REQUIRES_SMOOTH_SCROLL: "accepted_design_dna",
    SelectionReason.DNA_REQUIRES_UI_PRIMITIVES: "accepted_design_dna",
    SelectionReason.DNA_REQUIRES_RICH_COMPOSITION: "accepted_design_dna",
    SelectionReason.DNA_REQUIRES_INTERACTIVE_PATTERNS: "accepted_design_dna",
    SelectionReason.DNA_REQUIRES_TRANSITION_PATTERNS: "accepted_design_dna",
    SelectionReason.DNA_SIMPLE_MINIMAL: "accepted_design_dna",
    SelectionReason.USER_REQUIREMENT: "explicit_user_and_reference_requirements",
    SelectionReason.USER_FORBIDDEN: "explicit_user_and_reference_requirements",
}


# ---------------------------------------------------------------------------
# Static rejection warnings
# ---------------------------------------------------------------------------

#: Sanitized, path-free, content-free reasons a resource was rejected. These are
#: the ONLY strings that may appear in a rejection's ``reason_detail``.
WARNING_UNAVAILABLE = "resource is not available on this host; nothing was selected"
WARNING_DEGRADED = "resource degraded; selection is based on policy, not retrieved content"
WARNING_REFERENCE_NO_INTEGRATION = (
    "no retrievable integration exists for this reference on this host; "
    "no content was invented"
)
WARNING_USER_FORBADE = "an explicit user requirement forbids optional dependencies here"
WARNING_ALREADY_RESERVED = (
    "an explicit user requirement already reserved this dependency; "
    "a weaker justification cannot add it"
)


# ---------------------------------------------------------------------------
# Design requirements -- a DERIVED view of the accepted Design DNA
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignRequirementSet:
    """What the accepted Design DNA *actually demands*, derived deterministically.

    A derived view, **not** a second design-state document and not a schema
    rewrite. Every field is a boolean answer to one closed question, computed by
    :func:`derive_design_requirements` from the existing accepted DNA keys. The
    persisted ``design-dna.json`` remains authoritative and unchanged; this is
    the projection selection reads so selection does not re-parse free prose.

    The DNA contract is NOT widened. No new required key is added to
    :data:`app.core.design_dna.CANONICAL_DESIGN_DNA_KEYS`, and a DNA document
    with none of these signals still loads and still validates exactly as
    before. An absent signal is False (nothing demanded), which is the honest
    reading of a document that does not mention motion.
    """

    requires_3d: bool = False
    requires_complex_timeline: bool = False
    requires_smooth_scroll: bool = False
    requires_ui_primitives: bool = False
    requires_rich_composition: bool = False
    requires_interactive_patterns: bool = False
    requires_transition_patterns: bool = False

    @property
    def is_minimal(self) -> bool:
        """True when the DNA demands no optional dependency whatsoever."""
        return not any(self.to_dict().values())

    def to_dict(self) -> Dict[str, bool]:
        return {
            "requires_3d": self.requires_3d,
            "requires_complex_timeline": self.requires_complex_timeline,
            "requires_smooth_scroll": self.requires_smooth_scroll,
            "requires_ui_primitives": self.requires_ui_primitives,
            "requires_rich_composition": self.requires_rich_composition,
            "requires_interactive_patterns": self.requires_interactive_patterns,
            "requires_transition_patterns": self.requires_transition_patterns,
        }


# ---------------------------------------------------------------------------
# Selection result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyDecision:
    """The state machine outcome for ONE project dependency.

    ``state`` is restricted to ``known`` / ``available_for_project_on_demand`` /
    ``selected``. D2 can promote a dependency **to** ``selected`` and never to
    ``installed``: an installed claim requires a VERIFIED project-local install,
    which is D3a's job and is not performed here. ``is_installed`` is therefore
    structurally always False in D2, stated as a property so a caller cannot read
    an installed claim off this object without going through here.
    """

    resource_id: str
    state: str
    reason: str
    detail: Optional[str] = None

    @property
    def is_installed(self) -> bool:
        return False

    @property
    def is_selected(self) -> bool:
        return self.state == STATE_SELECTED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "is_installed": self.is_installed,
        }


@dataclass(frozen=True)
class SelectionEntry:
    """One selected (or rejected) design resource, with its explicit reason."""

    resource_id: str
    resource_kind: str
    selected: bool
    reason: str
    component_source: Optional[str] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "selected": self.selected,
            "reason": self.reason,
            "component_source": self.component_source,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DesignResourceSelectionPlan:
    """The canonical, serializable D2 output.

    Deterministic: every list is built in a fixed order (manifest/resource order
    for selections, sorted for rejections), so the same DNA and the same
    capability report always serialize to identical bytes. There is no set or
    dict-iteration order in the output path.
    """

    requested_resources: Tuple[str, ...]
    selected_resources: Tuple[SelectionEntry, ...]
    rejected_resources: Tuple[SelectionEntry, ...]
    dependency_decisions: Tuple[DependencyDecision, ...]
    component_sources: Dict[str, str]
    reasons: Dict[str, str]
    degraded: bool
    warnings: Tuple[str, ...]

    @property
    def selected_ids(self) -> Tuple[str, ...]:
        return tuple(entry.resource_id for entry in self.selected_resources)

    @property
    def is_minimal(self) -> bool:
        """True when no optional dependency was selected."""
        return not any(d.is_selected for d in self.dependency_decisions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_resources": list(self.requested_resources),
            "selected_resources": [e.to_dict() for e in self.selected_resources],
            "rejected_resources": [e.to_dict() for e in self.rejected_resources],
            "dependency_decisions": [d.to_dict() for d in self.dependency_decisions],
            "component_sources": dict(self.component_sources),
            "reasons": dict(self.reasons),
            "degraded": self.degraded,
            "warnings": list(self.warnings),
        }



# ---------------------------------------------------------------------------
# Requirement derivation -- reading the EXISTING Design DNA contract
# ---------------------------------------------------------------------------
#
# Design DNA is owned by FRONTEND and validated by app.core.design_dna. This
# module does not redefine, extend, or version that schema. It derives one
# boolean per closed question from the fields the contract ALREADY has, and it
# only ever READS them.
#
# The signals are grouped by which canonical Design DNA key they live under:
#
#   motion            -> complex timeline / smooth scroll / transitions /
#                        interactive patterns
#   layout            -> 3d (and the explicit "depth only" counter-signal)
#   typography, palette, spacing, primary_cta -> ui primitives
#
# Matching is deliberately CONSERVATIVE and, for 3D, ASYMMETRIC: depth-sounding
# language selects ``three`` only when it names a real scene, camera, WebGL or
# shader. "Decorative depth", "CSS perspective", and "layered shadow" are
# explicitly COUNTER-signals, because the whole point of the three gate is that a
# flat composition implying depth must not drag in a WebGL runtime.


def _text_blob(value: Any, depth: int = 0) -> str:
    """Flatten a Design DNA value to lowercase text, bounded in depth.

    Bounded so a deeply nested or huge document cannot turn derivation into
    unbounded work. Design DNA is small, but "bounded by default" is the posture
    this module takes everywhere.
    """
    if depth > 4:
        return ""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, Mapping):
        parts = []
        for key in sorted(value):
            parts.append(str(key).lower())
            parts.append(_text_blob(value[key], depth + 1))
        return " ".join(parts)
    if isinstance(value, (list, tuple)):
        return " ".join(_text_blob(item, depth + 1) for item in value)
    if isinstance(value, bool) or value is None:
        return ""
    return str(value).lower()


def _motion_text(design_dna: Mapping[str, Any]) -> str:
    return _text_blob(design_dna.get("motion"))


def _layout_text(design_dna: Mapping[str, Any]) -> str:
    return _text_blob(design_dna.get("layout"))


def _has_any(text: str, needles: Sequence[str]) -> bool:
    return any(needle in text for needle in needles)


#: Real 3D / WebGL demands. A scene, a camera, a shader, an actual canvas the
#: user interacts with in three dimensions.
_3D_POSITIVE = (
    "webgl",
    "three.js",
    "threejs",
    "shader",
    "3d scene",
    "3d model",
    "3d object",
    "raycast",
    "orbit control",
    "globe",
    "particle system",
    "canvas render",
)

#: Depth language that is emphatically NOT a 3D requirement. These are the words
#: that make a naive keyword scan select Three.js for a flat page with a
#: perspective tilt, which is exactly the mistake the gate exists to prevent.
_3D_NEGATIVE = (
    "css perspective",
    "perspective only",
    "decorative depth",
    "fake depth",
    "layered shadow",
    "drop shadow",
    "depth illusion",
    "parallax layer",
    "subtle depth",
)

#: Sequenced, interruptible, scroll-linked motion CSS cannot express.
_TIMELINE_POSITIVE = (
    "timeline",
    "sequenced",
    "sequence of",
    "scroll-linked",
    "scroll linked",
    "scrub",
    "keyframe sequence",
    "interruptible",
    "parallax scroll",
    "staggered sequence",
    "orchestrated",
    "storyboard",
)

#: Momentum-based scrolling as a requirement of the design.
_SMOOTH_SCROLL_POSITIVE = (
    "smooth scroll",
    "smooth-scroll",
    "momentum scroll",
    "momentum-scroll",
    "inertial scroll",
    "lenis",
    "butterfly scroll",
)

#: Ordinary, accessible UI primitives.
_UI_PRIMITIVE_POSITIVE = (
    "button",
    "dialog",
    "modal",
    "tab",
    "card",
    "form",
    "input",
    "select",
    "checkbox",
    "tooltip",
    "accordion",
    "breadcrumb",
    "alert",
    "switch",
    "radio",
    "combobox",
    "popover",
    "drawer",
    "menu",
)

#: A composition richer than ordinary primitives.
_RICH_COMPOSITION_POSITIVE = (
    "showcase",
    "bento",
    "immersive composition",
    "rich composition",
    "complex layout",
    "magazine layout",
    "editorial layout",
    "asymmetric grid",
    "mosaic",
    "orchestrated layout",
    "data-dense",
    "animated hero composition",
)

#: Interactive / motion-heavy UI patterns.
_INTERACTIVE_POSITIVE = (
    "microinteraction",
    "micro-interaction",
    "interactive",
    "gesture",
    "drag",
    "swipe",
    "cursor-follow",
    "cursor follow",
    "magnetic",
    "mousemove",
    "hover state",
)

#: Meaningful transition patterns beyond a plain hover/fade.
_TRANSITION_POSITIVE = (
    "page transition",
    "route transition",
    "shared element",
    "flip transition",
    "morph",
    "enter transition",
    "exit transition",
    "view transition",
)


def derive_design_requirements(
    design_dna: Optional[Mapping[str, Any]],
    *,
    user_requirements: Optional[Mapping[str, Any]] = None,
) -> DesignRequirementSet:
    """Derive the closed requirement set from the accepted Design DNA.

    ``user_requirements`` is an OPTIONAL mapping of explicitly declared user or
    reference requirements. It is applied with the highest authority: a truthy
    value for a signal FORCES that signal on, and the string ``"forbid"`` /
    ``"none"`` for the motion family forces every optional dependency off.

    No LLM, no network, no prose parsing beyond substring checks against the
    closed needle lists above. Deterministic: the same DNA yields the same set.
    """
    dna = design_dna if isinstance(design_dna, Mapping) else {}
    user = user_requirements if isinstance(user_requirements, Mapping) else {}

    motion = _motion_text(dna)
    layout = _layout_text(dna)
    body = _text_blob(dna)

    forbidden = _is_forbidden(user)

    def forced(name: str) -> bool:
        value = user.get(name)
        return value is True or (isinstance(value, str) and value.strip().lower() not in ("", "false", "no", "none"))

    # A field-level counter-signal is a VETO, not a tie. It is checked against
    # both the layout field and the whole document so "decorative depth only, no
    # real 3d scene" cannot be re-triggered by the very phrase it is negating:
    # a veto that another field can overrule is not a veto.
    depth_vetoed = _has_any(layout, _3D_NEGATIVE) or _has_any(body, _3D_NEGATIVE)
    requires_3d = (not forbidden) and (
        forced("requires_3d")
        or (
            not depth_vetoed
            and (_has_any(layout, _3D_POSITIVE) or _has_any(body, _3D_POSITIVE))
        )
    )
    requires_timeline = (not forbidden) and (
        forced("requires_complex_timeline")
        or _has_any(motion, _TIMELINE_POSITIVE)
    )
    requires_smooth_scroll = (not forbidden) and (
        forced("requires_smooth_scroll")
        or _has_any(motion, _SMOOTH_SCROLL_POSITIVE)
    )
    requires_interactive = (not forbidden) and (
        forced("requires_interactive_patterns")
        or _has_any(motion, _INTERACTIVE_POSITIVE)
    )
    requires_transitions = (not forbidden) and (
        forced("requires_transition_patterns")
        or _has_any(motion, _TRANSITION_POSITIVE)
    )
    requires_primitives = (not forbidden) and (
        forced("requires_ui_primitives") or _has_any(body, _UI_PRIMITIVE_POSITIVE)
    )
    requires_rich = (not forbidden) and (
        forced("requires_rich_composition") or _has_any(body, _RICH_COMPOSITION_POSITIVE)
    )

    return DesignRequirementSet(
        requires_3d=requires_3d,
        requires_complex_timeline=requires_timeline,
        requires_smooth_scroll=requires_smooth_scroll,
        requires_ui_primitives=requires_primitives,
        requires_rich_composition=requires_rich,
        requires_interactive_patterns=requires_interactive,
        requires_transition_patterns=requires_transitions,
    )


def _is_forbidden(user: Mapping[str, Any]) -> bool:
    """Whether the user explicitly forbade optional dependencies.

    Recognises ``forbid`` / ``forbidden`` / ``none`` / ``false`` on any of the
    opt-out keys, because a user who says "no libraries" should never have one
    selected regardless of what the DNA implies.
    """
    for key in ("forbid", "forbids", "forbidden", "optional_dependencies", "no_optional"):
        value = user.get(key)
        if isinstance(value, str):
            return value.strip().lower() in ("forbid", "forbidden", "true", "yes", "1")
        if isinstance(value, bool):
            return value
    return False



# ---------------------------------------------------------------------------
# Dependency selection policy -- TABLE-DRIVEN
# ---------------------------------------------------------------------------
#
# Which Design DNA signal justifies which dependency, and with which reason.
#
# Table-driven rather than a chain of conditionals: adding a dependency is a row
# here, and "which signal selects what" is readable in one place instead of
# spread across an if-ladder that silently grows a new branch per dependency.
#
# Each row is a GATE, not a command. A dependency with no satisfied gate is
# rejected with the reason of its simplest default, which is what makes the
# resting state "no optional dependency" the answer for a minimal design.

#: dependency id -> (requirement attribute, reason, authority note).
DEPENDENCY_SELECTION_RULES: Dict[str, Tuple[str, str]] = {
    "three": ("requires_3d", SelectionReason.DNA_REQUIRES_3D),
    "gsap": ("requires_complex_timeline", SelectionReason.DNA_REQUIRES_COMPLEX_TIMELINE),
    "lenis": ("requires_smooth_scroll", SelectionReason.DNA_REQUIRES_SMOOTH_SCROLL),
    "shadcn": ("requires_ui_primitives", SelectionReason.DNA_REQUIRES_UI_PRIMITIVES),
}

#: Reference corpora, which are CONSIDERED rather than installed. A reference is
#: never a project dependency; it is design guidance selected for reading.
REFERENCE_SELECTION_RULES: Dict[str, Tuple[str, str]] = {
    "twenty_first": ("requires_rich_composition", SelectionReason.DNA_REQUIRES_RICH_COMPOSITION),
    "react_bits": ("requires_interactive_patterns", SelectionReason.DNA_REQUIRES_INTERACTIVE_PATTERNS),
    "transitions_dev": ("requires_transition_patterns", SelectionReason.DNA_REQUIRES_TRANSITION_PATTERNS),
    "refero": ("requires_rich_composition", SelectionReason.DNA_REQUIRES_RICH_COMPOSITION),
}

#: The bounded-design-guidance resource. Selected when the caller asks for it and
#: it is AVAILABLE; retrieval itself is D1's job and is invoked separately.
GUIDANCE_RESOURCE = "ui_ux_pro_max"

#: The critic resource. SCHEMA/CAPABILITY ONLY in this batch: D2 never selects it
#: into a build, because there is no critic loop until D3b and selecting it now
#: would imply a capability that does not exist.
CRITIC_RESOURCE = "impeccable"


@dataclass(frozen=True)
class _RuleOutcome:
    """Internal: the resolved verdict for one resource."""

    selected: bool
    reason: str
    detail: Optional[str] = None
    component_source: Optional[str] = None


def _capability_status(
    capability_report: Optional[DesignCapabilityReport], resource_id: str
) -> Optional[str]:
    if capability_report is None:
        return None
    capability = capability_report.resources.get(resource_id)
    return capability.status if capability is not None else None


def _is_reachable(
    capability_report: Optional[DesignCapabilityReport], resource_id: str
) -> bool:
    """Whether a resource can actually be READ on this host right now.

    ``available`` is the honest signal. A deferred reference (no local, verified
    content) is NOT reachable, so it degrades honestly instead of pretending.
    """
    if capability_report is None:
        # With no capability report we cannot prove reachability. Assume NOT
        # reachable for references (never invent), but allow on-demand
        # dependencies, whose resting state is already "available on demand".
        return False
    capability = capability_report.resources.get(resource_id)
    if capability is None:
        return False
    return bool(capability.available)


def _reserved_by_user(
    requirements: DesignRequirementSet,
    user_requirements: Mapping[str, Any],
    resource_id: str,
) -> Optional[str]:
    """Return the user reason if the user explicitly RESERVED ``resource_id``.

    Reservation is the precedence mechanism: an explicit requirement outranks
    every DNA-derived justification, and it also outranks a *weaker* claim on a
    dependency the user already spoke for.
    """
    reserved = user_requirements.get("reserved_dependencies")
    if isinstance(reserved, (list, tuple, set)) and resource_id in reserved:
        return SelectionReason.USER_REQUIREMENT
    return None


def _select_dependency(
    resource_id: str,
    requirements: DesignRequirementSet,
    user_requirements: Mapping[str, Any],
    forbidden: bool,
) -> _RuleOutcome:
    """Evaluate one dependency against the derived requirements."""
    rule = DEPENDENCY_SELECTION_RULES.get(resource_id)
    if rule is None:
        return _RuleOutcome(False, SelectionReason.DNA_SIMPLE_MINIMAL)

    if forbidden:
        return _RuleOutcome(
            False, SelectionReason.USER_FORBIDDEN, detail=WARNING_USER_FORBADE
        )

    attribute, reason = rule
    if getattr(requirements, attribute):
        return _RuleOutcome(True, reason)

    return _RuleOutcome(False, SelectionReason.DNA_SIMPLE_MINIMAL)


def _select_reference(
    resource_id: str,
    requirements: DesignRequirementSet,
    capability_report: Optional[DesignCapabilityReport],
    forbidden: bool,
) -> _RuleOutcome:
    """Evaluate one reference corpus.

    A reference is selected for READING only when the DNA justifies it AND a
    real integration is reachable. When it is not reachable the reference is
    rejected with the honest no-integration warning -- never replaced by a
    synthesized summary.
    """
    rule = REFERENCE_SELECTION_RULES.get(resource_id)
    if rule is None:
        return _RuleOutcome(False, SelectionReason.DNA_SIMPLE_MINIMAL)

    if forbidden:
        return _RuleOutcome(
            False, SelectionReason.USER_FORBIDDEN, detail=WARNING_USER_FORBADE
        )

    attribute, reason = rule
    if not getattr(requirements, attribute):
        return _RuleOutcome(False, SelectionReason.DNA_SIMPLE_MINIMAL)

    if not _is_reachable(capability_report, resource_id):
        return _RuleOutcome(
            False,
            reason,
            detail=WARNING_REFERENCE_NO_INTEGRATION,
        )

    return _RuleOutcome(True, reason)


def _component_source_for(
    resource_id: str, requirements: DesignRequirementSet
) -> Optional[str]:
    """The declared component-source preference for this selection.

    For ``shadcn``/``twenty_first`` this is the declared preference keyed off the
    REQUIREMENT, not off which library is easier to install.
    """
    if resource_id == "shadcn":
        return COMPONENT_SOURCE_PREFERENCE["ordinary_primitive"]
    if resource_id == "twenty_first":
        return COMPONENT_SOURCE_PREFERENCE["richer_composition_required_by_design_dna"]
    return None


def select_design_resources(
    design_dna: Optional[Mapping[str, Any]],
    *,
    requested: Sequence[str],
    manifest: Optional[DesignResourceManifest] = None,
    capability_report: Optional[DesignCapabilityReport] = None,
    user_requirements: Optional[Mapping[str, Any]] = None,
    include_guidance: bool = False,
) -> DesignResourceSelectionPlan:
    """Select design resources and dependencies justified by the accepted DNA.

    ``requested`` is processed in the given order; selections preserve that
    order and rejections are sorted for determinism. ``include_guidance``
    optionally selects ``ui_ux_pro_max`` (bounded design guidance) when it is
    actually available.

    **This batch never installs anything.** Every dependency decision is at most
    ``selected``; ``installed`` is unreachable here by construction.

    Raises :class:`DesignResourceManifestError` only for an undeclared resource
    id, matching D0/D1: a typo in a resource name should be loud.
    """
    if manifest is None:
        from app.core.design_resources import load_design_resource_manifest

        manifest = load_design_resource_manifest()

    user = user_requirements if isinstance(user_requirements, Mapping) else {}
    requirements = derive_design_requirements(design_dna, user_requirements=user)
    forbidden = _is_forbidden(user)

    selected: List[SelectionEntry] = []
    rejected: List[SelectionEntry] = []
    decisions: List[DependencyDecision] = []
    reasons: Dict[str, str] = {}
    component_sources: Dict[str, str] = {}
    warnings: List[str] = []
    degraded = False

    ordered: List[str] = []
    seen = set()
    for resource_id in requested:
        if resource_id in seen:
            continue
        seen.add(resource_id)
        ordered.append(resource_id)

    if include_guidance and GUIDANCE_RESOURCE not in seen:
        ordered.append(GUIDANCE_RESOURCE)

    for resource_id in ordered:
        resource = manifest.get(resource_id)  # raises on undeclared id

        # --- The critic is never selected into a build in this batch -------
        if resource_id == CRITIC_RESOURCE:
            entry = SelectionEntry(
                resource_id=resource_id,
                resource_kind=resource.kind,
                selected=False,
                reason=SelectionReason.DNA_SIMPLE_MINIMAL,
                detail="critic schema/capability only; not wired into the build",
            )
            rejected.append(entry)
            reasons[resource_id] = entry.reason
            continue

        kind = resource.kind
        if kind in ("registry", "npm_optional"):
            outcome = _select_dependency(resource_id, requirements, user, forbidden)
        elif kind == "reference":
            outcome = _select_reference(
                resource_id, requirements, capability_report, forbidden
            )
        elif kind == "skill":
            if resource_id == GUIDANCE_RESOURCE:
                if _is_reachable(capability_report, resource_id):
                    outcome = _RuleOutcome(True, SelectionReason.DNA_SIMPLE_MINIMAL)
                else:
                    outcome = _RuleOutcome(
                        False,
                        SelectionReason.DNA_SIMPLE_MINIMAL,
                        detail=WARNING_UNAVAILABLE,
                    )
            else:
                outcome = _RuleOutcome(
                    False,
                    SelectionReason.DNA_SIMPLE_MINIMAL,
                    detail=WARNING_UNAVAILABLE,
                )
        else:
            outcome = _RuleOutcome(False, SelectionReason.DNA_SIMPLE_MINIMAL)

        # --- An explicit user reservation wins over a weaker DNA reason ----
        #
        # A blanket ``forbid`` is evaluated FIRST and is not overridable: a
        # user who says "no libraries at all" must not have one added by a
        # narrower allowance recorded in the same requirements mapping.
        reserved = (
            None
            if forbidden
            else _reserved_by_user(requirements, user, resource_id)
        )
        if reserved is not None:
            if not outcome.selected:
                # Weaker claim suppressed in favour of the explicit reservation.
                warnings.append(WARNING_ALREADY_RESERVED)
            outcome = _RuleOutcome(True, reserved, detail=outcome.detail)

        # A reference that is genuinely absent must SAY SO, whether or not a
        # selection rule was reached. "Not selected because the DNA did not ask"
        # and "not selected because there is no local verified content" are
        # materially different statements for an operator reading the plan, and a
        # silent rejection looks like the first when it is the second.
        if (
            kind == "reference"
            and not outcome.selected
            and outcome.detail is None
            and not _is_reachable(capability_report, resource_id)
        ):
            outcome = _RuleOutcome(
                False, outcome.reason, detail=WARNING_REFERENCE_NO_INTEGRATION
            )
            degraded = True

        component_source = (
            _component_source_for(resource_id, requirements)
            if outcome.selected
            else None
        )

        entry = SelectionEntry(
            resource_id=resource_id,
            resource_kind=kind,
            selected=outcome.selected,
            reason=outcome.reason,
            component_source=component_source,
            detail=outcome.detail,
        )
        reasons[resource_id] = outcome.reason

        if outcome.selected:
            selected.append(entry)
            if component_source:
                component_sources[resource_id] = component_source
            # A selected optional dependency records a state decision.
            if kind in ("registry", "npm_optional"):
                decisions.append(
                    DependencyDecision(
                        resource_id=resource_id,
                        state=STATE_SELECTED,
                        reason=outcome.reason,
                        detail=outcome.detail,
                    )
                )
        else:
            rejected.append(entry)
            if outcome.detail:
                if outcome.detail == WARNING_UNAVAILABLE and kind != "skill":
                    degraded = True
                if outcome.detail in (
                    WARNING_UNAVAILABLE,
                    WARNING_REFERENCE_NO_INTEGRATION,
                    WARNING_DEGRADED,
                ):
                    degraded = True
            if kind in ("registry", "npm_optional"):
                decisions.append(
                    DependencyDecision(
                        resource_id=resource_id,
                        state=STATE_AVAILABLE_ON_DEMAND,
                        reason=outcome.reason,
                        detail=outcome.detail,
                    )
                )

    plan = DesignResourceSelectionPlan(
        requested_resources=tuple(ordered),
        selected_resources=tuple(selected),
        rejected_resources=tuple(sorted(rejected, key=lambda e: e.resource_id)),
        dependency_decisions=tuple(decisions),
        component_sources=dict(sorted(component_sources.items())),
        reasons=dict(sorted(reasons.items())),
        degraded=degraded,
        warnings=tuple(dict.fromkeys(warnings)),
    )
    logger.info(
        "Design selection: selected=%s rejected=%d degraded=%s",
        ",".join(plan.selected_ids) or "-",
        len(rejected),
        degraded,
    )
    return plan


__all__ = [
    "COMPONENT_SOURCE_PREFERENCE",
    "DEPENDENCY_STATES",
    "DESIGN_AUTHORITY_PRECEDENCE",
    "REASON_AUTHORITY",
    "SELECTION_REASONS",
    "STATE_AVAILABLE_ON_DEMAND",
    "STATE_SELECTED",
    "DesignRequirementSet",
    "DesignResourceSelectionPlan",
    "DependencyDecision",
    "SelectionEntry",
    "SelectionReason",
    "WARNING_ALREADY_RESERVED",
    "WARNING_DEGRADED",
    "WARNING_REFERENCE_NO_INTEGRATION",
    "WARNING_UNAVAILABLE",
    "WARNING_USER_FORBADE",
    "CRITIC_RESOURCE",
    "DEPENDENCY_SELECTION_RULES",
    "GUIDANCE_RESOURCE",
    "REFERENCE_SELECTION_RULES",
    "derive_design_requirements",
    "select_design_resources",
]
