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

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode, urlsplit

from app.core.contracts import OperationResult

logger = logging.getLogger(__name__)


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


# Fixed, content-free bootstrap payload. Exists ONLY to consume Vercel's
# unavoidable first-deployment auto-promotion behavior before any real user
# artifact is ever deployed. Never shown to a user, never sent to Telegram,
# never referenced by latest_shown_preview/approval/revision history.
_BOOTSTRAP_HTML = b'<!doctype html><title>.</title>'


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

    def _project_valid(self, project, app_id, expected_name=None):
        """Ownership/identity check. ``expected_name`` defaults to the
        opaque hash-derived name; callers using a friendly Vercel slug pass
        it explicitly. The WEBSITE_BUILDER_OWNER marker (derived only from
        the immutable internal ``app_id``) remains the SOLE ownership
        authority regardless of which name is being validated — the slug
        itself never proves ownership.
        """
        expected_name = expected_name or self.project_name_for(app_id)
        return (isinstance(project, dict) and bool(project.get('id'))
                and project.get('name') == expected_name
                and project.get('accountId') == self.team_id
                and any(e.get('key') == 'WEBSITE_BUILDER_OWNER'
                        and e.get('value') == self._marker(app_id)
                        and e.get('type') == 'plain'
                        for e in project.get('env', []) if isinstance(e, dict)))

    def lookup_project(self, app_id, *, expected_name=None):
        """Read-only reconciliation; absence never authorizes another create.

        ``expected_name`` is the canonical Vercel project name, determined
        independently from trusted application state (the bound friendly
        slug); None keeps the legacy opaque hash-derived lookup key. The
        same value is BOTH the lookup key and the validated name -- never
        project.get('name') from the response itself.
        """
        try:
            name = expected_name or self.project_name_for(app_id)
            status, project = self._call('GET', '/v9/projects/' + name)
            if status != 200 or not self._project_valid(project, app_id, expected_name=expected_name):
                return _fail('PROJECT_RECONCILIATION_REQUIRED')
            return OperationResult.ok({'project': project, 'app_id': app_id})
        except Exception:
            return _fail('PROJECT_RECONCILIATION_REQUIRED')

    def ensure_project(self, app_id):
        """GET deterministic name; create only after explicit 404. No retry.

        Reconciliation is a subsequent call. Ownership is set atomically in
        create, then read back; timeout/conflict never causes another POST.

        BUG 3: a non-2xx create response is NOT automatically "ambiguous".
        Classification is evidence-based on the provider's status code (see
        ``_classify_create_failure``): a deterministic validation rejection is
        a CONFIRMED failure, a collision is proven by a read-only lookup, and
        only a transport/5xx/rate-limit case is genuinely ambiguous.
        """
        try:
            name = self.project_name_for(app_id)
            status, project = self._call('GET', '/v9/projects/' + name)
            create_attempted = False
            if status == 404:
                create_attempted = True
                status, project = self._call('POST', '/v11/projects', {
                    'name': name, 'framework': None,
                    'environmentVariables': [{'key': 'WEBSITE_BUILDER_OWNER',
                        'value': self._marker(app_id), 'type': 'plain', 'target': ['preview']}],
                })
                if status not in (200, 201):
                    return self._classify_create_failure(
                        app_id, name, status, expected_name=None)
                status, project = self._call('GET', '/v9/projects/' + name)
            if status != 200 or not self._project_valid(project, app_id):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            return OperationResult.ok({'project': project, 'app_id': app_id})
        except Exception:
            # A transport failure DURING create leaves the outcome unknown ->
            # ambiguous. A pre-create lookup failure created nothing ->
            # reconciliation-required (preserving the prior contract).
            if create_attempted:
                return _fail('AMBIGUOUS_PROJECT_CREATE')
            return _fail('PROJECT_RECONCILIATION_REQUIRED')

    # Status codes that prove a DETERMINISTIC, non-ambiguous create rejection
    # (the request was received and rejected on its merits — never transport
    # ambiguity, never "maybe it was created").
    _CREATE_CONFIRMED_REJECTION = frozenset({400, 401, 403, 404, 405, 415, 422})

    def _classify_create_failure(self, app_id, name, status, *, expected_name):
        """Classify a non-2xx POST /v11/projects response.

        Never blindly reports AMBIGUOUS_PROJECT_CREATE for every non-2xx.
        """
        if status in self._CREATE_CONFIRMED_REJECTION:
            # Deterministic validation/auth rejection: the provider received
            # and rejected the request. This is a CONFIRMED create failure.
            return _fail('PROJECT_CREATE_REJECTED')
        if status == 409:
            # Collision: a project with this name already exists. It may be
            # OURS (a concurrent/previous create that beat us) or FOREIGN.
            # Resolve with a READ-ONLY lookup — never a second POST.
            return self._reconcile_after_create_collision(
                app_id, name, expected_name=expected_name)
        # 408 / 429 / 5xx / anything else: the request may or may not have
        # been accepted. Ambiguous — never a blind retry.
        return _fail('AMBIGUOUS_PROJECT_CREATE')

    def _reconcile_after_create_collision(self, app_id, name, *, expected_name):
        """Read-only resolution after a 409 create collision.

        Chooses the canonical name for the lookup: the friendly slug when one
        was requested, else the opaque hash-derived default.
        """
        lookup_name = expected_name or self.project_name_for(app_id)
        try:
            status, project = self._call('GET', '/v9/projects/' + lookup_name)
        except Exception:
            return _fail('AMBIGUOUS_PROJECT_CREATE')
        if status != 200 or not isinstance(project, dict):
            # Cannot prove who owns the colliding name -> ambiguous.
            return _fail('AMBIGUOUS_PROJECT_CREATE')
        if self._project_valid(project, app_id, expected_name=lookup_name):
            # The colliding project is OURS after all: created concurrently.
            return OperationResult.ok({'project': project, 'app_id': app_id})
        if not project.get('id') or project.get('name') != lookup_name:
            # Malformed/incomplete foreign record -> not proof of anything.
            return _fail('AMBIGUOUS_PROJECT_CREATE')
        # A well-formed, distinct project owned by someone else: a proven
        # name collision (never transport ambiguity).
        return _fail('PROJECT_NAME_TAKEN')

    def _fail_slug_collision_or_own(self, app_id, slug):
        """Read-only resolution of a 409 create response on the friendly-slug
        path. Same evidence discipline as ``_reconcile_after_create_collision``
        but keeps the slug-specific ``SLUG_COLLISION`` code the product
        surfaces to the user."""
        try:
            status, project = self._call('GET', '/v9/projects/' + slug)
        except Exception:
            return _fail('AMBIGUOUS_PROJECT_CREATE')
        if status != 200 or not isinstance(project, dict):
            return _fail('AMBIGUOUS_PROJECT_CREATE')
        if self._project_valid(project, app_id, expected_name=slug):
            return OperationResult.ok({'project': project, 'app_id': app_id, 'slug': slug})
        if not project.get('id') or project.get('name') != slug:
            return _fail('AMBIGUOUS_PROJECT_CREATE')
        return _fail('SLUG_COLLISION')

    def ensure_project_with_slug(self, app_id, slug):
        """Like ``ensure_project``, but requests the human-friendly ``slug``
        as the Vercel project name instead of the opaque hash-derived name.

        The WEBSITE_BUILDER_OWNER marker (derived only from the immutable
        ``app_id``) remains the SOLE ownership/dedup authority — the slug
        is never trusted for identity. Four possible outcomes, matching the
        product's collision-handling contract:

          A. slug available (404)            -> create, mark owned, return ok
          B. slug names THIS owned project    -> reconcile, return ok
          C. slug exists, marker mismatch/
             missing (foreign/unowned project) -> SLUG_COLLISION, never
             adopt/overwrite
          D. ambiguous/malformed response      -> fail closed
             (PROJECT_RECONCILIATION_REQUIRED), never assume availability

        No retry/second POST on ambiguity — same no-duplicate-create
        discipline as ``ensure_project``.
        """
        create_attempted = False
        try:
            status, project = self._call('GET', '/v9/projects/' + slug)
            if status == 404:
                create_attempted = True
                status, project = self._call('POST', '/v11/projects', {
                    'name': slug, 'framework': None,
                    'environmentVariables': [{'key': 'WEBSITE_BUILDER_OWNER',
                        'value': self._marker(app_id), 'type': 'plain', 'target': ['preview']}],
                })
                if status not in (200, 201):
                    # BUG 3: classify by evidence, never blanket-ambiguous.
                    # A deterministic 4xx rejection is a confirmed failure; a
                    # 409 is a PROVEN collision resolved read-only; only a
                    # transport/5xx/rate-limit case is ambiguous.
                    if status == 409:
                        return self._fail_slug_collision_or_own(app_id, slug)
                    return self._classify_create_failure(
                        app_id, slug, status, expected_name=slug)
                status, project = self._call('GET', '/v9/projects/' + slug)
                if status != 200 or not self._project_valid(project, app_id, expected_name=slug):
                    return _fail('PROJECT_IDENTITY_MISMATCH')
                return OperationResult.ok({'project': project, 'app_id': app_id, 'slug': slug})
            if status != 200 or not isinstance(project, dict):
                return _fail('PROJECT_RECONCILIATION_REQUIRED')
            if self._project_valid(project, app_id, expected_name=slug):
                # Outcome B: already our own project under this exact slug.
                return OperationResult.ok({'project': project, 'app_id': app_id, 'slug': slug})
            # A confirmed collision requires a WELL-FORMED foreign project
            # record (real id + matching name) whose ownership marker simply
            # doesn't match this app_id -- that is proof Vercel returned an
            # actual, different project under this slug. Anything malformed
            # or incomplete (missing id/name) is NOT proof of a foreign
            # project -- it is an ambiguous provider response and must fail
            # closed as reconciliation-required, never be reported to the
            # user as "name already taken".
            if not project.get('id') or project.get('name') != slug:
                return _fail('PROJECT_RECONCILIATION_REQUIRED')
            # Outcome C: slug belongs to a real, distinct project we do not
            # own (ownership marker mismatch/missing) -- never adopt or
            # overwrite; surface as a distinct proven collision.
            return _fail('SLUG_COLLISION')
        except Exception:
            # BUG 3: distinguish a transport failure DURING create (outcome
            # unknown -> ambiguous, never a blind retry) from a pre-create
            # lookup failure (nothing was created -> reconciliation).
            if create_attempted:
                return _fail('AMBIGUOUS_PROJECT_CREATE')
            return _fail('PROJECT_RECONCILIATION_REQUIRED')

    def _meta(self, app_id, operation_id, source_revision, artifact_sha256):
        if (not operation_id or len(operation_id) > 128 or type(source_revision) is not int
                or source_revision < 1 or not re.fullmatch('[a-f0-9]{64}', artifact_sha256)):
            raise ValueError('Invalid operation identity')
        return {'wbOwner': self._marker(app_id), 'wbOperation': operation_id,
                'wbRevision': str(source_revision), 'wbArtifact': artifact_sha256}

    def _current_production_id(self, project):
        """Read-only fetch of the project's CURRENT canonical production
        deployment id, straight from the provider (never from the
        deployment response body itself, which is not authoritative for
        "is this the live target").

        Returns ``None`` when the project has no production deployment yet.
        Any malformed/ambiguous shape raises -- callers must fail closed,
        never assume "not live" from an unreadable response.
        """
        status, fresh = self._call('GET', '/v9/projects/' + project['name'])
        if status != 200 or not isinstance(fresh, dict) or fresh.get('id') != project['id']:
            raise ValueError('PRODUCTION_BINDING_LOOKUP_FAILED')
        targets = fresh.get('targets')
        if targets is None:
            return None
        if not isinstance(targets, dict):
            raise ValueError('PRODUCTION_BINDING_LOOKUP_FAILED')
        prod = targets.get('production')
        if prod is None:
            return None
        if not isinstance(prod, dict) or not prod.get('id'):
            raise ValueError('PRODUCTION_BINDING_LOOKUP_FAILED')
        return prod['id']

    def _deployment(self, body, project, meta):
        # Vercel's documented behavior: a brand-new project's FIRST
        # deployment is automatically promoted to target="production" even
        # when no production target was requested (there is no "preview"
        # literal accepted/returned by this API -- omission is the only
        # preview request shape). Because of that, `target` alone is
        # NEITHER proof this deployment is live NOR safe to auto-accept as
        # a preview candidate. The only authoritative proof of "currently
        # serving as the project's canonical production target" is the
        # project's own `targets.production` binding, fetched fresh here --
        # never inferred from this deployment response body.
        team = body.get('teamId') or (body.get('team') or {}).get('id')
        project_id = body.get('projectId') or (body.get('project') or {}).get('id')
        if (not body.get('id') or project_id != project['id'] or team != self.team_id
                or body.get('name') != project['name'] or 'target' not in body
                or body['target'] not in (None, 'production')
                or any((body.get('meta') or {}).get(k) != v for k, v in meta.items())):
            return _fail('DEPLOYMENT_IDENTITY_MISMATCH')
        if body['target'] == 'production':
            try:
                current_prod_id = self._current_production_id(project)
            except Exception:
                # Ambiguous/malformed binding lookup: never assume safe.
                return _fail('DEPLOYMENT_IDENTITY_MISMATCH')
            if current_prod_id == body['id']:
                # This exact deployment IS the project's live production
                # target -- never show it as an unpublished preview
                # candidate, even though every other identity marker matches.
                return _fail('DEPLOYMENT_ALREADY_LIVE')
        url = 'https://' + body.get('url', '')
        if not _safe_origin(url):
            return _fail('INVALID_PREVIEW_URL')
        return OperationResult.ok({'deployment_id': body['id'], 'preview_url': url,
                                  'state': body.get('readyState') or body.get('status'),
                                  'deployment': body})

    def deploy_static_files(self, app_id, project, files, operation_id,
                            source_revision, artifact_sha256, *, expected_name=None):
        """files maps canonical relative POSIX names to immutable bytes.

        Uses Vercel's static v2 builder with inline exact bytes. No package
        install/git build; no production target (omission means preview).
        Caller supplies its snapshot fingerprint as durable metadata.
        ``expected_name`` is the caller-determined canonical Vercel project
        name (the friendly slug resolved before ensure_project_with_slug, or
        None for the legacy opaque name) -- never inferred from the project
        response itself.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
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

    def _find_unique_deployment_by_meta_key(self, project, meta_key, meta_value, max_pages=100):
        """Shared pagination core for "find the ONE deployment in this
        project whose meta[meta_key] == meta_value". Used by both the real
        operation-id lookup and the bootstrap-deployment lookup so both
        share the exact same fail-closed pagination discipline (malformed
        pagination, repeated cursors, missing meta all fail closed; NOT_FOUND
        is not proof a resend/create is safe).

        Returns (failure_result_or_None, identifier). On success, the first
        element is ``None`` and ``identifier`` is set to the unique matching
        deployment id. On failure (including NOT_FOUND/AMBIGUOUS), the first
        element is the ``OperationResult`` to propagate and ``identifier`` is
        ``None``.
        """
        matches, seen, query = {}, set(), {'projectId': project['id'], 'limit': 100}
        for _ in range(max_pages):
            status, body = self._call('GET', '/v6/deployments', **query)
            if (status != 200 or not isinstance(body.get('deployments'), list)
                    or not isinstance(body.get('pagination'), dict)
                    or 'next' not in body['pagination']):
                return _fail('INCOMPLETE_LOOKUP'), None
            for item in body['deployments']:
                if not isinstance(item.get('meta'), dict):
                    return _fail('INCOMPLETE_LOOKUP'), None
                if item['meta'].get(meta_key) == meta_value:
                    identifier = item.get('uid') or item.get('id')
                    if not identifier:
                        return _fail('INCOMPLETE_LOOKUP'), None
                    matches[identifier] = item
            cursor = body['pagination']['next']
            if cursor is None:
                break
            if type(cursor) is not int or cursor in seen:
                return _fail('INCOMPLETE_LOOKUP'), None
            seen.add(cursor)
            query['until'] = cursor
        else:
            return _fail('INCOMPLETE_LOOKUP'), None
        if len(matches) != 1:
            return _fail('AMBIGUOUS_DEPLOYMENT' if matches else 'NOT_FOUND'), None
        return None, next(iter(matches))

    def _bootstrap_operation_id(self, app_id):
        # Distinct namespace from real content operation ids -- can never
        # collide with a genuine user-content operation_id (those are
        # produced by the caller from project/source/snapshot identity, never
        # from this fixed 'bootstrap' literal).
        return hashlib.sha256((self.namespace + '\0bootstrap\0' + app_id).encode()).hexdigest()

    def ensure_bootstrap(self, app_id, project, max_polls=20, interval=0.25,
                         *, expected_name=None):
        """Consume Vercel's unavoidable first-deployment auto-promotion with
        deterministic, content-free bytes -- BEFORE any real user artifact is
        ever deployed. No new local state/lifecycle: this is a pure remote
        reconciliation call, safe to repeat on every preview run.

        Success is proven ONLY by remote confirmation: after creating or
        reconciling the bootstrap deployment, this method polls (bounded)
        until ``project.targets.production.id`` actually equals the
        bootstrap deployment id AND that deployment is READY -- Vercel's
        promotion/alias assignment is eventually consistent, so a bare
        successful POST or a found ``wbBootstrap``-tagged row is NOT proof
        the project's production slot has actually been consumed yet.
        Callers (PreviewOrchestrator) must never deploy real user content
        until this method returns success.

        Outcomes:
          * project already has a production binding (real content already
            live, or a prior bootstrap already consumed it) -> no-op, no POST,
            no polling needed -- confirmation already stands.
          * no production binding yet, but a bootstrap deployment matching
            this project's deterministic identity already exists (crash
            after a prior POST) -> reconciled (no duplicate POST), then
            polled for confirmation exactly like a fresh create.
          * no production binding and no existing bootstrap -> POST the fixed
            minimal static payload once, then polled for confirmation.
          * confirmation not reached within the bounded poll -> fail closed;
            real content must NOT be deployed.
          * anything ambiguous/malformed -> fail closed, never guess.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            try:
                current = self._current_production_id(project)
            except Exception:
                return _fail('PROJECT_RECONCILIATION_REQUIRED')
            if current is not None:
                # Production already exists -- real content (already live)
                # or a previously-consumed bootstrap. Never create another;
                # the binding is already proven, no polling needed.
                return OperationResult.ok({'bootstrapped': False, 'already_current_production': True})

            operation_id = self._bootstrap_operation_id(app_id)
            marker = self._marker(app_id)
            fail, identifier = self._find_unique_deployment_by_meta_key(
                project, 'wbBootstrap', operation_id)
            if fail is not None and fail.error_code != 'NOT_FOUND':
                return fail
            reconciled = fail is None
            if not reconciled:
                entries = [{'file': 'index.html',
                           'data': base64.b64encode(_BOOTSTRAP_HTML).decode(), 'encoding': 'base64'}]
                status, body = self._call('POST', '/v13/deployments', {
                    'name': project['name'], 'project': project['id'], 'version': 2,
                    'files': entries, 'builds': [{'src': '**', 'use': '@vercel/static'}],
                    'meta': {'wbOwner': marker, 'wbBootstrap': operation_id},
                    'projectSettings': {'framework': None},
                })
                if status not in (200, 201) or not body.get('id'):
                    return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')
                identifier = body['id']

            # ---- Remote confirmation barrier: a POST/reconcile alone is
            # never proof. Promotion/alias assignment is eventually
            # consistent -- poll (bounded) until targets.production.id
            # actually equals this bootstrap deployment AND it is READY.
            for _ in range(max_polls):
                try:
                    prod_id = self._current_production_id(project)
                except Exception:
                    return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')
                if prod_id == identifier:
                    status, dep = self._call(
                        'GET', '/v13/deployments/' + quote(identifier, safe=''))
                    if (status == 200 and dep.get('id') == identifier
                            and dep.get('readyState') == 'READY'):
                        return OperationResult.ok({'bootstrapped': True,
                                                   'deployment_id': identifier,
                                                   'reconciled': reconciled,
                                                   'confirmed': True})
                    return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')
                if prod_id is not None:
                    # Production binding points somewhere else entirely --
                    # never assume our bootstrap will still land; fail closed
                    # rather than keep polling against a moving target.
                    return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')
                time.sleep(interval)
            return _fail('BOOTSTRAP_CONFIRMATION_TIMEOUT')
        except Exception:
            return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')

    def find_deployment_by_operation_id(self, app_id, project, operation_id,
                                       source_revision, artifact_sha256, max_pages=100,
                                       *, expected_name=None):
        """Paginate entire project scope, then GET unique match for identity.

        Missing metadata, malformed pagination, repeated cursors and multiple
        matching IDs fail closed. NOT_FOUND is not permission to resend.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            meta = self._meta(app_id, operation_id, source_revision, artifact_sha256)
            fail, identifier = self._find_unique_deployment_by_meta_key(
                project, 'wbOperation', operation_id, max_pages=max_pages)
            if fail is not None:
                return fail
            status, body = self._call('GET', '/v13/deployments/' + quote(identifier, safe=''))
            if status != 200 or body.get('id') != identifier:
                return _fail('DEPLOYMENT_IDENTITY_MISMATCH')
            return self._deployment(body, project, meta)
        except Exception:
            return _fail('INCOMPLETE_LOOKUP')


    def promote_deployment(self, app_id, project, deployment_id, operation_id,
                           source_revision, artifact_sha256, *, expected_name=None):
        """Promote an EXISTING deployment (already built/deployed as preview)
        to the project's production alias. No rebuild, no new files.

        Verifies project/meta identity on the deployment both before the
        promote call and after, via GET — never trusts the promote response
        body alone. Caller supplies the exact operation identity that was
        bound to the original preview deploy_static_files call.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
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

    def add_domain(self, app_id, project, hostname, *, expected_name=None):
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
            if not self._project_valid(project, app_id, expected_name=expected_name):
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
            return self.get_project_domain(app_id, project, hostname, expected_name=expected_name)
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

    def verify_domain(self, app_id, project, hostname, *, expected_name=None):
        """POST verify endpoint; re-GETs the project-scoped domain record
        afterward and never trusts the POST response body alone for the
        verified flag (mirrors promote_deployment's before/after-GET
        pattern). Verifies project identity before ever calling verify.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            if not self._valid_hostname_for_api(hostname):
                return _fail('INVALID_HOSTNAME')
            status, body = self._call(
                'POST', '/v9/projects/' + project['id'] + '/domains/'
                + quote(hostname, safe='') + '/verify', {},
            )
            if status not in (200, 201) or body.get('name') != hostname:
                return _fail('DOMAIN_VERIFY_RECONCILIATION_REQUIRED')
            return self.get_project_domain(app_id, project, hostname, expected_name=expected_name)
        except Exception:
            return _fail('DOMAIN_VERIFY_RECONCILIATION_REQUIRED')

    def get_project_domain(self, app_id, project, hostname, *, expected_name=None):
        """Read-only reconciliation of a project-scoped domain binding.

        Never mutates anything -- used to re-check verified status without
        repeating a verify POST once one has already been attempted.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
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

    def check_domain_production(self, app_id, project, identity, *, expected_name=None):
        """Exact current project target, not newest historical production row."""
        try:
            current = self.lookup_project(app_id, expected_name=expected_name)
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

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        """Read-only lookup of the CURRENT production deployment, if any.

        Used to capture last-known-good identity before promoting a new
        deployment, so a failed post-promotion smoke check can roll back.

        Returns the FULL trusted identity of the current production
        deployment -- deployment_id plus the ``wbOperation``/``wbRevision``/
        ``wbArtifact`` metadata the deployment was created with. A rollback
        must re-promote that deployment using ITS OWN identity (the same
        meta the promote path validates); deriving identity from the current
        promotion operation instead is a guaranteed meta mismatch. When the
        current production deployment cannot be identified unambiguously, or
        its stored identity is incomplete, this fails closed rather than
        returning a partial identity a caller might guess around.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
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
            # The deployment metadata carries the exact repository identity
            # the deployment was created with. Absent/incomplete metadata
            # means we cannot safely re-promote this deployment as a
            # rollback target -- fail closed instead of guessing.
            meta = item.get('meta')
            if not isinstance(meta, dict):
                return _fail('INCOMPLETE_LOOKUP')
            operation_id = meta.get('wbOperation')
            revision_raw = meta.get('wbRevision')
            artifact_sha256 = meta.get('wbArtifact')
            if (not isinstance(operation_id, str) or not operation_id
                    or not isinstance(artifact_sha256, str)
                    or not re.fullmatch('[a-f0-9]{64}', artifact_sha256)
                    or not isinstance(revision_raw, str) or not revision_raw.isdigit()
                    or int(revision_raw) < 1):
                return _fail('INCOMPLETE_LOOKUP')
            return OperationResult.ok({
                'deployment_id': identifier,
                'operation_id': operation_id,
                'source_revision': int(revision_raw),
                'artifact_sha256': artifact_sha256,
            })
        except Exception:
            return _fail('INCOMPLETE_LOOKUP')

    def reconcile_production_deployment(self, app_id, project, expected_identity, *,
                                        expected_name=None):
        """Read-only reconciliation of CURRENT production truth against an
        expected deployment identity -- used after an AMBIGUOUS promote
        (timeout / transport error) where the caller cannot tell whether the
        promote request reached Vercel.

        Truth is proven exclusively from provider state, exactly like
        ``find_production_deployment``: the project's current
        ``targets.production`` binding (never a promote response body), looked
        up by deployment id and re-validated against the project + the
        deployment's OWN ``wbOperation``/``wbRevision``/``wbArtifact`` meta.

        ``expected_identity`` is the COMPLETE trusted identity tuple:
        deployment_id, operation_id, source_revision, artifact_sha256. A
        caller must never pass a partial identity (never infer success from a
        URL or a bare deployment_id).

        Outcomes (``OperationResult``):
          * ``ok`` with ``{'status': 'PROMOTED', 'deployment_id': ...}`` --
            current production IS the expected deployment, with every trusted
            identity field matching exactly.
          * ``ok`` with ``{'status': 'NOT_PROMOTED', 'deployment_id': ...}`` --
            provider truth conclusively shows a DIFFERENT (or no) production
            deployment; the expected deployment was not promoted.
          * ``fail('PROMOTE_RECONCILIATION_REQUIRED')`` -- production truth is
            still ambiguous, could not be read, or the expected deployment's
            stored identity is incomplete. Callers MUST fail closed here and
            MUST NOT blindly re-issue a promote request.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
                return _fail('PROMOTE_RECONCILIATION_REQUIRED')
            if not isinstance(expected_identity, dict):
                return _fail('PROMOTE_RECONCILIATION_REQUIRED')
            deployment_id = expected_identity.get('deployment_id')
            operation_id = expected_identity.get('operation_id')
            source_revision = expected_identity.get('source_revision')
            artifact_sha256 = expected_identity.get('artifact_sha256')
            if (not deployment_id
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', deployment_id)
                    or not isinstance(operation_id, str) or not operation_id
                    or type(source_revision) is not int or source_revision < 1
                    or not isinstance(artifact_sha256, str)
                    or not re.fullmatch('[a-f0-9]{64}', artifact_sha256)):
                # Incomplete trusted identity -> cannot prove anything.
                return _fail('PROMOTE_RECONCILIATION_REQUIRED')
            # The expected deployment must exist, belong to this project, and
            # carry EXACTLY the expected operation identity. Anything else is
            # ambiguous -- never guess.
            status, body = self._call('GET', '/v13/deployments/' + quote(deployment_id, safe=''))
            if status != 200 or body.get('id') != deployment_id:
                return _fail('PROMOTE_RECONCILIATION_REQUIRED')
            team = body.get('teamId') or (body.get('team') or {}).get('id')
            project_id_field = body.get('projectId') or (body.get('project') or {}).get('id')
            meta = body.get('meta')
            if (project_id_field != project['id'] or team != self.team_id
                    or not isinstance(meta, dict)
                    or meta.get('wbOperation') != operation_id
                    or meta.get('wbRevision') != str(source_revision)
                    or meta.get('wbArtifact') != artifact_sha256):
                return _fail('PROMOTE_RECONCILIATION_REQUIRED')
            # Authoritative production binding, straight from the project.
            try:
                current_prod_id = self._current_production_id(project)
            except Exception:
                return _fail('PROMOTE_RECONCILIATION_REQUIRED')
            if current_prod_id == deployment_id:
                if body.get('readyState') != 'READY':
                    return _fail('PROMOTE_RECONCILIATION_REQUIRED')
                return OperationResult.ok({'status': 'PROMOTED',
                                           'deployment_id': deployment_id})
            # A conclusive, well-formed production binding to someone else
            # (or no production at all) proves the expected deployment is not
            # the live target.
            return OperationResult.ok({'status': 'NOT_PROMOTED',
                                       'deployment_id': current_prod_id})
        except Exception:
            return _fail('PROMOTE_RECONCILIATION_REQUIRED')


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


