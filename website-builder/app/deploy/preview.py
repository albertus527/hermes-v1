"""Phase 9 preview orchestration.

Owns nothing about Vercel/Telegram/browser directly — that lives in
app.deploy.adapters. This module owns:

  * binding the exact tested source+dist bytes to a preview operation
    (rejecting anything that drifted since Phase 8 QA passed),
  * committing those exact bytes into the separate output Git repository,
  * persisting durable operation intent BEFORE any external side effect,
  * reconciling ambiguous/ timed-out provider calls via lookup-by-identity
    rather than blind resend,
  * mandatory anonymous smoke test before any preview is ever shown,
  * only marking "latest shown preview" after screenshot delivery AND the
    preview URL text delivery both succeeded.

No network call happens here without every prerequisite adapter being
constructed and passed in explicitly by the caller. There is no default
Vercel/Telegram credential resolution in this module.
"""
from __future__ import annotations

import hashlib
import time
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.contracts import OperationResult
from app.core.state import ProjectStateStore
from app.deploy.git_output import OutputGitRepository
from app.deploy.snapshot import TestedSnapshot, source_fingerprint


@dataclass
class PreviewDeps:
    """All external boundary objects. None of these are constructed here."""

    vercel: Any
    telegram: Any
    smoke: Any
    output_repo: OutputGitRepository
    chat_id_for: Any  # callable(project_id, state) -> str, may return None
    app_id_for: Any = field(default=lambda project_id: project_id)


