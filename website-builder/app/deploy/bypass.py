"""PHASE E — project-scoped automation-bypass provisioning coordinator.

Wraps the tiny ``BypassSecretStore`` and the ``VercelAdapter``'s
``ensure_protection_bypass`` provider call into the required operating model:

    ensure/create Vercel project
        -> durably bind Vercel project identity
        -> ensure project-specific automation bypass exists
        -> store secret securely outside ProjectState
        -> (deploy preview)
        -> smoke uses bypass
        -> (Telegram preview)

Case semantics:
  CASE A stored secret exists              -> reuse, never regenerate.
  CASE B no stored secret                  -> RECONCILE the remote project
                                              FIRST: if Vercel already holds
                                              exactly one bypass entry (e.g.
                                              a prior generation whose response
                                              parse failed) adopt that MAP KEY
                                              as the secret (source
                                              "reconciled") -- never PATCH.
                                              Only a genuinely absent remote
                                              bypass is generated once.
  CASE C generation transport/ambiguous    -> fail closed (no blind repeated
                                              generation); the caller may
                                              retry the whole operation later.
  CASE D definitive forbidden/unsupported  -> sanitized failure; the caller
                                              must NOT continue assuming smoke
                                              access.
  CASE E an invalid stored secret is proven by a smoke auth failure -> the
         caller may explicitly re-provision; normal retries NEVER rotate.
  CASE F remote holds MULTIPLE bypass entries (or an unreadable/malformed
         shape) -> fail closed BYPASS_RECONCILIATION_REQUIRED without any
         PATCH: never guess which secret belongs to this app, never rotate.

NO ROTATION ON NORMAL RETRY: a retry reuses the same stored secret for the
same Vercel project id. Rotation only happens through an explicit
``reprovision=True`` request (CASE E).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.core.contracts import OperationResult

logger = logging.getLogger(__name__)


@dataclass
class BypassProvisioner:
    """Resolve/provision the project-scoped bypass secret.

    ``vercel`` must expose ``ensure_protection_bypass(app_id, project,
    *, expected_name=None)``. When available it may ALSO expose a read-only
    ``read_protection_bypass(app_id, project, *, expected_name=None)`` used to
    reconcile an already-created remote bypass before ever generating one.
    ``secret_store`` is a ``BypassSecretStore``.
    """

    vercel: Any
    secret_store: Any

    def _read_is_authoritative(self) -> bool:
        """Whether an empty read result is a PROVEN "no remote bypass".

        A recoverable-scan surface (``reconcile_protection_bypass``) returns
        an explicit ``{'exists': False}`` for a proven absence and fails closed
        for anything unreadable/ambiguous, so empty data is trustworthy. The
        adapter's ``read_protection_bypass`` cannot distinguish "the key is
        absent" from "the provider response omitted it", so it is NOT
        authoritative: fail closed rather than risk rotating an existing
        secret. Defaults to False (fail closed) for any unknown collaborator.
        """
        return callable(getattr(self.vercel, 'reconcile_protection_bypass', None))

    def _reconcile_remote(self, app_id, project, expected_name):
        """Read the owned project's CURRENT remote bypass without generating.

        Returns an ``OperationResult`` (ok with the reconciled secret, ok with
        an empty ``data`` meaning "no remote bypass exists", or a sanitized
        fail-closed failure), or ``None`` when the read-only surface is not
        available on this adapter.
        """
        reader = getattr(self.vercel, 'reconcile_protection_bypass', None)
        if reader is None:
            return None

        try:
            return reader(app_id, project, expected_name=expected_name)
        except Exception:
            # A scan that cannot complete is NEVER permission to generate
            # (that could duplicate/rotate). Provider exception text is never
            # surfaced -- it can embed tokens.
            logger.warning(
                "Bypass reconciliation read failed; failing closed (redacted)"
            )
            return OperationResult.fail(
                'BYPASS_RECONCILIATION_REQUIRED',
                error_code='BYPASS_RECONCILIATION_REQUIRED',
            )

    def ensure(
        self,
        app_id: str,
        project: dict,
        *,
        expected_name: Optional[str] = None,
        reprovision: bool = False,
    ) -> OperationResult:
        """Return ``OperationResult.ok({'secret': ..., 'source': ...})`` or a
        sanitized failure. Never returns/logs the secret inside an error."""
        project_id = project.get('id') if isinstance(project, dict) else None
        if not project_id:
            return OperationResult.fail(
                'BYPASS_PROVISION_UNAVAILABLE',
                error_code='BYPASS_PROVISION_UNAVAILABLE',
            )

        # CASE A: a stored project-specific secret already exists -> reuse.
        # (Only skipped when an explicit reprovision was requested, CASE E.)
        if not reprovision:
            stored = self.secret_store.get(project_id)
            if stored:
                return OperationResult.ok({'secret': stored, 'source': 'stored'})

        # CASE B/F: no usable local secret. Reconcile the REMOTE state FIRST
        # so a normal retry never blind-PATCHes a project that already has a
        # valid bypass (the real p7 shape: creation succeeded but the response
        # parse failed -> local store empty while the remote key exists).
        if not reprovision:
            remote = self._reconcile_remote(app_id, project, expected_name)
            if remote is not None:
                if not remote.success:
                    return remote
                remote_secret = (remote.data or {}).get('secret')
                if remote_secret:
                    # Adopt the EXISTING remote MAP KEY as the secret: no
                    # rotation, no second bypass. Persist BEFORE returning so
                    # a later run reuses it (CASE A) instead of reconciling.
                    return self._persist(
                        project_id, remote_secret, source='reconciled'
                    )
                if not self._read_is_authoritative():
                    # The read-only surface cannot distinguish "no remote
                    # bypass" from "cannot tell" -- generating could rotate an
                    # existing secret. Fail closed instead of guessing.
                    return OperationResult.fail(
                        'BYPASS_RECONCILIATION_REQUIRED',
                        error_code='BYPASS_RECONCILIATION_REQUIRED',
                    )
                # A proven "no remote bypass exists" is the ONLY state that
                # authorizes a single generation below. (A fail-closed read
                # returned above, so empty data here is absence, not unknown.)

        # CASE B / D / C: generate via the provider.
        result = self.vercel.ensure_protection_bypass(
            app_id, project, expected_name=expected_name
        )
        if not result.success:
            # D (definitive forbidden) and C (ambiguous) are both surfaced
            # verbatim as sanitized codes; the caller decides.
            return result

        secret = result.data.get('secret')
        if not secret:
            return OperationResult.fail(
                'AMBIGUOUS_BYPASS_PROVISION',
                error_code='AMBIGUOUS_BYPASS_PROVISION',
            )
        return self._persist(project_id, secret, source='generated')

    def _persist(self, project_id: str, secret: str, *, source: str) -> OperationResult:
        """Persist a resolved secret before returning OK (no rotation).

        Persisting BEFORE returning means a crash after generation/reconcile
        still reuses the same secret on the next run. A local durability
        failure fails closed so the caller never proceeds with a secret that
        the next retry would rotate.
        """
        try:
            self.secret_store.set(project_id, secret)
        except Exception:
            logger.exception(
                "Failed to persist automation-bypass secret for project (redacted)"
            )
            return OperationResult.fail(
                'BYPASS_SECRET_PERSIST_FAILED',
                error_code='BYPASS_SECRET_PERSIST_FAILED',
            )
        return OperationResult.ok({'secret': secret, 'source': source})

    def resolve(self, project_id: Optional[str]) -> Optional[str]:
        """Read-only resolution used by smoke wiring (stored -> env fallback)."""
        return self.secret_store.resolve(project_id)
