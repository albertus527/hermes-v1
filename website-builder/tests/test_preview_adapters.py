"""Local behavioral adapter tests. No provider/browser/network calls."""
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.deploy.adapters import (HttpResponse, PreviewSmokeTester, TelegramAdapter,
                                 UrllibHttpTransport, VercelAdapter, _NoRedirect)

PNG = b'\x89PNG\r\n\x1a\nlocal-fake-image'
SHA = 'a' * 64


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, body = value
        return HttpResponse(status, json.dumps(body).encode())


def adapter(*responses):
    transport = Transport(*responses)
    return VercelAdapter('secret', 'team_1', 'installation', transport), transport


def project(a):
    return {'id': 'prj_1', 'name': a.project_name_for('app'), 'accountId': 'team_1',
            'env': [{'key': 'WEBSITE_BUILDER_OWNER', 'value': a._marker('app'), 'type': 'plain'}]}


def deployment(a):
    return {'id': 'dpl_1', 'projectId': 'prj_1', 'teamId': 'team_1',
            'name': a.project_name_for('app'), 'target': None, 'url': 'tested.vercel.app',
            'readyState': 'READY', 'meta': a._meta('app', 'operation', 2, SHA)}


def test_project_deterministic_collision_resistant():
    a, _ = adapter()
    assert a.project_name_for('A_B') != a.project_name_for('a-b')
    assert a.project_name_for('app') == a.project_name_for('app')


def test_project_creation_readback_ownership():
    a, t = adapter()
    p = project(a)
    t.responses = [(404, {}), (201, {'id': 'prj_1'}), (200, p)]
    assert a.ensure_project('app').data['project'] == p
    payload = json.loads(t.calls[1][2]['data'])
    assert payload['environmentVariables'][0]['value'] == p['env'][0]['value']
    assert all('teamId=team_1' in c[1] for c in t.calls)


@pytest.mark.parametrize('field,value', [('env', []), ('accountId', 'foreign'), ('id', ''), ('name', 'foreign')])
def test_foreign_project_never_adopted(field, value):
    a, t = adapter()
    p = project(a)
    p[field] = value
    t.responses = [(200, p)]
    assert not a.ensure_project('app').success
    assert len(t.calls) == 1


def test_creation_timeout_reconcile_without_duplicate_post():
    a, t = adapter((404, {}), TimeoutError('secret'))
    assert not a.ensure_project('app').success
    t.responses = [(200, project(a))]
    assert a.ensure_project('app').success
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_exact_static_files_preview_only():
    a, t = adapter()
    t.responses = [(201, deployment(a))]
    files = {'index.html': b'<html>exact</html>', 'assets/a.bin': bytes(range(256))}
    result = a.deploy_static_files('app', project(a), files, 'operation', 2, SHA)
    assert result.success
    body = json.loads(t.calls[0][2]['data'])
    assert {e['file']: base64.b64decode(e['data']) for e in body['files']} == files
    assert 'target' not in body and 'gitSource' not in body
    assert body['builds'] == [{'src': '**', 'use': '@vercel/static'}]
    assert result.data['deployment_id'] == 'dpl_1'


@pytest.mark.parametrize('name', ['../secret', '/root', 'a\\b', 'a//b', 'vercel.json', 'C:secret'])
def test_unsafe_files_rejected_without_write(name):
    a, t = adapter()
    assert not a.deploy_static_files('app', project(a), {'index.html': b'x', name: b'x'},
                                     'operation', 2, SHA).success
    assert not t.calls


@pytest.mark.parametrize('field,value', [('teamId', None), ('projectId', 'foreign'),
    ('meta', {}), ('url', 'evil.example'), ('name', 'foreign')])
def test_deployment_identity_failclosed(field, value):
    a, t = adapter()
    d = deployment(a)
    d[field] = value
    t.responses = [(201, d)]
    result = a.deploy_static_files('app', project(a), {'index.html': b'x'}, 'operation', 2, SHA)
    assert not result.success and not result.retryable


