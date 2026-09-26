"""Local behavioral adapter tests. No provider/browser/network calls."""
import asyncio
import base64
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.deploy.adapters import (HttpResponse, PreviewSmokeTester, TelegramAdapter,
                                 UrllibHttpTransport, VercelAdapter, _NoRedirect,
                                 _calling_thread_has_running_loop)

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

# ---------------------------------------------------------------------------
# BL-2 — reconcile_production_deployment: read-only remote-truth comparison
# against the COMPLETE trusted identity tuple.
# ---------------------------------------------------------------------------

def _reconcile_identity():
    return {'deployment_id': 'dpl_1', 'operation_id': 'operation',
            'source_revision': 2, 'artifact_sha256': SHA}

def test_reconcile_production_confirms_promoted_deployment():
    """Current production IS the expected deployment (exact identity tuple)
    -> PROMOTED, without ever POSTing a promote."""
    a, t = adapter()
    d = deployment(a)
    p = project(a)
    t.responses = [
        (200, d),
        (200, {**p, 'targets': {'production': {'id': d['id']}}}),
    ]
    result = a.reconcile_production_deployment('app', p, _reconcile_identity())
    assert result.success and result.data['status'] == 'PROMOTED'
    assert all(c[0] == 'GET' for c in t.calls)

def test_reconcile_production_stale_binding_is_ambiguous_not_not_promoted():
    """Current production is a DIFFERENT deployment with no job record. That is
    NOT a confirmed negative: Vercel keeps the remote promotion running after a
    client stops waiting, so a stale binding can only be reported as ambiguous
    and must stay resumable."""
    a, t = adapter()
    d = deployment(a)
    p = project(a)
    t.responses = [
        (200, d),
        (200, {**p, 'targets': {'production': {'id': 'dpl_other'}}}),
    ]
    result = a.reconcile_production_deployment('app', p, _reconcile_identity())
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_reconcile_production_no_binding_is_ambiguous_not_not_promoted():
    """No production binding at all is likewise not proof the promote did not
    land -- a queued/queued-then-aliased promotion is exactly this shape."""
    a, t = adapter()
    d = deployment(a)
    p = project(a)
    t.responses = [(200, d), (200, {**p, 'targets': {}})]
    result = a.reconcile_production_deployment('app', p, _reconcile_identity())
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'


def test_reconcile_production_terminal_job_failure_is_not_promoted():
    """NOT_PROMOTED requires POSITIVE evidence: the provider's own alias job
    reports this promotion as terminally failed."""
    a, t = adapter()
    d = deployment(a)
    p = project(a)
    t.responses = [
        (200, d),
        (200, {**p, 'targets': {'production': {'id': 'dpl_other'}},
               'lastAliasRequest': {'jobStatus': 'failed',
                                    'toDeploymentId': _reconcile_identity()['deployment_id'],
                                    'requestedAt': 1000, 'type': 'promote'}}),
    ]
    result = a.reconcile_production_deployment('app', p, _reconcile_identity())
    assert result.success and result.data['status'] == 'NOT_PROMOTED'


def test_reconcile_production_in_flight_job_stays_ambiguous():
    """An in-flight job for OUR deployment is the pure async case: keep waiting,
    never conclude."""
    a, t = adapter()
    d = deployment(a)
    p = project(a)
    t.responses = [
        (200, d),
        (200, {**p, 'targets': {'production': {'id': 'dpl_other'}},
               'lastAliasRequest': {'jobStatus': 'in-progress',
                                    'toDeploymentId': _reconcile_identity()['deployment_id'],
                                    'requestedAt': 1000, 'type': 'promote'}}),
    ]
    result = a.reconcile_production_deployment('app', p, _reconcile_identity())
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'

