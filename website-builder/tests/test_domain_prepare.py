"""Split domain commands through real dispatch, adapter and persisted state."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.deploy.adapters import HttpResponse, VercelAdapter
from app.projects.domain import CustomDomainOrchestrator
from app.sandbox.runner import ProjectRunner
from test_custom_domain import HOST, setup


def domain_dispatch(tmp_path):
    store, provider, browsers, _, deps = setup(tmp_path)
    with store.acquire_writer('app') as state:
        state.roles['owner'] = 'telegram:1'
        store.save(state)

    def call(action, event):
        # Fresh orchestrator/dispatcher per call exercises persisted preparation.
        domain = CustomDomainOrchestrator(ProjectRunner(tmp_path / 'workspaces', store), store, deps)
        dispatcher = TelegramDispatcher(store, None, domain=domain, workspace_for=lambda _: tmp_path / 'smoke')
        payload = {'update_id': event, 'message': {'from': {'id': 1}, 'chat': {'id': 555}, 'text': 'domain'}}
        return dispatcher.dispatch(payload, 'app', action,
            authenticated=AuthenticatedTelegramContext('1', '555'), hostname=HOST, ownership_claim=True)

    return store, provider, browsers, deps, call


@pytest.mark.parametrize('verified', [False, True])
@pytest.mark.parametrize('smoke_available', [False, True])
def test_prepare_ready_dns_never_verifies_smokes_or_attaches(tmp_path, verified, smoke_available):
    store, provider, browsers, deps, call = domain_dispatch(tmp_path)
    provider.misconfigured = False
    provider.verified = verified
    if not smoke_available:
        deps.smoke = None
    for event in (1, 2):
        result = call('domain_prepare', event)
        assert result.success and result.data['stage'] == 'DNS_PENDING'
        state = store.load('app')
        assert state.domain.connection_stage == 'DNS_PENDING'
        assert state.domain.attached_at is None and state.domain.verified_at is None
        assert state.domain.connection['add_attempted'] is True
        assert state.domain.connection['verify_attempted'] is False
        assert result.data['instructions'] and result.data['dns_records']
    posts = [url for method, url, _ in provider.calls if method == 'POST']
    assert len(posts) == 1 and '/verify' not in posts[0]
    assert provider.bound and not browsers
    assert not (tmp_path / 'smoke').exists()


def test_verify_requires_local_preparation_even_if_remote_ready(tmp_path):
    store, provider, browsers, _, call = domain_dispatch(tmp_path)
    provider.bound = provider.verified = True
    provider.misconfigured = False
    result = call('domain_verify', 1)
    assert result.error_code == 'DOMAIN_PREPARE_REQUIRED'
    assert not provider.calls and not browsers
    assert not store.load('app').domain.connection


@pytest.mark.parametrize('followup', ['domain_verify', 'domain_connect'])
def test_prepared_binding_verifies_after_restart_without_readding(tmp_path, followup):
    store, provider, browsers, _, call = domain_dispatch(tmp_path)
    assert call('domain_prepare', 1).success
    provider.misconfigured = False
    result = call(followup, 2)
    assert result.success and result.data['stage'] == 'ATTACHED'
    assert store.load('app').domain.attached_at is not None
    assert browsers
    posts = [url for method, url, _ in provider.calls if method == 'POST']
    assert len(posts) == 2
    assert sum('/verify' in url for url in posts) == 1


def test_domain_connect_remains_one_call_compatible(tmp_path):
    store, provider, browsers, _, call = domain_dispatch(tmp_path)
    provider.misconfigured = False
    result = call('domain_connect', 1)
    assert result.success and result.data['stage'] == 'ATTACHED'
    assert store.load('app').domain.attached_at is not None and browsers
    assert len([m for m, _, _ in provider.calls if m == 'POST']) == 2


class ConfigTransport:
    def __init__(self, cname):
        self.cname = cname

    def request(self, method, url, **kwargs):
        assert method == 'GET' and '/config?' in url
        return HttpResponse(200, json.dumps({'misconfigured': False,
            'recommendedCNAME': [{'rank': 1, 'value': self.cname}],
            'recommendedIPv4': [{'rank': 1, 'value': ['76.76.21.21']}]}).encode())


@pytest.mark.parametrize('target', ['cname.vercel-dns.com', 'project.vercel.app', 'CNAME.Vercel-DNS.com.'])
def test_cname_scalar_shape_preserved(target):
    result = VercelAdapter('key', 'team', 'namespace', ConfigTransport(target)).get_domain_config(HOST)
    assert result.success
    assert result.data['recommendedCNAME'][0]['value'] == target
    assert result.data['recommendedIPv4'][0]['value'] == ['76.76.21.21']


@pytest.mark.parametrize('target', [[], ['cname.vercel-dns.com'], None, 1, '', '-', 'a..com',
    '-a.com', 'a-.com', 'a' * 64 + '.com', 'https://cname.vercel-dns.com', 'a.com/path'])
def test_malformed_cname_fails_closed(target):
    result = VercelAdapter('key', 'team', 'namespace', ConfigTransport(target)).get_domain_config(HOST)
    assert not result.success and result.error_code == 'DOMAIN_CONFIG_LOOKUP_FAILED'
