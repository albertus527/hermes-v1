"""BUG 4 regression: bootstrap production is distinguished from real production.

The p9 E2E could never complete its first publish. Vercel's unavoidable
first-deployment auto-promotion means a brand-new project ALWAYS has a
production deployment before the first real publish -- and that deployment is
Hermes' own content-free bootstrap placeholder. It carries no content identity
(wbOperation / wbRevision / wbArtifact), so the previous-production lookup
could only read it as "unknown" and refused to promote:

    lifecycle = PREVIEW_READY, production_url = null, promotion_intent = null
    -> INCOMPLETE_LOOKUP, forever

Four answers are now explicit, and only ONE of them may skip the rollback
target:

    NO_PRODUCTION                     -> previous_production = null
    KNOWN_BOOTSTRAP (positively
      proven from provider state)     -> previous_production = null
    REAL_PRODUCTION                   -> previous_production = full identity,
                                         rollback mandatory and unchanged
    UNKNOWN_OR_INCOMPLETE_PRODUCTION  -> fail closed, no promote at all

These tests pin that a bootstrap is only ever accepted on POSITIVE proof
(wbOwner + the deterministic wbBootstrap id + the project's own production
binding, re-read from the authoritative per-deployment endpoint), and that
every near-miss still fails closed.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.contracts import OperationResult  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.adapters import VercelAdapter  # noqa: E402
from app.projects.promote import (  # noqa: E402
    PREVIOUS_PRODUCTION_BOOTSTRAP,
    PREVIOUS_PRODUCTION_NONE,
    PREVIOUS_PRODUCTION_REAL,
    PREVIOUS_PRODUCTION_UNKNOWN,
    _classify_previous_production,
)
from test_promote import (  # noqa: E402
    OWNER,
    FakeSmoke,
    FakeTelegram,
    _approved_state,
    _make_workspace,
    _publishing_state,
    _runner,
)

APP_ID = "tg-1"
NAMESPACE = "test-namespace"
TEAM = "team-1"
BOOTSTRAP_ID = "dpl_bootstrap"


def _marker(app_id=APP_ID, namespace=NAMESPACE):
    """The ownership marker, derived only from the immutable app_id."""
    return hashlib.sha256(f"{namespace}\0{app_id}".encode()).hexdigest()


def _bootstrap_operation_id(app_id=APP_ID, namespace=NAMESPACE):
    # Recomputed here from the documented construction rather than read from
    # the adapter, so the test pins the contract instead of mirroring the
    # implementation it is checking.
    return hashlib.sha256(
        f"{namespace}\0bootstrap\0{app_id}".encode()
    ).hexdigest()


def _adapter(tmp_path, namespace=NAMESPACE):
    return VercelAdapter(token="t", team_id=TEAM, ownership_namespace=namespace)


def _project(production_id=BOOTSTRAP_ID):
    """A project carrying the ownership marker, i.e. one this installation
    actually owns (the marker -- not the friendly name -- is the authority)."""
    return {
        "id": "prj_1",
        "name": "pokeplay",
        "accountId": TEAM,
        "env": [{"key": "WEBSITE_BUILDER_OWNER", "value": _marker(),
                 "type": "plain"}],
        "targets": {"production": {"id": production_id, "type": "production"}},
    }


class _AdapterQueue:
    """Replays queued transport replies and records the requested paths."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.paths = []

    def request(self, method, path, *, headers=None, json_body=None, timeout=None):
        self.paths.append(path)
        if not self.replies:
            raise AssertionError(f"unexpected extra request: {method} {path}")
        return self.replies.pop(0)

    def invalidate(self):
        return None


# ---------------------------------------------------------------------------
# Classification contract
# ---------------------------------------------------------------------------