def test_deployment_target_production_rejected_when_already_canonical():
    """Vercel's documented first-deployment auto-promotion means target can
    legitimately be "production" -- but if the project's OWN production
    binding already points at this exact deployment id, it is genuinely
    LIVE and must never be treated as an unpublished preview candidate,
    even though every other identity marker (project/team/meta) matches.
    """
    a, t = adapter()
    d = deployment(a)
    d['target'] = 'production'
    p = project(a)
    t.responses = [
        (201, d),
        (200, {**p, 'targets': {'production': {'id': d['id']}}}),
    ]
    result = a.deploy_static_files('app', p, {'index.html': b'x'}, 'operation', 2, SHA)
    assert not result.success and not result.retryable
    assert result.error_code == 'DEPLOYMENT_ALREADY_LIVE'


def test_deployment_target_production_accepted_when_not_yet_canonical():
    """A first deployment auto-promoted to target="production" by Vercel is
    a SAFE staged candidate as long as the project's production binding does
    not yet point at it (e.g. binding is empty, or points elsewhere).
    """
    a, t = adapter()
    d = deployment(a)
    d['target'] = 'production'
    p = project(a)
    t.responses = [
        (201, d),
        (200, {**p, 'targets': {}}),
    ]
    result = a.deploy_static_files('app', p, {'index.html': b'x'}, 'operation', 2, SHA)
    assert result.success


def test_deployment_production_binding_lookup_ambiguous_failclosed():
    """A malformed/ambiguous production-binding response must never be
    treated as "not live" -- fail closed instead of assuming safety.
    """
    a, t = adapter()
    d = deployment(a)
    d['target'] = 'production'
    p = project(a)
    t.responses = [
        (201, d),
        (200, {**p, 'targets': 'not-a-dict'}),
    ]
    result = a.deploy_static_files('app', p, {'index.html': b'x'}, 'operation', 2, SHA)
    assert not result.success and not result.retryable
    assert result.error_code == 'DEPLOYMENT_IDENTITY_MISMATCH'


def test_reconciliation_lookup_landing_on_canonical_production_failclosed():
    """A reconciliation lookup that resolves to the project's CURRENT
    canonical production deployment must fail closed exactly like a direct
    deploy response would -- and _deploy_or_reconcile's caller must never
    turn that into a second blind POST.
    """
    a, t = adapter()
    d = deployment(a)
    d['target'] = 'production'
    p = project(a)
    t.responses = [
        (200, {'deployments': [d], 'pagination': {'next': None}}),
        (200, d),
        (200, {**p, 'targets': {'production': {'id': d['id']}}}),
    ]
    result = a.find_deployment_by_operation_id('app', p, 'operation', 2, SHA)
    assert not result.success
    assert result.error_code == 'DEPLOYMENT_ALREADY_LIVE'


def test_lookup_paginated_then_authoritative_detail():
    a, t = adapter()
    d = deployment(a)
    t.responses = [(200, {'deployments': [], 'pagination': {'next': 10}}),
                   (200, {'deployments': [d], 'pagination': {'next': None}}), (200, d)]
    assert a.find_deployment_by_operation_id('app', project(a), 'operation', 2, SHA).success
    assert 'until=10' in t.calls[1][1]


def test_lookup_ambiguity_across_pages():
    a, t = adapter()
    d = deployment(a)
    t.responses = [(200, {'deployments': [d], 'pagination': {'next': 10}}),
                   (200, {'deployments': [{**d, 'id': 'dpl_2'}], 'pagination': {'next': None}})]
    result = a.find_deployment_by_operation_id('app', project(a), 'operation', 2, SHA)
    assert result.error_code == 'AMBIGUOUS_DEPLOYMENT'


@pytest.mark.parametrize('body', [{}, {'deployments': [], 'pagination': {}},
    {'deployments': [{'id': 'unknown'}], 'pagination': {'next': None}}])
