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

# Provider readyState values that PROVE the bootstrap deployment can never
# become READY. Once the project's production binding points at our bootstrap
# deployment, observing one of these is a terminal failure (fail closed); any
# OTHER non-READY value (BUILDING, QUEUED, INITIALIZING, ...) is treated as a
# normal transient state and simply keeps polling within the bounded loop.
_BOOTSTRAP_TERMINAL_STATES = frozenset({'ERROR', 'CANCELED'})

# Vercel automation-bypass secrets are 32 alphanumeric characters
# (``^[a-zA-Z0-9]{32}$``). Used to conservatively validate the LIVE map-key
# shape (the map key IS the secret) before accepting it.
_BYPASS_SECRET_RE = re.compile(r'[A-Za-z0-9]{32}')


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

    # ------------------------------------------------------------------
    # PHASE E — Vercel Deployment Protection automation bypass
    # ------------------------------------------------------------------

    def ensure_protection_bypass(self, app_id, project, *, expected_name=None):
        """Provision a PROJECT-SPECIFIC automation bypass secret for the
        owned project, via the official Vercel API:

            PATCH /v1/projects/{idOrName}/protection-bypass

        The automation bypass is PROJECT-SPECIFIC — one global secret does
        NOT work across projects. This call generates (or returns) the
        project's bypass secret. The secret is returned to the CALLER, which
        is responsible for storing it securely (see ``BypassSecretStore``);
        it is NEVER persisted by the adapter or included in any
        OperationResult error message.

        Documented request/response contract (Vercel REST API —
        ``update-protection-bypass-for-automation``):

          * Request body ``{"generate": {}}`` — ``generate`` is an OBJECT
            (``{secret?, note?}``), NOT a boolean. An empty object asks
            Vercel to generate a random secret. The secret is generated
            SERVER-SIDE: we never fabricate one locally.
          * 200 response — LIVE-EVIDENCE shapes (see ``_parse_bypass_secret``):
            the ``protectionBypass`` value is a MAP keyed by the secret
            itself, e.g. ``{"<secret>": {"createdAt", "createdBy",
            "isEnvVar", "scope"}}`` — the map KEY is the secret. The
            documented per-actor shape ``{"<createdBy>": {...,"secret":
            "<str>"}}`` and the top-level ``{"secret": "<str>"}`` string are
            also accepted.

        Conversation/reconciliation semantics (PHASE E):
          * 200 with EXACTLY ONE well-formed ``protectionBypass`` entry
            (map-key secret, or a record carrying a non-empty ``secret``) or a
            top-level ``secret`` string -> ok, secret returned.
          * deterministic request validation (400/422) -> sanitized
            ``BYPASS_PROVISION_REJECTED`` (a deterministic 4xx is NEVER
            reported as ambiguous).
          * definitive authz failure (401/403) -> ``BYPASS_PROVISION_FORBIDDEN``.
          * unsupported/not-found endpoint as documented (404/405/501) ->
            explicit sanitized ``BYPASS_PROVISION_UNSUPPORTED``.
          * 429 / 5xx / transport uncertainty -> ``AMBIGUOUS_BYPASS_PROVISION``
            (never blind-repeated generation; the caller reconciles).
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            status, body = self._call(
                'PATCH', '/v1/projects/' + quote(project['id'], safe='') + '/protection-bypass',
                # Documented shape: ``generate`` is an OBJECT; an empty one
                # asks Vercel to create a random server-side secret.
                {'generate': {}},
            )
        except Exception:
            # Transport ambiguity: generation may or may not have happened.
            # Never blind-repeat -- the caller reconciles/reuses. Provider
            # exception text is never surfaced (it can embed tokens).
            return _fail('AMBIGUOUS_BYPASS_PROVISION')
        if status in (400, 422):
            # Deterministic request-validation rejection: the request itself
            # is invalid. This is NOT ambiguous -- a retry cannot help.
            return _fail('BYPASS_PROVISION_REJECTED')
        if status in (401, 403):
            return _fail('BYPASS_PROVISION_FORBIDDEN')
        if status in (404, 405, 501):
            # Documented: the endpoint/project does not exist or the method
            # is unsupported. Explicit, sanitized, deterministic -- never
            # ambiguous.
            return _fail('BYPASS_PROVISION_UNSUPPORTED')
        if status not in (200, 201):
            # 429 / 5xx / any other non-deterministic status -> ambiguous.
            return _fail('AMBIGUOUS_BYPASS_PROVISION')
        secret = self._parse_bypass_secret(body)
        if not secret:
            # Malformed/ambiguous response: can't prove we have a usable
            # secret -> fail closed, never guess (no loose recursive scan).
            return _fail('AMBIGUOUS_BYPASS_PROVISION')
        # NOTE: the secret is deliberately NOT placed in a logged code, and
        # error strings here never embed it.
        return OperationResult.ok({
            'project_id': project['id'],
            'secret': secret,
        })

    # ------------------------------------------------------------------
    # PROJECT_IDENTITY_MISMATCH / read-only recovery
    # ------------------------------------------------------------------

    def read_protection_bypass(self, app_id, project, *, expected_name=None):
        """Read-ONLY recovery: fetch the owned project and classify its
        existing automation-bypass entry WITHOUT issuing any PATCH.

        ``GET /v9/projects/{idOrName}`` returns the LIVE ``protectionBypass``
        map whose KEY is the secret (see ``_bypass_entries``). This exists so
        a run whose local secure store is empty (e.g. the response parse
        failed AFTER Vercel already created the bypass) can RECONCILE the
        existing remote secret instead of blindly rotating it.

        Outcomes:
          * exactly ONE valid bypass entry -> ok({'secret': <key>}).
          * explicit null/empty ``protectionBypass`` -> ok({}) (no secret
            exists yet; the caller may generate one exactly once).
          * more than one entry, or a malformed body/shape -> fail closed
            ``BYPASS_RECONCILIATION_REQUIRED`` (never guess, never rotate).
          * unreadable provider response -> ``AMBIGUOUS_BYPASS_PROVISION``.
          * identity mismatch -> ``PROJECT_IDENTITY_MISMATCH``.

        The secret is returned in ``data`` only; it is never logged or
        embedded in an error string.
        """
        try:
            if not self._project_valid(project, app_id, expected_name=expected_name):
                return _fail('PROJECT_IDENTITY_MISMATCH')
            name = expected_name or self.project_name_for(app_id)
            status, body = self._call('GET', '/v9/projects/' + quote(name, safe=''))
            if status == 404:
                return _fail('PROJECT_RECONCILIATION_REQUIRED')
            if status != 200:
                return _fail('AMBIGUOUS_BYPASS_PROVISION')
            if not self._project_valid(body, app_id, expected_name=expected_name):
                # A READ response without the ownership marker is a malformed
                # identity response, NOT proof that we do not own the project.
                return _fail('PROJECT_RECONCILIATION_REQUIRED')
        except Exception:
            return _fail('AMBIGUOUS_BYPASS_PROVISION')
        kind, secret = self._bypass_entries(body)
        if kind == 'secret':
            return OperationResult.ok({'project_id': project['id'], 'secret': secret})
        if kind == 'none':
            return OperationResult.ok({})
        # 'multiple' or 'malformed' -> fail closed; never guess the entry that
        # belongs to this app, never rotate to "fix" an unknown remote state.
        return _fail('BYPASS_RECONCILIATION_REQUIRED')

    def reconcile_protection_bypass(self, app_id, project, *, expected_name=None):
        result = self.read_protection_bypass(
            app_id, project, expected_name=expected_name
        )
        if not result.success:
            return result
        secret = (result.data or {}).get('secret')
        if secret:
            return OperationResult.ok({'exists': True, 'secret': secret})
        return OperationResult.ok({'exists': False})

    @staticmethod
    def _valid_bypass_secret(value):
        """Conservative validation of a candidate bypass secret.

        Vercel documents the automation bypass secret as exactly 32
        alphanumeric characters (``^[a-zA-Z0-9]{32}$``). Anything else is NOT
        accepted as a secret we would report/persist. The value is never
        logged or embedded in an error.
        """
        return isinstance(value, str) and _BYPASS_SECRET_RE.fullmatch(value) is not None

    @classmethod
    def _bypass_entries(cls, body):
        """Classify a protection-bypass response body WITHOUT extracting.

        Returns ``(kind, value)`` where ``kind`` is one of:
          * ``'secret'``   — ``value`` is ONE valid secret (list below);
          * ``'none'``     — a PROVEN absence: an explicit null/empty
                             ``protectionBypass`` map, or the key absent from a
                             read response with no top-level secret;
          * ``'multiple'`` — more than one ``protectionBypass`` entry: a
                             pre-existing bypass we did NOT rotate -- the
                             caller must fail closed and never guess which
                             entry belongs to this app;
          * ``'malformed'``— anything else (wrong types, invalid key, value
                             that is not a well-formed metadata object).

        Accepted ONE-secret shapes (all require EXACTLY ONE entry):
          1. LIVE map-key shape (authoritative evidence):
             ``{"protectionBypass": {"<secret>": {"createdAt": ...,
             "createdBy": ..., "isEnvVar": ..., "scope": ...}}}`` — the map
             KEY is the secret (validated against ``^[a-zA-Z0-9]{32}$``) and
             the value is a well-formed metadata object.
          2. Documented per-actor shape:
             ``{"protectionBypass": {"<createdBy>": {..., "secret": "<str>"}}}``
             — the record carries a non-empty secret string.
          3. ``{"secret": "<str>"}`` — the generated secret returned directly
             as a top-level string field.

        There is NO recursive scan of arbitrary fields.
        """
        if not isinstance(body, dict):
            return 'malformed', None
        if cls._valid_bypass_secret(body.get('secret')):
            # Documented generation shape: the top-level ``secret`` string.
            return 'secret', body['secret']
        if 'protectionBypass' not in body:
            # No map at all and no top-level secret. A provider READ body
            # simply omits an empty map -> a PROVEN absence; a response that
            # is not a project body at all is caught upstream by the
            # ownership check.
            return 'none', None
        bypass = body['protectionBypass']
        if bypass is None or (isinstance(bypass, dict) and not bypass):
            # Explicit null/empty map: PROVEN absence of a remote bypass.
            return 'none', None
        if not isinstance(bypass, dict):
            return 'malformed', None
        if len(bypass) != 1:
            # More than one entry -> never mine a pre-existing bypass.
            return 'multiple', None
        key, value = next(iter(bypass.items()))
        if isinstance(value, dict):
            # Shape 1: the key IS the secret and the value is metadata.
            if cls._valid_bypass_secret(key) and cls._bypass_metadata_well_formed(value):
                return 'secret', key
            # Shape 2: the record carries the secret itself.
            record_secret = value.get('secret')
            if isinstance(record_secret, str) and record_secret:
                return 'secret', record_secret
        return 'malformed', None

    @staticmethod
    def _bypass_metadata_well_formed(record):
        """A bypass metadata object must look like a real per-entry record.

        LIVE evidence keys: ``createdAt``, ``createdBy``, ``isEnvVar``,
        ``scope``. Require at least one recognised field with the right type;
        anything else (an empty/opaque value) is malformed, never mined.
        """
        checks = (
            ('createdAt', (int, float)),
            ('createdBy', str),
            ('isEnvVar', bool),
            ('scope', str),
        )
        for field, types in checks:
            if field in record:
                value = record[field]
                if isinstance(value, bool) and types is not bool:
                    continue
                if types is bool and isinstance(value, bool):
                    return True
                if types is not bool and isinstance(value, types):
                    return True
        return False

    @classmethod
    def _parse_bypass_secret(cls, body):
        """Extract the ONE generated secret, or None (caller fails closed)."""
        kind, secret = cls._bypass_entries(body)
        return secret if kind == 'secret' else None

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
          * production bound to our bootstrap, deployment still BUILDING/
            QUEUED/INITIALIZING/other transient state -> keep polling within
            the bound (never a premature terminal failure).
          * production bound to our bootstrap, deployment ERROR/CANCELED ->
            terminal failure (BOOTSTRAP_DEPLOYMENT_FAILED), never a second
            bootstrap POST.
          * confirmation never reaches READY within the bounded poll -> fail
            closed (BOOTSTRAP_CONFIRMATION_TIMEOUT); real content must NOT be
            deployed.
          * production bound to a DIFFERENT deployment, or anything
            ambiguous/malformed -> fail closed, never guess, never a second
            bootstrap POST.
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
            # actually equals this bootstrap deployment AND that deployment
            # is READY.
            #
            # State classification (do NOT treat every not-yet-READY poll as
            # terminal): once production is bound to OUR bootstrap id we must
            # distinguish a NORMAL transient provider state (buildingAt set,
            # promotion still settling) from a TERMINAL provider failure.
            #   * production id != ours and not None -> fail closed (moved).
            #   * production id is None              -> keep polling (bounded).
            #   * production id == ours:
            #       READY                       -> success.
            #       BUILDING/QUEUED/INITIALIZING/... -> keep polling (bounded).
            #       ERROR/CANCELED              -> terminal, fail closed.
            #       malformed/wrong identity    -> fail closed.
            for _ in range(max_polls):
                try:
                    prod_id = self._current_production_id(project)
                except Exception:
                    return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')
                if prod_id is not None and prod_id != identifier:
                    # Production binding points somewhere else entirely --
                    # never assume our bootstrap will still land; fail closed
                    # rather than keep polling against a moving target.
                    return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')
                if prod_id == identifier:
                    status, dep = self._call(
                        'GET', '/v13/deployments/' + quote(identifier, safe=''))
                    # Malformed / wrong-identity response is never evidence of
                    # success OR of a terminal failure -- fail closed now.
                    if (status != 200 or not isinstance(dep, dict)
                            or dep.get('id') != identifier):
                        return _fail('BOOTSTRAP_RECONCILIATION_REQUIRED')
                    state = dep.get('readyState')
                    if state == 'READY':
                        return OperationResult.ok({'bootstrapped': True,
                                                   'deployment_id': identifier,
                                                   'reconciled': reconciled,
                                                   'confirmed': True})
                    if state in _BOOTSTRAP_TERMINAL_STATES:
                        # Definitively failed/canceled -- never retry by
                        # creating a second bootstrap deployment.
                        return _fail('BOOTSTRAP_DEPLOYMENT_FAILED')
                    # Anything else is a transient (or unknown) provider state:
                    # keep polling within the same bounded loop. Unknown state
                    # is NEVER treated as success.
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

    def _authoritative_deployment_meta(self, identifier, project):
        """Re-read ONE deployment's stored metadata from the authoritative
        per-deployment endpoint, or return ``None``.

        The list endpoint (``/v6/deployments``) does not always carry a
        deployment's ``meta`` block. Absent metadata must not be read as "this
        deployment has no identity" — that turned a perfectly identifiable
        production deployment into an unresolvable one. This re-read is
        read-only, revalidates the same three identity fields
        ``reconcile_production_deployment`` revalidates (id, project, team),
        and returns ``None`` on ANY doubt so the caller keeps failing closed.
        """
        status, body = self._call('GET', '/v13/deployments/' + quote(identifier, safe=''))
        if status != 200 or not isinstance(body, dict) or body.get('id') != identifier:
            return None
        team = body.get('teamId') or (body.get('team') or {}).get('id')
        project_id_field = body.get('projectId') or (body.get('project') or {}).get('id')
        if project_id_field != project['id'] or team != self.team_id:
            return None
        meta = body.get('meta')
        return meta if isinstance(meta, dict) else None

    def _is_proven_bootstrap(self, meta, app_id, identifier, project):
        """True only when provider state PROVES this deployment is this
        project's own Hermes bootstrap placeholder.

        The bootstrap is created by ``ensure_bootstrap`` with a deterministic,
        app-scoped marker pair: ``wbOwner`` (the immutable ownership marker for
        this ``app_id``) and ``wbBootstrap`` (a dedicated operation id derived
        from the same namespace + app_id, in its own namespace so it can never
        collide with a real content operation). A real user-content deployment
        never carries ``wbBootstrap`` and always carries a complete
        ``wbOperation``/``wbRevision``/``wbArtifact`` identity.

        Three independent conditions must all hold, so "metadata is missing" is
        never enough on its own:
          * the app-scoped owner marker matches,
          * the bootstrap operation id matches AND no content identity is
            present (a deployment that has both is ambiguous, not a
            placeholder),
          * the project's CURRENT production binding actually points at this
            deployment (read from the project, never from the deployment body).

        Returns the proof dict, or ``None`` when it is not proven.
        """
        if not isinstance(meta, dict):
            return None
        if meta.get('wbOwner') != self._marker(app_id):
            return None
        if meta.get('wbBootstrap') != self._bootstrap_operation_id(app_id):
            return None
        if meta.get('wbOperation') or meta.get('wbRevision') or meta.get('wbArtifact'):
            # Carries a content identity as well: not a pure placeholder.
            return None
        try:
            if self._current_production_id(project) != identifier:
                return None
        except Exception:
            return None
        return {
            'deployment_id': identifier,
            'bootstrap_operation_id': meta['wbBootstrap'],
        }

    def find_production_deployment(self, app_id, project, *, expected_name=None):
        """Read-only lookup and CLASSIFICATION of the CURRENT production
        deployment, if any.

        Used to capture last-known-good identity before promoting a new
        deployment, so a failed post-promotion smoke check can roll back.

        Returns one of three shapes, all proven exclusively from provider
        state:

          * ``{'deployment_id': None}`` — there is no production deployment.
          * the full trusted identity of a REAL production deployment
            (deployment_id plus the ``wbOperation``/``wbRevision``/
            ``wbArtifact`` metadata it was created with). A rollback must
            re-promote that deployment using ITS OWN identity (the same meta
            the promote path validates); deriving identity from the current
            promotion operation instead is a guaranteed meta mismatch.
          * the same, plus ``bootstrap_proof`` — a POSITIVE proof that the
            current production deployment is this project's own Hermes
            bootstrap placeholder, not a real user site (see
            ``_is_proven_bootstrap``). It carries no content identity, so it
            is never a meaningful user rollback target.

        When the current production deployment cannot be identified
        unambiguously, or its stored identity is incomplete, this fails closed
        with ``INCOMPLETE_LOOKUP`` rather than returning a partial identity a
        caller might guess around. "Metadata is missing" is not a reason to
        guess: the metadata is re-read from the authoritative per-deployment
        endpoint first.
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
            # rollback target -- so re-read it authoritatively before
            # concluding anything, then fail closed if it is still absent.
            meta = item.get('meta')
            if not self._content_identity_complete(meta):
                meta = self._authoritative_deployment_meta(identifier, project)
            bootstrap_proof = self._is_proven_bootstrap(meta, app_id, identifier, project)
            if not bootstrap_proof and not self._content_identity_complete(meta):
                return _fail('INCOMPLETE_LOOKUP')
            # A proven bootstrap has no content identity by construction, so its
            # revision is not a number at all. Never coerce a missing revision.
            revision_raw = meta.get('wbRevision')
            source_revision = int(revision_raw) if isinstance(revision_raw, str) else None
            result = {
                'deployment_id': identifier,
                'operation_id': meta.get('wbOperation'),
                'source_revision': source_revision,
                'artifact_sha256': meta.get('wbArtifact'),
            }
            if bootstrap_proof:
                # Positive bootstrap identification. The content identity
                # fields are absent by construction, so they stay None and the
                # caller can tell "no rollback target" from "unknown target".
                result['bootstrap_proof'] = bootstrap_proof
            return OperationResult.ok(result)
        except Exception:
            return _fail('INCOMPLETE_LOOKUP')

    @staticmethod
    def _content_identity_complete(meta) -> bool:
        """True when ``meta`` carries the complete, re-promotable content
        identity (wbOperation + wbRevision >= 1 + a well-formed wbArtifact).

        Anything less is NOT proof of absence -- the caller re-reads the
        authoritative metadata and, failing that, fails closed.
        """
        if not isinstance(meta, dict):
            return False
        operation_id = meta.get('wbOperation')
        revision_raw = meta.get('wbRevision')
        artifact_sha256 = meta.get('wbArtifact')
        return bool(
            isinstance(operation_id, str) and operation_id
            and isinstance(artifact_sha256, str)
            and re.fullmatch('[a-f0-9]{64}', artifact_sha256)
            and isinstance(revision_raw, str) and revision_raw.isdigit()
            and int(revision_raw) >= 1
        )

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


