"""Phase 16: explicitly claimed existing domains, manual DNS, no commerce.

The writer stays held through ALL adapter/browser effects. Vercel add is
already a remote binding, not a delayed attachment after smoke. ATTACHED is
only local evidence of verified DNS/ownership plus successful HTTPS smoke.
Ambiguous writes are durable: subsequent invocations reconcile using GET,
never repeat add/verify POST. No API retries, DNS edits, purchases or deploys.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Any

from app.core.authz import AuthzError, require_owner_role
from app.core.contracts import OperationResult
from app.deploy.adapters import valid_custom_hostname, _fail


@dataclass
class DomainDeps:
    vercel: Any
    smoke: Any  # injected run(url, out_dir); PreviewSmokeTester custom_hostname opt-in
    app_id_for: Any = field(default=lambda project_id: project_id)
    # Optional callable(project_id, state) -> Optional[str]. Returns the
    # ALREADY-BOUND friendly Vercel slug from trusted registry state -- never
    # derived fresh here (this flow only runs after a live/approved
    # production, which is downstream of the preview that binds the slug).
    # None disables slug resolution: the legacy opaque hash-derived project
    # name governs, exactly as before.
    slug_for: Any = None


class CustomDomainOrchestrator:
    def __init__(self, runner, store, deps):
        self.runner, self.store, self.deps = runner, store, deps

    def prepare(self, project_id, hostname, workspace, *, ownership_claim=False,
                principal_id=None, reference_token=None):
        """Bind once and return manual DNS guidance; never verify or smoke."""
        return self._run(project_id, hostname, workspace, ownership_claim=ownership_claim,
                         principal_id=principal_id, reference_token=reference_token, mode="prepare")

    def verify(self, project_id, hostname, workspace, *, ownership_claim=False,
               principal_id=None, reference_token=None):
        """Verify an existing prepared binding; never initiate a new add."""
        return self._run(project_id, hostname, workspace, ownership_claim=ownership_claim,
                         principal_id=principal_id, reference_token=reference_token, mode="verify")

    def connect(self, project_id, hostname, workspace, *, ownership_claim=False,
                principal_id=None, reference_token=None):
        """Compatibility path: prepare plus verification when DNS is ready."""
        return self._run(project_id, hostname, workspace, ownership_claim=ownership_claim,
                         principal_id=principal_id, reference_token=reference_token, mode="connect")

    def _run(self, project_id, hostname, workspace, *, ownership_claim,
             principal_id, reference_token, mode):
        with self.store.acquire_writer(project_id) as state:
            try:
                require_owner_role(state, principal_id, reference_token)
            except AuthzError as exc:
                return _fail(exc.error_code)
            if not valid_custom_hostname(hostname):
                return _fail('INVALID_HOSTNAME')
            if ownership_claim is not True:
                return _fail('OWNERSHIP_CLAIM_REQUIRED')
            if state.lifecycle not in ('LIVE', 'PREVIEW_READY'):
                return _fail('DOMAIN_LIFECYCLE_NOT_ALLOWED')
            live = state.deployment.get('last_live_deployment') or {}
            approval = state.deployment.get('approval') or {}
            keys = ('operation_id', 'deployment_id', 'source_revision',
                    'source_sha256', 'artifact_sha256')
            if (any(not live.get(k) or live.get(k) != approval.get(k) for k in keys)
                    or type(live.get('source_revision')) is not int
                    or live['source_revision'] != state.revisions.live_revision
                    or live['source_revision'] != state.revisions.approved_revision
                    or not all(isinstance(live.get(k), str) and
                               re.fullmatch('[a-f0-9]{64}', live[k])
                               for k in ('source_sha256', 'artifact_sha256'))):
                return _fail('APPROVED_PRODUCTION_REQUIRED')
            if mode == 'verify' and not state.domain.connection.get('add_attempted'):
                return _fail('DOMAIN_PREPARE_REQUIRED')
            if mode != 'prepare' and (self.deps.smoke is None or not callable(getattr(self.deps.smoke, 'run', None))):
                return _fail('SMOKE_REQUIRED')
            if not self.runner.acquire_project(project_id):
                return _fail('WORKER_BUSY')
            try:
                return self._connect(state, hostname, Path(workspace), principal_id,
                                     {k: live[k] for k in keys}, mode)
            except Exception:
                return self._error(state, 'DOMAIN_RECONCILIATION_REQUIRED')
            finally:
                self.runner.release_project(project_id)

    def _error(self, state, code):
        domain = state.domain
        if domain.connection:
            domain.last_error = code
            domain.attached_at = None
            domain.connection_stage = ('DNS_PENDING' if code == 'DNS_PENDING' else 'FAILED')
            self.store.save(state)
        return _fail(code)

    def _connect(self, state, hostname, workspace, principal, identity, mode):
        v = self.deps.vercel
        app_id = self.deps.app_id_for(state.project_id)
        slug = None
        if self.deps.slug_for is not None:
            try:
                slug = self.deps.slug_for(state.project_id, state)
            except Exception:
                slug = None
        # Canonical Vercel project name from trusted bound registry state,
        # resolved independently of any provider response and threaded
        # through every owned-project revalidation below (same contract as
        # PreviewOrchestrator/PromotionOrchestrator).
        expected_name = slug if slug else None
        domain = state.domain
        binding = {'hostname': hostname, 'principal_id': principal, 'app_id': app_id,
                   'team_id': v.team_id, 'namespace': v.namespace, **identity}
        if domain.connection and any(domain.connection.get(k) != value for k, value in binding.items()):
            return _fail('DOMAIN_BINDING_MISMATCH')
        result = v.lookup_project(app_id, expected_name=expected_name)
        if not result.success:
            return self._error(state, result.error_code)
        project = result.data['project']
        binding['project_id'] = project['id']
        if domain.connection and domain.connection.get('project_id') != project['id']:
            return self._error(state, 'DOMAIN_BINDING_MISMATCH')
        result = v.check_domain_production(app_id, project, identity, expected_name=expected_name)
        if not result.success:
            return self._error(state, result.error_code)
        if not domain.connection:
            domain.connection = {**binding, 'claimed_at': time.time(),
                                 'add_attempted': False, 'verify_attempted': False}
            domain.connection_stage = 'CLAIMED'
            self.store.save(state)
        intent = domain.connection
        if intent['add_attempted']:
            added = v.get_project_domain(app_id, project, hostname, expected_name=expected_name)
        else:
            intent['add_attempted'] = True
            domain.connection_stage = 'ADDING'
            self.store.save(state)
            added = v.add_domain(app_id, project, hostname, expected_name=expected_name)
        if not added.success:
            return self._error(state, added.error_code)
        intent['remote_bound'] = True
        domain.connection_stage = 'DNS_PENDING'
        domain.attached_at = None
        domain.verified_at = None
        self.store.save(state)
        checked = v.check_domain_production(app_id, project, identity, expected_name=expected_name)
        if not checked.success:
            return self._error(state, checked.error_code)
        config = v.get_domain_config(hostname)
        if not config.success:
            return self._error(state, config.error_code)
        domain.dns_records = list(added.data['verification'])
        for key in ('recommendedIPv4', 'recommendedCNAME'):
            for recommendation in config.data[key]:
                domain.dns_records.append({'recommendation': key, **recommendation})
        intent['dns_config'] = config.data
        intent['manual_dns_instructions'] = (
            'At your DNS provider, apply the Vercel recommendations and ownership '
            'challenges below for this hostname. Preserve unrelated records. '
            'No DNS changes are automated. After propagation, call verify (or connect) '
            'with the same hostname and explicit ownership claim.')
        self.store.save(state)
        if mode == 'prepare':
            domain.last_error = None
            self.store.save(state)
            return OperationResult.ok({'hostname': hostname, 'stage': 'DNS_PENDING',
                                       'dns_records': domain.dns_records,
                                       'instructions': intent['manual_dns_instructions']})
        if config.data['misconfigured'] is not False:
            result = self._error(state, 'DNS_PENDING')
            result.data = {'hostname': hostname, 'dns_records': domain.dns_records,
                           'instructions': intent['manual_dns_instructions']}
            return result
        if added.data['verified'] is not True:
            if intent['verify_attempted']:
                return self._error(state, 'DOMAIN_VERIFY_RECONCILIATION_REQUIRED')
            intent['verify_attempted'] = True
            domain.connection_stage = 'VERIFYING'
            self.store.save(state)
            verified = v.verify_domain(app_id, project, hostname, expected_name=expected_name)
            if not verified.success or verified.data.get('verified') is not True:
                return self._error(state, 'DOMAIN_VERIFY_RECONCILIATION_REQUIRED')
        # Fresh project-domain/config reads even when verify POST reported success.
        checked = v.check_domain_production(app_id, project, identity, expected_name=expected_name)
        record = v.get_project_domain(app_id, project, hostname, expected_name=expected_name)
        config = v.get_domain_config(hostname)
        if not checked.success:
            return self._error(state, checked.error_code)
        if (not record.success or record.data.get('verified') is not True
                or not config.success or config.data.get('misconfigured') is not False):
            return self._error(state, 'DOMAIN_VERIFICATION_FAILED')
        domain.verified_at = time.time()
        domain.connection_stage = 'VERIFIED'
        self.store.save(state)
        url = 'https://' + hostname
        smoke = self.deps.smoke.run(url, workspace / 'qa' / 'custom_domain_smoke')
        intent['smoke'] = smoke.data
        if not smoke.success:
            return self._error(state, 'SMOKE_FAILED')
        checked = v.check_domain_production(app_id, project, identity, expected_name=expected_name)
        record = v.get_project_domain(app_id, project, hostname, expected_name=expected_name)
        config = v.get_domain_config(hostname)
        if not checked.success:
            return self._error(state, checked.error_code)
        if (not record.success or record.data.get('verified') is not True
                or not config.success or config.data.get('misconfigured') is not False):
            return self._error(state, 'DOMAIN_VERIFICATION_FAILED')
        domain.connection_stage = 'ATTACHED'
        domain.attached_at = time.time()
        domain.last_error = None
        self.store.save(state)
        return OperationResult.ok({'hostname': hostname, 'url': url, 'stage': 'ATTACHED',
                                   'dns_records': domain.dns_records})