def test_lookup_incomplete_is_not_absence(body):
    a, _ = adapter((200, body))
    assert a.find_deployment_by_operation_id('app', project(a), 'operation', 2, SHA).error_code == 'INCOMPLETE_LOOKUP'


def test_lookup_repeated_cursor_fails():
    page = {'deployments': [], 'pagination': {'next': 10}}
    a, _ = adapter((200, page), (200, page))
    assert not a.find_deployment_by_operation_id('app', project(a), 'operation', 2, SHA).success


def test_telegram_text_photo_return_identity(tmp_path):
    reply = {'ok': True, 'result': {'message_id': 7, 'chat': {'id': 123}}}
    t = Transport((200, reply), (200, reply))
    a = TelegramAdapter('123:secret', t)
    assert a.send_text('123', 'hello').data['message_id'] == 7
    path = tmp_path / 'image.png'
    path.write_bytes(PNG)
    assert a.send_photo('123', path, 'caption').data['message_id'] == 7
    assert PNG in t.calls[1][2]['data']
    assert b'filename="preview.png"' in t.calls[1][2]['data']


@pytest.mark.parametrize('response', [TimeoutError('token'), (500, {}), (200, {'ok': True}),
    (200, {'ok': True, 'result': {'message_id': 1, 'chat': {'id': 999}}})])
def test_telegram_ambiguous_never_retries(response):
    t = Transport(response)
    result = TelegramAdapter('123:secret', t).send_text('123', 'hi')
    assert result.error_code == 'AMBIGUOUS_SEND' and not result.retryable
    assert len(t.calls) == 1 and 'secret' not in result.error


def test_stdlib_transport_no_redirect_or_proxy(monkeypatch):
    response = Mock(code=302)
    response.read.return_value = b'{}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr('urllib.request.build_opener', build)
    assert UrllibHttpTransport().request('GET', 'https://api.vercel.com', {}).status == 302
    assert build.call_args.args[0].proxies == {}
    assert isinstance(build.call_args.args[1], _NoRedirect)
    assert build.call_args.args[1].redirect_request(None, None, 302, '', {}, 'https://evil') is None


class Browser:
    def __init__(self, request_url=None, redirect=False, bad_assets=False):
        self.request_url, self.redirect, self.bad_assets = request_url, redirect, bad_assets
        self.closed = self.context_closed = False
        self.handlers = {}
        self.blocked = False

    def new_context(self, **kwargs):
        self.options = kwargs
        return self

    def route(self, pattern, callback):
        self.callback = callback

    def route_web_socket(self, pattern, callback):
        self.socket_callback = callback

    def new_page(self):
        return self

    def on(self, event, callback):
        self.handlers[event] = callback

    def goto(self, url, **kwargs):
        self.url = url
        request = SimpleNamespace(url=self.request_url or url, method='GET',
                                  redirected_from=object() if self.redirect else None)
        route = SimpleNamespace(request=request, continue_=lambda: None,
                                abort=lambda: setattr(self, 'blocked', True))
        self.callback(route)
        if self.bad_assets:
            self.handlers['response'](SimpleNamespace(status=404))
        return SimpleNamespace(status=200)

    def evaluate(self, script):
        return True

    def screenshot(self, **kwargs):
        return PNG

    def close(self):
        self.closed = True


def test_smoke_desktop_mobile_anonymous(tmp_path):
    browsers = []
    def factory():
        browser = Browser()
        browsers.append(browser)
        return browser
    result = PreviewSmokeTester(factory, lambda _: ['8.8.8.8']).run('https://test.vercel.app/', tmp_path)
    assert result.success
    assert len(browsers) == 2 and all(b.closed for b in browsers)
    assert [b.options['viewport']['width'] for b in browsers] == [1440, 390]
    assert all(b.options['service_workers'] == 'block' for b in browsers)
    assert Path(result.data['desktop_screenshot']).read_bytes() == PNG