def _stop_browser_playwright(browser):
    """Deterministically stop the Playwright driver that owns *browser*.

    MUST be called on the same thread that created the browser (Playwright
    sync objects are thread-affine). Best-effort by design: it runs during
    cleanup, after ``browser.close()``, so a failure (or an owner-less
    injected/test Browser) must never convert an already-classified smoke
    result into a different one. A Browser without a
    ``_wb_playwright_owner`` attribute (e.g. test doubles) is a no-op.
    """
    owner = getattr(browser, '_wb_playwright_owner', None)
    if owner is None:
        return
    stop = getattr(owner, 'stop', None)
    if stop is None:
        return
    try:
        stop()
    except BaseException:
        # Cleanup-only failure; the browser/context are already closed. Log
        # with traceback for operators, never replace the primary result.
        logger.warning(
            'Preview smoke: Playwright stop (cleanup) failed', exc_info=True)


def _calling_thread_has_running_loop():
    """True when THIS thread currently owns a RUNNING asyncio loop.

    Playwright's synchronous API refuses to start on such a thread
    (``sync_playwright().start()`` raises "using Playwright Sync API inside
    the asyncio loop"); it also cannot be driven from a different thread once
    started (greenlet: "cannot switch to a different thread"). This mirrors
    the guard installed by playwright's own sync context manager.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return loop.is_running()


def _run_on_dedicated_thread(fn):
    """Run *fn* to completion on a dedicated short-lived worker thread.

    The calling thread blocks until the worker finishes, so project
    orchestration stays synchronous and sequential. The worker owns no
    asyncio state, so Playwright's sync API never observes a running loop
    there, and every Playwright object it creates lives and dies on this one
    thread. Returned values (and raised exceptions) cross the thread boundary
    verbatim; only thread-neutral results are propagated.
    """
    box = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # propagate verbatim, never swallow
            box["error"] = exc

    worker = threading.Thread(target=_target, name="wb-smoke-browser", daemon=True)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class PreviewSmokeTester:
    """Fresh anonymous browser contexts; same-origin GET/HEAD requests only.

    Blocks redirects before following, websockets, workers, cross-origin and
    private DNS requests. Strict policy intentionally fails externally hosted
    assets; caller must self-host assets. Factory returns a Playwright-style
    Browser. Requires route_web_socket support, otherwise fails closed.

    Thread affinity: when the calling thread owns a RUNNING asyncio loop
    (the state left by the in-process Hermes FAST turn on the runtime path),
    the ENTIRE synchronous smoke lifecycle -- factory(), new_context, route,
    new_page, goto, evaluate, screenshot and every close -- runs on one
    dedicated worker thread that stays alive for the whole lifecycle. This is
    mandatory because Playwright Sync API objects are thread-affine: bridging
    only the launch and then using the returned Browser on the caller fails
    with greenlet "cannot switch to a different thread". In the normal
    no-loop path (startup preflight, non-async callers) the direct
    synchronous path is preserved unchanged.
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
        # Thread-affinity dispatch: Playwright Sync objects (Browser,
        # BrowserContext, Page, greenlets) are bound to the thread that
        # created them. If the caller owns a running asyncio loop, run the
        # WHOLE synchronous lifecycle on one dedicated worker thread so that
        # no Playwright object ever crosses a thread boundary.
        if _calling_thread_has_running_loop():
            logger.warning(
                "Preview smoke: running asyncio loop detected on the calling "
                "thread — executing the full Playwright lifecycle on a "
                "dedicated worker thread"
            )
            return _run_on_dedicated_thread(lambda: self._run_sync(url, out_dir))
        return self._run_sync(url, out_dir)

    def _run_sync(self, url, out_dir):
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
                        # Deterministic Playwright driver cleanup (H-8): runs on
                        # the SAME thread that launched the browser (this frame),
                        # after the browser has closed, and is best-effort so it
                        # never masks a smoke failure. No-op for injected/test
                        # factories whose Browser carries no Playwright owner.
                        _stop_browser_playwright(browser)
        except Exception as exc:
            # Operator logs get the full exception + traceback. Persisted
            # failures carry ONLY the exception TYPE -- never str(exc), which
            # can embed URLs/query params or other sensitive runtime detail.
            logger.exception("Preview smoke browser failure for %s", url)
            failures.append(f'browser smoke failed: {type(exc).__name__}')
        return OperationResult(
            success=not failures,
            data={**shots, 'url': url, 'failures': failures},
            error_code='SMOKE_FAILED' if failures else None,
        )
