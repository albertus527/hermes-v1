# Fix publish-recovery to use promoted_deployment_id as authoritative production identity

**Root cause**: In the recovery path (`resume_publish` → `_promote_authorized`), when `is_same_operation` and the operation has already crossed the promotion boundary (`promotion_intent.promoted_deployment_id` set), the code uses `intended_identity` built from the approval's preview `deployment_id` as the production identity. This causes `_adopt_external_promotion` and related code to reconcile against the original preview deployment (A) instead of the actual promoted production deployment (B), leading to `PROMOTED_UNPROVEN` and recovery failure.

**Exact state field incorrectly used**: `intended_identity` constructed from `approval["deployment_id"]` (the preview deployment) is used during recovery instead of `promotion_intent.promoted_deployment_id` (the actual production deployment that was promoted).

**What happened during the original failure**:
1. Preview deployment `A` (dpl_8mHFMR...) was approved and promoted
2. Promotion by creation minted production deployment `B` (dpl_Asi...)
3. Vercel production was bound to `B`
4. `_post_promote` ensured `PRODUCTION_CONFIRMED` and resolved canonical URL (wrong host due to slug misconfiguration)
5. Production smoke ran against wrong host → failed
6. Lifecycle transitioned to `FAILED`

**Why recovery fails**:
1. `resume_publish` detects the stalled FAILED state with `pending.stage >= PRODUCTION_CONFIRMED`
2. `reached_production = True` → `recovery=True`
3. `_promote_authorized` reuses `existing_intent["previous_production"]` (from bootstrap) and keeps `promotion_intent` intact
4. `_confirm_git` resums Git publication (read-only)
5. `_adopt_external_promotion(recovery=True)` calls `reconcile_external_promotion` with `intended_identity = approved["deployment_id"]` (A!)
6. `reconcile_external_promotion` checks:
   - `direct = binding_id == intended_id` → False (B != A)
   - `lineage`: checks `lastAliasRequest` job for `fromDeploymentId == A` and `toDeploymentId == B`
7. Vercel's lineage record is absent/failed → status `PROMOTED_UNPROVEN`
8. With `recovery=True` that returns `PROMOTED_UNPROVEN` → `PROMOTION_IDENTITY_UNPROVEN` failure

**Expected recovery semantics for already-promoted operations**:
When `promotion_intent.promoted_deployment_id != null` and `stage >= PRODUCTION_CONFIRMED / smoked`:
1. Treat `promoted_deployment_id` as the exact production deployment identity
2. Reconcile that exact deployment against Vercel provider state
3. Verify that this exact deployment is still production
4. Resolve canonical production host for this promoted deployment
5. Run production smoke against host
6. If smoke passes → finish release commit / transition LIVE
7. If smoke fails → remain non-LIVE with truthful failure state

**Changes**:

## 1. `website-builder/app/projects/promote.py`

Modify `_promote_authorized` in `PromotionOrchestrator` to handle already-promoted recovery:

Add a recovery path after `_confirm_git` that checks if the operation has already crossed the promotion boundary:

```python
# ---- After _confirm_git, before adoption/reconcile: detect already-promoted recovery
if is_same_operation:
    existing_intent = state.deployment.get("promotion_intent") or {}
    promoted_id = existing_intent.get("promoted_deployment_id")
    promoted_stage = existing_intent.get("stage")

    # If the intent already has a promoted_deployment_id and stage indicates
    # the operation crossed the promotion boundary, this is an already-promoted
    # recovery. Use the promoted deployment as the authoritative production identity.
    if (
        promoted_id
        and isinstance(promoted_id, str)
        and released_contract.at_least(promoted_stage, released_contract.STAGE_PRODUCTION_CONFIRMED)
    ):
        # Reconstruct the promoted deployment identity using approval fields.
        promoted_identity = {
            "deployment_id": promoted_id,
            "operation_id": operation_id,
            "source_revision": source_revision,
            "artifact_sha256": artifact_sha256,
        }
        # Reconcile the promoted deployment directly.
        reconcile = self.deps.vercel.reconcile_production_deployment(
            app_id, vercel_project, promoted_identity, expected_name=expected_name,
        )
        if not reconcile.success:
            # Provider cannot confirm this is still production → fail closed
            self._mark_publication_terminal_failure(
                project_id, operation_id,
                reconcile.error_code or "PRODUCTION_RECONCILIATION_FAILED",
            )
            self._fail(project_id, "PRODUCTION_RECONCILIATION_FAILED", reconcile.error_code)
            return reconcile

        status = (reconcile.data or {}).get("status")
        if status != "PROMOTED":
            # Provider reports this is NOT production → fail closed
            self._mark_publication_terminal_failure(
                project_id, operation_id,
                "NOT_PROMOTED",
            )
            self._fail(project_id, "PRODUCTION_RECONCILIATION_FAILED", "NOT_PROMOTED")
            return reconcile

        deployment_url = reconcile.data.get("deployment_url") or self._deployment_url_or_fallback(
            reconcile.data.get("deployment_id"),
            promoted_identity,
        )
        logger.info(
            "Promotion adopted production deployment=%s (recovery)", pushed_deployment_id
        )
        self._update_intent(
            project_id, operation_id,
            stage="promoted",
            deployment_url=deployment_url,
        )
        return self._post_promote(
            project_id, workspace, app_id, vercel_project,
            previous_identity,
            deployment_url=deployment_url,
            intended_identity=promoted_identity,
            expected_name=expected_name,
            reconciled=True,
            approval=approval,
            source_revision=source_revision,
        )
```