def _no_smuggling_chars(url):
    """Reject control/space/backslash characters anywhere in a URL.

    Defense-in-depth against header/URL smuggling: Playwright/Python already
    normalize the obvious cases, but a redirect Location is provider-sourced
    and must never be trusted to be free of them.
    """
    return not any(c.isspace() or c == '\\' or ord(c) < 0x20 or ord(c) == 0x7f
                   for c in url)


def _is_global_address(address):
    """True only for a globally routable, public unicast address.

    Returns False for private/loopback/link-local/reserved/multicast or any
    value that is not a parsable IP literal -- i.e. fail closed.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (ip.is_global and not ip.is_private and not ip.is_loopback
            and not ip.is_link_local and not ip.is_reserved
            and not ip.is_multicast and not ip.is_unspecified)


def _is_same_origin_preview_redirect(source_url, target_url, *, dest_resolver,
                                     max_redirects):
    """True when *target_url* is a BOUNDED SAME-ORIGIN redirect from *source_url*.

    The protected Vercel preview 307s its canonical root
    (``https://x.vercel.app`` -> ``https://x.vercel.app/``); that safe,
    same-origin canonical hop must NOT be reported as a navigation failure.
    Everything else stays blocked:

      * scheme MUST remain ``https`` (no http downgrade),
      * hostname MUST be byte-identical (no cross-origin hop),
      * port MUST stay equivalent / default :443,
      * no username/password (no credential-bearing URLs),
      * no fragment-based trickery (fragments only ever live client-side),
      * both URLs free of whitespace/backslash/control characters,
      * destination DNS MUST resolve exclusively to global/public IPs
        (private/link-local/loopback/reserved fail closed),
      * redirect depth stays within ``max_redirects``.

    This deliberately does NOT loosen the general cross-origin asset policy:
    it only sanctions canonical, same-origin, method-preserving navigation.
    """
    try:
        source = urlsplit(source_url)
        target = urlsplit(target_url)
    except ValueError:
        return False
    if not _no_smuggling_chars(target_url):
        return False
    # Scheme stays https (block downgrade off https) and host is identical.
    if target.scheme != 'https' or source.scheme != 'https':
        return False
    source_host = source.hostname or ''
    target_host = target.hostname or ''
    if not target_host or target_host != source_host:
        return False
    # Port must remain equivalent / default 443 on both sides.
    try:
        source_port = source.port
        target_port = target.port
    except ValueError:
        return False
    if source_port not in (None, 443) or target_port not in (None, 443):
        return False
    # No credential-bearing URL, no fragment trickery.
    if target.username or target.password or target.fragment:
        return False
    # Redirect depth stays within the small explicit bound.
    depth = getattr(dest_resolver, 'count', 0)
    if depth >= max_redirects:
        return False
    # Destination DNS must resolve ONLY to global/public IPs.
    try:
        addresses = dest_resolver(target_host)
    except Exception:
        return False
    if not addresses:
        return False
    return all(_is_global_address(a) for a in addresses)


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


# ---------------------------------------------------------------------------
# Sanitized smoke-failure diagnostics
# ---------------------------------------------------------------------------
#
# Every smoke failure is recorded as a bounded, STRUCTURED record so an
# operator (or a persisting caller) can act on it -- which viewport failed,
# what KIND of resource, the host, the path -- WITHOUT ever persisting or
# logging query strings, credentials, headers, or the Vercel bypass secret.
#
# Failure categories (stable, sanitized):
#   blocked_request       -- the route policy aborted a request (see subtype)
#   navigation_redirect   -- final URL was not the requested document
#   http_error            -- a same-origin response returned status >= 400
#   console_error         -- page console reported an error
#   page_error            -- an uncaught page/runtime exception
#   request_failed        -- a request failed at the network layer
#   broken_image          -- page health check failed (empty body / broken img)
#   networkidle_timeout   -- page.goto(..., wait_until='networkidle') timed out
#   browser_exception     -- the browser/driver raised
#   auth_wall             -- Vercel Deployment Protection login detected
#   missing_screenshot    -- a valid PNG screenshot was not produced
#
# Classification (what the caller should DO):
#   artifact_defect  -- deterministic; the generated artifact references an
#                       external/off-origin or otherwise unfetchable resource.
#                       Repairable by a new revision that removes that
#                       dependency; retrying the SAME bytes cannot fix it.
#   policy_failure   -- a security-policy rule fired (cross-origin, downgrade,
#                       private target, non-GET/HEAD). Also deterministic.
#   transient        -- infrastructure (timeout, networkidle, driver) that a
#                       bounded retry of the SAME bytes could plausibly clear.
#   ambiguous        -- outcome not conclusively classified.
_SMOKE_CATEGORY_CLASSIFICATION = {
    'blocked_request': 'artifact_defect',
    'navigation_redirect': 'artifact_defect',
    'http_error': 'artifact_defect',
    'console_error': 'artifact_defect',
    'page_error': 'artifact_defect',
    'request_failed': 'ambiguous',
    'broken_image': 'artifact_defect',
    'networkidle_timeout': 'transient',
    'browser_exception': 'ambiguous',
    'auth_wall': 'policy_failure',
    'missing_screenshot': 'transient',
    'invalid_screenshot': 'transient',
}

# Bounded so a pathological page cannot persist an unbounded failure list.
_MAX_SMOKE_FAILURE_RECORDS = 24
_MAX_SMOKE_FIELD_LEN = 253


def _sanitize_smoke_url(url):
    """Split an absolute request URL into ``(host, path)`` with NO query,
    credentials, fragment, or scheme. Returns ``('', '')`` for anything that is
    not a well-formed absolute http(s) URL (never persist garbage as a path).

    The path is included (bounded) because it is the single most useful
    actionable datum ("which asset 404'd / was blocked"), and it never carries
    query-string credentials. No Host header, no full URL, ever.
    """
    if not isinstance(url, str) or not url:
        return '', ''
    try:
        parts = urlsplit(url)
    except ValueError:
        return '', ''
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return '', ''
    host = (parts.hostname or '')[: _MAX_SMOKE_FIELD_LEN]
    path = (parts.path or '')[: _MAX_SMOKE_FIELD_LEN]
    return host, path


def _sanitize_resource_type(request):
    """Best-effort resource type WITHOUT any value from the page.

    Playwright exposes ``request.resource_type``; fall back to the method when
    a test double omits it. Never derived from a query string or header.
    """
    value = getattr(request, 'resource_type', None)
    if isinstance(value, str) and value:
        return value[:32]
    return ''


def _smoke_failure_record(category, *, viewport, request=None, host='', path='',
                          status=None, detail='', reason=None, classification=None,
                          caused_by=None, primary=False, viewport_size=None):
    """Build one bounded, URL/secret-safe structured smoke failure record."""
    method = ''
    resource_type = ''
    if request is not None:
        method_value = getattr(request, 'method', None)
        if isinstance(method_value, str) and method_value:
            method = method_value[:32]
        resource_type = _sanitize_resource_type(request)
        if not host:
            host, path = _sanitize_smoke_url(getattr(request, 'url', None))
    record = {
        'category': str(category or 'unknown')[:64],
        'classification': str(
            classification
            or _SMOKE_CATEGORY_CLASSIFICATION.get(str(category or ''), 'ambiguous')
        )[:32],
        'viewport': str(viewport or 'unknown')[:32],
    }
    if resource_type:
        record['resource_type'] = resource_type
    if method:
        record['method'] = method
    if host:
        record['host'] = str(host).lower()[:_MAX_SMOKE_FIELD_LEN]
    if path:
        record['path'] = str(path)[:_MAX_SMOKE_FIELD_LEN]
    if status is not None:
        try:
            record['status'] = int(status)
        except (TypeError, ValueError):
            pass
    detail_text = str(detail or '')[:120]
    record['reason'] = str(reason or detail_text or category or 'unknown')[:120]
    if detail_text:
        record['detail'] = detail_text
    if caused_by:
        record['caused_by'] = str(caused_by)[:96]
    if primary:
        record['primary'] = True
    if viewport_size:
        try:
            if isinstance(viewport_size, dict):
                width = viewport_size.get('width')
                height = viewport_size.get('height')
            else:
                width, height = viewport_size
            record['viewport_size'] = {
                'width': int(width), 'height': int(height),
            }
        except (TypeError, ValueError, IndexError, KeyError):
            pass
    return record


def _request_failure_reason(request):
    value = getattr(request, 'failure', None)
    text = str(value or '').lower()
    if 'timed out' in text or 'timeout' in text:
        return 'timeout'
    if 'reset' in text:
        return 'connection_reset'
    if 'refused' in text:
        return 'connection_refused'
    if 'name' in text or 'resolve' in text or 'dns' in text:
        return 'dns_failure'
    if 'abort' in text or 'blocked' in text:
        return 'blocked_or_aborted'
    return 'request_failed'


def _smoke_classification(records):
    """Roll retained primary records up to one action-oriented class."""
    active = [r for r in records if not r.get('caused_by')
              and r.get('classification') != 'secondary']
    if not active:
        return None
    classes = {r.get('classification', 'ambiguous') for r in active}
    if 'policy_failure' in classes:
        return 'policy_failure'
    if 'artifact_defect' in classes:
        return 'artifact_defect'
    if classes == {'transient'}:
        return 'transient'
    return 'ambiguous'


def _smoke_failure_summary(records):
    """One bounded, human-readable line per record, secret-free.

    Example: ``desktop blocked_request external:fonts.googleapis.com/css2``.
    The PATH is included (no query); the host+path is the actionable part.
    """
    lines = []
    for record in records[: _MAX_SMOKE_FAILURE_RECORDS]:
        parts = [record.get('viewport', '?'), record.get('category', 'failure')]
        target = record.get('host', '')
        if record.get('path'):
            target = target + record['path'] if target else record['path']
        if record.get('method') or record.get('resource_type'):
            request_shape = '/'.join(
                part for part in (record.get('method', ''), record.get('resource_type', ''))
                if part
            )
            if request_shape:
                parts.append(request_shape)
        if record.get('reason'):
            parts.append(str(record['reason']))
        if record.get('status') is not None:
            parts.append(f"status={record['status']}")
        if target:
            parts.append(target)
        lines.append(': '.join(parts[:2]) + (' ' + ' '.join(parts[2:]) if parts[2:] else ''))
    return lines


class PreviewSmokeTester:
    """Fresh anonymous browser contexts; same-origin GET/HEAD requests only.

    Bounded SAME-ORIGIN redirects are followed: the protected Vercel preview
    canonically 307s ``https://x.vercel.app`` -> ``https://x.vercel.app/``, and
    that safe hop is equivalent to the original request. All other redirects
    stay blocked before following -- cross-origin hops, http downgrades,
    private/link-local/loopback destinations, credential-bearing URLs,
    non-GET/HEAD methods, fragments, and redirect loops / excessive depth.
    Websockets, workers, cross-origin and private DNS requests are blocked.
    Strict policy intentionally fails externally hosted assets; caller must
    self-host assets. Factory returns a Playwright-style Browser. Requires
    route_web_socket support, otherwise fails closed.

    The general cross-origin ASSET policy is intentionally NOT loosened.

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
    # Small explicit bound on same-origin redirect hops per navigation.
    max_redirects = 5

    def __init__(self, browser_factory, resolver=None, *, custom_hostname=None):
        if custom_hostname is not None and not valid_custom_hostname(custom_hostname):
            raise ValueError('INVALID_HOSTNAME')
        self.custom_hostname = custom_hostname
        self.factory = browser_factory
        self.resolver = resolver or (lambda h: [a[4][0] for a in socket.getaddrinfo(h, 443)])

    @staticmethod
    def _is_vercel_preview_origin(url):
        """True when ``url`` is an ``https://*.vercel.app`` origin that may be
        behind Vercel Deployment Protection. The bypass credential is scoped
        to exactly these origins -- never sent to unrelated hosts."""
        try:
            parts = urlsplit(url)
            host = parts.hostname or ''
        except ValueError:
            return False
        return (
            parts.scheme == 'https'
            and (host == 'vercel.app' or host.endswith('.vercel.app'))
        )

    @staticmethod
    def _bypass_headers(bypass_secret, url):
        """Build the protection-bypass headers for a protected Vercel preview.

        Returns {} when there is no secret or the URL is not a Vercel preview
        origin, so the credential is NEVER leaked to arbitrary asset hosts or
        non-Vercel domains.
        """
        if not bypass_secret:
            return {}
        if not PreviewSmokeTester._is_vercel_preview_origin(url):
            return {}
        return {
            'x-vercel-protection-bypass': bypass_secret,
            'x-vercel-set-bypass-cookie': 'true',
        }

    @staticmethod
    def _looks_like_vercel_auth_wall(url, title):
        """Detect a Vercel Deployment Protection auth wall.

        HTTP 200 is NOT enough: a protected preview redirects to Vercel's SSO
        login. Detect at minimum a /login final path, an /api/sso redirect, or
        the Vercel login page title/markers.
        """
        try:
            path = urlsplit(url).path or ''
        except ValueError:
            path = ''
        if path.rstrip('/').endswith('/login') or '/api/sso' in path:
            return True
        lowered = (title or '').lower()
        if 'login' in lowered and 'vercel' in lowered:
            return True
        return False

    @staticmethod
    def _canonical(url):
        """Normalize a same-origin URL for trailing-slash equivalence.

        ``https://x.vercel.app`` and ``https://x.vercel.app/`` denote the same
        document; the protected preview's 307 to the canonical root slash must
        not be read as "navigated somewhere else". Everything else (lowercased
        scheme/host, :443 elision) is preserved so the comparison can never
        erase a real cross-origin move. Query strings are significant.
        """
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme not in ('https', '') or parts.username or parts.password:
            return None
        try:
            port = parts.port
        except ValueError:
            return None
        if port not in (None, 443):
            return None
        path = parts.path or '/'
        if path != '/' and path.endswith('/'):
            path = path.rstrip('/')
        return (parts.hostname or '', path, parts.query)

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

    def run(self, url, out_dir, *, bypass_secret=None):
        if not self._allowed_origin(url):
            return _fail('INVALID_PREVIEW_URL')
        # PHASE F: the project-specific Vercel automation-bypass secret is
        # injected per run (dependency injection / runtime composition). It is
        # scoped to the protected *.vercel.app preview origin only and is never
        # sent to non-Vercel domains or arbitrary asset hosts.
        headers = self._bypass_headers(bypass_secret, url)
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
            return _run_on_dedicated_thread(
                lambda: self._run_sync(url, out_dir, headers=headers)
            )
        return self._run_sync(url, out_dir, headers=headers)

    def _run_sync(self, url, out_dir, headers=None):
        failures: List[str] = []
        shots: Dict[str, str] = {}
        records: List[dict] = []
        primary_failure = None
        observed_classes = set()
        blocked_by_key: Dict[tuple, dict] = {}
        record_counter = 0
        auth_wall = False
        headers = dict(headers or {})
        origin = urlsplit(url).netloc

        def _viewport_size(label):
            return {'width': 1440, 'height': 900} if label == 'desktop' else {
                'width': 390, 'height': 844,
            }

        def _request_key(label, request=None, host='', path=''):
            method = str(getattr(request, 'method', '') or '') if request is not None else ''
            if not host:
                host, path = _sanitize_smoke_url(str(getattr(request, 'url', '') or '')) \
                    if request is not None else ('', '')
            return (label, method, str(host or '').lower(), str(path or ''))

        def _record(category, label, *, request=None, host='', path='',
                    status=None, detail='', reason=None, classification=None,
                    caused_by=None, legacy=None, primary=True):
            nonlocal record_counter, primary_failure
            record_counter += 1
            record = _smoke_failure_record(
                category,
                viewport=label,
                request=request,
                host=host,
                path=path,
                status=status,
                detail=detail,
                reason=reason,
                classification=classification,
                caused_by=caused_by,
                primary=primary and not caused_by,
                viewport_size=_viewport_size(label),
            )
            record['id'] = f'smoke-{record_counter}'
            active_class = record.get('classification')
            if not caused_by and active_class != 'secondary':
                observed_classes.add(active_class)
                priority = {
                    'policy_failure': 4,
                    'artifact_defect': 3,
                    'transient': 2,
                    'ambiguous': 1,
                }.get(active_class, 0)
                if primary_failure is None or priority > primary_failure[0]:
                    primary_failure = (priority, record)
            if len(records) < _MAX_SMOKE_FAILURE_RECORDS:
                records.append(record)
            if len(failures) < _MAX_SMOKE_FAILURE_RECORDS:
                failures.append(legacy or str(category).replace('_', ' '))
            return record

        def _mark_secondary(record, cause):
            nonlocal primary_failure
            if not record or not cause:
                return
            previous_class = record.get('classification')
            if previous_class != cause.get('classification'):
                observed_classes.discard(previous_class)
            record['caused_by'] = cause.get('id')
            record['classification'] = 'secondary'
            record['primary'] = False
            if primary_failure is not None and primary_failure[1].get('id') == record.get('id'):
                primary_failure = (
                    {'policy_failure': 4, 'artifact_defect': 3,
                     'transient': 2, 'ambiguous': 1}.get(
                         cause.get('classification'), 0),
                    cause,
                )

        def _correlate_console(label, msg):
            text = str(getattr(msg, 'text', '') or '').lower()
            location = getattr(msg, 'location', None) or {}
            location_url = location.get('url') if isinstance(location, dict) else ''
            host, path = _sanitize_smoke_url(location_url or '')
            if not host:
                candidates = [
                    value for key, value in blocked_by_key.items() if key[0] == label
                ]
                cause = candidates[-1] if candidates else None
            else:
                cause = blocked_by_key.get((label, '', host, path))
                if cause is None:
                    candidates = [
                        value for key, value in blocked_by_key.items()
                        if key[0] == label and key[2] == host and key[3] == path
                    ]
                    cause = candidates[-1] if candidates else None
            if cause is not None and (
                not text or 'err_' in text or 'failed' in text or 'resource' in text
            ):
                return cause
            return None

        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            for label in ('desktop', 'mobile'):
                viewport = _viewport_size(label)
                browser = self.factory()
                context = None
                try:
                    context = browser.new_context(
                        viewport=viewport, service_workers='block',
                        accept_downloads=False, ignore_https_errors=False)
                    if headers:
                        context.set_extra_http_headers(headers)
                    redirects = {'count': 0}

                    def route_request(route):
                        request = route.request
                        block_reason = ''
                        try:
                            url_ok = (self._allowed_origin(request.url)
                                      and urlsplit(request.url).netloc == origin
                                      and request.method in ('GET', 'HEAD'))
                            if not url_ok:
                                block_reason = 'off_origin_or_method'
                            redirected = request.redirected_from is not None
                            if redirected:
                                allowed = url_ok and _is_same_origin_preview_redirect(
                                    request.redirected_from.url, request.url,
                                    dest_resolver=self.resolver,
                                    max_redirects=self.max_redirects)
                                if url_ok and not allowed:
                                    block_reason = 'unsafe_redirect'
                                if allowed:
                                    redirects['count'] += 1
                                    if redirects['count'] > self.max_redirects:
                                        allowed = False
                                        block_reason = 'redirect_depth_exceeded'
                            else:
                                allowed = url_ok
                            addresses = (
                                self.resolver(urlsplit(request.url).hostname)
                                if allowed else []
                            )
                            allowed = bool(addresses) and all(
                                _is_global_address(address) for address in addresses)
                            if not allowed and not block_reason:
                                block_reason = 'non_public_destination'
                        except Exception:
                            allowed = False
                            block_reason = 'policy_evaluation_error'
                        if allowed:
                            route.continue_()
                        else:
                            record = _record(
                                'blocked_request', label, request=request,
                                detail=block_reason or 'blocked',
                                reason=block_reason or 'blocked',
                                classification='artifact_defect',
                                legacy='blocked request',
                            )
                            blocked_by_key[
                                _request_key(label, request)
                            ] = record
                            route.abort()

                    context.route('**/*', route_request)

                    def block_socket(ws):
                        _record(
                            'blocked_request', label, detail='websocket',
                            reason='websocket', classification='artifact_defect',
                            legacy='blocked websocket',
                        )
                        ws.close()

                    context.route_web_socket('**/*', block_socket)
                    page = context.new_page()
                    page.on('pageerror', lambda _: _record(
                        'page_error', label, reason='page_error',
                        classification='artifact_defect', legacy='runtime error'))

                    def on_console(msg):
                        if msg.type != 'error':
                            return
                        record = _record(
                            'console_error', label, reason='console_error',
                            classification='artifact_defect', legacy='console error')
                        cause = _correlate_console(label, msg)
                        if cause is not None:
                            _mark_secondary(record, cause)

                    def on_request_failed(req):
                        record = _record(
                            'request_failed', label, request=req,
                            reason=_request_failure_reason(req), classification='ambiguous',
                            legacy='asset/request failure')
                        cause = blocked_by_key.get(_request_key(label, req))
                        if cause is not None:
                            _mark_secondary(record, cause)

                    page.on('console', on_console)
                    page.on('requestfailed', on_request_failed)
                    page.on('response', lambda response: _record(
                        'http_error', label,
                        request=getattr(response, 'request', None),
                        status=response.status,
                        reason='http_status',
                        classification=(
                            'policy_failure' if response.status in (401, 403)
                            else 'transient' if response.status == 429 or response.status >= 500
                            else 'artifact_defect'
                        ),
                        legacy='HTTP asset failure')
                        if response.status >= 400 else None)
                    try:
                        response = page.goto(url, wait_until='networkidle', timeout=30000)
                    except Exception as exc:
                        exception_name = type(exc).__name__
                        if 'timeout' in exception_name.lower():
                            _record(
                                'networkidle_timeout', label, reason='navigation_timeout',
                                classification='transient', legacy=f'browser smoke failed: {exception_name}',
                            )
                        else:
                            _record(
                                'browser_exception', label, reason='navigation_exception',
                                classification='ambiguous', legacy=f'browser smoke failed: {exception_name}',
                            )
                        raise
                    try:
                        title = page.title()
                    except Exception:
                        title = ''
                    try:
                        final_url = page.url
                    except Exception:
                        final_url = url
                    final_host, final_path = _sanitize_smoke_url(final_url)
                    if self._looks_like_vercel_auth_wall(final_url, title):
                        auth_wall = True
                        auth_record = _record(
                            'auth_wall', label, host=final_host, path=final_path,
                            reason='vercel_auth_wall', classification='policy_failure',
                            legacy='vercel auth wall',
                        )
                        for record in records:
                            if (record.get('viewport') == label
                                    and record.get('category') in {'navigation_redirect', 'http_error'}
                                    and record.get('id') != auth_record.get('id')):
                                _mark_secondary(record, auth_record)
                    if (response is None or response.status != 200
                            or self._canonical(final_url) != self._canonical(url)):
                        _record(
                            'navigation_redirect', label, host=final_host, path=final_path,
                            status=(response.status if response is not None else None),
                            reason='navigation_mismatch', classification='artifact_defect',
                            legacy='navigation failed/redirected',
                        )
                    healthy = page.evaluate('''() => Boolean(document.body &&
                        document.body.innerText.trim().length &&
                        [...document.images].every(i => i.complete && i.naturalWidth > 0))''')
                    if healthy is not True:
                        try:
                            broken_targets = page.evaluate(
                                '''() => [...document.images]
                                    .filter(i => !i.complete || i.naturalWidth <= 0)
                                    .map(i => i.src).slice(0, 5)'''
                            )
                        except Exception:
                            broken_targets = []
                        if isinstance(broken_targets, list) and broken_targets:
                            for target in broken_targets:
                                broken_host, broken_path = _sanitize_smoke_url(str(target))
                                _record(
                                    'broken_image', label, host=broken_host, path=broken_path,
                                    reason='broken_image', classification='artifact_defect',
                                    legacy='empty page or broken images',
                                )
                        else:
                            _record(
                                'broken_image', label, reason='empty_or_broken_image',
                                classification='artifact_defect',
                                legacy='empty page or broken images',
                            )
                    try:
                        png = page.screenshot(full_page=True, type='png')
                    except Exception:
                        png = None
                        _record(
                            'missing_screenshot', label, reason='screenshot_error',
                            classification='transient', legacy='screenshot missing',
                        )
                    if png is not None:
                        if not isinstance(png, bytes) or not png.startswith(b'\x89PNG\r\n\x1a\n'):
                            _record(
                                'invalid_screenshot', label, reason='invalid_png',
                                classification='transient', legacy='invalid screenshot',
                            )
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
                        _stop_browser_playwright(browser)
        except Exception as exc:
            host, path = _sanitize_smoke_url(url)
            logger.error(
                "Preview smoke browser failure type=%s host=%s path=%s",
                type(exc).__name__, host, path,
            )
            if primary_failure is None or primary_failure[0] < 1:
                _record(
                    'browser_exception', 'all', reason=type(exc).__name__,
                    classification='ambiguous', legacy=f'browser smoke failed: {type(exc).__name__}',
                )
        if primary_failure is not None and primary_failure[1] not in records:
            if len(records) >= _MAX_SMOKE_FAILURE_RECORDS:
                records[-1] = primary_failure[1]
            else:
                records.append(primary_failure[1])
        if observed_classes:
            if 'policy_failure' in observed_classes:
                classification = 'policy_failure'
            elif 'artifact_defect' in observed_classes:
                classification = 'artifact_defect'
            elif observed_classes == {'transient'}:
                classification = 'transient'
            else:
                classification = 'ambiguous'
        else:
            classification = None
        target_host, target_path = _sanitize_smoke_url(url)
        return OperationResult(
            success=not failures,
            data={**shots,
                  'target_host': target_host, 'target_path': target_path,
                  'viewport_sizes': {
                      'desktop': _viewport_size('desktop'),
                      'mobile': _viewport_size('mobile'),
                  },
                  'failures': failures,
                  'failure_records': records,
                  'failure_classification': classification,
                  'failure_summary': _smoke_failure_summary(records)},
            error_code=(
                'VERCEL_BYPASS_AUTH_FAILED' if auth_wall
                else ('SMOKE_FAILED' if failures else None)
            ),
        )