@pytest.mark.parametrize('mutate', [
    lambda d: d.__setitem__('id', 'dpl_9'),
    lambda d: d.__setitem__('projectId', 'foreign'),
    lambda d: d.__setitem__('teamId', 'foreign'),
    lambda d: d.__setitem__('meta', {**d['meta'], 'wbOperation': 'other'}),
    lambda d: d.__setitem__('meta', {**d['meta'], 'wbRevision': '3'}),
    lambda d: d.__setitem__('meta', {**d['meta'], 'wbArtifact': 'b' * 64}),
    lambda d: d.__setitem__('readyState', 'BUILDING'),
])
def test_reconcile_production_identity_mismatch_ambiguous(mutate):
    """Same/other deployment_id but any mismatched trusted field, or not
    READY -> reconciliation-required (never inferred success)."""
    a, t = adapter()
    d = deployment(a)
    mutate(d)
    p = project(a)
    t.responses = [
        (200, d),
        (200, {**p, 'targets': {'production': {'id': d.get('id')}}}),
    ]
    result = a.reconcile_production_deployment('app', p, _reconcile_identity())
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'

def test_reconcile_production_ambiguous_lookup_fails_closed():
    """Malformed production binding -> reconciliation-required."""
    a, t = adapter()
    d = deployment(a)
    p = project(a)
    t.responses = [(200, d), (200, {**p, 'targets': 'not-a-dict'})]
    result = a.reconcile_production_deployment('app', p, _reconcile_identity())
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'

@pytest.mark.parametrize('identity', [
    {'deployment_id': 'dpl_1'},  # partial
    {'deployment_id': 'dpl_1', 'operation_id': '', 'source_revision': 2,
     'artifact_sha256': SHA},
    {'deployment_id': 'dpl_1', 'operation_id': 'operation', 'source_revision': 0,
     'artifact_sha256': SHA},
    {'deployment_id': 'dpl_1', 'operation_id': 'operation', 'source_revision': 2,
     'artifact_sha256': 'nope'},
    None,
])
def test_reconcile_production_incomplete_identity_fails_closed(identity):
    """A partial/absent expected identity can never prove success."""
    a, t = adapter()
    result = a.reconcile_production_deployment('app', project(a), identity)
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'
    assert not t.calls

def test_reconcile_production_transport_error_fails_closed():
    a, t = adapter(TimeoutError('secret'))
    result = a.reconcile_production_deployment('app', project(a), _reconcile_identity())
    assert not result.success
    assert result.error_code == 'PROMOTE_RECONCILIATION_REQUIRED'

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
    def __init__(self, request_url=None, redirect=False, bad_assets=False,
                 resource_type='document'):
        self.request_url, self.redirect, self.bad_assets = request_url, redirect, bad_assets
        self.resource_type = resource_type
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
                                  resource_type=self.resource_type,
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


