"""BUG 5 regression: important successful transitions are operator-visible.

The p9 E2E "looked hung" largely because nothing between FAST and the user's
next message produced an application-level record of what the system had
decided. ``app/projects/promote.py`` had no logging at all, so an approval
that succeeded, a publish that started, a previous production that was
classified, a durable intent that was written, a promotion that was confirmed,
a production smoke that passed, and a project that went LIVE were all
invisible to an operator reading the logs.

These tests pin the boundary logs that matter, and pin what must NEVER appear
in them: a deployment-protection bypass token, an Authorization header, a
protected preview URL, or user payload text.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.contracts import OperationResult  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.projects.promote import (  # noqa: E402
    PREVIOUS_PRODUCTION_BOOTSTRAP,
    PREVIOUS_PRODUCTION_REAL,
)
from test_promote import (  # noqa: E402
    OWNER,
    FakeSmoke,
    FakeTelegram,
    FakeVercel,
    _approved_state,
    _make_workspace,
    _runner,
)
from test_promote_bootstrap_classification import (  # noqa: E402
    BOOTSTRAP_ID,
    _bootstrap_meta,
    _fake_provider,
)

SECRET = "bypass-secret-9f2a-do-not-log"


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


def _capture_promote_logs():
    from app.projects import promote as promote_module
    handler = _LogCapture()
    logger = promote_module.logger
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler, logger, previous_level


def _promote_deps(vercel, telegram, smoke):
    from app.projects.promote import PromoteDeps
    return PromoteDeps(vercel=vercel, telegram=telegram, smoke=smoke,
                       chat_id_for=lambda p, s: '123')


def test_happy_publish_logs_every_meaningful_boundary(tmp_path):
    handler, logger, previous = _capture_promote_logs()
    try:
        runner, store = _runner(tmp_path)
        ws = _make_workspace(tmp_path)
        _approved_state(store, 'proj')
        vercel = FakeVercel(previous_production='dpl_old')
        deps = _promote_deps(vercel, FakeTelegram(), FakeSmoke())
        from app.projects.promote import PromotionOrchestrator
        orch = PromotionOrchestrator(runner, store, deps)

        assert orch.approve('proj', principal_id=OWNER).success
        result = orch.promote('proj', ws, principal_id=OWNER)
        assert result.success, result.error
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    text = handler.text()
    assert "Approval accepted project=proj revision=1 deployment=dpl_1" in text
    assert "Publish starting project=proj revision=1" in text
    assert f"Previous production classified={PREVIOUS_PRODUCTION_REAL}" in text
    assert "Promotion intent persisted project=proj operation=op-1" in text
    assert "Promotion confirmed deployment=dpl_1" in text
    assert "Production smoke passed project=proj" in text
    assert "Project LIVE production_url=https://prod.vercel.app" in text
    # Every line is INFO: this is signal, not noise.
    assert all(r.levelno == logging.INFO for r in handler.records)
    # Concise: one record per boundary, no per-poll spam.
    assert len(handler.records) <= 8


class _BootstrapFirstPromoteVercel(FakeVercel):
    """Real bootstrap classification for the previous-production read, while
    promote/reconcile keep the ordinary fake behaviour."""

    def __init__(self, adapter, project):
        super().__init__()
        self._adapter = adapter
        self._project = project

    def lookup_project(self, app_id, *, expected_name=None):
        return OperationResult.ok({'project': self._project, 'app_id': app_id})

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        return self._adapter.find_production_deployment(
            app_id, project, expected_name=expected_name,
        )


def test_classification_line_reports_a_proven_bootstrap(tmp_path):
    handler, logger, previous = _capture_promote_logs()
    try:
        from app.core.contracts import OperationResult
        from app.projects.promote import PromotionOrchestrator
        from test_promote_bootstrap_classification import (
            NAMESPACE, TEAM, _adapter, _bootstrap_operation_id)
        runner, store = _runner(tmp_path)
        ws = _make_workspace(tmp_path)
        _approved_state(store, 'proj')
        adapter = _adapter(tmp_path)
        meta = {"wbOwner": adapter._marker('proj'),
                "wbBootstrap": _bootstrap_operation_id(
                    'proj', NAMESPACE)}
        adapter._call = _fake_provider(deployment_meta=meta)
        owned_project = {
            'id': 'prj_1', 'name': adapter.project_name_for('proj'),
            'accountId': TEAM,
            'env': [{'key': 'WEBSITE_BUILDER_OWNER',
                     'value': adapter._marker('proj'), 'type': 'plain'}],
            'targets': {'production': {'id': BOOTSTRAP_ID}},
        }
        vercel = _BootstrapFirstPromoteVercel(adapter, owned_project)
        orch = PromotionOrchestrator(
            runner, store, _promote_deps(vercel, FakeTelegram(), FakeSmoke()),
        )
        assert orch.approve('proj', principal_id=OWNER).success
        result = orch.promote('proj', ws, principal_id=OWNER)
        assert result.success, result.error
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    text = handler.text()
    assert f"Previous production classified={PREVIOUS_PRODUCTION_BOOTSTRAP}" \
        in text
    assert f"deployment={BOOTSTRAP_ID}" in text


def test_logs_never_carry_secrets_or_user_payload(tmp_path):
    handler, logger, previous = _capture_promote_logs()
    try:
        from app.projects.promote import PromotionOrchestrator
        runner, store = _runner(tmp_path)
        ws = _make_workspace(tmp_path)
        shown = _approved_state(store, 'proj')
        vercel = FakeVercel()
        # Any attempt to interpolate a secret or a protected preview URL into a
        # log line would show up here.
        vercel.bypass_secret = SECRET
        orch = PromotionOrchestrator(
            runner, store, _promote_deps(vercel, FakeTelegram(), FakeSmoke()),
        )
        assert orch.approve('proj', principal_id=OWNER).success
        assert orch.promote('proj', ws, principal_id=OWNER).success
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    text = handler.text()
    assert SECRET not in text
    assert "Authorization" not in text and "Bearer" not in text
    # The protected PREVIEW url is never logged (production url is fine -- it is
    # the site the user asked to publish).
    assert shown['preview_url'] not in text
    assert "https://prod.vercel.app" in text


def test_failed_publish_still_reaches_the_operator(tmp_path):
    """A fail-closed publish is not silent either: the classification line is
    logged before the refusal, so an operator can see WHY nothing happened."""
    from app.projects.promote import PromotionOrchestrator
    from app.projects.promote import PREVIOUS_PRODUCTION_UNKNOWN
    handler, logger, previous = _capture_promote_logs()
    try:
        runner, store = _runner(tmp_path)
        ws = _make_workspace(tmp_path)
        _approved_state(store, 'proj')
        vercel = FakeVercel()
        vercel.find_production_deployment = lambda *a, **k: __import__(
            'app.core.contracts', fromlist=['OperationResult']
        ).OperationResult.ok({'deployment_id': 'dpl_mystery',
                              'operation_id': None, 'source_revision': None,
                              'artifact_sha256': None})
        orch = PromotionOrchestrator(
            runner, store, _promote_deps(vercel, FakeTelegram(), FakeSmoke()),
        )
        assert orch.approve('proj', principal_id=OWNER).success
        result = orch.promote('proj', ws, principal_id=OWNER)
        assert not result.success
        assert result.error_code == 'INCOMPLETE_LOOKUP'
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    text = handler.text()
    assert f"Previous production classified={PREVIOUS_PRODUCTION_UNKNOWN}" \
        in text
    # Refused before any remote side effect: no intent, no confirmation.
    assert "Promotion intent persisted" not in text
    assert "Promotion confirmed" not in text


def test_a_smoke_failure_does_not_log_a_live_promotion(tmp_path):
    handler, logger, previous = _capture_promote_logs()
    try:
        from app.projects.promote import PromotionOrchestrator
        runner, store = _runner(tmp_path)
        ws = _make_workspace(tmp_path)
        _approved_state(store, 'proj')
        orch = PromotionOrchestrator(
            runner, store,
            _promote_deps(FakeVercel(previous_production='dpl_old'),
                          FakeTelegram(), FakeSmoke(success=False)),
        )
        assert orch.approve('proj', principal_id=OWNER).success
        assert not orch.promote('proj', ws, principal_id=OWNER).success
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    text = handler.text()
    # The promote DID happen remotely and was confirmed; the smoke is what
    # failed, so no LIVE line may exist.
    assert "Promotion confirmed deployment=dpl_1" in text
    assert "Production smoke passed" not in text
    assert "Project LIVE" not in text
    assert store.load('proj').lifecycle == ProjectLifecycle.FAILED.value