This path:
- Reuses the existing promotion_intent (not recreated)
- Uses `promoted_deployment_id` as the production identity
- Skips `promote_deployment` call (no repromote)
- Skips `_adopt_external_promotion` lineage checks
- Calls `reconcile_production_deployment` directly on promoted deployment
- Proceeds to `_post_promote` which resolves canonical URL for promoted deployment, runs smoke, and commits

---

## 2. Tests

Add regression test file `website-builder/app/tests/test_promote_recovery.py`:

### Test fixture: p16-like already-promoted failed state

```python
def create_project_state_pre_promoted(
    store: ProjectStateStore,
    project_id: str,
    operation_id: str,
    preview_deployment_id: str,
    promoted_deployment_id: str,
    stage: str = "smoked",
) -> None:
    """Create state matching p16 shape: preview approved, already promoted to production, smoke failed."""
    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.FAILED.value
        state.deployment["approval"] = {
            "operation_id": operation_id,
            "source_revision": 1,
            "deployment_id": preview_deployment_id,
            "source_sha256": "abc123",
            "artifact_sha256": "def456",
        }
        state.deployment["preview_intent"] = {
            "operation_id": operation_id,
            "git": {
                "commit": "a" * 40,
                "tree": "b" * 40,
            },
        }
        state.deployment["latest_shown_preview"] = {
            "operation_id": operation_id,
            "deployment_id": preview_deployment_id,
            "source_sha256": "abc123",
            "artifact_sha256": "def456",
        }
        state.deployment["promotion_intent"] = {
            "operation_id": operation_id,
            "deployment_id": preview_deployment_id,
            "promoted_deployment_id": promoted_deployment_id,
            "stage": stage,
            "previous_production": None,  # bootstrap placeholder
            "previous_production_class": "KNOWN_BOOTSTRAP",
        }
        state.failure = {
            "phase": "promotion",
            "error": "PRODUCTION_SMOKE_FAILED",
            "error_code": "SMOKE_FAILED",
            "failed_at": 1000.0,
        }
        # pending_publication at PRODUCTION_CONFIRMED with smoke failure
        state.deployment["pending_publication"] = {
            "operation_id": operation_id,
            "stage": "PRODUCTION_CONFIRMED",
            "publication": {
                "configured": False,  # or True if publication was configured
                "intended_commit": "a" * 40,
            },
            "production": {
                "deployment_id": promoted_deployment_id,
                "promoted_deployment_id": promoted_deployment_id,
                "smoke": {
                    "status": "FAILED",
                    "target_host": "testbakery.vercel.app",  # old wrong host
                    "target_path": "/",
                    "failure_classification": "HTTP_500",
                },
            },
        }
        store.save(state)
```

### Test cases