def test_smoke_records_sanitized_blocked_request_and_causal_events(tmp_path, caplog):
    secret = 'https://fonts.googleapis.com/css2?family=Inter&token=sekret'
    request_url = secret
    console_messages = []

    class AssetBrowser(Browser):
        def goto(self, url, **kwargs):
            self.url = url
            request = SimpleNamespace(
                url=request_url, method='GET', resource_type='stylesheet',
                redirected_from=None,
            )
            route = SimpleNamespace(
                request=request, continue_=lambda: None,
                abort=lambda: setattr(self, 'blocked', True),
            )
            self.callback(route)
            self.handlers['requestfailed'](request)
            console_messages.append(SimpleNamespace(
                type='error', text='Failed to load resource: net::ERR_FAILED',
                location={'url': request_url},
            ))
            self.handlers['console'](console_messages[-1])
            return SimpleNamespace(status=200)

    with caplog.at_level('ERROR', logger='app.deploy.adapters'):
        result = PreviewSmokeTester(
            lambda: AssetBrowser(), lambda _: ['8.8.8.8']
        ).run('https://test.vercel.app/', tmp_path)

    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    assert result.data['failure_classification'] == 'artifact_defect'
    assert 'url' not in result.data
    assert 'sekret' not in repr(result.data)
    assert 'sekret' not in '\n'.join(record.getMessage() for record in caplog.records)
    records = result.data['failure_records']
    blocked = [r for r in records if r['category'] == 'blocked_request']
    assert blocked
    first = blocked[0]
    assert first['host'] == 'fonts.googleapis.com'
    assert first['path'] == '/css2'
    assert first['method'] == 'GET'
    assert first['resource_type'] == 'stylesheet'
    assert first['reason'] == 'off_origin_or_method'
    assert first['viewport_size'] == {'width': 1440, 'height': 900}
    secondary = [r for r in records if r['category'] in {'request_failed', 'console_error'}]
    assert secondary
    blocked_by_viewport = {r['viewport']: r['id'] for r in blocked}
    assert all(
        record.get('caused_by') == blocked_by_viewport.get(record.get('viewport'))
        for record in secondary
    )


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
# Smoke lifecycle thread-affinity + deterministic cleanup (BL-1 / H-8)
# ---------------------------------------------------------------------------
#
# Playwright's synchronous API is thread-affine: (a) ``sync_playwright().start()``
# refuses to start on a thread that owns a RUNNING asyncio loop (raises "using
# Playwright Sync API inside the asyncio loop"), and (b) a Browser created on one
# thread cannot then be driven from another (greenlet: "cannot switch to a
# different thread"). The runtime reaches PreviewSmokeTester from the Telegram
# dispatch chain while an asyncio loop is running on the calling thread, so the
# ENTIRE synchronous smoke lifecycle must execute on one dedicated worker thread.

class _LifecyclePlaywrightStack:
    """Stub Playwright sync stack that records the (op, thread) of every call.

    ``start()`` mimics playwright's real guard, so this stub is only usable
    from a thread that owns no running asyncio loop (the dedicated worker),
    exactly like production.
    """

    def __init__(self, record, *, goto_raises=None):
        self._rec = record
        self._goto_raises = goto_raises

    def _note(self, op):
        self._rec.append((op, threading.current_thread().name))

    def start(self):
        try:
            running = asyncio.get_running_loop().is_running()
        except RuntimeError:
            running = False
        if running:
            raise RuntimeError(
                'It looks like you are using Playwright Sync API inside the '
                'asyncio loop.'
            )
        self._note('pw.start')
        return self

    @property
    def chromium(self):
        return self

    def launch(self, **kwargs):
        self._note('chromium.launch')
        return _LifecycleBrowser(self._rec, self._goto_raises)

    def stop(self):
        self._note('pw.stop')


class _LifecycleBrowser:
    def __init__(self, record, goto_raises):
        self._rec = record
        self._goto_raises = goto_raises

    def new_context(self, **kwargs):
        self._rec.append(('browser.new_context', threading.current_thread().name))
        return _LifecycleContext(self._rec, self._goto_raises)

    def close(self):
        self._rec.append(('browser.close', threading.current_thread().name))


class _LifecycleContext:
    def __init__(self, record, goto_raises):
        self._rec = record
        self._goto_raises = goto_raises

    def route(self, pattern, callback):
        self._rec.append(('context.route', threading.current_thread().name))

    def route_web_socket(self, pattern, callback):
        self._rec.append(('context.route_web_socket', threading.current_thread().name))

    def new_page(self):
        self._rec.append(('context.new_page', threading.current_thread().name))
        return _LifecyclePage(self._rec, self._goto_raises)

    def close(self):
        self._rec.append(('context.close', threading.current_thread().name))


class _LifecyclePage:
    def __init__(self, record, goto_raises):
        self._rec = record
        self._goto_raises = goto_raises
        self.url = 'https://test.vercel.app/'

    def on(self, event, callback):
        pass

    def goto(self, url, **kwargs):
        self._rec.append(('page.goto', threading.current_thread().name))
        if self._goto_raises is not None:
            raise self._goto_raises
        return SimpleNamespace(status=200)

    def evaluate(self, script):
        self._rec.append(('page.evaluate', threading.current_thread().name))
        return True

    def screenshot(self, **kwargs):
        self._rec.append(('page.screenshot', threading.current_thread().name))
        return PNG