class PreviewOrchestrator:
    """Application-owned Phase 9 orchestration. Caller supplies all adapters."""

    def __init__(self, store: ProjectStateStore, deps: PreviewDeps, smoke_dir_root: Optional[Path] = None):
        self.store = store
        self.deps = deps
        self.smoke_dir_root = smoke_dir_root

    # ------------------------------------------------------------------
    # Entry point — runs inside the same worker ownership as Phase 7/8.
    # ------------------------------------------------------------------

    def run_owned(self, project_id: str, workspace: Path) -> OperationResult:
        """Serialize preview orchestrators without nesting project writer locks."""
        lock = self.store._locks_dir / 'preview-worker'
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return OperationResult.fail('PREVIEW_BUSY', error_code='PREVIEW_BUSY')
        os.close(fd)
        try:
            return self._run(project_id, workspace)
        except Exception:
            # Persisted intents survive crashes/exceptions; retry only reconciles.
            return OperationResult.fail('PREVIEW_RECONCILIATION_REQUIRED',
                                        error_code='PREVIEW_RECONCILIATION_REQUIRED')
        finally:
            lock.unlink(missing_ok=True)

    def _run(self, project_id, workspace):
        state = self.store.load(project_id)
        if state is None:
            return OperationResult.fail("NO_PROJECT_STATE", error_code="NO_PROJECT_STATE")
        if (state.lifecycle != 'PREVIEW_READY' or state.revisions.source_revision < 1
                or state.revisions.source_revision != state.revisions.qa_revision
                or not state.deployment.get('tested_snapshot')):
            return OperationResult.fail('QA_REQUIRED', error_code='QA_REQUIRED')
        checked = state.deployment.get("checked") or {}
        expected_source = checked.get("source_sha256")
        expected_artifact = checked.get("artifact_sha256")
        if not expected_source or not expected_artifact:
            return OperationResult.fail(
                "NO_TESTED_SNAPSHOT", error_code="NO_TESTED_SNAPSHOT"
            )
        if checked.get("source_revision") != state.revisions.qa_revision:
            # Repair/revision bumped source_revision after this snapshot was
            # bound; the caller must re-run QA before a preview can be made.
            return OperationResult.fail("STALE_QA_BINDING", error_code="STALE_QA_BINDING")

        try:
            snapshot = TestedSnapshot.from_dict(state.deployment['tested_snapshot'])
            if (snapshot.source_sha256 != expected_source or
                    snapshot.artifact_sha256 != expected_artifact):
                raise ValueError('STALE_QA_BINDING')
            snapshot.verify(workspace)
        except ValueError as exc:
            return OperationResult.fail(str(exc), error_code=str(exc))

        operation_id = hashlib.sha256((project_id + ':' + str(state.revisions.source_revision)
                                      + ':' + snapshot.identity).encode()).hexdigest()
        shown = state.deployment.get('latest_shown_preview', {})
        if shown.get('operation_id') == operation_id:
            return OperationResult.ok(shown)
        source_revision = state.revisions.source_revision

        # ---- 1. Persist durable operation intent BEFORE any side effect ----
        with self.store.acquire_writer(project_id) as locked:
            intent = locked.deployment.get("preview_intent")
            if not intent or intent.get("operation_id") != operation_id:
                locked.deployment["preview_intent"] = {
                    "operation_id": operation_id,
                    "source_revision": source_revision,
                    "source_sha256": snapshot.source_sha256,
                    "artifact_sha256": snapshot.artifact_sha256,
                    "stage": "created",
                    "created_at": time.time(),
                }
                self.store.save(locked)
            state = locked

        try:
            git_identity = self.deps.output_repo.commit(project_id, snapshot)
        except Exception as exc:  # pragma: no cover - defensive: local git failure
            return OperationResult.fail(f"OUTPUT_COMMIT_FAILED: {exc}", error_code="OUTPUT_COMMIT_FAILED")
        self._update_intent(project_id, operation_id, stage="committed", git=git_identity)

        # ---- 2. Ensure owned Vercel project identity ----
        app_id = self.deps.app_id_for(project_id)
        previous = state.deployment.get('preview_intent', {})
        attempted = state.deployment.get('project_create_attempted', False)
        with self.store.acquire_writer(project_id) as locked:
            locked.deployment['project_create_attempted'] = True
            self.store.save(locked)
        self._update_intent(project_id, operation_id, project_attempted=True)
        project_result = (self.deps.vercel.lookup_project(app_id) if attempted
                          else self.deps.vercel.ensure_project(app_id))
        if not project_result.success:
            return project_result
        vercel_project = project_result.data["project"]

        # ---- 3. Deploy, reconciling any ambiguous prior attempt by lookup ----
        attempted = previous.get('deployment_attempted', False)
        self._update_intent(project_id, operation_id, deployment_attempted=True,
                            project=vercel_project)
        deployment = (self.deps.vercel.find_deployment_by_operation_id(
            app_id, vercel_project, operation_id, source_revision, snapshot.artifact_sha256
        ) if attempted else self._deploy_or_reconcile(
            app_id, vercel_project, snapshot, operation_id, source_revision
        ))
        if not deployment.success:
            return deployment
        preview_url = deployment.data["preview_url"]
        self._update_intent(project_id, operation_id, stage="deployed",
                            deployment_id=deployment.data["deployment_id"],
                            preview_url=preview_url)

        # ---- 4. Bounded readiness polling ----
        ready = self._await_ready(app_id, vercel_project, operation_id, source_revision, snapshot)
        if not ready.success:
            return ready

        if (ready.data.get('deployment_id') != deployment.data.get('deployment_id')
                or ready.data.get('preview_url') != preview_url):
            return OperationResult.fail('DEPLOYMENT_IDENTITY_MISMATCH',
                                        error_code='DEPLOYMENT_IDENTITY_MISMATCH')

        # ---- 5. Mandatory anonymous smoke test ----
        smoke_dir = (self.smoke_dir_root or workspace) / "qa" / "preview_smoke"
        smoke_result = self.deps.smoke.run(preview_url, smoke_dir)
        self._update_intent(project_id, operation_id, stage="smoked", smoke=smoke_result.data)
        if not smoke_result.success:
            return OperationResult(success=False, error="SMOKE_FAILED",
                                   error_code="SMOKE_FAILED", data=smoke_result.data)

        # ---- 6. Telegram delivery — durable identities, fail closed on ambiguity ----
        chat_id = self.deps.chat_id_for(project_id, state)
        if not chat_id:
            return OperationResult.fail("NO_DELIVERY_TARGET", error_code="NO_DELIVERY_TARGET")

        shots = {}
        for key in ('desktop_screenshot', 'mobile_screenshot'):
            path = Path(smoke_result.data.get(key, ''))
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(smoke_dir.resolve()):
                return OperationResult.fail('NO_SMOKE_SCREENSHOT', error_code='NO_SMOKE_SCREENSHOT')
            png = path.read_bytes()
            if not png.startswith(b'\x89PNG\r\n\x1a\n'):
                return OperationResult.fail('NO_SMOKE_SCREENSHOT', error_code='NO_SMOKE_SCREENSHOT')
            shots[key] = {'path': str(path), 'sha256': hashlib.sha256(png).hexdigest()}
        self._update_intent(project_id, operation_id, screenshots=shots)
        photo_path = shots['desktop_screenshot']['path']

        if previous.get('photo_attempted') or previous.get('text_attempted'):
            return OperationResult.fail('DELIVERY_RECONCILIATION_REQUIRED',
                                        error_code='DELIVERY_RECONCILIATION_REQUIRED')
        snapshot.verify(workspace)
        self._update_intent(project_id, operation_id, photo_attempted=True, chat_id=str(chat_id))
        photo_result = self.deps.telegram.send_photo(
            chat_id, photo_path, caption=f"Preview ready: {preview_url}"
        )
        if not photo_result.success:
            return photo_result
        self._update_intent(project_id, operation_id, stage="photo_sent",
                            photo_message_id=photo_result.data.get("message_id"))

        self._update_intent(project_id, operation_id, text_attempted=True)
        text_result = self.deps.telegram.send_text(
            chat_id, f"Preview: {preview_url}\nReply with what you'd like changed."
        )
        if not text_result.success:
            return text_result

        # ---- 7. Mark latest shown preview only after BOTH sends succeeded ----
        snapshot.verify(workspace)
        with self.store.acquire_writer(project_id) as locked:
            if (locked.revisions.source_revision != source_revision
                    or locked.revisions.qa_revision != source_revision
                    or locked.lifecycle != 'PREVIEW_READY'
                    or locked.deployment['preview_intent']['operation_id'] != operation_id):
                return OperationResult.fail('STALE_QA_BINDING', error_code='STALE_QA_BINDING')
            locked.revisions.preview_revision = source_revision
            locked.deployment["preview_intent"]["stage"] = "shown"
            locked.deployment["preview_intent"]["text_message_id"] = text_result.data.get("message_id")
            locked.deployment["latest_shown_preview"] = {
                "operation_id": operation_id,
                "source_revision": source_revision,
                "preview_url": preview_url,
                "deployment_id": deployment.data["deployment_id"],
                "source_sha256": snapshot.source_sha256,
                "artifact_sha256": snapshot.artifact_sha256,
                "shown_at": time.time(),
            }
            self.store.save(locked)

        return OperationResult.ok({
            "preview_url": preview_url,
            "deployment_id": deployment.data["deployment_id"],
            "operation_id": operation_id,
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _update_intent(self, project_id: str, operation_id: str, **fields) -> None:
        with self.store.acquire_writer(project_id) as state:
            intent = state.deployment.get("preview_intent")
            if intent and intent.get("operation_id") == operation_id:
                intent.update(fields)
                self.store.save(state)

    def _deploy_or_reconcile(self, app_id, project, snapshot, operation_id, source_revision):
        """Deploy; on ambiguity/timeout, reconcile via lookup — never resend blindly."""
        try:
            result = self.deps.vercel.deploy_static_files(
                app_id, project, dict(snapshot.dist),
                operation_id, source_revision, snapshot.artifact_sha256,
            )
        except Exception as exc:  # pragma: no cover - defensive
            result = OperationResult.fail(str(exc), error_code="DEPLOY_EXCEPTION")

        if result.success:
            return result

        # Any failure from deploy_static_files is already fail-closed
        # (adapters.py never marks retryable=True). Before giving up,
        # check whether the operation actually landed under a timeout.
        lookup = self.deps.vercel.find_deployment_by_operation_id(
            app_id, project, operation_id, source_revision, snapshot.artifact_sha256
        )
        if lookup.success:
            return lookup
        return result

    def _await_ready(self, app_id, project, operation_id, source_revision, snapshot,
                     max_polls: int = 20, interval: float = 1.5):
        for _ in range(max_polls):
            lookup = self.deps.vercel.find_deployment_by_operation_id(
                app_id, project, operation_id, source_revision, snapshot.artifact_sha256
            )
            if not lookup.success:
                if lookup.error_code == "NOT_FOUND":
                    time.sleep(interval)
                    continue
                return lookup
            state = lookup.data.get("state")
            if state in ("READY", "ready"):
                return lookup
            if state in ("ERROR", "error", "CANCELED", "canceled"):
                return OperationResult.fail("DEPLOYMENT_FAILED", error_code="DEPLOYMENT_FAILED")
            time.sleep(interval)
        return OperationResult.fail("DEPLOYMENT_TIMEOUT", error_code="DEPLOYMENT_TIMEOUT")
