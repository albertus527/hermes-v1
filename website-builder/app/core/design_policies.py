"""Declarative project-dependency policy for the Website Builder (Batch D1).

D0 proved *which* design resources are configured and *whether* they are
available. D1 makes them retrievable. Neither answers a third question: what
may the builder add to an individual project, and under what justification.

This module answers it, and answers it **declaratively**. It states policy as
data. It selects nothing, installs nothing, and runs no selection logic, because
a heuristic that quietly picks a component library is precisely the
non-convergence this batch exists to prevent.

Three things live here and nothing else:

1. The dependency state ladder (``known`` -> ``available_for_project_on_demand``
   -> ``selected`` -> ``installed``) with the declared resting state for each
   declared dependency.
2. The justification gates that a later batch would have to satisfy before
   promoting a dependency past ``available_for_project_on_demand``, together
   with the simpler default each gate exists to protect.
3. The authority precedence between constraint sources, recorded once so a later
   batch inherits an ordering instead of inventing one.

**Nothing in this module reaches the filesystem, the network, or the manifest.**
It is pure policy data plus lookup helpers. That is what makes it safe to state
a preference without implementing it: a declared preference cannot be mistaken
for an executed one.

**State conflation is the bug this module is shaped around.** An on-demand
dependency that is absent is *not installed*, and it is also not "unavailable"
in the sense that an optional reference corpus is. Reporting a dependency's
resting state as a degradation teaches operators to ignore degradations. So
the ladder keeps the four states distinct, and
:func:`dependency_state` will not report ``selected`` or ``installed`` for
anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# The dependency state ladder
# ---------------------------------------------------------------------------

#: The four distinct dependency states, in ascending order of commitment.
#:
#: They are deliberately NOT collapsed. Each answers a different question, and
#: conflating any adjacent pair produces a false claim:
#:
#:   known
#:       We know this dependency exists and could be used. Nothing has been
#:       promised and nothing has been fetched.
#:   available_for_project_on_demand
#:       This dependency may be added to an individual project when that
#:       project's design requires it. This is the RESTING state for every
#:       project-scoped dependency, and it is also the correct state for one
#:       that has never been provisioned.
#:   selected
#:       A design decision has chosen this dependency for a specific project,
#:       against a written justification. Selection is a decision, not a
#:       heuristic.
#:   installed
#:       It is present in that project. Scoped to ONE project; never global.
DEPENDENCY_STATES: Tuple[str, ...] = (
    "known",
    "available_for_project_on_demand",
    "selected",
    "installed",
)

#: The state at which a dependency is merely a possibility. This is where every
#: project-scoped dependency starts and where all of them remain in D1.
STATE_AVAILABLE_ON_DEMAND = "available_for_project_on_demand"

#: A design decision has chosen this dependency for a specific project, against a
#: written justification. Selection is a decision, not a heuristic.
#:
#: D2 is the first batch that may produce this state, and it can produce ONLY
#: this one. Reaching ``installed`` requires a VERIFIED project-local install,
#: which is D3a's responsibility; naming the state here keeps the ladder's
#: vocabulary explicit rather than leaving each consumer to spell the string.
STATE_SELECTED = "selected"


# ---------------------------------------------------------------------------
# Justification gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyPolicy:
    """Declared policy for one project-scoped dependency.

    ``justification_gate`` is the requirement a later batch must satisfy before
    this dependency may leave ``available_for_project_on_demand``. It is a
    statement of policy, not a predicate: nothing in D1 evaluates it, because
    D1 has no design requirement to evaluate it against.

    ``simpler_default`` records what to reach for when the gate is NOT met.
    Recording it here matters more than it looks: it means the preference for
    the simple option is inherited by whoever implements selection, instead of
    being re-derived from scratch under time pressure.
    """

    resource_id: str
    state: str
    justification_gate: str
    simpler_default: str

    @property
    def is_installed(self) -> bool:
        """Always False in D1. Stated as a property so callers cannot read an
        ``installed`` claim off a policy object without going through here."""
        return self.state == "installed"

    def to_dict(self) -> Dict[str, Any]:
        """Serializable policy. Names and gate prose only -- no paths."""
        return {
            "state": self.state,
            "justification_gate": self.justification_gate,
            "simpler_default": self.simpler_default,
        }


#: Declared policy per project-scoped dependency.
#:
#: Keyed by manifest resource id so the policy and the declaration cannot drift
#: apart silently: a dependency with no policy entry simply has no declared
#: justification gate.
#:
#: Every entry sits at ``available_for_project_on_demand``. That is a fact
#: about D1 (nothing selects or installs), not an aspiration.
DESIGN_DEPENDENCY_POLICIES: Dict[str, DependencyPolicy] = {
    "gsap": DependencyPolicy(
        resource_id="gsap",
        state=STATE_AVAILABLE_ON_DEMAND,
        justification_gate=(
            "Only a meaningful timeline or complex animation requirement -- "
            "sequenced, interruptible, scroll-linked motion that CSS "
            "animation and the Web Animations API cannot express."
        ),
        simpler_default=(
            "CSS transitions, CSS keyframe animations, and native browser "
            "scrolling behaviour."
        ),
    ),
    "three": DependencyPolicy(
        resource_id="three",
        state=STATE_AVAILABLE_ON_DEMAND,
        justification_gate=(
            "Only a genuinely 3D or WebGL requirement -- an actual scene, "
            "camera, or shader -- rather than a flat composition that merely "
            "implies depth."
        ),
        simpler_default=(
            "CSS 3D transforms, perspective, and layered SVG for depth "
            "illusion."
        ),
    ),
    "lenis": DependencyPolicy(
        resource_id="lenis",
        state=STATE_AVAILABLE_ON_DEMAND,
        justification_gate=(
            "Only explicitly justified smooth-scroll behaviour, where "
            "momentum-based scrolling is itself a requirement of the design."
        ),
        simpler_default=(
            "Native browser scrolling with CSS scroll-snap and "
            "scroll-behavior."
        ),
    ),
    "shadcn": DependencyPolicy(
        resource_id="shadcn",
        state=STATE_AVAILABLE_ON_DEMAND,
        justification_gate=(
            "Not gated on capability: the registry is the default source for "
            "ordinary UI primitives, and components are copied into the "
            "project rather than added as a runtime dependency."
        ),
        simpler_default=(
            "Hand-authored project components, or plain HTML elements styled "
            "to match the accepted Design DNA."
        ),
    ),
}


#: Source preference between the two component registries, declared once.
#:
#: ``shadcn`` supplies ordinary, accessible primitives. ``twenty_first``
#: (``21st.dev``) supplies richer compositions. The preference is stated in
#: terms of the REQUIREMENT, not in terms of which library is easier to
#: install, so a later batch decides from the design requirement rather than
#: from what happens to be reachable.
COMPONENT_SOURCE_PREFERENCE: Dict[str, str] = {
    "ordinary_primitive": "shadcn",
    "richer_composition_required_by_design_dna": "twenty_first",
}

#: Prose form of the same preference, for prompts and documentation.
COMPONENT_SOURCE_RATIONALE = (
    "An ordinary UI primitive comes from shadcn. A richer composition is only "
    "considered from 21st.dev when the accepted Design DNA actually requires "
    "one. Prefer the simpler, closer-to-the-DNA option in every other case."
)


# ---------------------------------------------------------------------------
# Authority precedence
# ---------------------------------------------------------------------------

#: Declared precedence between constraint sources, highest authority first.
#:
#: Documentation-grade in D1: recorded so a later batch inherits one ordering
#: instead of inventing one, and so the design intent is explicit rather than
#: implied. Nothing here enforces this ordering yet.
#:
#: The load-bearing entry is the last one. **The Impeccable critic critiques; it
#: never overrides an explicit user or reference requirement.** A critic that
#: could outrank the user would be a critic that rewrites the brief, and that is
#: the failure mode this tuple exists to rule out.
DESIGN_AUTHORITY_PRECEDENCE: Tuple[str, ...] = (
    "product_and_safety_constraints",
    "explicit_user_and_reference_requirements",
    "accepted_design_dna",
    "design_guidance_and_resources",
    "inspiration",
    "impeccable_critic",
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def dependency_state(resource_id: str) -> str:
    """Return the declared dependency state for ``resource_id``.

    An id with no declared policy is ``"known"``: we know the name exists in
    the manifest, and we have committed to nothing about it. That is the honest
    answer for a dependency this module has never been told about, and it is
    strictly weaker than every state that implies a commitment.
    """
    policy = DESIGN_DEPENDENCY_POLICIES.get(resource_id)
    return policy.state if policy is not None else "known"


def justification_gate(resource_id: str) -> Optional[str]:
    """Return the declared justification gate, or None if none is declared."""
    policy = DESIGN_DEPENDENCY_POLICIES.get(resource_id)
    return policy.justification_gate if policy is not None else None


def simpler_default(resource_id: str) -> Optional[str]:
    """Return the option to prefer when the gate is not met."""
    policy = DESIGN_DEPENDENCY_POLICIES.get(resource_id)
    return policy.simpler_default if policy is not None else None


def is_globally_installed(resource_id: str) -> bool:
    """Always False.

    Stated explicitly because "is this installed?" is the question a dependency
    ladder invites callers to ask, and the answer for a project-scoped
    dependency is structurally never "yes": availability is a property of one
    project, and no batch in this pipeline provisions anything globally.
    """
    policy = DESIGN_DEPENDENCY_POLICIES.get(resource_id)
    return bool(policy is not None and policy.is_installed)


def authority_rank(source: str) -> Optional[int]:
    """Return the precedence rank of ``source``, or None if unrecognised.

    Lower is more authoritative. Unrecognised sources rank ``None`` rather than
    defaulting to a position: silently assigning an unknown constraint source
    to the bottom would make an unmodelled requirement into a suggestion.
    """
    try:
        return DESIGN_AUTHORITY_PRECEDENCE.index(source)
    except ValueError:
        return None


def policy_snapshot(manifest: Optional[Any] = None) -> Dict[str, Any]:
    """Bounded, serializable view of every declared policy.

    ``manifest`` is accepted so a caller can report policies for the resources
    the manifest actually declares, rather than for a hardcoded list here that
    could drift. Anything the manifest declares but this module has no policy
    for is reported at ``known`` -- the honest state for "no policy exists".
    """
    if manifest is None:
        resource_ids = list(DESIGN_DEPENDENCY_POLICIES)
    else:
        resource_ids = list(manifest.resources)

    snapshot: Dict[str, Any] = {
        "dependency_states": list(DEPENDENCY_STATES),
        "authority_precedence": list(DESIGN_AUTHORITY_PRECEDENCE),
        "component_source_preference": dict(COMPONENT_SOURCE_PREFERENCE),
        "policies": {},
    }
    for resource_id in sorted(resource_ids):
        policy = DESIGN_DEPENDENCY_POLICIES.get(resource_id)
        snapshot["policies"][resource_id] = (
            policy.to_dict()
            if policy is not None
            else {
                "state": "known",
                "justification_gate": None,
                "simpler_default": None,
            }
        )
    return snapshot


__all__ = [
    "COMPONENT_SOURCE_PREFERENCE",
    "COMPONENT_SOURCE_RATIONALE",
    "DEPENDENCY_STATES",
    "DESIGN_AUTHORITY_PRECEDENCE",
    "DESIGN_DEPENDENCY_POLICIES",
    "STATE_AVAILABLE_ON_DEMAND",
    "STATE_SELECTED",
    "DependencyPolicy",
    "authority_rank",
    "dependency_state",
    "is_globally_installed",
    "justification_gate",
    "policy_snapshot",
    "simpler_default",
]