```python
def test_promote_recovery_already_promoted_uses_promoted_deployment_id(
    store, orchestrator, mock_vercel, mock_release_coordinator, mock_smoke
):
    """Recovery from an already-promoted operation must use promoted_deployment_id as production identity."""
    project_id = "test-project"
    operation_id = "978d54f30b864269bd02548a433f51c958a654c980d9e9b05c7023ba7a28d86b"
    preview_deployment_id = "dpl_8mHFMR2NWs1a7qztpqaY35xzbthS"
    promoted_deployment_id = "dpl_AsiNzqieqgw1tVXxmdRSgNWGAFhS"

    # Create state matching p16 shape
    create_project_state_pre_promoted(store, project_id, operation_id,
                                       preview_deployment_id,
                                       promoted_deployment_id)

    # Mock Vercel responses
    mock_vercel._current_production_id.return_value = promoted_deployment_id
    mock_vercel._authoritative_deployment_meta.return_value = {
        "wbOperation": operation_id,
        "wbRevision": "1",
        "wbArtifact": "def456",
        "alias": ["testbakery-eight.vercel.app"],  # correct host
    }
    mock_vercel.find_production_deployment.return_value = OperationResult.ok({
        "deployment_id": promoted_deployment_id,
        "operation_id": operation_id,
        "source_revision": 1,
        "artifact_sha256": "def456",
    })
    mock_vercel.reconcile_production_deployment.return_value = OperationResult.ok({
        "status": "PROMOTED",
        "deployment_id": promoted_deployment_id,
        "deployment_url": f"https://{promoted_deployment_id}.vercel.app",
    })
    mock_vercel.reconcile_external_promotion.return_value = OperationResult.ok({
        "status": "PROMOTED_UNPROVEN",
        "deployment_id": promoted_deployment_id,
    })

    # Run recovery
    result = orchestrator.resume_publish(
        project_id,
        Path("/workspace"),
        principal_id="owner",
        reference_token=None,
    )

    # Assertions
    assert result.success

    # Should have reconciled production_deployment directly, NOT external promotion
    mock_vercel.reconcile_production_deployment.assert_called_once_with(
        any(app_id),
        any(vercel_project),
        expected_identity={
            "deployment_id": promoted_deployment_id,
            "operation_id": operation_id,
            "source_revision": 1,
            "artifact_sha256": "def456",
        },
        expected_name=any(expected_name),
    )

    # reconcile_external_promotion should NOT be called (bypassed)
    mock_vercel.reconcile_external_promotion.assert_not_called()

    # Neither promote_deployment nor promote_by_creation should be called
    mock_vercel.promote_deployment.assert_not_called()

    # _post_promote should be called to smoke the promoted deployment
    orchestrator._post_promote.assert_called_once()

    # Smoke should be run against the CORRECT host (testbakery-eight.vercel.app)
    _, call_kwargs = orchestrator._post_promote.call_args
    canonical = call_kwargs.get("canonical")
    assert canonical is not None
    assert canonical["canonical_production_url"] == "https://testbakery-eight.vercel.app/"

    # Pending record should be updated with smoke pass and commit
    pending_after = store.load(project_id).deployment.get("pending_publication")
    # Stage should have advanced past PRODUCTION_CONFIRMED to SMOKE_PASSED
    # ( assertion depends on mock_smoke returning PASSED )
```

```python
def test_promote_recovery_already_promoted_smoke_fails_fails_closed(
    store, orchestrator, mock_vercel, mock_release_coordinator, mock_smoke
):
    """Smoke on promoted_deployment_id must fail the operation rather than retrying whole promote."""
    # Create state
    create_project_state_pre_promoted(store, "test-project", "op-id",
                                       "dpl_preview", "dpl_promoted",
                                       stage="smoked")

    # Mock Vercel: production confirmed, but smoke FAILS
    mock_vercel.reconcile_production_deployment.return_value = OperationResult.ok({
        "status": "PROMOTED",
        "deployment_id": "dpl_promoted",
        "deployment_url": "https://dpl_promoted.vercel.app",
    })
    mock_smoke.run.return_value = OperationResult.fail(
        "SMOKE_FAILED",
        error_code="HTTP_500",
        data={"target_host": "testbakery-eight.vercel.app"},
    )

    result = orchestrator.resume_publish("test-project", Path("/workspace"),
                                         principal_id="owner")

    # Should remain FAILED
    assert not result.success
    assert result.error_code == "PRODUCTION_SMOKE_FAILED"

    state = store.load("test-project")
    assert state.lifecycle == ProjectLifecycle.FAILED.value

    # Pending smoke evidence should show failure
    pending = state.deployment.get("pending_publication", {}).get("production", {}).get("smoke")
    assert pending is not None
    assert pending["status"] == "FAILED"
    assert pending["failure_classification"] == "HTTP_500"
    assert pending["target_host"] == "testbakery-eight.vercel.app"
```