@pytest.mark.parametrize('url', ['http://x.vercel.app', 'https://vercel.app',
    'https://x.vercel.app.evil', 'https://user@x.vercel.app', 'https://x.vercel.app:444',
    'https://x.vercel.app/#fragment'])
def test_invalid_preview_does_not_launch(url, tmp_path):
    factory = Mock()
    assert not PreviewSmokeTester(factory).run(url, tmp_path).success


# ---------------------------------------------------------------------------
# Friendly Vercel slug resolution (ensure_project_with_slug)
# ---------------------------------------------------------------------------


def _slug_project(a, slug, app_id='app'):
    return {'id': 'prj_1', 'name': slug, 'accountId': 'team_1',
            'env': [{'key': 'WEBSITE_BUILDER_OWNER', 'value': a._marker(app_id), 'type': 'plain'}]}


def test_slug_available_creates_project():
    a, t = adapter()
    p = _slug_project(a, 'dapur-kedaton')
    t.responses = [(404, {}), (201, {'id': 'prj_1'}), (200, p)]
    result = a.ensure_project_with_slug('app', 'dapur-kedaton')
    assert result.success
    assert result.data['project']['name'] == 'dapur-kedaton'
    payload = json.loads(t.calls[1][2]['data'])
    assert payload['name'] == 'dapur-kedaton'
    # No opaque app-added suffix anywhere in the requested name.
    assert payload['name'] == 'dapur-kedaton'


def test_slug_belongs_to_own_project_reconciles():
    a, t = adapter()
    p = _slug_project(a, 'dapur-kedaton')
    t.responses = [(200, p)]
    result = a.ensure_project_with_slug('app', 'dapur-kedaton')
    assert result.success
    assert result.data['project'] == p
    # Read-only reconciliation: no POST issued.
    assert all(c[0] == 'GET' for c in t.calls)


def test_slug_collision_with_foreign_project_never_adopted():
    a, t = adapter()
    foreign = {'id': 'prj_9', 'name': 'dapur-kedaton', 'accountId': 'team_1', 'env': []}
    t.responses = [(200, foreign)]
    result = a.ensure_project_with_slug('app', 'dapur-kedaton')
    assert not result.success
    assert result.error_code == 'SLUG_COLLISION'
    # Never overwritten/adopted: no POST issued.
    assert all(c[0] == 'GET' for c in t.calls)


def test_slug_collision_wrong_marker_never_adopted():
    """Slug exists, belongs to a DIFFERENT app_id's owned project (marker
    mismatch) -- still a collision, never adopted.
    """
    a, t = adapter()
    other_owned = {'id': 'prj_9', 'name': 'dapur-kedaton', 'accountId': 'team_1',
                   'env': [{'key': 'WEBSITE_BUILDER_OWNER', 'value': a._marker('other-app'),
                            'type': 'plain'}]}
    t.responses = [(200, other_owned)]
    result = a.ensure_project_with_slug('app', 'dapur-kedaton')
    assert not result.success
    assert result.error_code == 'SLUG_COLLISION'


def test_slug_ambiguous_response_fails_closed():
    """A malformed/incomplete response (missing id/name) is NOT proof of a
    foreign project -- it must never be reported to the user as a taken
    slug. Only a well-formed dict with a real, distinct project identity
    and a failing ownership marker proves a collision (see
    test_slug_collision_with_foreign_project_never_adopted below)."""
    a, t = adapter()
    t.responses = [(200, {'not': 'a valid project shape, missing id/name'})]
    result = a.ensure_project_with_slug('app', 'dapur-kedaton')
    assert not result.success
    assert result.error_code == 'PROJECT_RECONCILIATION_REQUIRED'


def test_slug_creation_timeout_reconciled_without_duplicate_post():
    a, t = adapter((404, {}), TimeoutError('secret'))
    assert not a.ensure_project_with_slug('app', 'dapur-kedaton').success
    p = _slug_project(a, 'dapur-kedaton')
    t.responses = [(200, p)]
    result = a.ensure_project_with_slug('app', 'dapur-kedaton')
    assert result.success
    assert sum(c[0] == 'POST' for c in t.calls) == 1