def test_classification_is_backward_compatible():
    """A collaborator that reports no classification at all is UNKNOWN, not a
    guess; the two previously meaningful shapes keep their exact old meaning."""
    assert _classify_previous_production(
        OperationResult.ok({"deployment_id": None})
    ) == (PREVIOUS_PRODUCTION_NONE, None)

    real = {"deployment_id": "dpl_9", "operation_id": "op-1",
            "source_revision": 2, "artifact_sha256": "c" * 64}
    assert _classify_previous_production(
        OperationResult.ok(real)
    ) == (PREVIOUS_PRODUCTION_REAL, real)

    # Incomplete identity, no discriminator -> fail closed.
    assert _classify_previous_production(
        OperationResult.ok({"deployment_id": "dpl_9", "operation_id": None,
                            "source_revision": None, "artifact_sha256": None})
    ) == (PREVIOUS_PRODUCTION_UNKNOWN, None)

    # A failed lookup is not classified at all.
    failed = OperationResult.fail("INCOMPLETE_LOOKUP",
                                  error_code="INCOMPLETE_LOOKUP")
    assert _classify_previous_production(failed)[0] == PREVIOUS_PRODUCTION_UNKNOWN


def test_bootstrap_discriminator_must_match_the_reported_deployment():
    """A proof for a different deployment is not proof for this one."""
    proof = {"deployment_id": BOOTSTRAP_ID, "bootstrap_operation_id": "x"}
    assert _classify_previous_production(OperationResult.ok({
        "deployment_id": "dpl_other", "operation_id": None,
        "source_revision": None, "artifact_sha256": None,
        "bootstrap_proof": proof,
    }))[0] == PREVIOUS_PRODUCTION_UNKNOWN

    assert _classify_previous_production(OperationResult.ok({
        "deployment_id": BOOTSTRAP_ID, "operation_id": None,
        "source_revision": None, "artifact_sha256": None,
        "bootstrap_proof": {"deployment_id": BOOTSTRAP_ID,
                            "bootstrap_operation_id": ""},
    }))[0] == PREVIOUS_PRODUCTION_UNKNOWN

    assert _classify_previous_production(OperationResult.ok({
        "deployment_id": BOOTSTRAP_ID, "operation_id": None,
        "source_revision": None, "artifact_sha256": None,
        "bootstrap_proof": proof,
    })) == (PREVIOUS_PRODUCTION_BOOTSTRAP, None)


# ---------------------------------------------------------------------------
# Adapter: positive proof only
# ---------------------------------------------------------------------------

def _bootstrap_meta():
    return {"wbOwner": _marker(), "wbBootstrap": _bootstrap_operation_id()}


def _fake_provider(*, list_meta="ABSENT", deployment_meta="ABSENT",
                   production_binding=BOOTSTRAP_ID, deployment_id=BOOTSTRAP_ID,
                   v13_id=None, v13_project_id=None, v13_team_id=None,
                   v9_id=None, calls=None):
    """A read-only provider fake over the three reads the lookup performs:

      * ``/v6/deployments?target=production``  -- the current production list
      * ``/v13/deployments/<id>``              -- authoritative per-deployment meta
      * ``/v9/projects/<name>``                -- the CURRENT production binding

    ``"ABSENT"`` means the field is genuinely missing from the response, which
    is the interesting case: absence must never be read as a fact.
    """
    def call(method, path, *args, **kwargs):
        if calls is not None:
            calls.append(path)
        if path.startswith('/v6/deployments'):
            item = {"uid": deployment_id, "readyState": "READY"}
            if list_meta != "ABSENT":
                item["meta"] = list_meta
            return 200, {"deployments": [item]}
        if '/v13/deployments/' in path:
            body = {"id": v13_id or deployment_id, "readyState": "READY",
                    "projectId": v13_project_id or "prj_1",
                    "teamId": v13_team_id or TEAM}
            if deployment_meta != "ABSENT":
                body["meta"] = deployment_meta
            return 200, body
        if path.startswith('/v9/projects/'):
            body = {"id": v9_id or "prj_1", "name": "pokeplay"}
            if production_binding is not None:
                body["targets"] = {"production": {"id": production_binding}}
            return 200, body
        raise AssertionError(f"unexpected request: {method} {path}")

    return call


