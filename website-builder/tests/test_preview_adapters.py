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
    ('meta', {}), ('target', 'production'), ('url', 'evil.example'), ('name', 'foreign')])
def test_deployment_identity_failclosed(field, value):
    a, t = adapter()
    d = deployment(a)
    d[field] = value
    t.responses = [(201, d)]
    result = a.deploy_static_files('app', project(a), {'index.html': b'x'}, 'operation', 2, SHA)
    assert not result.success and not result.retryable


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
    factory.assert_not_called()


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
