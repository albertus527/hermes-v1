"""Project lifecycle state machine for Website Builder R1.

Enforces valid transitions in deterministic application code.
No LLM has lifecycle authority.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class ProjectLifecycle(str, Enum):
    DISCOVERING = "DISCOVERING"
    WAITING_INPUT = "WAITING_INPUT"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PREVIEW_READY = "PREVIEW_READY"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    PUBLISHING = "PUBLISHING"
    LIVE = "LIVE"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELED = "CANCELED"


# Valid transitions: current -> allowed next states
_TRANSITIONS: Dict[ProjectLifecycle, Set[ProjectLifecycle]] = {
    ProjectLifecycle.DISCOVERING: {
        ProjectLifecycle.WAITING_INPUT,
        ProjectLifecycle.READY,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
        ProjectLifecycle.FAILED,
    },
    ProjectLifecycle.WAITING_INPUT: {
        ProjectLifecycle.DISCOVERING,
        ProjectLifecycle.READY,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
        ProjectLifecycle.FAILED,
    },
    ProjectLifecycle.READY: {
        ProjectLifecycle.QUEUED,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
        ProjectLifecycle.FAILED,
    },
    ProjectLifecycle.QUEUED: {
        ProjectLifecycle.RUNNING,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
        ProjectLifecycle.FAILED,
    },
    ProjectLifecycle.RUNNING: {
        ProjectLifecycle.PREVIEW_READY,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
        ProjectLifecycle.FAILED,
    },
    ProjectLifecycle.PREVIEW_READY: {
        ProjectLifecycle.REVISION_REQUESTED,
        ProjectLifecycle.PUBLISHING,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
        ProjectLifecycle.FAILED,
    },
    ProjectLifecycle.REVISION_REQUESTED: {
        ProjectLifecycle.QUEUED,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
        ProjectLifecycle.FAILED,
    },
    ProjectLifecycle.PUBLISHING: {
        ProjectLifecycle.LIVE,
        ProjectLifecycle.FAILED,
        ProjectLifecycle.CANCELED,
    },
    ProjectLifecycle.LIVE: {
        ProjectLifecycle.REVISION_REQUESTED,
        ProjectLifecycle.PAUSED,
        ProjectLifecycle.CANCELED,
    },
    ProjectLifecycle.FAILED: {
        ProjectLifecycle.DISCOVERING,
        ProjectLifecycle.READY,
        ProjectLifecycle.CANCELED,
    },
    ProjectLifecycle.PAUSED: {
        ProjectLifecycle.DISCOVERING,
        ProjectLifecycle.WAITING_INPUT,
        ProjectLifecycle.READY,
        ProjectLifecycle.QUEUED,
        ProjectLifecycle.RUNNING,
        ProjectLifecycle.PREVIEW_READY,
        ProjectLifecycle.REVISION_REQUESTED,
        ProjectLifecycle.PUBLISHING,
        ProjectLifecycle.LIVE,
        ProjectLifecycle.CANCELED,
    },
    ProjectLifecycle.CANCELED: set(),
}


class LifecycleError(ValueError):
    """Raised when an invalid lifecycle transition is attempted."""


def can_transition(current: ProjectLifecycle, target: ProjectLifecycle) -> bool:
    """Return True if transition from current to target is allowed."""
    return target in _TRANSITIONS.get(current, set())


def has_outgoing_transitions(current: ProjectLifecycle) -> bool:
    """Return True if *current* permits at least one transition.

    A state with no outgoing edge (CANCELED) can never accept a lifecycle
    change, so no orchestration layer that drives transitions can act on it.
    """
    return bool(_TRANSITIONS.get(current, set()))


def transition(current: ProjectLifecycle, target: ProjectLifecycle) -> ProjectLifecycle:
    """Validate and return the target lifecycle state.

    Raises LifecycleError if the transition is not allowed.
    """
    if not can_transition(current, target):
        raise LifecycleError(
            f"Invalid lifecycle transition: {current.value} -> {target.value}"
        )
    return target