def _factory_using_stack(record, holder, **kwargs):
    def factory():
        record.append(('factory', threading.current_thread().name))
        pw = _LifecyclePlaywrightStack(record, **kwargs)
        browser = pw.start().chromium.launch(headless=True)
        browser._wb_playwright_owner = pw  # mirrors runtime._launch_smoke_browser
        holder.append(pw)
        return browser
    return factory


def test_smoke_full_lifecycle_on_one_thread_inside_running_loop(tmp_path):
    """BL-1: inside a running asyncio loop, EVERY Playwright operation runs on
    exactly one dedicated worker thread (never the loop-owning caller), the
    worker stays alive for the whole lifecycle, the result propagates, and the
    Playwright driver is stopped."""
    record = []
    holder = []
    main_thread = threading.current_thread().name
    factory = _factory_using_stack(record, holder)
    result_holder = {}

    async def _drive():
        # Running loop on THIS thread -> the exact runtime ownership state that
        # broke production (the launch was bridged, then the Browser crossed back).
        result_holder['result'] = PreviewSmokeTester(factory, lambda _: ['8.8.8.8']).run(
            'https://test.vercel.app/', tmp_path)

    asyncio.run(_drive())

    result = result_holder['result']
    assert result.success, result.data.get('failures')

    ops = [op for op, _ in record]
    threads = {thread for op, thread in record if op != 'factory'}
    assert threads, 'no Playwright operations recorded'
    # Every lifecycle operation executed on one and the same worker thread.
    assert len(threads) == 1, f'operations spread across threads: {sorted(threads)}'
    worker_thread = threads.pop()
    assert worker_thread != main_thread, 'Playwright ran on the loop-owning caller thread'

    for required in ('pw.start', 'chromium.launch', 'browser.new_context',
                     'context.new_page', 'page.goto', 'page.evaluate',
                     'page.screenshot', 'context.close', 'browser.close', 'pw.stop'):
        assert required in ops, f'missing lifecycle op: {required}'
    # Two anonymous contexts (desktop + mobile) -> close/stop run twice.
    assert ops.count('browser.close') == 2 and ops.count('pw.stop') == 2
    assert 'desktop_screenshot' in result.data and 'mobile_screenshot' in result.data


def test_smoke_failure_mid_lifecycle_propagates_fail_closed(tmp_path):
    """BL-1 + H-8: a Playwright failure mid-smoke fails closed, the worker exits,
    cleanup is still attempted, pw.stop() still runs, and nothing is swallowed."""
    record = []
    holder = []
    factory = _factory_using_stack(record, holder, goto_raises=RuntimeError('boom-mid-smoke'))
    result_holder = {}

    async def _drive():
        result_holder['result'] = PreviewSmokeTester(factory, lambda _: ['8.8.8.8']).run(
            'https://test.vercel.app/', tmp_path)

    asyncio.run(_drive())

    result = result_holder['result']
    assert not result.success and result.error_code == 'SMOKE_FAILED'
    # Exception TYPE is classified; the raw message is never persisted.
    assert 'browser smoke failed: RuntimeError' in result.data['failures']
    assert not any('boom-mid-smoke' in f for f in result.data['failures'])

    ops = [op for op, _ in record]
    # Cleanup still attempted under the failure, and Playwright stopped. A
    # mid-smoke raise aborts the viewport loop (fail fast), so only the first
    # context was opened/closed — but its cleanup and the driver stop still ran.
    assert 'context.close' in ops and 'browser.close' in ops and 'pw.stop' in ops
    assert ops.count('pw.stop') == 1
    threads = {t for op, t in record if op != 'factory'}
    assert len(threads) == 1 and threading.current_thread().name not in threads


