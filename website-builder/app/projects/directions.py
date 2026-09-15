"""Phase 13 lightweight design directions for Website Builder R1.

For no-reference users who do NOT delegate design authority ("yang bagus
aja" / equivalent), FRONTEND proposes 2-3 LIGHTWEIGHT design directions
(short descriptors + palette swatches only -- never a full build) instead
of silently letting FRONTEND pick one direction or building three complete
sites. The user chooses one; exactly one real FrontendBuilder.build() then
proceeds using ONLY the selected direction.

This module does NOT touch the existing delegated-design path (Phase 7):
when a project has ``brief["design_authority_delegated"]`` truthy, or a
reference set already exists (Phase 12), Phase 13 direction proposal is
simply not applicable and FrontendBuilder.build() runs completely
unchanged, exactly as before this pass.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.core.authz import AuthzError, require_mutating_role
from app.core.contracts import OperationResult
from app.core.state import ProjectStateStore


@dataclass
class DirectionsResult:
    """Result of proposing (or failing to propose) lightweight directions."""

    success: bool
    project_id: str
    directions: List[Dict[str, Any]]
    error: Optional[str] = None
    error_code: Optional[str] = None


def design_authority_delegated(brief: Dict[str, Any]) -> bool:
    """True only when the user explicitly delegated design authority.

    Canonical spec §9: "If the user delegates design authority ('yang bagus
    aja'), FRONTEND may choose after business intent is clear." This is a
    narrow, explicit flag -- it is never inferred from the absence of
    references alone, so the existing Phase 7 delegated-design test suite
    and behavior are completely unaffected.
    """
    return brief.get("design_authority_delegated") is True


def has_references(references: Optional[Dict[str, Any]]) -> bool:
    """True when Phase 12 design references were attached to the project."""
    return bool(references)


def direction_choice_pending(state) -> bool:
    """A proposed set requires an explicit selection before build admission."""
    return bool(state.design_directions) and state.selected_direction is None


class DirectionsOrchestrator:
    """Application-owned Phase 13 orchestration.

    Proposes bounded (2-3), lightweight design directions via the Hermes
    adapter, persists them, and gates the user's choice through the same
    ``require_mutating_role`` authorization used by revise.py/promote.py.
    Never invokes a full FRONTEND build itself -- that stays the caller's
    responsibility (via FrontendBuilder.build(), unchanged).
    """

    def __init__(self, store: ProjectStateStore, hermes_adapter=None):
        self.store = store
        self.hermes_adapter = hermes_adapter

    @staticmethod
    def _guard(state, principal_id, reference_token):
        require_mutating_role(state, principal_id, reference_token)
        if (state.lifecycle not in {"DISCOVERING", "READY"}
                or state.revisions.source_revision != 0 or state.pause_state.get("paused")):
            raise AuthzError("DIRECTIONS_NOT_ALLOWED_IN_LIFECYCLE")

    def propose(
        self,
        project_id: str,
        brief: Dict[str, Any],
        workspace,
        principal_id: Optional[str] = None,
        reference_token: Optional[str] = None,
    ) -> DirectionsResult:
        """Propose 2-3 lightweight design directions and persist them.

        Only meaningful for the no-reference, non-delegated case; callers
        are expected to check ``design_authority_delegated()`` /
        ``has_references()`` themselves before calling this (mirrors how
        revise.py/promote.py leave lifecycle-applicability checks to the
        caller). Requires an owner/reviewer principal or reference token.
        """
        with self.store.acquire_writer(project_id) as state:
            try:
                self._guard(state, principal_id, reference_token)
            except AuthzError as exc:
                return DirectionsResult(
                    False, project_id, [], error=exc.error_code, error_code=exc.error_code
                )

            if has_references(state.design_references) or design_authority_delegated(state.brief):
                return DirectionsResult(True, project_id, [])
            brief = dict(state.brief)
            snapshot = state.to_dict()

        if self.hermes_adapter is None:
            return DirectionsResult(
                False, project_id, [],
                error="Hermes adapter not configured",
                error_code="HERMES_ADAPTER_UNAVAILABLE",
            )

        result = self.hermes_adapter.frontend_propose_directions(
            brief=brief, workspace=workspace
        )

        if not result.get("success"):
            return DirectionsResult(
                False, project_id, [],
                error=result.get("error", "Direction proposal failed"),
                error_code="DIRECTIONS_PROPOSAL_FAILED",
            )

        directions = result.get("directions") or []
        if not (2 <= len(directions) <= 3):
            return DirectionsResult(
                False, project_id, [],
                error="FRONTEND must propose exactly 2-3 directions",
                error_code="INVALID_DIRECTION_COUNT",
            )

        with self.store.acquire_writer(project_id) as state:
            try:
                self._guard(state, principal_id, reference_token)
            except AuthzError as exc:
                return DirectionsResult(
                    False, project_id, [], error=exc.error_code, error_code=exc.error_code
                )
            if state.to_dict() != snapshot:
                return DirectionsResult(False, project_id, [], error="STALE_DIRECTION_INPUT",
                                        error_code="STALE_DIRECTION_INPUT")
            state.design_directions = directions
            state.selected_direction = None
            self.store.save(state)

        return DirectionsResult(True, project_id, directions)

    def choose_direction(
        self,
        project_id: str,
        index: int,
        principal_id: Optional[str] = None,
        reference_token: Optional[str] = None,
    ) -> OperationResult:
        """Persist the user's choice of ONE proposed direction.

        Requires the mutating role, re-checked under the writer lock
        immediately before the mutation (same pattern as
        revise.py::reserve/apply and promote.py::approve/promote).
        """
        with self.store.acquire_writer(project_id) as state:
            try:
                self._guard(state, principal_id, reference_token)
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)

            if has_references(state.design_references) or design_authority_delegated(state.brief):
                return OperationResult.fail("DIRECTIONS_NOT_APPLICABLE", error_code="DIRECTIONS_NOT_APPLICABLE")
            directions = state.design_directions
            if not isinstance(directions, list) or not directions:
                return OperationResult.fail(
                    "NO_DIRECTIONS_PROPOSED", error_code="NO_DIRECTIONS_PROPOSED"
                )
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(directions)
            ):
                return OperationResult.fail(
                    "INVALID_DIRECTION_INDEX", error_code="INVALID_DIRECTION_INDEX"
                )

            selected = directions[index]
            state.selected_direction = selected
            self.store.save(state)

        return OperationResult.ok({"selected_direction": selected})


def direction_build_instructions(selected_direction: Optional[Dict[str, Any]]) -> Optional[str]:
    """Compose FRONTEND build instructions for the ONE selected direction.

    Returns None when no direction was selected (delegated-design or
    reference-driven paths pass their own instructions unchanged, and the
    caller must not fabricate direction instructions in that case).
    """
    if not selected_direction:
        return None

    label = selected_direction.get("label", "")
    descriptor = selected_direction.get("descriptor", "")
    palette = selected_direction.get("palette", {})

    return f"""SELECTED DESIGN DIRECTION (Phase 13 -- the user chose this
lightweight direction from 2-3 proposed options; build ONLY this one):

Label: {label}
Descriptor: {descriptor}
Palette: {json.dumps(palette)}

Follow this direction's style and palette. Do not invent a different
direction or blend in the unselected proposals.
"""
