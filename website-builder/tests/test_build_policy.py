"""Persisted composition and mutation admission behavior."""
from unittest.mock import Mock

import pytest

from app.core.state import ProjectStateStore
from app.core.references import ReferenceItem
from app.projects.build import FrontendBuilder
from app.qa.orchestrator import QAOrchestrator
from app.qa.findings import QAAttempt, DeterministicFindings, VisionFindings


@pytest.mark.parametrize('change,error', [
    ({'lifecycle': 'PAUSED'}, 'BUILD_NOT_ALLOWED_IN_LIFECYCLE'),
    ({'source': 1}, 'BUILD_NOT_ALLOWED_IN_LIFECYCLE'),
    ({'pending': True}, 'DIRECTION_CHOICE_PENDING'),
    ({'brief': {}}, 'REQUIREMENTS_INCOMPLETE'),
])
def test_build_guard_precedes_workspace(tmp_path, change, error):
    store = ProjectStateStore(tmp_path / 'state')
    with store.acquire_writer('project') as state:
        state.lifecycle = change.get('lifecycle', 'QUEUED')
        state.revisions.source_revision = change.get('source', 0)
        state.brief = change.get('brief', {'name': 'N', 'what': 'W', 'why': 'Y'})
        if change.get('pending'):
            state.design_directions = [{'label': 'A'}, {'label': 'B'}]
        store.save(state)
    before = store.load('project').to_dict()
    runner = Mock()
    runner.acquire_project.return_value = True
    result = FrontendBuilder(runner, store, Mock()).build('project', {})
    assert result.error == error
    runner.create_workspace.assert_not_called()
    runner.release_project.assert_called_once()
    assert store.load('project').to_dict() == before


@pytest.mark.parametrize('synthesis,success', [({'UX': 'Original hierarchy'}, True), ({}, False)])
def test_repair_composes_persisted_policy_validates_and_persists(tmp_path, synthesis, success):
    store = ProjectStateStore(tmp_path / 'state')
    with store.acquire_writer('project') as state:
        state.lifecycle = 'RUNNING'
        state.brief = {'name': 'Real', 'why_destination': 'owner@example.com'}
        state.design_dna = {'version': 1}
        state.design_references = {'UX': {'item': ReferenceItem(
            'UX', 'upload', 'a' * 64, 'image/png', 100).to_dict(), 'evidence': 'Clear hierarchy'}}
        state.selected_direction = {'label': 'Warm', 'descriptor': 'Quiet', 'palette': {}}
        state.revisions.approved_revision = 1
        state.deployment['approval'] = {'old': True}
        state.deployment['latest_shown_preview'] = {'old': True}
        store.save(state)
    adapter = Mock()
    dna = {'version': 2, 'reference_synthesis': synthesis}
    adapter.frontend_build.return_value = {'success': True, 'design_dna': dna}
    qa = QAOrchestrator(Mock(), store, adapter)
    attempt = QAAttempt(0, DeterministicFindings(failures=['broken']), VisionFindings(False))
    assert qa._repair('project', tmp_path, {'name': 'Wrong'}, {}, attempt) is success
    call = adapter.frontend_build.call_args.kwargs
    assert call['brief']['name'] == 'Real'
    for text in ('DESIGN REFERENCES', 'Warm', 'mailto:owner%40example.com', 'REPAIR TASK'):
        assert text in call['design_dna_instructions']
    fresh = ProjectStateStore(tmp_path / 'state').load('project')
    assert fresh.design_dna == (dna if success else {'version': 1})
    assert fresh.revisions.approved_revision == 0
    assert 'approval' not in fresh.deployment
    assert 'latest_shown_preview' not in fresh.deployment