def test_smoke_direct_sync_path_when_no_running_loop(tmp_path):
    """BL-1: with NO running loop on the caller, the direct synchronous path is
    used unchanged — no worker bridge — and the same pw.stop cleanup applies."""
    record = []
    holder = []
    factory = _factory_using_stack(record, holder)
    assert not _calling_thread_has_running_loop()

    result = PreviewSmokeTester(factory, lambda _: ['8.8.8.8']).run(
        'https://test.vercel.app/', tmp_path)

    assert result.success
    ops = [op for op, _ in record]
    for required in ('pw.start', 'chromium.launch', 'browser.new_context',
                     'context.new_page', 'page.goto', 'page.screenshot',
                     'context.close', 'browser.close', 'pw.stop'):
        assert required in ops
    # No dedicated worker thread: everything ran on this (caller) thread.
    threads = {t for op, t in record}
    assert threads == {threading.current_thread().name}, sorted(threads)


def test_smoke_cleanup_stop_failure_does_not_mask_success(tmp_path):
    """H-8: a pw.stop() cleanup failure is logged but must NOT turn a successful
    smoke into a failure, nor replace an existing primary failure."""
    record = []

    class _StopBoomPlaywright(_LifecyclePlaywrightStack):
        def stop(self):
            self._note('pw.stop')
            raise RuntimeError('stop-boom')

    def factory():
        record.append(('factory', threading.current_thread().name))
        pw = _StopBoomPlaywright(record)
        browser = pw.start().chromium.launch(headless=True)
        browser._wb_playwright_owner = pw
        return browser

    result = PreviewSmokeTester(factory, lambda _: ['8.8.8.8']).run(
        'https://test.vercel.app/', tmp_path)

    assert result.success
    assert [op for op, _ in record].count('pw.stop') == 2


def test_smoke_owner_less_factory_stop_is_noop(tmp_path):
    """H-8: an injected/test browser factory without a Playwright owner must
    still work (cleanup stop is a best-effort no-op)."""
    class OwnerlessBrowser(Browser):
        pass

    result = PreviewSmokeTester(lambda: OwnerlessBrowser(), lambda _: ['8.8.8.8']).run(
        'https://test.vercel.app/', tmp_path)
    assert result.success


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
# PHASE A -- bootstrap TRANSIENT-state classification.
#
# Real p7 evidence: the project's production slot was already bound to the
# deterministic bootstrap deployment (Vercel auto-promotes the first deploy),
# but that deployment was still BUILDING when polled. The old code treated
# "bound but not READY" as an immediate terminal failure
# (BOOTSTRAP_RECONCILIATION_REQUIRED) even though BUILDING/QUEUED are normal
# transient provider states. The fix classifies the state: READY -> success,
# clearly nonterminal -> keep polling, ERROR/CANCELED -> terminal, malformed
# or foreign binding -> fail closed. Exactly ONE bootstrap POST is ever made.
# ---------------------------------------------------------------------------


