"""Phase 9 adapters. Caller owns durable intents, snapshots, polling and retries.

All public operations return OperationResult (retryable is always False).
Vercel: ensure_project(app_id) -> data.project; deploy_static_files(project,
files, operation_id, source_revision, artifact_sha256) -> deployment identity;
find_deployment_by_operation_id takes the same identity (without files).
Persist project and operation BEFORE deployment. After uncertain creation,
lookup only: NOT_FOUND is not proof a timed-out write will never appear.
Telegram: send_text(chat_id, text), send_photo(chat_id, png_path, caption).
Persist each returned message_id. AMBIGUOUS_SEND requires caller intervention;
Telegram Bot API has no idempotency key or sent-message reconciliation API.
Smoke: run(url, out_dir) -> screenshots and findings. Inject a fresh browser
factory implementing the synchronous Playwright Browser interface. No browser
dependency is installed here. Factory must launch a credential-free browser
with sanitized process environment; context is anonymous/nonpersistent. DNS
checks are defense-in-depth, NOT DNS pinning: production must additionally
block private egress at the browser sandbox/network boundary.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import socket
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode, urlsplit

from app.core.contracts import OperationResult


@dataclass
class HttpResponse:
    status: int
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibHttpTransport:
    """request(method, url, headers, data=None, timeout=30) -> HttpResponse.

    No retries, redirects, ambient proxy credentials, cookies or auth handlers.
    """
    def request(self, method, url, headers, data=None, timeout=30):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise ValueError("Response too large")
            return HttpResponse(response.code, body)


def _fail(code):
    # Never expose provider responses/exceptions which can contain tokens.
    return OperationResult.fail(code, error_code=code, retryable=False)


def _json_call(transport, method, url, headers, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    response = transport.request(method, url, headers=headers, data=data, timeout=30)
    body = json.loads(response.body)
    if not isinstance(body, dict):
        raise ValueError("Invalid response")
    return response.status, body


_DNS_LABEL = r'(?!-)[A-Za-z0-9-]{1,63}(?<!-)'
_CNAME_TARGET_RE = re.compile(r'(?:' + _DNS_LABEL + r'\.)+' + _DNS_LABEL + r'\.?')


def _valid_dns_target(value):
    """Strict multi-label DNS record target (e.g. a recommended CNAME).

    Rejects malformed shapes that a loose character-class regex would
    accept (bare hyphens, consecutive dots, leading/trailing hyphen
    labels) -- the same per-label discipline as ``valid_custom_hostname``,
    but without the reserved-TLD/vercel.app restrictions
    since this validates a Vercel-recommended target, not a customer's
    own hostname claim.
    """
    return (isinstance(value, str) and 1 <= len(value) <= 253
            and bool(_CNAME_TARGET_RE.fullmatch(value)))


def valid_custom_hostname(hostname):
    """Canonical ASCII FQDN only; no URL normalization or implicit ownership."""
    if (not isinstance(hostname, str) or len(hostname) > 253
            or hostname != hostname.lower()
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', hostname)):
        return False
    labels = hostname.split('.')
    if (not re.fullmatch(r'[a-z]{2,63}', labels[-1])
            or labels[-1] in {'localhost', 'local', 'internal', 'test', 'invalid', 'example', 'onion'}
            or hostname == 'vercel.app' or hostname.endswith('.vercel.app')):
        return False
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        return True


class VercelAdapter:
    """Team-scoped, app-owned, static preview only; no git or promotion API.

    ownership_namespace is a stable installation ID, not a secret. Project
    marker is a plain preview env entry; require plaintext readback. Missing
    or masked marker fails closed, never adopts a colliding project.
    """
    def __init__(self, token, team_id, ownership_namespace, transport=None):
        if not all(isinstance(v, str) and v for v in (token, team_id, ownership_namespace)):
            raise ValueError("Token, team and ownership namespace required")
        self.token, self.team_id, self.namespace = token, team_id, ownership_namespace
        self.transport = transport or UrllibHttpTransport()

    def project_name_for(self, app_id):
        digest = hashlib.sha256((self.namespace + '\0' + app_id).encode()).hexdigest()
        return 'wb-' + digest[:40]

    def _marker(self, app_id):
        return hashlib.sha256((self.namespace + '\0' + app_id).encode()).hexdigest()

    def _call(self, method, path, payload=None, **query):
        query['teamId'] = self.team_id
        return _json_call(self.transport, method,
                          'https://api.vercel.com' + path + '?' + urlencode(query),
                          {'Authorization': 'Bearer ' + self.token,
                           'Content-Type': 'application/json'}, payload)

    def _project_valid(self, project, app_id):
        return (isinstance(project, dict) and bool(project.get('id'))
                and project.get('name') == self.project_name_for(app_id)
                and project.get('accountId') == self.team_id
                and any(e.get('key') == 'WEBSITE_BUILDER_OWNER'
                        and e.get('value') == self._marker(app_id)
                        and e.get('type') == 'plain'
                        for e in project.get('env', []) if isinstance(e, dict)))

    def lookup_project(self, app_id):
        """Read-only reconciliation; absence never authorizes another create."""
        try:
            status, project = self._call('GET', '/v9/projects/' + self.project_name_for(app_id))
            if status != 200 or not self._project_valid(project, app_id):
                return _fail('PROJECT_RECONCILIATION_REQUIRED')
            return OperationResult.ok({'project': project, 'app_id': app_id})
        except Exception:
            return _fail('PROJECT_RECONCILIATION_REQUIRED')

    def ensure_project(self, app_id):
        """GET deterministic name; create only after explicit 404. No retry.

        Reconciliation is a subsequent call. Ownership is set atomically in
        create, then read back; timeout/conflict never causes another POST.
        """
        try:
            name = self.project_name_for(app_id)
            status, project = self._call('GET', '/v9/projects/' + name)
            if status == 404:
                status, project = self._call('POST', '/v11/projects', {
                    'name': name, 'framework': None,
                    'environmentVariables': [{'key': 'WEBSITE_BUILDER_OWNER',
                        'value': self._marker(app_id), 'type': 'plain', 'target': ['preview']}],
                })
                if status not in (200, 201):
                    return _fail('AMBIGUOUS_PROJECT_CREATE')
                status, project = self._call('GET', '/v9/projects/' + name)
            if status != 200 or not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            return OperationResult.ok({'project': project, 'app_id': app_id})
        except Exception:
            return _fail('PROJECT_RECONCILIATION_REQUIRED')

    def _meta(self, app_id, operation_id, source_revision, artifact_sha256):
        if (not operation_id or len(operation_id) > 128 or type(source_revision) is not int
                or source_revision < 1 or not re.fullmatch('[a-f0-9]{64}', artifact_sha256)):
            raise ValueError('Invalid operation identity')
        return {'wbOwner': self._marker(app_id), 'wbOperation': operation_id,
                'wbRevision': str(source_revision), 'wbArtifact': artifact_sha256}

    def _deployment(self, body, project, meta):
        # Vercel preview target is null; "preview" is accepted in responses.
        team = body.get('teamId') or (body.get('team') or {}).get('id')
        project_id = body.get('projectId') or (body.get('project') or {}).get('id')
        if (not body.get('id') or project_id != project['id'] or team != self.team_id
                or body.get('name') != project['name'] or 'target' not in body
                or body['target'] not in (None, 'preview')
                or any((body.get('meta') or {}).get(k) != v for k, v in meta.items())):
            return _fail('DEPLOYMENT_IDENTITY_MISMATCH')
        url = 'https://' + body.get('url', '')
        if not _safe_origin(url):
            return _fail('INVALID_PREVIEW_URL')
        return OperationResult.ok({'deployment_id': body['id'], 'preview_url': url,
                                  'state': body.get('readyState') or body.get('status'),
                                  'deployment': body})

    def deploy_static_files(self, app_id, project, files, operation_id,
                            source_revision, artifact_sha256):
        """files maps canonical relative POSIX names to immutable bytes.

        Uses Vercel's static v2 builder with inline exact bytes. No package
        install/git build; no production target (omission means preview).
        Caller supplies its snapshot fingerprint as durable metadata.
        """
        try:
            if not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            meta = self._meta(app_id, operation_id, source_revision, artifact_sha256)
            if not files or 'index.html' not in files:
                return _fail('INVALID_STATIC_FILES')
            entries = []
            for name, content in sorted(files.items()):
                if (not isinstance(content, bytes) or not isinstance(name, str)
                        or str(PurePosixPath(name)) != name or name.startswith('/')
                        or any(p in ('.', '..', '') for p in name.split('/'))
                        or any(c in name for c in ('\\', ':', '\0'))
                        or name in ('vercel.json', '.vercel/project.json')):
                    return _fail('INVALID_STATIC_FILES')
                entries.append({'file': name, 'data': base64.b64encode(content).decode(),
                                'encoding': 'base64'})
            status, body = self._call('POST', '/v13/deployments', {
                'name': project['name'], 'project': project['id'], 'version': 2,
                'files': entries, 'builds': [{'src': '**', 'use': '@vercel/static'}],
                'meta': meta, 'projectSettings': {'framework': None},
            })
            if status not in (200, 201):
                return _fail('DEPLOYMENT_RECONCILIATION_REQUIRED')
            return self._deployment(body, project, meta)
        except Exception:
            return _fail('DEPLOYMENT_RECONCILIATION_REQUIRED')

    def find_deployment_by_operation_id(self, app_id, project, operation_id,
                                       source_revision, artifact_sha256, max_pages=100):
        """Paginate entire project scope, then GET unique match for identity.

        Missing metadata, malformed pagination, repeated cursors and multiple
        matching IDs fail closed. NOT_FOUND is not permission to resend.
        """
        try:
            if not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            meta = self._meta(app_id, operation_id, source_revision, artifact_sha256)
            matches, seen, query = {}, set(), {'projectId': project['id'], 'limit': 100}
            for _ in range(max_pages):
                status, body = self._call('GET', '/v6/deployments', **query)
                if (status != 200 or not isinstance(body.get('deployments'), list)
                        or not isinstance(body.get('pagination'), dict)
                        or 'next' not in body['pagination']):
                    return _fail('INCOMPLETE_LOOKUP')
                for item in body['deployments']:
                    if not isinstance(item.get('meta'), dict):
                        return _fail('INCOMPLETE_LOOKUP')
                    if item['meta'].get('wbOperation') == operation_id:
                        identifier = item.get('uid') or item.get('id')
                        if not identifier:
                            return _fail('INCOMPLETE_LOOKUP')
                        matches[identifier] = item
                cursor = body['pagination']['next']
                if cursor is None:
                    break
                if type(cursor) is not int or cursor in seen:
                    return _fail('INCOMPLETE_LOOKUP')
                seen.add(cursor)
                query['until'] = cursor
            else:
                return _fail('INCOMPLETE_LOOKUP')
            if len(matches) != 1:
                return _fail('AMBIGUOUS_DEPLOYMENT' if matches else 'NOT_FOUND')
            identifier = next(iter(matches))
            status, body = self._call('GET', '/v13/deployments/' + quote(identifier, safe=''))
            if status != 200 or body.get('id') != identifier:
                return _fail('DEPLOYMENT_IDENTITY_MISMATCH')
            return self._deployment(body, project, meta)
        except Exception:
            return _fail('INCOMPLETE_LOOKUP')


    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256):
        """Promote an EXISTING deployment (already built/deployed as preview)
        to the project's production alias. No rebuild, no new files.

        Verifies project/meta identity on the deployment both before the
        promote call and after, via GET — never trusts the promote response
        body alone. Caller supplies the exact operation identity that was
        bound to the original preview deploy_static_files call.
        """
        try:
            if not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            meta = self._meta(app_id, operation_id, source_revision, artifact_sha256)
            if not deployment_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', deployment_id):
                return _fail('INVALID_DEPLOYMENT_ID')
            # Confirm the deployment we are about to promote is the exact one
            # bound to this operation identity BEFORE promoting anything.
            status, body = self._call('GET', '/v13/deployments/' + quote(deployment_id, safe=''))
            if (status != 200 or body.get('id') != deployment_id
                    or any((body.get('meta') or {}).get(k) != v for k, v in meta.items())):
                return _fail('DEPLOYMENT_IDENTITY_MISMATCH')
            status, _ = self._call(
                'POST', '/v10/projects/' + project['id'] + '/promote/' + quote(deployment_id, safe=''),
                {},
            )
            if status not in (200, 201, 204):
                return _fail('PROMOTE_RECONCILIATION_REQUIRED')
            # Re-GET after promote; never trust the promote response body for
            # production identity/target.
            status, body = self._call('GET', '/v13/deployments/' + quote(deployment_id, safe=''))
            team = body.get('teamId') or (body.get('team') or {}).get('id')
            project_id_field = body.get('projectId') or (body.get('project') or {}).get('id')
            if (status != 200 or body.get('id') != deployment_id or project_id_field != project['id']
                    or team != self.team_id or body.get('target') != 'production'
                    or any((body.get('meta') or {}).get(k) != v for k, v in meta.items())):
                return _fail('PROMOTE_VERIFICATION_FAILED')
            url = 'https://' + body.get('url', '')
            if not _safe_origin(url):
                return _fail('INVALID_PRODUCTION_URL')
            return OperationResult.ok({'deployment_id': body['id'], 'production_url': url,
                                       'state': body.get('readyState') or body.get('status'),
                                       'deployment': body})
        except Exception:
            return _fail('PROMOTE_RECONCILIATION_REQUIRED')

    def _valid_hostname_for_api(self, hostname):
        return valid_custom_hostname(hostname)

    def add_domain(self, app_id, project, hostname):
        """Attach an existing user-owned hostname to the owned project.

        This is the REAL remote binding call -- Vercel considers the domain
        attached to the project immediately on success, independent of DNS
        or HTTPS readiness. Callers must not conflate this with "safe to
        serve traffic"; that requires verify_domain() AND a passing HTTPS
        smoke test. A 409 (already attached to this exact project) is
        reconciled via GET -- never re-POSTed, matching the ensure_project
        no-duplicate-create convention.
        """
        try:
            if not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            if not self._valid_hostname_for_api(hostname):
                return _fail('INVALID_HOSTNAME')
            status, body = self._call(
                'POST', '/v10/projects/' + project['id'] + '/domains', {'name': hostname},
            )
            if status == 409:
                status, body = self._call(
                    'GET', '/v9/projects/' + project['id'] + '/domains/' + quote(hostname, safe=''),
                )
            if status not in (200, 201) or body.get('name') != hostname:
                return _fail('DOMAIN_ATTACH_RECONCILIATION_REQUIRED')
            return self.get_project_domain(app_id, project, hostname)
        except Exception:
            return _fail('DOMAIN_ATTACH_RECONCILIATION_REQUIRED')

    def get_domain_config(self, hostname):
        """Read-only DNS configuration status for a hostname (misconfigured bool).

        Never resolves DNS locally; ownership/config are proven exclusively
        via this Vercel API response. `misconfigured` must be an actual
        bool in the response -- any other shape fails closed rather than
        being truthy/falsy-coerced.
        """
        try:
            if not self._valid_hostname_for_api(hostname):
                return _fail('INVALID_HOSTNAME')
            status, body = self._call('GET', '/v6/domains/' + quote(hostname, safe='') + '/config')
            misconfigured = body.get('misconfigured')
            if status != 200 or not isinstance(misconfigured, bool):
                return _fail('DOMAIN_CONFIG_LOOKUP_FAILED')
            recommendations = {}
            for key in ('recommendedIPv4', 'recommendedCNAME'):
                value = body.get(key, [])
                if not isinstance(value, list):
                    return _fail('DOMAIN_CONFIG_LOOKUP_FAILED')
                for item in value:
                    if not isinstance(item, dict) or type(item.get('rank')) is not int:
                        return _fail('DOMAIN_CONFIG_LOOKUP_FAILED')
                    target = item.get('value')
                    if key == 'recommendedIPv4':
                        if (not isinstance(target, list) or not target
                                or not all(isinstance(ip, str) and
                                           ipaddress.IPv4Address(ip).is_global for ip in target)):
                            return _fail('DOMAIN_CONFIG_LOOKUP_FAILED')
                    elif not _valid_dns_target(target):
                        return _fail('DOMAIN_CONFIG_LOOKUP_FAILED')
                recommendations[key] = value
            return OperationResult.ok({'misconfigured': misconfigured, **recommendations})
        except Exception:
            return _fail('DOMAIN_CONFIG_LOOKUP_FAILED')

    def verify_domain(self, app_id, project, hostname):
        """POST verify endpoint; re-GETs the project-scoped domain record
        afterward and never trusts the POST response body alone for the
        verified flag (mirrors promote_deployment's before/after-GET
        pattern). Verifies project identity before ever calling verify.
        """
        try:
            if not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            if not self._valid_hostname_for_api(hostname):
                return _fail('INVALID_HOSTNAME')
            status, body = self._call(
                'POST', '/v9/projects/' + project['id'] + '/domains/'
                + quote(hostname, safe='') + '/verify', {},
            )
            if status not in (200, 201) or body.get('name') != hostname:
                return _fail('DOMAIN_VERIFY_RECONCILIATION_REQUIRED')
            return self.get_project_domain(app_id, project, hostname)
        except Exception:
            return _fail('DOMAIN_VERIFY_RECONCILIATION_REQUIRED')

    def get_project_domain(self, app_id, project, hostname):
        """Read-only reconciliation of a project-scoped domain binding.

        Never mutates anything -- used to re-check verified status without
        repeating a verify POST once one has already been attempted.
        """
        try:
            if not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            if not self._valid_hostname_for_api(hostname):
                return _fail('INVALID_HOSTNAME')
            status, body = self._call(
                'GET', '/v9/projects/' + project['id'] + '/domains/' + quote(hostname, safe=''),
            )
            verified = body.get('verified')
            if status != 200 or body.get('name') != hostname or not isinstance(verified, bool):
                return _fail('DOMAIN_LOOKUP_FAILED')
            verification = body.get('verification', [])
            if (not isinstance(verification, list)
                    or not all(isinstance(v, dict) and all(isinstance(v.get(k), str)
                               and v[k] for k in ('type', 'domain', 'value')) for v in verification)
                    or body.get('redirect') is not None or body.get('gitBranch') is not None
                    or body.get('customEnvironmentId') is not None):
                return _fail('DOMAIN_LOOKUP_FAILED')
            return OperationResult.ok({'hostname': hostname, 'verified': verified,
                                       'verification': verification})
        except Exception:
            return _fail('DOMAIN_LOOKUP_FAILED')

    def check_domain_production(self, app_id, project, identity):
        """Exact current project target, not newest historical production row."""
        try:
            current = self.lookup_project(app_id)
            if not current.success:
                return current
            fresh = current.data['project']
            identifier = identity['deployment_id']
            if (fresh['id'] != project['id']
                    or (fresh.get('targets') or {}).get('production', {}).get('id') != identifier):
                return _fail('PRODUCTION_IDENTITY_MISMATCH')
            meta = self._meta(app_id, identity['operation_id'], identity['source_revision'],
                              identity['artifact_sha256'])
            status, body = self._call('GET', '/v13/deployments/' + quote(identifier, safe=''))
            if (status != 200 or body.get('id') != identifier
                    or body.get('projectId') != fresh['id'] or body.get('teamId') != self.team_id
                    or body.get('name') != fresh['name'] or body.get('target') != 'production'
                    or body.get('readyState') != 'READY'
                    or any((body.get('meta') or {}).get(k) != v for k, v in meta.items())):
                return _fail('PRODUCTION_IDENTITY_MISMATCH')
            return OperationResult.ok({'project': fresh})
        except Exception:
            return _fail('PRODUCTION_IDENTITY_MISMATCH')

    def find_production_deployment(self, app_id, project):
        """Read-only lookup of the CURRENT production deployment, if any.

        Used to capture last-known-good identity before promoting a new
        deployment, so a failed post-promotion smoke check can roll back.
        """
        try:
            if not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            status, body = self._call('GET', '/v6/deployments',
                                      projectId=project['id'], target='production', limit=1)
            if status != 200 or not isinstance(body.get('deployments'), list):
                return _fail('INCOMPLETE_LOOKUP')
            deployments = body['deployments']
            if not deployments:
                return OperationResult.ok({'deployment_id': None})
            item = deployments[0]
            identifier = item.get('uid') or item.get('id')
            if not identifier:
                return _fail('INCOMPLETE_LOOKUP')
            return OperationResult.ok({'deployment_id': identifier})
        except Exception:
            return _fail('INCOMPLETE_LOOKUP')


class TelegramAdapter:
    def __init__(self, bot_token, transport=None):
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', bot_token):
            raise ValueError('Invalid bot token')
        self.token, self.transport = bot_token, transport or UrllibHttpTransport()

    def _send(self, method, chat_id, body, content_type):
        try:
            response = self.transport.request('POST',
                'https://api.telegram.org/bot' + self.token + '/' + method,
                headers={'Content-Type': content_type}, data=body, timeout=30)
            payload = json.loads(response.body)
            if response.status >= 500:
                return _fail('AMBIGUOUS_SEND')
            if payload.get('ok') is False:
                return _fail('TELEGRAM_REJECTED')
            result = payload.get('result', {})
            if (response.status != 200 or payload.get('ok') is not True
                    or type(result.get('message_id')) is not int or result['message_id'] <= 0
                    or str(result.get('chat', {}).get('id')) != str(chat_id)):
                return _fail('AMBIGUOUS_SEND')
            return OperationResult.ok({'message_id': result['message_id'], 'chat_id': str(chat_id)})
        except Exception:
            return _fail('AMBIGUOUS_SEND')

    def send_text(self, chat_id, text):
        if not isinstance(text, str) or not 0 < len(text) <= 4096:
            return _fail('INVALID_TEXT')
        return self._send('sendMessage', chat_id,
                          json.dumps({'chat_id': str(chat_id), 'text': text}).encode(),
                          'application/json')

    def send_photo(self, chat_id, png_path, caption=''):
        try:
            path = Path(png_path)
            if path.is_symlink() or path.stat().st_size > 10 * 1024 * 1024:
                return _fail('INVALID_PNG')
            png = path.read_bytes()
            if not png.startswith(b'\x89PNG\r\n\x1a\n') or len(caption) > 1024:
                return _fail('INVALID_PNG')
        except (OSError, ValueError):
            return _fail('INVALID_PNG')
        boundary = uuid.uuid4().hex
        fields = []
        for key, value in (('chat_id', str(chat_id)), ('caption', caption)):
            fields.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                           f'name="{key}"\r\n\r\n{value}\r\n').encode())
        fields.append((f'--{boundary}\r\nContent-Disposition: form-data; '
                       'name="photo"; filename="preview.png"\r\n'
                       'Content-Type: image/png\r\n\r\n').encode() + png)
        fields.append(f'\r\n--{boundary}--\r\n'.encode())
        return self._send('sendPhoto', chat_id, b''.join(fields),
                          'multipart/form-data; boundary=' + boundary)


def _safe_origin(url):
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.port in (None, 443)
                and not p.username and not p.password and not p.fragment
                and not any(c.isspace() or c == '\\' for c in url)
                and bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.vercel\.app',
                                      p.hostname or '')))
    except ValueError:
        return False


class PreviewSmokeTester:
    """Fresh anonymous browser contexts; same-origin GET/HEAD requests only.

    Blocks redirects before following, websockets, workers, cross-origin and
    private DNS requests. Strict policy intentionally fails externally hosted
    assets; caller must self-host assets. Factory returns a Playwright-style
    Browser. Requires route_web_socket support, otherwise fails closed.
    """
    def __init__(self, browser_factory, resolver=None, *, custom_hostname=None):
        if custom_hostname is not None and not valid_custom_hostname(custom_hostname):
            raise ValueError('INVALID_HOSTNAME')
        self.custom_hostname = custom_hostname
        self.factory = browser_factory
        self.resolver = resolver or (lambda h: [a[4][0] for a in socket.getaddrinfo(h, 443)])

    def _allowed_origin(self, url):
        if self.custom_hostname is None:
            return _safe_origin(url)
        try:
            p = urlsplit(url)
            return (p.scheme == 'https' and p.netloc == self.custom_hostname
                    and not p.username and not p.password and not p.fragment
                    and not any(c.isspace() or c == '\\' for c in url))
        except ValueError:
            return False

    def run(self, url, out_dir):
        if not self._allowed_origin(url):
            return _fail('INVALID_PREVIEW_URL')
        failures, shots = [], {}
        origin = urlsplit(url).netloc
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            for label, viewport in (('desktop', {'width': 1440, 'height': 900}),
                                    ('mobile', {'width': 390, 'height': 844})):
                browser = self.factory()
                context = None
                try:
                    context = browser.new_context(viewport=viewport, service_workers='block',
                                                  accept_downloads=False, ignore_https_errors=False)
                    def route_request(route):
                        request = route.request
                        try:
                            allowed = (self._allowed_origin(request.url)
                                and urlsplit(request.url).netloc == origin
                                and request.method in ('GET', 'HEAD')
                                and request.redirected_from is None)
                            addresses = self.resolver(urlsplit(request.url).hostname) if allowed else []
                            allowed = bool(addresses) and all(
                                ipaddress.ip_address(a).is_global for a in addresses)
                        except Exception:
                            allowed = False
                        if allowed:
                            route.continue_()
                        else:
                            failures.append(label + ': blocked request')
                            route.abort()
                    context.route('**/*', route_request)
                    def block_socket(ws):
                        failures.append(label + ': blocked websocket')
                        ws.close()
                    context.route_web_socket('**/*', block_socket)
                    page = context.new_page()
                    page.on('pageerror', lambda _: failures.append('runtime error'))
                    page.on('console', lambda msg: failures.append('console error')
                            if msg.type == 'error' else None)
                    page.on('requestfailed', lambda _: failures.append('asset/request failure'))
                    page.on('response', lambda response: failures.append('HTTP asset failure')
                            if response.status >= 400 else None)
                    response = page.goto(url, wait_until='networkidle', timeout=30000)
                    if response is None or response.status != 200 or page.url != url:
                        failures.append(label + ': navigation failed/redirected')
                    healthy = page.evaluate('''() => Boolean(document.body &&
                        document.body.innerText.trim().length &&
                        [...document.images].every(i => i.complete && i.naturalWidth > 0))''')
                    if healthy is not True:
                        failures.append(label + ': empty page or broken images')
                    png = page.screenshot(full_page=True, type='png')
                    if not isinstance(png, bytes) or not png.startswith(b'\x89PNG\r\n\x1a\n'):
                        failures.append(label + ': invalid screenshot')
                    else:
                        path = Path(out_dir) / (label + '.png')
                        path.write_bytes(png)
                        shots[label + '_screenshot'] = str(path)
                finally:
                    try:
                        if context is not None:
                            context.close()
                    finally:
                        browser.close()
        except Exception:
            failures.append('browser smoke failed')
        return OperationResult(
            success=not failures,
            data={**shots, 'url': url, 'failures': failures},
            error_code='SMOKE_FAILED' if failures else None,
        )