```python
def test_promote_recovery_already_promoted_production_moved_fails_closed(
    store, orchestrator, mock_vercel
):
    """If promoted_deployment_id no longer matches production provider state, recovery must fail closed."""
    # Create state
    create_project_state_pre_promoted(store, "test-project", "op-id",
                                       "dpl_preview", "dpl_promoted",
                                       stage="promoted")

    # Mock Vercel: current production is SOME_OTHER_DEPLOYMENT
    mock_vercel._current_production_id.return_value = "dpl_other"

    result = orchestrator.resume_publish("test-project", Path("/workspace"),
                                         principal_id="owner")

    # Should fail with reconciliation error
    assert not result.success
    assert result.error_code == "PRODUCTION_RECONCILIATION_FAILED"

    state = store.load("test-project")
    assert state.lifecycle == ProjectLifecycle.PUBLISHING.value  # Not FAILED
```

```python
def test_promote_recovery_without_promoted_deployment_falls_back_to_normal_path(
    store, orchestrator, mock_vercel, mock_release_coordinator, mock_commit
):
    """Without promoted_deployment_id set, normal same-operation resume path should be used."""
    project_id = "test-project"
    operation_id = "978d54f30b864269bd02548a433f51c958a654c980d9e9b05c7023ba7a28d86b"

    with store.acquire_writer(project_id) as state:
        state.lifecycle = ProjectLifecycle.PUBLISHING.value  # Not FAILED
        state.deployment["approval"] = {
            "operation_id": operation_id,
            "source_revision": 1,
            "deployment_id": "dpl_preview",
            "source_sha256": "abc123",
            "artifact_sha256": "def456",
        }
        state.deployment["preview_intent"] = {
            "operation_id": operation_id,
            "git": {"commit": "a" * 40},
        }
        state.deployment["latest_shown_preview"] = {
            "operation_id": operation_id,
            "deployment_id": "dpl_preview",
            "source_sha256": "abc123",
            "artifact_sha256": "def456",
        }
        state.deployment["promotion_intent"] = {
            "operation_id": operation_id,
            "deployment_id": "dpl_preview",
            # No promoted_deployment_id key
            "stage": "publishing",
        }
        state.deployment["pending_publication"] = {
            "operation_id": operation_id,
            "stage": "GIT_CONFIRMED",
            "publication": {"configured": True, "intended_commit": "a" * 40},
        }

    # Mock non-specialized path
    mock_vercel.promote_deployment.return_value = OperationResult.ok({
        "deployment_id": "dpl_promoted",
        "promoted_deployment_id": "dpl_promoted",
        "production_url": "https://dpl_promoted.vercel.app",
    })

    result = orchestrator.resume_publish(project_id, Path("/workspace"),
                                         principal_id="owner")

    # Should proceed with normal promote flow (not the new path)
    mock_vercel.promote_deployment.assert_called_once()
    assert result.success
```

---

## 3. Full test suite

Run the full test suite to ensure no regressions:

```bash
python -m pytest tests/ -x --tb=short --no-header -v 2>&1 | head -100
```

---

## Verification checklist

- [ ] `reconcile_publish` now successfully recovers p16 state using promoted_deployment_id
- [ ] Recovery path skips promote_deployment (no repromote) and verify_production_reconciliation
- [ ] Recovery path skips _adopt_external_promotion (bypassed by direct reconcile)
- [ ] Canonical URL resolved for promoted_deployment_id (testbakery-eight.vercel.app, not testbakery.vercel.app)
- [ ] Production smoke runs on resolved host, passes with fix
- [ ] State transitions to LIVE on smoke pass
- [ ] If smoke fails, state remains FAILED with truthful failure evidence
- [ ] If production moved since promoted_deployment_id, recovery fails closed appropriately
- [ ] Recovery without promoted_deployment_id still uses normal promote path
- [ ] No new deployment created during recovery
- [ ] No new Git push during recovery
- [ ] No modification of previous_production evidence

---

## Plan details

### Files to modify
1. `website-builder/app/projects/promote.py` — Add recovery path for already-promoted operations (add ~60 lines around line 782 after `_confirm_git`)
2. `website-builder/app/tests/test_promote_recovery.py` — New test file (~250 lines)

### Files to run but not modify
- All existing tests in `tests/` (ensure no regressions)

### Expected full-suite result
All tests pass, with new recovery tests green. Existing tests continue to pass without modification.

### Safe reconciliation of untouched p16 state
Yes — the fix restores `resumable = True` and uses the correct production identity (promoted_deployment_id), so the existing untouched p16 state can now be reconciled without manual intervention.