def test_bootstrap_bound_but_building_then_ready_succeeds():
    """PHASE A #1: production id == bootstrap id with deployment BUILDING is
    a NORMAL transient state -- poll again (bounded) and succeed once READY.
    Must never fail here, and must never POST a second bootstrap."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),                                     # no production yet
        (200, {'deployments': [], 'pagination': {'next': None}}),         # no existing bootstrap
        (201, {'id': 'dpl_bootstrap_1'}),                                 # POST create
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),  # poll 1: bound
        (200, {'id': 'dpl_bootstrap_1', 'readyState': 'BUILDING'}),       # poll 1: BUILDING
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),  # poll 2: still bound
        (200, {'id': 'dpl_bootstrap_1', 'readyState': 'READY'}),          # poll 2: READY
    ]
    result = a.ensure_bootstrap('app', p, interval=0)
    assert result.success
    assert result.data == {'bootstrapped': True, 'deployment_id': 'dpl_bootstrap_1',
                           'reconciled': False, 'confirmed': True}
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_bootstrap_building_until_timeout_fails_closed_no_duplicate_post():
    """PHASE A #2: bound but BUILDING for the whole bounded poll -> timeout/
    fail closed. Never a second POST, never success."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, {'id': 'dpl_bootstrap_1'}),
    ]
    for _ in range(3):  # max_polls=3 iterations: bound + BUILDING each time
        t.responses.append((200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}))
        t.responses.append((200, {'id': 'dpl_bootstrap_1', 'readyState': 'BUILDING'}))
    result = a.ensure_bootstrap('app', p, max_polls=3, interval=0)
    assert not result.success
    assert result.error_code == 'BOOTSTRAP_CONFIRMATION_TIMEOUT'
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_bootstrap_terminal_error_fails_closed_no_duplicate_post():
    """PHASE A #3: bound to our bootstrap but the deployment reports ERROR ->
    terminal failure. Never a second POST."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, {'id': 'dpl_bootstrap_1'}),
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),
        (200, {'id': 'dpl_bootstrap_1', 'readyState': 'ERROR'}),
    ]
    result = a.ensure_bootstrap('app', p, interval=0)
    assert not result.success
    assert result.error_code == 'BOOTSTRAP_DEPLOYMENT_FAILED'
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_bootstrap_terminal_canceled_fails_closed_no_duplicate_post():
    """PHASE A #4: bound but CANCELED -> terminal failure."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, {'id': 'dpl_bootstrap_1'}),
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),
        (200, {'id': 'dpl_bootstrap_1', 'readyState': 'CANCELED'}),
    ]
    result = a.ensure_bootstrap('app', p, interval=0)
    assert not result.success
    assert result.error_code == 'BOOTSTRAP_DEPLOYMENT_FAILED'
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_bootstrap_malformed_deployment_response_fails_closed():
    """PHASE A #6: bound to our bootstrap but the deployment GET returns a
    malformed / wrong-identity payload -> fail closed (never success, never
    a second POST)."""
    a, t = adapter()
    p = project(a)
    t.responses = [
        (200, {**p, 'targets': {}}),
        (200, {'deployments': [], 'pagination': {'next': None}}),
        (201, {'id': 'dpl_bootstrap_1'}),
        (200, {**p, 'targets': {'production': {'id': 'dpl_bootstrap_1'}}}),
        (200, {'id': 'dpl_OTHER', 'readyState': 'READY'}),   # wrong identity
    ]
    result = a.ensure_bootstrap('app', p, interval=0)
    assert not result.success
    assert result.error_code == 'BOOTSTRAP_RECONCILIATION_REQUIRED'
    assert sum(c[0] == 'POST' for c in t.calls) == 1


def test_bootstrap_existing_ready_on_retry_no_post_success():
    """PHASE A #7: on retry the bootstrap deployment already exists AND is
    already READY + bound -> no POST, success (crash-replay reconciliation)."""
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


def test_smoke_exception_persists_type_only_and_is_logged(tmp_path, caplog):
    """A raised browser/Playwright exception must still fail closed, keep the
    SMOKE_FAILED code, persist ONLY the exception type (never the message,
    which can carry URLs/query params), and be logged for operators."""
    secret = 'https://user:pw@test.vercel.app/?token=sekret'

    class BoomBrowser(Browser):
        def goto(self, url, **kwargs):
            raise RuntimeError(secret)

    def factory():
        return BoomBrowser()

    with caplog.at_level('ERROR', logger='app.deploy.adapters'):
        result = PreviewSmokeTester(factory, lambda _: ['8.8.8.8']).run('https://test.vercel.app/', tmp_path)

    # fail-closed + unchanged public code
    assert not result.success
    assert result.error_code == 'SMOKE_FAILED'
    # exception TYPE persisted, message is NOT
    assert 'browser smoke failed: RuntimeError' in result.data['failures']
    assert not any(secret in f for f in result.data['failures'])
    # The operator log is sanitized and does not attach raw exception text.
    assert any('Preview smoke browser failure' in r.getMessage() for r in caplog.records)
    assert all(not r.exc_info for r in caplog.records)
    assert secret not in '\n'.join(r.getMessage() for r in caplog.records)
    assert secret not in result.data['failures'][-1]
