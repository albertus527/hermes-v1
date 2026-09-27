"""Real FRONTEND CLI adapter with injected config resolver/process runner."""
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.state import ProjectStateStore
from app.hermes.adapter import HermesAdapter
from app.hermes import watchdog as wd


def adapter_for(tmp_path):
    return HermesAdapter(ProjectStateStore(tmp_path / 'state'),
                         hermes_home=tmp_path / 'profile', repo_root=tmp_path / 'repo')


def completed_run(stdout):
    """A successful ``SupervisedRun`` for a child that exited cleanly.

    FRONTEND builds are supervised (activity-aware watchdog), so the real
    execution seam is ``supervise_frontend_run`` -> ``subprocess.Popen``.
    Mocking ``subprocess.run`` alone does NOT intercept the child; the
    watchdog spawns through Popen. These tests assert the CLI argv that was
    actually handed to the supervisor, so they must patch the supervisor's
    own spawn.
    """
    return wd.SupervisedRun(returncode=0, stdout=stdout, stderr='',
                            outcome=None, diagnostics={})


def test_frontend_build_uses_configured_role_in_real_cli_argv(tmp_path, monkeypatch):
    adapter = adapter_for(tmp_path)
    cfg = {'model': {'default': 'wrong-default', 'provider': 'wrong-provider'},
           'website_builder': {'models': {'FRONTEND': {'model': ' frontend-model ', 'provider': ' router '}}}}
    resolver = Mock(return_value=cfg)
    runner = Mock(return_value=completed_run(json.dumps({'design_dna': {'brand': 'Northcut'}})))
    monkeypatch.setattr('app.hermes.adapter.load_config', resolver)
    # The adapter imports ``watchdog as wd`` lazily inside the method, so the
    # patch target is the watchdog module, not an adapter attribute.
    monkeypatch.setattr('app.hermes.watchdog.supervise_frontend_run', runner)
    result = adapter.frontend_build('app', {'name': 'Northcut', 'what': 'shop', 'why': 'visit'}, tmp_path)
    assert result['success'], result
    resolver.assert_called_once_with()
    runner.assert_called_once()
    # supervise_frontend_run receives the argv positionally.
    argv = runner.call_args.args[0]
    assert argv[argv.index('--model') + 1] == 'frontend-model'
    assert argv[argv.index('--provider') + 1] == 'router'
    assert argv[argv.index('--toolsets') + 1] == 'file,terminal,skills'
    assert 'Northcut' in argv[argv.index('-z') + 1]
    assert 'wrong-default' not in argv and 'wrong-provider' not in argv
    # The supervisor receives the workspace as a Path and stringifies it only
    # at the Popen boundary; the contract under test is "the FRONTEND cwd is
    # the project workspace", not the concrete type.
    assert str(runner.call_args.kwargs['cwd']) == str(tmp_path)
    env = runner.call_args.kwargs['env']
    assert env['HERMES_HOME'] == str(tmp_path / 'profile')
    assert env['PROJECT_ID'] == 'app'
    # R2-B1: the supervised FRONTEND child gets a role-scoped environment,
    # never a copy of the parent process environment.
    assert set(env) != set(os.environ)


@pytest.mark.parametrize('selection', [None, [], 'frontend-model', {}, {'model': 'm'},
    {'provider': 'router'}, {'model': '', 'provider': 'router'},
    {'model': 'm', 'provider': ' '}, {'model': 1, 'provider': 'router'},
    {'model': 'm', 'provider': False}])
def test_malformed_frontend_role_fails_before_process_or_skill_writes(tmp_path, monkeypatch, selection):
    adapter = adapter_for(tmp_path)
    resolver = Mock(return_value={'model': {'default': 'available-default', 'provider': 'router'},
        'website_builder': {'models': {'FRONTEND': selection}}})
    runner = Mock(side_effect=AssertionError('must not launch'))
    monkeypatch.setattr('app.hermes.adapter.load_config', resolver)
    monkeypatch.setattr('app.hermes.adapter.subprocess.run', runner)
    result = adapter.frontend_build('app', {'name': 'N'}, tmp_path)
    assert not result['success'] and 'malformed' in result['error']
    runner.assert_not_called()
    assert not (tmp_path / 'profile').exists()


def test_missing_frontend_role_never_falls_back_to_default(tmp_path, monkeypatch):
    adapter = adapter_for(tmp_path)
    monkeypatch.setattr('app.hermes.adapter.load_config', lambda: {'model': {'default': 'available-default'}})
    runner = Mock()
    monkeypatch.setattr('app.hermes.adapter.subprocess.run', runner)
    assert not adapter.frontend_build('app', {}, tmp_path)['success']
    runner.assert_not_called()


def test_frontend_role_config_resolved_under_website_profile_scope(tmp_path, monkeypatch):
    """H-3: the FRONTEND role mapping must be read from the WEBSITE profile's
    config.yaml, not the DEFAULT Hermes profile.

    `load_config()` resolves the config path via `get_hermes_home()`, which is
    only scoped by `set_hermes_home_override`. The CLI boundary previously
    loaded the role config OUTSIDE `_hermes_home_scope()`, so a role configured
    only in the website profile could pass preflight yet resolve against the
    default profile (or fail) at real build time.
    """
    adapter = adapter_for(tmp_path)
    observed = {}

    def resolver():
        from hermes_constants import get_hermes_home
        observed['home'] = get_hermes_home()
        return {
            'model': {'default': 'wrong-default', 'provider': 'wrong-provider'},
            'website_builder': {'models': {'FRONTEND': {'model': 'frontend-model', 'provider': 'router'}}},
        }

    runner = Mock(return_value=completed_run(json.dumps({'design_dna': {'brand': 'Northcut'}})))
    monkeypatch.setattr('app.hermes.adapter.load_config', resolver)
    monkeypatch.setattr('app.hermes.watchdog.supervise_frontend_run', runner)

    result = adapter.frontend_build('app', {'name': 'Northcut', 'what': 'shop', 'why': 'visit'}, tmp_path)

    assert result['success'], result
    # The override must have been installed while the role config was read.
    assert observed['home'] == adapter.hermes_home
    # And the profile override must be released afterwards.
    from hermes_constants import get_hermes_home_override
    assert get_hermes_home_override() is None