# ---------------------------------------------------------------------------
# Bootstrap deployment (consumes Vercel's unavoidable first-deployment
# auto-promotion so real user content is never that first deployment)
# ---------------------------------------------------------------------------

def test_bootstrap_noop_when_production_already_exists():
    """Real content already live, or a prior bootstrap already consumed the
    promotion -- either way, never create a second bootstrap. Confirmation
    is already proven by the existing binding, so no polling occurs."""
    a, t = adapter()
    p = project(a)
    t.responses = [(200, {**p, 'targets': {'production': {'id': 'dpl_existing'}}})]
    result = a.ensure_bootstrap('app', p)
    assert result.success
    assert result.data == {'bootstrapped': False, 'already_current_production': True}
    assert len(t.calls) == 1  # read-only; no POST


def test_bootstrap_created_and_confirmed_current_before_returning_success():
    """POST succeeding, or a wbBootstrap-tagged row existing, is NOT proof
    the project's production slot was consumed -- ensure_bootstrap must poll
    project.targets.production.id until it equals the bootstrap deployment
    AND that deployment reports READY before returning success."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),                                    # no production yet
        (200, {'deployments': [], 'pagination': {'next': None}}),        # no existing bootstrap
        (201, {'id': 'dpl_bootstrap_1'}),                                 # POST create
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),  # confirm poll: bound
        (200, {'id': 'dpl_bootstrap_1', 'readyState': 'READY'}),         # confirm poll: READY
    ]
    result = a.ensure_bootstrap('app', p)
    assert result.success
    assert result.data == {'bootstrapped': True, 'deployment_id': 'dpl_bootstrap_1',
                           'reconciled': False, 'confirmed': True}
    post_calls = [c for c in t.calls if c[0] == 'POST']
    assert len(post_calls) == 1
    body = json.loads(post_calls[0][2]['data'])
    assert body['meta']['wbBootstrap'] == a._bootstrap_operation_id('app')
    assert 'wbOperation' not in body['meta']  # distinct namespace from real content


def test_bootstrap_delayed_promotion_polled_then_confirmed():
    """Vercel's promotion/alias assignment is eventually consistent: the
    first poll finds no production binding yet; only the second poll
    observes the bootstrap as current production. Must not fail or
    re-POST -- must keep polling within the bound and then succeed."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, {'id': 'dpl_bootstrap_1'}),
        (200, {**p, 'targets': {}}),                                      # poll 1: not yet bound
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),  # poll 2: bound
        (200, {'id': 'dpl_bootstrap_1', 'readyState': 'READY'}),
    ]
    result = a.ensure_bootstrap('app', p, interval=0)
    assert result.success
    assert result.data['confirmed'] is True
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_bootstrap_confirmation_timeout_fails_closed_no_duplicate_post():
    """Confirmation never lands within the bounded poll -- fail closed with
    a distinct timeout error code. Real content must never be deployed off
    the back of this. Only ONE bootstrap POST is ever issued, never a retry
    loop of creates."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, {'id': 'dpl_bootstrap_1'}),
    ] + [(200, {**p, 'targets': {}})] * 3  # never binds within the bound
    result = a.ensure_bootstrap('app', p, max_polls=3, interval=0)
    assert not result.success
    assert result.error_code == 'BOOTSTRAP_CONFIRMATION_TIMEOUT'
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_bootstrap_crash_replay_reconciles_without_duplicate_post():
    """Process crashes after the bootstrap POST landed but before local
    confirmation. A restart must find the existing bootstrap by its
    deterministic identity, never POST a second one, then still run the
    same remote confirmation barrier before ever succeeding."""
    a, t = adapter()
    p = project(a)
    existing = {'id': 'dpl_bootstrap_1', 'meta': {'wbBootstrap': a._bootstrap_operation_id('app')}}
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [existing], 'pagination': {'next': None}}),
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),
        (200, {'id': 'dpl_bootstrap_1', 'readyState': 'READY'}),
    ]
    result = a.ensure_bootstrap('app', p)
    assert result.success
    assert result.data == {'bootstrapped': True, 'deployment_id': 'dpl_bootstrap_1',
                           'reconciled': True, 'confirmed': True}
    assert not any(c[0] == 'POST' for c in t.calls)


def test_bootstrap_ambiguous_lookup_fails_closed_no_post():
    a, t = adapter()
    p = project(a)
    d1 = {'id': 'dpl_a', 'meta': {'wbBootstrap': a._bootstrap_operation_id('app')}}
    d2 = {'id': 'dpl_b', 'meta': {'wbBootstrap': a._bootstrap_operation_id('app')}}
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [d1, d2], 'pagination': {'next': None}}),
    ]
    result = a.ensure_bootstrap('app', p)
    assert not result.success
    assert result.error_code == 'AMBIGUOUS_DEPLOYMENT'
    assert not any(c[0] == 'POST' for c in t.calls)


def test_bootstrap_production_lookup_ambiguous_fails_closed():
    a, t = adapter()
    p = project(a)
    t.responses = [(200, {**p, 'targets': 'not-a-dict'})]
    result = a.ensure_bootstrap('app', p)
    assert not result.success
    assert result.error_code == 'PROJECT_RECONCILIATION_REQUIRED'
    assert not any(c[0] == 'POST' for c in t.calls)


def test_bootstrap_production_moves_elsewhere_during_poll_fails_closed():
    """If, mid-poll, targets.production comes to point at some OTHER
    deployment entirely (never our bootstrap), never keep polling against a
    moving target -- fail closed immediately."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, {'id': 'dpl_bootstrap_1'}),
        (200, {**p, 'targets': {'production': {'id': 'dpl_something_else'}}}),
    ]
    result = a.ensure_bootstrap('app', p, interval=0)
    assert not result.success
    assert result.error_code == 'BOOTSTRAP_RECONCILIATION_REQUIRED'


