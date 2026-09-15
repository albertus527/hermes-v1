"""Phase 12 workspace staging + persistence wiring for design references.

Bridges bounded intake (``app.core.references``) to VISION extraction
(``HermesAdapter.vision_extract_references``) and persisted per-project
state. Callers must already hold an authenticated principal/reference
token — this module enforces the same mutating-role gate as revisions.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from app.core.authz import AuthzError, require_mutating_role
from app.core.contracts import OperationResult
from app.core.references import (
    ReferenceItem,
    fetch_reference_url,
    validate_role,
    normalized_image,
    validate_characteristics,
)


class ReferenceIntake:
    """Stages one validated reference per role and persists VISION evidence."""

    def __init__(self, store, hermes_adapter=None):
        self.store = store
        self.hermes_adapter = hermes_adapter

    @staticmethod
    def _guard(state, principal_id, reference_token):
        require_mutating_role(state, principal_id, reference_token)
        if (state.revisions.source_revision != 0
                or state.lifecycle not in {"DISCOVERING", "WAITING_INPUT", "READY"}
                or state.pause_state.get("paused")):
            raise AuthzError("REFERENCE_NOT_ALLOWED_IN_LIFECYCLE")

    def _extract(self, role, clean_bytes):
        if self.hermes_adapter is None:
            return OperationResult.fail("VISION_UNAVAILABLE", error_code="VISION_UNAVAILABLE")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reference.png"
            path.write_bytes(clean_bytes)
            result = self.hermes_adapter.vision_extract_references({role: path})
        if not result.get("success"):
            return OperationResult.fail(
                result.get("error") or "VISION_EXTRACTION_FAILED",
                error_code="VISION_EXTRACTION_FAILED",
            )
        try:
            notes = validate_characteristics(result.get("characteristics"), [role])
        except ValueError:
            return OperationResult.fail("VISION_NO_EVIDENCE", error_code="VISION_NO_EVIDENCE")
        return OperationResult.ok({"evidence": notes[role]})

    def _persist(self, project_id, item: ReferenceItem, evidence, principal_id, reference_token, snapshot):
        with self.store.acquire_writer(project_id) as state:
            try:
                self._guard(state, principal_id, reference_token)
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
            if state.to_dict() != snapshot:
                return OperationResult.fail("STALE_REFERENCE_INPUT", error_code="STALE_REFERENCE_INPUT")
            state.design_directions = []
            state.selected_direction = None
            state.design_references[item.role] = {
                "item": item.to_dict(),
                "evidence": evidence,
            }
            self.store.save(state)
        return OperationResult.ok({"role": item.role})

    def add_upload(self, project_id, data, role, *, principal_id=None, reference_token=None):
        with self.store.acquire_writer(project_id) as state:
            try:
                self._guard(state, principal_id, reference_token)
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
            snapshot = state.to_dict()
        try:
            role = validate_role(role)
            clean = normalized_image(data)
            item = ReferenceItem(role, "upload", hashlib.sha256(clean).hexdigest(), "image/png", len(clean))
        except ValueError as exc:
            return OperationResult.fail(str(exc), error_code="INVALID_REFERENCE_UPLOAD")
        extracted = self._extract(item.role, clean)
        if not extracted.success:
            return extracted
        return self._persist(project_id, item, extracted.data["evidence"], principal_id, reference_token, snapshot)

    def add_url(self, project_id, url, role, *, principal_id=None, reference_token=None):
        with self.store.acquire_writer(project_id) as state:
            try:
                self._guard(state, principal_id, reference_token)
            except AuthzError as exc:
                return OperationResult.fail(exc.error_code, error_code=exc.error_code)
            snapshot = state.to_dict()
        try:
            validate_role(role)
        except ValueError as exc:
            return OperationResult.fail(str(exc), error_code="INVALID_REFERENCE_ROLE")
        fetched = fetch_reference_url(url, role)
        if not fetched.success:
            return fetched
        item: ReferenceItem = fetched.data["item"]
        clean = fetched.data["bytes"]
        extracted = self._extract(item.role, clean)
        if not extracted.success:
            return extracted
        return self._persist(project_id, item, extracted.data["evidence"], principal_id, reference_token, snapshot)
