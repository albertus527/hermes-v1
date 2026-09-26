"""Phase 16 real adapters/orchestrator/shared smoke, entirely offline."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.core.state import ProjectStateStore
from app.deploy.adapters import HttpResponse, PreviewSmokeTester, VercelAdapter, valid_custom_hostname
from app.projects.domain import CustomDomainOrchestrator, DomainDeps
from app.sandbox.runner import ProjectRunner
from test_preview_adapters import Browser

HOST = 'www.customer.com'


class Provider:
    def __init__(self):
        self.calls = []
        self.bound = False
        self.verified = False
        self.misconfigured = True
        self.add_timeout = False
        self.verify_timeout = False
        self.missing = False
        self.domain_override = {}
        self.on_post = lambda: None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert parse_qs(urlsplit(url).query)['teamId'] == ['team']
        path = urlsplit(url).path
        if method == 'POST':
            self.on_post()
            if path.endswith('/verify'):
                self.verified = not self.verify_timeout
                if self.verify_timeout:
                    raise TimeoutError('secret')
            elif path.endswith('/domains'):
                assert json.loads(kwargs['data']) == {'name': HOST}
                self.bound = True
                if self.add_timeout:
                    raise TimeoutError('secret')
            else:
                pytest.fail('unexpected mutation')
            body = {'name': HOST, 'verified': self.verified}
        elif '/deployments/' in path:
            body = self.deployment
        elif path.endswith('/config'):
            body = {'misconfigured': self.misconfigured,
                    'recommendedCNAME': [{'rank': 1, 'value': 'cname.vercel-dns.com'}],
                    'recommendedIPv4': [{'rank': 1, 'value': ['76.76.21.21']}]}
        elif '/domains/' in path:
            if not self.bound or self.missing:
                return HttpResponse(404, b'{}')
            body = {'name': HOST, 'verified': self.verified,
                    'verification': [{'type': 'TXT', 'domain': '_vercel.' + HOST,
                                      'value': 'vc-domain-verify=challenge'}], **self.domain_override}
        else:
            # Project-name lookup (GET /v9/projects/<name>). The provider only
            # answers under the project's REAL name -- any other key 404s.
            # This is what proves the canonical slug is used as the lookup
            # KEY (an opaque-name fallback misses entirely).
            if path.rsplit('/', 1)[-1] != self.project['name']:
                return HttpResponse(404, b'{}')
            body = self.project
        return HttpResponse(200, json.dumps(body).encode())


def setup(tmp_path, lifecycle='LIVE', browser_options=None, slug=None):
    store = ProjectStateStore(tmp_path / 'state')
    runner = ProjectRunner(tmp_path / 'workspaces', store)
    provider = Provider()
    adapter = VercelAdapter('secret', 'team', 'installation', provider)
    identity = {'operation_id': 'op', 'deployment_id': 'dpl', 'source_revision': 1,
                'source_sha256': 'a' * 64, 'artifact_sha256': 'b' * 64}
    # slug=None keeps the legacy opaque project; slug='...' models a
    # preview-created project living under the canonical friendly slug.
    name = slug or adapter.project_name_for('app')
    provider.project = {'id': 'prj', 'accountId': 'team', 'name': name,
                        'env': [{'key': 'WEBSITE_BUILDER_OWNER', 'type': 'plain',
                                 'value': adapter._marker('app')}],
                        'targets': {'production': {'id': 'dpl'}}}
    provider.deployment = {'id': 'dpl', 'projectId': 'prj', 'teamId': 'team',
                           'name': name, 'target': 'production',
                           'readyState': 'READY', 'meta': adapter._meta('app', 'op', 1, 'b' * 64)}
    with store.acquire_writer('app') as state:
        state.roles = {'owner': 'owner', 'reviewers': ['reviewer'], 'viewers': ['viewer']}
        state.lifecycle = lifecycle
        state.revisions.live_revision = state.revisions.approved_revision = 1
        state.deployment = {'approval': identity.copy(), 'last_live_deployment': identity.copy()}
        state.domain.status = 'TAKEN'
        store.save(state)
    browsers = []
    def factory():
        browser = Browser(**(browser_options or {}))
        browsers.append(browser)
        return browser
    smoke = PreviewSmokeTester(factory, lambda _: ['8.8.8.8'], custom_hostname=HOST)
    deps = DomainDeps(adapter, smoke,
                      slug_for=(lambda pid, state: slug) if slug else None)
    def restart():
        return CustomDomainOrchestrator(runner, ProjectStateStore(store.root), deps)
    def connect(**kwargs):
        return restart().connect('app', kwargs.pop('hostname', HOST), tmp_path / 'smoke',
                                 principal_id=kwargs.pop('principal_id', 'owner'),
                                 ownership_claim=kwargs.pop('ownership_claim', True), **kwargs)
    return store, provider, browsers, connect, deps


@pytest.mark.parametrize('hostname', ['', 'localhost', 'a.local', '127.0.0.1', '[::1]',
    'https://customer.com', 'x.com/path', 'x.com:443', 'u@x.com', '*.x.com',
    'x.com.', 'X.com', ' x.com', 'x.com\n', 'a..com', '-a.com', 'a-.com',
    'a_b.com', 'a.com?x', 'a.com#x', 'é.com', 'a' * 64 + '.com', 'x.vercel.app'])
def test_strict_hostname(hostname):
    assert not valid_custom_hostname(hostname)


@pytest.mark.parametrize('principal', [None, 'reviewer', 'viewer', 'stranger'])
def test_owner_gate_before_all_effects(tmp_path, principal):
    store, p, browsers, connect, _ = setup(tmp_path)
    before = store._project_path('app').read_bytes()
    assert connect(principal_id=principal).error_code == 'UNAUTHORIZED_ROLE'
    assert store._project_path('app').read_bytes() == before
    assert not p.calls and not browsers and not (tmp_path / 'smoke').exists()


@pytest.mark.parametrize('claim', [False, None, 'true', 1])
def test_explicit_claim(tmp_path, claim):
    _, p, _, connect, _ = setup(tmp_path)
    assert connect(ownership_claim=claim).error_code == 'OWNERSHIP_CLAIM_REQUIRED'
    assert not p.calls


@pytest.mark.parametrize('lifecycle', ['LIVE', 'PREVIEW_READY'])
def test_manual_dns_then_verified_smoke_restart(tmp_path, lifecycle):
    store, p, browsers, connect, _ = setup(tmp_path, lifecycle)
    def durable():
        saved = store.load('app').domain.connection
        assert saved['add_attempted']
        if p.calls[-1][1].split('?')[0].endswith('/verify'):
            assert saved['verify_attempted']
        assert store._lock_path('app').exists()
    p.on_post = durable
    pending = connect()
    assert pending.error_code == 'DNS_PENDING'
    assert pending.data['dns_records'][0]['type'] == 'TXT'
    assert 'No DNS changes are automated' in pending.data['instructions']
    assert not browsers
    p.misconfigured = False
    result = connect()
    assert result.success
    state = store.load('app')
    assert state.domain.connection_stage == 'ATTACHED'
    assert state.domain.verified_at <= state.domain.attached_at
    assert state.domain.status == 'TAKEN' and state.lifecycle == lifecycle
    assert len(browsers) == 2
    assert all(b.options['ignore_https_errors'] is False for b in browsers)
    assert connect().success
    assert sum(c[0] == 'POST' for c in p.calls) == 2


@pytest.mark.parametrize('missing', [True, False])
def test_add_timeout_never_reposts(tmp_path, missing):
    store, p, _, connect, _ = setup(tmp_path)
    p.add_timeout = True
    assert not connect().success
    p.missing = missing
    result = connect()
    assert not result.success
    assert sum(c[0] == 'POST' for c in p.calls) == 1
    assert store.load('app').domain.connection['add_attempted'] is True


def test_verify_timeout_requires_get_proof(tmp_path):
    store, p, _, connect, _ = setup(tmp_path)
    p.misconfigured = False
    p.verify_timeout = True
    assert not connect().success
    assert connect().error_code == 'DOMAIN_VERIFY_RECONCILIATION_REQUIRED'
    assert sum(c[0] == 'POST' for c in p.calls) == 2
    p.verified = True
    assert connect().success
    assert sum(c[0] == 'POST' for c in p.calls) == 2
    assert store.load('app').domain.connection_stage == 'ATTACHED'


@pytest.mark.parametrize('value', [None, 0, 1, 'false', 'true', [], {}])
def test_config_requires_actual_bool(tmp_path, value):
    _, p, browsers, connect, _ = setup(tmp_path)
    p.misconfigured = value
    assert not connect().success
    assert not browsers


@pytest.mark.parametrize('override', [{'verified': 'true'}, {'verified': 1},
    {'name': 'other.com'}, {'verification': {}}, {'verification': [{}]},
    {'redirect': 'evil.com'}, {'gitBranch': 'preview'}])
def test_domain_readback_failclosed(tmp_path, override):
    _, p, browsers, connect, _ = setup(tmp_path)
    p.domain_override = override
    p.misconfigured = False
    assert not connect().success
    assert not browsers


@pytest.mark.parametrize('field,value', [('teamId', 'foreign'), ('projectId', 'foreign'),
    ('id', 'other'), ('readyState', 'BUILDING'), ('meta', {})])
def test_exact_production_identity_before_writes(tmp_path, field, value):
    _, p, _, connect, _ = setup(tmp_path)
    p.deployment[field] = value
    assert not connect().success
    assert all(c[0] == 'GET' for c in p.calls)


def test_preview_labelled_but_genuinely_bound_deployment_is_production(tmp_path):
    """``deployment.target`` is NOT part of the production proof.

    Vercel does not rewrite a promoted deployment's own target, so a
    deployment that IS the project's production binding can still be labelled
    preview. Requiring ``target == 'production'`` would refuse to attach a
    domain to a genuinely live production deployment, while adding no real
    safety: the binding plus the exact identity is the proof.
    """
    store, p, _, connect, _ = setup(tmp_path)
    p.misconfigured = False
    p.deployment['target'] = 'preview'
    assert connect().success
    assert store.load('app').domain.connection_stage == 'ATTACHED'


def test_historical_production_not_current(tmp_path):
    _, p, _, connect, _ = setup(tmp_path)
    p.project['targets']['production']['id'] = 'other'
    assert not connect().success
    assert all(c[0] == 'GET' for c in p.calls)


def test_drift_after_smoke_prevents_attached(tmp_path):
    store, p, _, connect, deps = setup(tmp_path)
    p.misconfigured = False
    original = deps.smoke.run
    def run(url, out_dir):
        result = original(url, out_dir)
        p.project['targets']['production']['id'] = 'other'
        return result
    deps.smoke.run = run
    assert not connect().success
    assert store.load('app').domain.connection_stage == 'FAILED'
    assert store.load('app').domain.attached_at is None


@pytest.mark.parametrize('options', [{'redirect': True}, {'bad_assets': True},
    {'request_url': 'https://other.com'}, {'request_url': 'http://127.0.0.1'}])
def test_shared_smoke_failure_never_attached(tmp_path, options):
    store, p, browsers, connect, _ = setup(tmp_path, browser_options=options)
    p.misconfigured = False
    assert connect().error_code == 'SMOKE_FAILED'
    assert browsers and store.load('app').domain.attached_at is None
    assert p.bound  # remote add is NOT rolled back or misrepresented


def test_default_smoke_policy_unchanged_and_private_blocked(tmp_path):
    assert not PreviewSmokeTester(lambda: pytest.fail('must not launch')).run(
        'https://' + HOST, tmp_path).success
    smoke = PreviewSmokeTester(Browser, lambda _: ['127.0.0.1'], custom_hostname=HOST)
    assert not smoke.run('https://' + HOST, tmp_path).success


@pytest.mark.parametrize('change', ['approval', 'live', 'lifecycle', 'smoke'])
def test_prerequisites_no_effects(tmp_path, change):
    store, p, browsers, connect, deps = setup(tmp_path, 'PREVIEW_READY')
    with store.acquire_writer('app') as state:
        if change == 'approval':
            state.deployment['approval']['deployment_id'] = 'new-preview'
        elif change == 'live':
            state.deployment.pop('last_live_deployment')
        elif change == 'lifecycle':
            state.lifecycle = 'RUNNING'
        else:
            deps.smoke = None
        store.save(state)
    assert not connect().success
    assert not p.calls and not browsers


def test_pinned_binding_rejects_changed_hostname_team_principal(tmp_path):
    store, p, _, connect, deps = setup(tmp_path)
    connect()
    count = len(p.calls)
    assert connect(hostname='other.customer.com').error_code == 'DOMAIN_BINDING_MISMATCH'
    deps.vercel.team_id = 'other'
    assert connect().error_code == 'DOMAIN_BINDING_MISMATCH'
    deps.vercel.team_id = 'team'
    with store.acquire_writer('app') as state:
        state.roles['owner'] = 'new-owner'
        store.save(state)
    assert connect(principal_id='new-owner').error_code == 'DOMAIN_BINDING_MISMATCH'
    assert len(p.calls) == count


def test_revocation_wins_writer_before_effects(tmp_path):
    store, p, browsers, connect, _ = setup(tmp_path)
    started = Event()
    def queued():
        started.set()
        return connect()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.acquire_writer('app') as state:
            future = pool.submit(queued)
            assert started.wait(5)
            state.roles['owner'] = 'replacement'
            store.save(state)
            before = store._project_path('app').read_bytes()
        assert future.result(timeout=10).error_code == 'UNAUTHORIZED_ROLE'
    assert store._project_path('app').read_bytes() == before
    assert not p.calls and not browsers


def test_project_drift_after_add_blocks_smoke(tmp_path):
    store, p, browsers, connect, _ = setup(tmp_path)
    p.misconfigured = False
    def drift():
        p.project['env'] = []
    p.on_post = drift
    assert not connect().success
    assert p.bound and not browsers
    assert store.load('app').domain.attached_at is None


@pytest.mark.parametrize('method', ['add_domain', 'verify_domain', 'get_project_domain'])
def test_adapter_rejects_foreign_project_before_transport(tmp_path, method):
    _, p, _, _, deps = setup(tmp_path)
    foreign = {**p.project, 'accountId': 'other'}
    result = getattr(deps.vercel, method)('app', foreign, HOST)
    assert not result.success and not p.calls


def test_tls_exception_failclosed(tmp_path):
    store, p, _, connect, deps = setup(tmp_path)
    p.misconfigured = False
    class TLSBrowser(Browser):
        def goto(self, url, **kwargs):
            raise OSError('certificate verify failed')
    deps.smoke = PreviewSmokeTester(TLSBrowser, lambda _: ['8.8.8.8'], custom_hostname=HOST)
    assert connect().error_code == 'SMOKE_FAILED'
    assert store.load('app').domain.attached_at is None


# ---------------------------------------------------------------------------
# Slug-identity regression (same class as tg-6329821361-p4): the custom
# domain flow must resolve the owned project by its canonical friendly slug
# (lookup key AND validated name), never via the opaque hash-derived name.
# The provider only answers under the project's REAL name -- an opaque
# fallback 404s, which surfaces as PROJECT_RECONCILIATION_REQUIRED.
# ---------------------------------------------------------------------------


def _project_name_lookup_keys(p):
    return [urlsplit(c[1]).path for c in p.calls
            if c[0] == 'GET' and '/v9/projects/' in urlsplit(c[1]).path
            and '/domains' not in urlsplit(c[1]).path]


def test_slug_project_custom_domain_full_flow_via_canonical_lookup(tmp_path):
    """Preview-created slug project: claim -> add -> check -> verify -> smoke
    -> ATTACHED, every owned-project lookup keyed by the canonical slug."""
    store, p, browsers, connect, _ = setup(tmp_path, slug='dapur-kedaton')
    pending = connect()
    assert pending.error_code == 'DNS_PENDING'  # lookup + add + checks all passed
    p.misconfigured = False
    result = connect()
    assert result.success, result.error
    assert result.data['stage'] == 'ATTACHED'
    keys = _project_name_lookup_keys(p)
    assert keys and all(key.endswith('/dapur-kedaton') for key in keys)


def test_slug_project_domain_flow_never_falls_back_to_opaque_lookup(tmp_path):
    """Fault injection: slug configured in the provider but the orchestrator
    is NOT given the bound slug (the unfixed behavior) -> the opaque-name
    lookup 404s and the flow fails closed before any domain mutation."""
    store, p, browsers, connect, deps = setup(tmp_path, slug='dapur-kedaton')
    deps.slug_for = lambda pid, state: None
    result = connect()
    assert result.error_code == 'PROJECT_RECONCILIATION_REQUIRED'
    assert not p.bound and not p.verified
    assert store.load('app').domain.attached_at is None


def test_opaque_legacy_domain_flow_lookup_key_unchanged(tmp_path):
    """No slug bound (legacy opaque project): the lookup key IS the opaque
    hash-derived name and the existing flow is untouched."""
    store, p, browsers, connect, _ = setup(tmp_path)
    pending = connect()
    assert pending.error_code == 'DNS_PENDING'
    keys = _project_name_lookup_keys(p)
    assert keys and all(key.endswith('/' + p.project['name']) for key in keys)