def test_real_content_deploy_after_bootstrap_consumed_promotion_is_accepted():
    """Once bootstrap has consumed the project's one-time auto-promotion,
    the real user-content deployment is provably NOT the project's first
    deployment, so it is not auto-promoted -- target stays unset/preview
    and it is accepted as a normal preview candidate. No first-deployment
    exception is needed in _deployment(); remote truth alone is enough."""
    a, t = adapter()
    p = project(a)
    d = deployment(a)
    d['target'] = None  # real content deploy, NOT auto-promoted this time
    t.responses = [(201, d)]
    result = a.deploy_static_files('app', p, {'index.html': b'x'}, 'operation', 2, SHA)
    assert result.success


# ---------------------------------------------------------------------------
# tg-6329821361-p4 regression: slug-named projects + canonical expected_name
# threaded through every downstream revalidation.
#
# Before the fix, only ensure_project_with_slug passed expected_name (the
# slug); every downstream method revalidated with the opaque hash-derived
# default and failed a project it had just created itself with
# PROJECT_IDENTITY_MISMATCH. These tests reproduce the real production
# sequence (slug = "dapur-kedaton") and pin the ownership/name assertions
# in BOTH directions: a correct slug project passes only when the canonical
# name is threaded, and every mismatch still fails closed.
# ---------------------------------------------------------------------------


def _slug_deployment(a, slug, app_id='app'):
    d = deployment(a)
    d['name'] = slug
    return d


