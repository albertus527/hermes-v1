"""Publish-recovery regression tests for the promoted-deployment binding.

The p16 incident: a publish whose promote-by-creation SUCCEEDED (minting a
new production deployment ``B``), whose production smoke ran, and whose
operation then failed -- leaving durable state with
``promotion_intent.deployment_id == A`` (the approved preview),
``promotion_intent.promoted_deployment_id == B``, intent stage ``smoked``,
lifecycle FAILED. Recovery reconciled the provider's production binding
against ``A`` instead of ``B``, so an already-promoted, already-smoked
operation failed with ``PROMOTION_IDENTITY_UNPROVEN``.

These tests pin the contract that fixes it:

  * ``promoted_deployment_id`` is the AUTHORITATIVE production deployment
    identity once the promotion boundary is provably crossed.
  * ``promotion_intent.deployment_id`` (the approval preview ``A``) is
    durable provenance: never rewritten, never reinterpreted as "previous
    production".
  * Recovery is read-only against the provider until proven otherwise: no
    new deployment, no rebuild, no Git repush, no re-promote.
  * Fail-closed semantics are preserved when ``B`` is no longer production.

Local behavioral tests only: fake collaborators, no network, no credentials,
no Telegram, no real Vercel mutation.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects.promote import (
    PREVIOUS_PRODUCTION_BOOTSTRAP, PromoteDeps, PromotionOrchestrator,
)
from app.projects.release import (
    STAGE_PRODUCTION_CONFIRMED, build_pending_publication,
)
from app.sandbox.runner import ProjectRunner

# --- the exact p16 identity under test ------------------------------------
P16 = 'tg-6329821361-p16'
P16_OPERATION_ID = ('978d54f30b864269bd02548a433f51c958a654c980d9e9b05c'
                    '7023ba7a28d86b')
# A: the approved preview deployment (provenance / rollback source)
P16_PREVIEW_DEPLOYMENT_ID = 'dpl_8mHFMR2NWs1a7qztpqaY35xzbthS'
# B: the promoted production deployment minted by promote-by-creation
P16_PROMOTED_DEPLOYMENT_ID = 'dpl_AsiNzqieqgw1tVXxmdRSgNWGAFhS'
P16_SOURCE_REVISION = 4
P16_ARTIFACT = 'f' * 64
P16_SOURCE = 'e' * 64
P16_CANONICAL_URL = 'https://testbakery-eight.vercel.app/'
OWNER = 'owner-1'


class P16Vercel:
    """Fake that models p16's remote truth and records every remote call.

    ``remote`` selects what provider truth says about the production
    binding: ``'promoted_b'`` (binding is B, meta matches -- the DIRECT
    proof), ``'wrong'`` (binding is some other deployment), ``'unreadable'``.
    """

    def __init__(self, remote):
        self.remote = remote
        self.promote_calls = []
        self.reconcile_external_calls = []
        self.reconcile_calls = []
        self.canonical_calls = []
        self._project = {'id': 'prj_1', 'name': 'testbakery-eight',
                         'accountId': 'team_1'}

    def lookup_project(self, app_id, *, expected_name=None):
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def canonical_production_url(self, app_id, project, *, expected_name=None,
                                 expected_deployment_id=None):
        self.canonical_calls.append(expected_deployment_id)
        return OperationResult.ok({
            'canonical_production_url': P16_CANONICAL_URL,
            'canonical_source': 'VERCEL_PROJECT_DOMAIN',
            'project_name': 'testbakery-eight',
            'canonical_host': 'testbakery-eight.vercel.app',
        })

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        # Post-promotion provider truth: production IS the promoted deployment
        # B, and it carries the complete trusted identity, so the fresh lookup
        # classifies it REAL_PRODUCTION -- exactly what p16's log recorded.
        # The orchestrator must treat this as a read-only observation, never
        # as a rollback target: the persisted previous_production (None,
        # KNOWN_BOOTSTRAP) is what governs.
        return OperationResult.ok({
            'deployment_id': P16_PROMOTED_DEPLOYMENT_ID,
            'operation_id': P16_OPERATION_ID,
            'source_revision': P16_SOURCE_REVISION,
            'artifact_sha256': P16_ARTIFACT,
        })

    def reconcile_external_promotion(self, app_id, project, intended_identity, *,
                                     expected_name=None):
        self.reconcile_external_calls.append(dict(intended_identity))
        if self.remote == 'promoted_b':
            if intended_identity.get('deployment_id') == P16_PROMOTED_DEPLOYMENT_ID:
                return OperationResult.ok({
                    'status': 'PROMOTED',
                    'deployment_id': P16_PROMOTED_DEPLOYMENT_ID,
                    'proof': 'DIRECT_IDENTITY',
                    'production_url': 'https://' + P16_PROMOTED_DEPLOYMENT_ID
                                      + '.vercel.app',
                })
            # Identity is the preview A: the provider binding is B, so the
            # direct proof fails -- the exact p16 bug.
            return OperationResult.ok({
                'status': 'PROMOTED_UNPROVEN',
                'deployment_id': P16_PROMOTED_DEPLOYMENT_ID,
            })
        if self.remote == 'wrong':
            return OperationResult.ok({
                'status': 'PROMOTED_UNPROVEN',
                'deployment_id': 'dpl_someone_elses',
            })
        return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                    error_code='PROMOTE_RECONCILIATION_REQUIRED')

    def reconcile_production_deployment(self, app_id, project, expected_identity, *,
                                        expected_name=None):
        self.reconcile_calls.append(dict(expected_identity))
        return OperationResult.fail('PROMOTE_RECONCILIATION_REQUIRED',
                                    error_code='PROMOTE_RECONCILIATION_REQUIRED')

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        return OperationResult.ok({
            'deployment_id': deployment_id,
            'production_url': 'https://should-never-happen.vercel.app',
        })


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return OperationResult.ok({'message_id': 1})


class FakeSmoke:
    def __init__(self, success=True):
        self.success = success
        self.calls = []

    def run(self, url, out_dir):
        self.calls.append(url)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return OperationResult(
            success=self.success,
            data={'url': url, 'failures': [] if self.success else ['broken']},
            error_code=None if self.success else 'SMOKE_FAILED')


def _seed_p16_state(store, project_id=P16):
    """p16 exactly as it is on disk: FAILED, promotion-phase failure, an
    intact promotion_intent whose deployment_id is the PREVIEW A, whose
    promoted_deployment_id is the PRODUCTION B, whose stage is ``smoked``,
    KNOWN_BOOTSTRAP previous production, and a pending publication at
    PRODUCTION_CONFIRMED (the promotion boundary is provably crossed).
    """
    shown = {
        'operation_id': P16_OPERATION_ID,
        'source_revision': P16_SOURCE_REVISION,
        'preview_url': 'https://protected-preview.vercel.app',
        'deployment_id': P16_PREVIEW_DEPLOYMENT_ID,
        'source_sha256': P16_SOURCE,
        'artifact_sha256': P16_ARTIFACT,
        'shown_at': 1.0,
    }
    approval = dict(shown, approved_by=OWNER, approved_at=2.0)
    with store.acquire_writer(project_id) as state:
        state.owner_id = OWNER
        state.roles['owner'] = OWNER
        state.lifecycle = ProjectLifecycle.FAILED.value
        state.revisions.source_revision = P16_SOURCE_REVISION
        state.revisions.qa_revision = P16_SOURCE_REVISION
        state.revisions.preview_revision = P16_SOURCE_REVISION
        state.deployment['latest_shown_preview'] = shown
        state.deployment['approval'] = approval
        state.deployment['promotion_intent'] = {
            'operation_id': P16_OPERATION_ID,
            'deployment_id': P16_PREVIEW_DEPLOYMENT_ID,
            'promoted_deployment_id': P16_PROMOTED_DEPLOYMENT_ID,
            'previous_production': None,
            'previous_production_class': PREVIOUS_PRODUCTION_BOOTSTRAP,
            'previous_production_deployment_id': None,
            'stage': 'smoked',
            'created_at': 3.0,
        }
        pending = build_pending_publication(
            operation_id=P16_OPERATION_ID,
            source_revision=P16_SOURCE_REVISION,
            source_sha256=P16_SOURCE,
            artifact_sha256=P16_ARTIFACT,
            now=3.0,
        )
        pending['stage'] = STAGE_PRODUCTION_CONFIRMED
        state.deployment['pending_publication'] = pending
        state.failure = {
            'phase': 'promotion', 'error': 'PROMOTE_FAILED',
            'error_code': 'PROMOTION_IDENTITY_UNPROVEN', 'failed_at': 4.0,
        }
        state.production_url = None
        store.save(state)
    return shown


def _orchestrator(tmp_path, vercel, smoke=None, project_id=P16):
    store = ProjectStateStore(tmp_path / 'state')
    runner = ProjectRunner(workspace_root=tmp_path / 'ws',
                           state_store=store,
                           hermes_home=tmp_path / 'hermes')
    orch = PromotionOrchestrator(runner, store, PromoteDeps(
        vercel=vercel, telegram=FakeTelegram(), smoke=smoke or FakeSmoke(),
        chat_id_for=lambda pid, state: 'chat-1'))
    return orch, store


def _workspace(tmp_path, project_id=P16):
    workspace = tmp_path / 'ws' / project_id
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


# ---------------------------------------------------------------------------
# The core contract: recovery binds the PROMOTED deployment, not the preview
# ---------------------------------------------------------------------------

def test_recovery_binds_promoted_deployment_not_preview(tmp_path):
    """p16 exactly: recovery must reconcile the provider binding against the
    PROMOTED deployment B, adopt it with the DIRECT proof, re-run the
    production smoke, and finish LIVE -- with zero promote POSTs."""
    vercel = P16Vercel('promoted_b')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p16_state(store)

    result = orch.resume_publish(P16, _workspace(tmp_path), principal_id=OWNER)

    assert result.success, result.error_code
    # The reconciliation carried the PROMOTED deployment id, not the preview.
    assert len(vercel.reconcile_external_calls) == 1
    assert vercel.reconcile_external_calls[0]['deployment_id'] == \
        P16_PROMOTED_DEPLOYMENT_ID
    # No promote, no new deployment, no rebuild.
    assert vercel.promote_calls == []
    # The canonical host was resolved against B.
    assert vercel.canonical_calls == [P16_PROMOTED_DEPLOYMENT_ID]
    # The smoke ran against the canonical production URL.
    assert orch.deps.smoke.calls == [P16_CANONICAL_URL]
    # LIVE only after the smoke passed.
    state = store.load(P16)
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    assert state.production_url == P16_CANONICAL_URL
    assert state.failure is None


def test_recovery_preserves_provenance_and_previous_production_evidence(tmp_path):
    """Durable provenance is NEVER rewritten: the intent's deployment_id stays
    the preview A, its promoted_deployment_id stays B, and the persisted
    previous-production evidence is byte-identical before and after."""
    vercel = P16Vercel('promoted_b')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p16_state(store)
    before_intent = dict(store.load(P16).deployment['promotion_intent'])

    result = orch.resume_publish(P16, _workspace(tmp_path), principal_id=OWNER)

    assert result.success, result.error_code
    state = store.load(P16)
    intent = state.deployment['promotion_intent']
    # The approval/promotion provenance is untouched.
    assert intent['deployment_id'] == P16_PREVIEW_DEPLOYMENT_ID
    assert intent['promoted_deployment_id'] == P16_PROMOTED_DEPLOYMENT_ID
    assert intent['operation_id'] == P16_OPERATION_ID
    # The previous-production evidence is untouched.
    assert intent['previous_production'] == before_intent['previous_production']
    assert intent['previous_production_class'] == \
        before_intent['previous_production_class']
    assert intent['previous_production_deployment_id'] == \
        before_intent['previous_production_deployment_id']
    # The durable stage evidence survives: "smoked" is not walked back to
    # "promoted".
    assert intent['stage'] == 'live'
    # Every durable provenance field is byte-identical to the seeded record.
    for key in ('deployment_id', 'promoted_deployment_id', 'operation_id',
                'previous_production', 'previous_production_class',
                'previous_production_deployment_id', 'created_at'):
        assert intent[key] == before_intent[key]


def test_recovery_fails_closed_when_promoted_deployment_is_not_production(tmp_path):
    """When B no longer matches the provider's production binding, recovery
    fails closed: PROMOTION_IDENTITY_UNPROVEN, zero promotes, zero smoke,
    lifecycle stays FAILED, provenance untouched."""
    vercel = P16Vercel('wrong')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p16_state(store)

    result = orch.resume_publish(P16, _workspace(tmp_path), principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'PROMOTION_IDENTITY_UNPROVEN'
    assert vercel.promote_calls == []
    assert orch.deps.smoke.calls == []
    state = store.load(P16)
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    intent = state.deployment['promotion_intent']
    assert intent['deployment_id'] == P16_PREVIEW_DEPLOYMENT_ID
    assert intent['promoted_deployment_id'] == P16_PROMOTED_DEPLOYMENT_ID
    assert intent['stage'] == 'smoked'


def test_recovery_fails_closed_on_operation_id_mismatch(tmp_path):
    """A tampered promotion_intent.operation_id is refused before any remote
    call: RESUME_NOT_APPLICABLE with zero remote calls."""
    vercel = P16Vercel('promoted_b')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p16_state(store)
    with store.acquire_writer(P16) as state:
        state.deployment['promotion_intent']['operation_id'] = 'tampered-op'
        store.save(state)

    result = orch.resume_publish(P16, _workspace(tmp_path), principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'RESUME_NOT_APPLICABLE'
    assert vercel.reconcile_external_calls == []
    assert vercel.promote_calls == []
    assert orch.deps.smoke.calls == []


def test_recovery_without_promoted_deployment_is_unchanged(tmp_path):
    """Absent promoted_deployment_id (pre-boundary): the binding falls back to
    the approval preview A and the pre-promotion semantics are unchanged --
    the p9 shape still recovers by DIRECT identity on A."""
    from tests.test_promote_parity import P9Vercel, _seed_p9_state, P9, \
        P9_OPERATION_ID, P9_DEPLOYMENT_ID

    vercel = P9Vercel('promoted_by_id')
    store = ProjectStateStore(tmp_path / 'state')
    runner = ProjectRunner(workspace_root=tmp_path / 'ws',
                           state_store=store,
                           hermes_home=tmp_path / 'hermes')
    orch = PromotionOrchestrator(runner, store, PromoteDeps(
        vercel=vercel, telegram=FakeTelegram(), smoke=FakeSmoke(),
        chat_id_for=lambda pid, state: 'chat-1'))
    _seed_p9_state(store)
    workspace = tmp_path / 'ws' / P9
    workspace.mkdir(parents=True)

    result = orch.resume_publish(P9, workspace, principal_id=OWNER)

    assert result.success, result.error_code
    assert vercel.reconcile_external_calls[0]['deployment_id'] == P9_DEPLOYMENT_ID
    assert vercel.promote_calls == []
    state = store.load(P9)
    assert state.lifecycle == ProjectLifecycle.LIVE.value


def test_recovery_never_creates_a_deployment_or_rebuilds(tmp_path):
    """The fake records every promote POST; the recovery must issue none, and
    the deployment_url must never be a synthesized <name>.vercel.app."""
    vercel = P16Vercel('promoted_b')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p16_state(store)

    result = orch.resume_publish(P16, _workspace(tmp_path), principal_id=OWNER)

    assert result.success, result.error_code
    assert vercel.promote_calls == []
    state = store.load(P16)
    # The production URL is the canonical host resolved for B, never a
    # synthesized deployment host.
    assert state.production_url == P16_CANONICAL_URL
    assert state.production_url.startswith('https://')
    assert 'dpl_' not in state.production_url


def test_recovery_smoke_failure_stays_non_live_with_truthful_state(tmp_path):
    """When the production smoke fails, the project stays non-LIVE with a
    truthful production-smoke failure state, and no rollback target exists
    (p16's previous production was a bootstrap placeholder)."""
    vercel = P16Vercel('promoted_b')
    orch, store = _orchestrator(tmp_path, vercel, smoke=FakeSmoke(success=False))
    _seed_p16_state(store)

    result = orch.resume_publish(P16, _workspace(tmp_path), principal_id=OWNER)

    assert not result.success
    assert orch.deps.smoke.calls == [P16_CANONICAL_URL]
    state = store.load(P16)
    assert state.lifecycle != ProjectLifecycle.LIVE.value
    assert state.failure is not None
    assert state.failure['phase'] == 'promotion'
    # Provenance is still untouched.
    intent = state.deployment['promotion_intent']
    assert intent['deployment_id'] == P16_PREVIEW_DEPLOYMENT_ID
    assert intent['promoted_deployment_id'] == P16_PROMOTED_DEPLOYMENT_ID


def test_recovery_reruns_smoke_only_not_the_whole_operation(tmp_path):
    """The pending stage is PRODUCTION_CONFIRMED (not SMOKE_PASSED), so the
    smoke re-runs; but the promotion itself is NOT re-issued (zero promote
    POSTs) and the canonical resolution runs exactly once."""
    vercel = P16Vercel('promoted_b')
    orch, store = _orchestrator(tmp_path, vercel)
    _seed_p16_state(store)

    result = orch.resume_publish(P16, _workspace(tmp_path), principal_id=OWNER)

    assert result.success, result.error_code
    assert len(orch.deps.smoke.calls) == 1
    assert len(vercel.canonical_calls) == 1
    assert vercel.promote_calls == []
