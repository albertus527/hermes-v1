"""Real FRONTEND CLI adapter with injected config resolver/process runner."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.state import ProjectStateStore
from app.hermes.adapter import HermesAdapter


def adapter_for(tmp_path):
    return HermesAdapter(ProjectStateStore(tmp_path / 'state'),
                         hermes_home=tmp_path / 'profile', repo_root=tmp_path / 'repo')


def test_frontend_build_uses_configured_role_in_real_cli_argv(tmp_path, monkeypatch):
    adapter = adapter_for(tmp_path)
    cfg = {'model': {'default': 'wrong-default', 'provider': 'wrong-provider'},
           'website_builder': {'models': {'FRONTEND': {'model': ' frontend-model ', 'provider': ' router '}}}}
    resolver = Mock(return_value=cfg)
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({'design_dna': {'brand': 'Northcut'}}), stderr=''))
    monkeypatch.setattr('app.hermes.adapter.load_config', resolver)
    monkeypatch.setattr('app.hermes.adapter.subprocess.run', runner)
    result = adapter.frontend_build('app', {'name': 'Northcut', 'what': 'shop', 'why': 'visit'}, tmp_path)
    assert result['success'], result
    resolver.assert_called_once_with()
    runner.assert_called_once()
    argv = runner.call_args.args[0]
    assert argv[argv.index('--model') + 1] == 'frontend-model'
    assert argv[argv.index('--provider') + 1] == 'router'
    assert argv[argv.index('--toolsets') + 1] == 'file,terminal,skills'
    assert 'Northcut' in argv[argv.index('-z') + 1]
    assert 'wrong-default' not in argv and 'wrong-provider' not in argv
    assert runner.call_args.kwargs['cwd'] == str(tmp_path)
    assert runner.call_args.kwargs['env']['HERMES_HOME'] == str(tmp_path / 'profile')
    assert runner.call_args.kwargs['env']['PROJECT_ID'] == 'app'


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

    runner = Mock(return_value=SimpleNamespace(
        returncode=0, stdout=json.dumps({'design_dna': {'brand': 'Northcut'}}), stderr=''))
    monkeypatch.setattr('app.hermes.adapter.load_config', resolver)
    monkeypatch.setattr('app.hermes.adapter.subprocess.run', runner)

    result = adapter.frontend_build('app', {'name': 'Northcut', 'what': 'shop', 'why': 'visit'}, tmp_path)

    assert result['success'], result
    # The override must have been installed while the role config was read.
    assert observed['home'] == adapter.hermes_home
    # And the profile override must be released afterwards.
    from hermes_constants import get_hermes_home_override
    assert get_hermes_home_override() is None