def test_slug_project_full_preview_sequence_passes_with_expected_name():
    """REAL production sequence for tg-6329821361-p4, fixed: create/reconcile
    the slug-named project, then bootstrap + real-content deploy + readiness
    reconciliation lookup on the SAME returned project, threading the
    canonical slug. All four steps succeed; the same sequence WITHOUT the
    threaded name is pinned by the tripwire test below."""
    a, t = adapter()
    p = _slug_project(a, 'dapur-kedaton')
    # 1. project create under the friendly slug (validated with expected_name=slug)
    t.responses = [(404, {}), (201, {'id': 'prj_1'}), (200, p)]
    created = a.ensure_project_with_slug('app', 'dapur-kedaton')
    assert created.success
    project = created.data['project']
    # 2. bootstrap consumes the first-deployment auto-promotion (production already consumed)
    t.responses = [(200, {**p, 'targets': {'production': {'id': 'dpl_boot'}}})]
    assert a.ensure_bootstrap('app', project, expected_name='dapur-kedaton').success
    # 3. real content deploy
    d = _slug_deployment(a, 'dapur-kedaton')
    t.responses = [(201, d)]
    deployed = a.deploy_static_files('app', project, {'index.html': b'x'}, 'operation', 2, SHA,
                                     expected_name='dapur-kedaton')
    assert deployed.success
    assert deployed.data['preview_url'] == 'https://tested.vercel.app'
    # 4. subsequent readiness/reconciliation lookup on the same project object
    t.responses = [(200, {'deployments': [d], 'pagination': {'next': None}}), (200, d)]
    found = a.find_deployment_by_operation_id('app', project, 'operation', 2, SHA,
                                              expected_name='dapur-kedaton')
    assert found.success


def test_opaque_project_downstream_passes_without_expected_name():
    """Legacy/default path preserved: a project whose name IS the opaque
    hash-derived name passes every downstream revalidation with no
    expected_name threaded (adapter falls back to the opaque default)."""
    a, t = adapter()
    p = project(a)
    t.responses = [(200, {**p, 'targets': {'production': {'id': 'dpl_existing'}}})]
    assert a.ensure_bootstrap('app', p).success
    t.responses = [(201, deployment(a))]
    assert a.deploy_static_files('app', p, {'index.html': b'x'}, 'operation', 2, SHA).success
    d = deployment(a)
    t.responses = [(200, {'deployments': [d], 'pagination': {'next': None}}), (200, d)]
    assert a.find_deployment_by_operation_id('app', p, 'operation', 2, SHA).success


def test_downstream_foreign_marker_fails_despite_matching_expected_name():
    """Ownership assertion stays independent: a project whose NAME matches
    the canonical slug but whose WEBSITE_BUILDER_OWNER marker belongs to a
    different app_id must still fail closed -- the threaded expected_name
    never weakens the marker check."""
    a, t = adapter()
    foreign = _slug_project(a, 'dapur-kedaton')
    foreign['env'] = [{'key': 'WEBSITE_BUILDER_OWNER',
                       'value': a._marker('other-app'), 'type': 'plain'}]
    for result in (
        a.ensure_bootstrap('app', foreign, expected_name='dapur-kedaton'),
        a.deploy_static_files('app', foreign, {'index.html': b'x'}, 'operation', 2, SHA,
                              expected_name='dapur-kedaton'),
        a.find_deployment_by_operation_id('app', foreign, 'operation', 2, SHA,
                                          expected_name='dapur-kedaton'),
    ):
        assert not result.success and not result.retryable
        assert result.error_code == 'PROJECT_IDENTITY_MISMATCH'
    assert not t.calls  # identity rejected before any provider I/O


def test_downstream_correct_marker_wrong_slug_fails():
    """Name assertion stays independent: correct ownership marker for this
    app_id but the project's name is NOT the canonical slug (e.g. a stale
    project under a different slug) -> fail closed, never adopt."""
    a, t = adapter()
    wrong_slug = _slug_project(a, 'warung-maju')  # marker for 'app', name mismatch
    for result in (
        a.ensure_bootstrap('app', wrong_slug, expected_name='dapur-kedaton'),
        a.deploy_static_files('app', wrong_slug, {'index.html': b'x'}, 'operation', 2, SHA,
                              expected_name='dapur-kedaton'),
        a.find_deployment_by_operation_id('app', wrong_slug, 'operation', 2, SHA,
                                          expected_name='dapur-kedaton'),
    ):
        assert not result.success and not result.retryable
        assert result.error_code == 'PROJECT_IDENTITY_MISMATCH'
    assert not t.calls


