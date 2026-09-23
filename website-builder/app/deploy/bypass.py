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
  CASE B no stored secret                  -> generate once, persist, use.
  CASE C generation transport/ambiguous    -> fail closed (no blind repeated
                                              generation); the caller may
                                              retry the whole operation later.
  CASE D definitive forbidden/unsupported  -> sanitized failure; the caller
                                              must NOT continue assuming smoke
                                              access.
  CASE E an invalid stored secret is proven by a smoke auth failure -> the
         caller may explicitly re-provision; normal retries NEVER rotate.

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
    *, expected_name=None)``. ``secret_store`` is a ``BypassSecretStore``.
    """

    vercel: Any
    secret_store: Any

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
        # Persist BEFORE returning so a crash after generation still reuses
        # the same secret on the next run (no rotation).
        try:
            self.secret_store.set(project_id, secret)
        except Exception:
            logger.exception(
                "Failed to persist automation-bypass secret for project (redacted)"
            )
            # We DID generate a usable secret; not persisting it is a local
            # durability failure. Fail closed so the caller does not proceed
            # with an unpersisted secret that a retry would rotate.
            return OperationResult.fail(
                'BYPASS_SECRET_PERSIST_FAILED',
                error_code='BYPASS_SECRET_PERSIST_FAILED',
            )
        return OperationResult.ok({'secret': secret, 'source': 'generated'})

    def resolve(self, project_id: Optional[str]) -> Optional[str]:
        """Read-only resolution used by smoke wiring (stored -> env fallback)."""
        return self.secret_store.resolve(project_id)
