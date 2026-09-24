"""Local behavioral regression tests; no network or credentials."""
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.core.state import ProjectStateStore
from app.qa.findings import DeterministicFindings, QAAttempt, VisionFindings
from app.qa.orchestrator import QAOrchestrator
from app.sandbox.runner import ProjectRunner, WorkspaceError


@pytest.mark.parametrize('project_id', ['../escape', 'x/y', 'x\\y', 'CON', 'NUL', 'x\n', '', 'a' * 65])
def test_state_rejects_unsafe_ids(tmp_path, project_id):
    store = ProjectStateStore(tmp_path)
    with pytest.raises(ValueError):
        store.load(project_id)
    with pytest.raises(ValueError):
        with store.acquire_writer(project_id):
            pass


def test_zero_timeout_acquires_available_lock(tmp_path):
    store = ProjectStateStore(tmp_path)
    with store.acquire_writer('a', timeout=0) as state:
        state.brief['name'] = 'Local'
        store.save(state)
        with pytest.raises(TimeoutError):
            with store.acquire_writer('a', timeout=0):
                pass
    assert store.load('a').brief['name'] == 'Local'


def test_runner_rejects_sibling_prefix(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    runner = ProjectRunner(tmp_path / 'work', store)
    runner.create_workspace('a')
    sibling = runner.create_workspace('ab')
    for invoke in (runner.run_command, runner.start_background):
        with pytest.raises(WorkspaceError):
            invoke('a', [sys.executable, '-c', 'pass'], cwd=sibling)


def test_command_timeout_reaps_process(tmp_path):
    runner = ProjectRunner(tmp_path / 'work', ProjectStateStore(tmp_path / 'state'))
    runner.create_workspace('a')
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run_command('a', [sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.1)
    assert runner.process_tracker.cleanup_all() == 0


def test_failed_rebuild_never_renders_stale_output(tmp_path):
    store = ProjectStateStore(tmp_path / 'state')
    with store.acquire_writer('project') as state:
        state.lifecycle = 'RUNNING'
        state.revisions.source_revision = 5
        state.revisions.qa_revision = 5
        state.revisions.preview_revision = 5
        state.revisions.approved_revision = 5
        store.save(state)
    adapter = Mock()
    adapter.frontend_build.return_value = {'success': True, 'design_dna': {'version': 1}}
    qa = QAOrchestrator(ProjectRunner(tmp_path / 'work', store), store, adapter)
    blocked = QAAttempt(0, DeterministicFindings(failures=['broken']), VisionFindings(False, blocking_findings=['broken']))
    with patch.object(qa, '_run_one_attempt', return_value=blocked) as render, patch.object(
        qa, '_run_rebuild_checks', return_value=(False, False, True)
    ):
        result = qa.run('project', tmp_path, {})
    assert not result.success
    assert render.call_count == 1
    assert adapter.frontend_build.call_count == 2
    assert [a.attempt for a in result.attempts] == [0, 1, 2]
    state = store.load('project')
    assert state.revisions.source_revision == 7
    assert state.revisions.qa_revision == state.revisions.preview_revision == state.revisions.approved_revision == 0
    assert state.lifecycle == 'FAILED'