def test_proven_bootstrap_is_classified(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._call = _fake_provider(deployment_meta=_bootstrap_meta())

    result = adapter.find_production_deployment(APP_ID, _project(),
                                                expected_name="pokeplay")

    assert result.success
    assert result.data["deployment_id"] == BOOTSTRAP_ID
    assert result.data["bootstrap_proof"] == {
        "deployment_id": BOOTSTRAP_ID,
        "bootstrap_operation_id": _bootstrap_operation_id(),
    }
    # A bootstrap carries no content identity, by construction.
    assert result.data["operation_id"] is None
    assert result.data["source_revision"] is None
    assert result.data["artifact_sha256"] is None
    assert _classify_previous_production(result)[0] == PREVIOUS_PRODUCTION_BOOTSTRAP


def test_bootstrap_proof_survives_a_list_without_meta(tmp_path):
    """The /v6 list does not always carry meta; the authoritative per-deployment
    read is what actually establishes the proof."""
    adapter = _adapter(tmp_path)
    calls = []
    adapter._call = _fake_provider(deployment_meta=_bootstrap_meta(),
                                   calls=calls)

    result = adapter.find_production_deployment(APP_ID, _project(),
                                                expected_name="pokeplay")

    assert result.success
    assert result.data["bootstrap_proof"]["deployment_id"] == BOOTSTRAP_ID
    assert any('/v13/deployments/' in p for p in calls)
    # The proof is completed by the authoritative production-binding read, not
    # by the deployment body.
    assert any(p.startswith('/v9/projects/') for p in calls)


@pytest.mark.parametrize("list_meta,deployment_meta,reason", (
    ("ABSENT", "ABSENT", "no metadata at all"),
    ("ABSENT", {}, "empty metadata"),
    ("ABSENT", {"wbOwner": "someone-elses-marker"}, "a different owner marker"),
    ("ABSENT", {"wbOwner": None, "wbBootstrap": None}, "null markers"),
    ("ABSENT", {"wbOwner": "wrong", "wbBootstrap": "wrong"}, "mismatched markers"),
    ("ABSENT", {"wbOwner": None, "wbBootstrap": "wrong"},
     "mismatched bootstrap id"),
    (_bootstrap_meta(), "ABSENT", "markers only on the list, absent on re-read"),
))
def test_unproven_bootstrap_fails_closed(tmp_path, list_meta, deployment_meta,
                                         reason):
    """Missing metadata is never enough. Only the exact app-scoped marker pair,
    confirmed by the authoritative re-read and the current production binding,
    proves a bootstrap; anything else stays INCOMPLETE_LOOKUP."""
    adapter = _adapter(tmp_path)
    adapter._call = _fake_provider(list_meta=list_meta,
                                   deployment_meta=deployment_meta)

    result = adapter.find_production_deployment(APP_ID, _project(),
                                                expected_name="pokeplay")

    assert not result.success, f"{reason} must not be treated as a bootstrap"
    assert result.error_code == "INCOMPLETE_LOOKUP"


def test_bootstrap_also_needs_the_content_identity_absent(tmp_path):
    """A deployment carrying BOTH the bootstrap marker and a content identity
    is ambiguous, not a pure placeholder."""
    adapter = _adapter(tmp_path)
    both = dict(_bootstrap_meta(), wbOperation="op-1", wbRevision="1",
                wbArtifact="a" * 64)
    adapter._call = _fake_provider(deployment_meta=both)

    result = adapter.find_production_deployment(APP_ID, _project(),
                                                expected_name="pokeplay")

    # A complete content identity wins: it is a REAL deployment, not a
    # placeholder, and it is a legitimate rollback target.
    assert result.success
    assert _classify_previous_production(result)[0] == PREVIOUS_PRODUCTION_REAL


def test_bootstrap_must_be_the_actual_production_binding(tmp_path):
    """Metadata alone is not enough: the project's CURRENT production binding
    (read from the provider) must point at this deployment, so a stray
    proven-bootstrap deployment elsewhere cannot be mistaken for the live one."""
    adapter = _adapter(tmp_path)
    adapter._call = _fake_provider(deployment_meta=_bootstrap_meta(),
                                   production_binding="dpl_something_else")

    result = adapter.find_production_deployment(
        APP_ID, _project(production_id="dpl_something_else"),
        expected_name="pokeplay",
    )

    assert not result.success
    assert result.error_code == "INCOMPLETE_LOOKUP"


def test_no_production_binding_reports_no_production_never_a_bootstrap(
        tmp_path):
    """No production binding means the project has NO production deployment --
    the honest answer, and never "this deployment is production". The caller
    classifies that as unknown and fails closed on it, which is the real
    invariant."""
    adapter = _adapter(tmp_path)
    adapter._call = _fake_provider(deployment_meta=_bootstrap_meta(),
                                   production_binding=None)

    result = adapter.find_production_deployment(APP_ID, _project(None),
                                                expected_name="pokeplay")

    assert result.success
    assert result.data == {"deployment_id": None}
    assert "bootstrap_proof" not in result.data
    # Classified as "no production at all" -- never as a rollback target.
    assert _classify_previous_production(result)[0] == "NO_PRODUCTION"


def test_authoritative_reread_mismatch_fails_closed(tmp_path):
    """A re-read that does not match the deployment id / project / team is not
    trusted, and the lookup fails closed."""
    for kwargs in (
        {"v13_id": "dpl_other"},
        {"v13_project_id": "prj_other"},
        {"v13_team_id": "team_other"},
        {"v9_id": "prj_other"},
    ):
        adapter = _adapter(tmp_path)
        adapter._call = _fake_provider(deployment_meta=_bootstrap_meta(), **kwargs)
        result = adapter.find_production_deployment(APP_ID, _project(),
                                                    expected_name="pokeplay")
        assert not result.success, kwargs
        assert result.error_code == "INCOMPLETE_LOOKUP", kwargs


def test_real_production_with_complete_metadata_needs_no_reread(tmp_path):
    """The common case is untouched: a complete content identity from the
    authoritative per-deployment read is used as-is, with no second read."""
    adapter = _adapter(tmp_path)
    calls = []
    real = dict(_bootstrap_meta(), wbOperation="op-7", wbRevision="3",
                wbArtifact="b" * 64)
    adapter._call = _fake_provider(deployment_meta=real, calls=calls,
                                   deployment_id="dpl_live",
                                   production_binding="dpl_live")

    result = adapter.find_production_deployment(APP_ID, _project("dpl_live"),
                                                expected_name="pokeplay")

    assert result.success
    assert _classify_previous_production(result)[0] == PREVIOUS_PRODUCTION_REAL
    # The production binding is resolved from the project, and the deployment
    # read happens exactly once.
    assert sum(1 for p in calls if p.startswith('/v9/projects/')) == 1
    assert sum(1 for p in calls if '/v13/deployments/' in p) == 1
    assert not any(p.startswith('/v6/deployments') for p in calls)


# ---------------------------------------------------------------------------
# Promotion: bootstrap is not a rollback target, real production still is
# ---------------------------------------------------------------------------

class _BootstrapVercel:
    """Fake Vercel reporting a PROVEN bootstrap as current production."""

    def __init__(self, project=None, **kw):
        from test_promote import FakeVercel
        self._inner = FakeVercel(**kw)
        self._project = project or {'id': 'prj_1', 'name': 'pokeplay',
                                    'accountId': 'team'}
        self.promote_calls = []
        self.promoted = []

    def lookup_project(self, app_id, *, expected_name=None):
        return self._inner.lookup_project(app_id, expected_name=expected_name)

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        return OperationResult.ok({
            'deployment_id': BOOTSTRAP_ID, 'operation_id': None,
            'source_revision': None, 'artifact_sha256': None,
            'bootstrap_proof': {'deployment_id': BOOTSTRAP_ID,
                                'bootstrap_operation_id': _bootstrap_operation_id()},
        })

    def reconcile_production_deployment(self, app_id, project, expected_identity, *,
                                        expected_name=None):
        return self._inner.reconcile_production_deployment(
            app_id, project, expected_identity, expected_name=expected_name)

    def reconcile_external_promotion(self, app_id, project, intended_identity, *,
                                     expected_name=None):
        return self._inner.reconcile_external_promotion(
            app_id, project, intended_identity, expected_name=expected_name)

    @property
    def production_now(self):
        return self._inner.production_now

    @production_now.setter
    def production_now(self, value):
        self._inner.production_now = value

    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        self.promote_calls.append(deployment_id)
        self.promoted.append((deployment_id, operation_id, source_revision,
                              artifact_sha256))
        return OperationResult.ok({
            'deployment_id': deployment_id,
            'production_url': 'https://prod.vercel.app',
            'state': 'READY',
        })


def _bootstrap_promote(tmp_path, smoke=None):
    from app.projects.promote import PromoteDeps, PromotionOrchestrator
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = _BootstrapVercel()
    deps = PromoteDeps(vercel=vercel, telegram=FakeTelegram(),
                       smoke=smoke or FakeSmoke(), chat_id_for=lambda p, s: '123')
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success
    return orch, store, ws, vercel, deps


def test_first_publish_over_a_proven_bootstrap_succeeds(tmp_path):
    orch, store, ws, vercel, deps = _bootstrap_promote(tmp_path)

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert result.success, result.error
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    assert state.production_url == 'https://prod.vercel.app'
    assert state.revisions.live_revision == 1
    # Exactly one promote, of the APPROVED deployment.
    assert vercel.promote_calls == ['dpl_1']
    # The intent records the classification and carries no rollback target.
    intent = state.deployment['promotion_intent']
    assert intent['previous_production_class'] == PREVIOUS_PRODUCTION_BOOTSTRAP
    assert intent['previous_production'] is None
    assert intent['previous_production_deployment_id'] is None
    # The exact approved artifact identity is still what got promoted.
    assert vercel.promoted[0] == ('dpl_1', 'op-1', 1, 'b' * 64)


def test_bootstrap_is_never_a_rollback_target(tmp_path):
    """If production smoke fails after the first publish, there is nothing
    meaningful to roll back to: the bootstrap is a placeholder, not a user
    site, and it is never re-promoted."""
    orch, store, ws, vercel, _deps = _bootstrap_promote(
        tmp_path, smoke=FakeSmoke(success=False)
    )

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.FAILED.value
    assert state.failure['error_code'] == 'SMOKE_FAILED'
    assert result.data['rollback'] == 'NO_PREVIOUS_PRODUCTION'
    # Exactly one promote total: the approved one. The bootstrap was never
    # promoted as a rollback.
    assert vercel.promote_calls == ['dpl_1']
    assert not state.production_url


def test_real_previous_production_still_uses_rollback(tmp_path):
    """REAL_PRODUCTION is unchanged: the full identity is persisted, a failed
    production smoke re-promotes the PREVIOUS deployment with ITS OWN
    identity, and the alias ends on the last known good."""
    from app.projects.promote import PromoteDeps, PromotionOrchestrator
    from test_promote import FakeVercel
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel(previous_production='dpl_prev')
    calls = []
    orig = vercel.promote_deployment

    def spy(app_id, project, deployment_id, operation_id, source_revision,
            artifact_sha256, *, expected_name=None):
        calls.append((deployment_id, operation_id, source_revision,
                      artifact_sha256))
        return orig(app_id, project, deployment_id, operation_id,
                    source_revision, artifact_sha256,
                    expected_name=expected_name)

    vercel.promote_deployment = spy
    deps = PromoteDeps(vercel=vercel, telegram=FakeTelegram(),
                       smoke=FakeSmoke(success=False),
                       chat_id_for=lambda p, s: '123')
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    state = store.load('proj')
    intent = state.deployment['promotion_intent']
    assert intent['previous_production_class'] == PREVIOUS_PRODUCTION_REAL
    assert intent['previous_production']['deployment_id'] == 'dpl_prev'
    assert state.failure['rollback'] == 'SUCCEEDED'
    assert state.failure['rollback_target']['deployment_id'] == 'dpl_prev'
    # Two promotes: the approved one, then the previous deployment re-promoted
    # with ITS OWN identity (op-0 / rev 1 / its own artifact).
    assert vercel.promote_calls == ['dpl_1', 'dpl_prev']
    assert calls[-1] == ('dpl_prev', 'op-0', 1, 'c' * 64)


def test_unknown_production_still_fails_closed_before_any_promote(tmp_path):
    from app.projects.promote import PromoteDeps, PromotionOrchestrator
    from test_promote import FakeVercel
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _approved_state(store, 'proj')
    vercel = FakeVercel()
    # A production deployment exists but is not positively identified and
    # carries no bootstrap proof.
    vercel.find_production_deployment = lambda *a, **k: OperationResult.ok(
        {'deployment_id': 'dpl_mystery', 'operation_id': None,
         'source_revision': None, 'artifact_sha256': None}
    )
    deps = PromoteDeps(vercel=vercel, telegram=FakeTelegram(), smoke=FakeSmoke(),
                       chat_id_for=lambda p, s: '123')
    orch = PromotionOrchestrator(runner, store, deps)
    assert orch.approve('proj', principal_id=OWNER).success

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'INCOMPLETE_LOOKUP'
    assert vercel.promote_calls == []
    state = store.load('proj')
    # Untouched: no PUBLISHING, no durable intent, no production URL.
    assert state.lifecycle == ProjectLifecycle.PREVIEW_READY.value
    assert state.deployment.get('promotion_intent') is None
    assert state.production_url is None


def _crashed_promotion(store, intent):
    """A PUBLISHING project with a durable promotion intent already on disk."""
    _approved_state(store, 'proj')
    with store.acquire_writer('proj') as state:
        state.lifecycle = ProjectLifecycle.PUBLISHING.value
        state.roles['owner'] = OWNER
        state.deployment['approval'] = {
            'operation_id': 'op-1', 'source_revision': 1,
            'preview_url': 'https://tested.vercel.app', 'deployment_id': 'dpl_1',
            'source_sha256': 'a' * 64, 'artifact_sha256': 'b' * 64,
            'approved_by': OWNER, 'approved_at': 1.0,
        }
        state.revisions.approved_revision = 1
        state.deployment['promotion_intent'] = intent
        store.save(state)


def test_same_operation_retry_over_a_bootstrap_reuses_the_intent(tmp_path):
    """Resume semantics are unchanged by the classification: a retry of the
    SAME operation reuses the persisted intent (and its class) and reconciles
    remote truth before acting, rather than re-classifying."""
    from app.projects.promote import PromoteDeps, PromotionOrchestrator
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _crashed_promotion(store, {
        'operation_id': 'op-1',
        'deployment_id': 'dpl_1',
        'previous_production': None,
        'previous_production_class': PREVIOUS_PRODUCTION_BOOTSTRAP,
        'previous_production_deployment_id': None,
        'stage': 'publishing',
        'created_at': 1.0,
    })
    vercel = _BootstrapVercel()
    # Remote truth: the promote already landed.
    vercel.production_now = {'deployment_id': 'dpl_1', 'operation_id': 'op-1',
                             'source_revision': 1, 'artifact_sha256': 'b' * 64}
    deps = PromoteDeps(vercel=vercel, telegram=FakeTelegram(), smoke=FakeSmoke(),
                       chat_id_for=lambda p, s: '123')
    orch = PromotionOrchestrator(runner, store, deps)

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert result.success, result.error
    # Adopted from the reconcile: NO second promote POST.
    assert vercel.promote_calls == []
    state = store.load('proj')
    assert state.lifecycle == ProjectLifecycle.LIVE.value
    assert state.deployment['promotion_intent']['previous_production_class'] == \
        PREVIOUS_PRODUCTION_BOOTSTRAP
    assert state.deployment['promotion_intent']['previous_production'] is None


def test_legacy_intent_without_previous_production_still_fails_closed(tmp_path):
    """A pre-existing partial intent cannot prove its rollback target, and the
    new classification path does not become a way around that."""
    from app.projects.promote import PromoteDeps, PromotionOrchestrator
    runner, store = _runner(tmp_path)
    ws = _make_workspace(tmp_path)
    _crashed_promotion(store, {
        # The pre-hardening durable shape: a deployment id was recorded, but
        # never the full identity a safe rollback needs.
        'operation_id': 'op-1', 'deployment_id': 'dpl_1',
        'previous_production_deployment_id': 'dpl_prev',
        'stage': 'publishing', 'created_at': 1.0,
    })
    vercel = _BootstrapVercel()
    deps = PromoteDeps(vercel=vercel, telegram=FakeTelegram(), smoke=FakeSmoke(),
                       chat_id_for=lambda p, s: '123')
    orch = PromotionOrchestrator(runner, store, deps)

    result = orch.promote('proj', ws, principal_id=OWNER)

    assert not result.success
    assert result.error_code == 'INCOMPLETE_LOOKUP'
    assert vercel.promote_calls == []
    assert store.load('proj').lifecycle == ProjectLifecycle.PUBLISHING.value