def test_downstream_without_threaded_name_still_requires_opaque_name():
    """Tripwire pinning the unfixed failure class AND proving the check was
    not weakened into self-reference: when the caller does NOT thread the
    canonical slug, the adapter still validates against the opaque
    hash-derived name -- so a slug-named project fails closed. `expected_name`
    is never inferred from the project response itself."""
    a, _ = adapter()
    p = _slug_project(a, 'dapur-kedaton')
    for result in (
        a.ensure_bootstrap('app', p),
        a.deploy_static_files('app', p, {'index.html': b'x'}, 'operation', 2, SHA),
        a.find_deployment_by_operation_id('app', p, 'operation', 2, SHA),
    ):
        assert result.error_code == 'PROJECT_IDENTITY_MISMATCH'


# ---------------------------------------------------------------------------
# lookup_project(expected_name=...) -- canonical slug as BOTH the lookup key
# and the validated name (promote/custom-domain reconciliation path).
# ---------------------------------------------------------------------------


def test_lookup_with_canonical_slug_uses_slug_key_and_validates():
    """The canonical slug is the lookup KEY, not merely a post-hoc check --
    never 'look up opaque, then trust the response name'."""
    a, t = adapter()
    p = _slug_project(a, 'dapur-kedaton')
    t.responses = [(200, p)]
    result = a.lookup_project('app', expected_name='dapur-kedaton')
    assert result.success and result.data['project'] == p
    assert t.calls[0][1].split('?')[0].endswith('/v9/projects/dapur-kedaton')


def test_lookup_correct_owner_marker_wrong_slug_fails_closed():
    """Correct WEBSITE_BUILDER_OWNER marker but the project returned is NOT
    the canonical slug -> fail closed (ownership and name stay independent)."""
    a, t = adapter()
    t.responses = [(200, _slug_project(a, 'warung-maju'))]
    result = a.lookup_project('app', expected_name='dapur-kedaton')
    assert not result.success and not result.retryable
    assert result.error_code == 'PROJECT_RECONCILIATION_REQUIRED'


def test_lookup_correct_slug_foreign_owner_fails_closed():
    """Right slug, wrong ownership marker -> fail closed; the threaded
    expected_name never weakens the marker check."""
    a, t = adapter()
    foreign = _slug_project(a, 'dapur-kedaton')
    foreign['env'] = [{'key': 'WEBSITE_BUILDER_OWNER',
                       'value': a._marker('other-app'), 'type': 'plain'}]
    t.responses = [(200, foreign)]
    result = a.lookup_project('app', expected_name='dapur-kedaton')
    assert not result.success and not result.retryable
    assert result.error_code == 'PROJECT_RECONCILIATION_REQUIRED'


def test_lookup_opaque_legacy_project_unchanged():
    """No expected_name threaded -> lookup key is the legacy opaque
    hash-derived name, validated exactly as before."""
    a, t = adapter()
    p = project(a)
    t.responses = [(200, p)]
    result = a.lookup_project('app')
    assert result.success and result.data['project'] == p
    assert ('/v9/projects/' + a.project_name_for('app')) in t.calls[0][1]


@pytest.mark.parametrize('options,addresses', [({'redirect': True}, ['8.8.8.8']),
    ({'request_url': 'http://127.0.0.1/secret'}, ['8.8.8.8']), ({}, ['127.0.0.1']),
    ({}, ['8.8.8.8', '::1']), ({'bad_assets': True}, ['8.8.8.8'])])
def test_smoke_blocks_redirect_private_and_asset_failures(options, addresses, tmp_path):
    browsers = []
    def factory():
        b = Browser(**options)
        browsers.append(b)
        return b
    result = PreviewSmokeTester(factory, lambda _: addresses).run('https://test.vercel.app/', tmp_path)
    assert not result.success
    assert all(b.closed for b in browsers)
