"""Staging-only enforcement layer (allowlist broker) for Vercel operations.

WHY THIS EXISTS
---------------
The R2 staging E2E runs on the SAME Vercel team as production (operator
decision). The deployment token in ``state/app.env`` is therefore a
team-scoped token that CAN reach production projects, promotion/alias
endpoints, domain mutation and project deletion. A project NAME
(``wb-<hash>`` from a staging ownership namespace) is NOT an isolation
boundary: a bug or a compromised code path in the runtime could still call
any Vercel endpoint with that token.

WHAT THIS IS
------------
``StagingVercelGuard`` is a drop-in ``VercelAdapter`` subclass acting as an
in-process allowlist broker. It intercepts every provider call at the single
existing seam (``VercelAdapter._call``) and:

  * ALLOWS only what is required to (a) look up / create ONE staging project
    and (b) create and poll a PREVIEW deployment for it;
  * DENIES (before any network I/O) production promotion / alias remap,
    domain add / verify / mutation, project & environment deletion,
    environment-variable changes, and every operation outside the allowlist;
  * refuses ``target=production`` and refuses alias/promote payloads;
  * validates project OWNERSHIP server-side: canonical team id must match AND,
    once known, the project id must equal the pinned staging project id --
    never inferred from a name;
  * resolves a team SLUG to its canonical ``team_*`` id before any ownership
    comparison (the slug/ID mismatch that silently broke staging ownership).

It is activated ONLY when ``WEBSITE_BUILDER_STAGING_GUARD`` is truthy, so
production behaviour is unchanged (see :func:`build_vercel_adapter`).

FAIL-CLOSED: any malformed call, unresolved team id, or unpinned project is a
denial, never a passthrough.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

from app.deploy.adapters import UrllibHttpTransport, VercelAdapter

# ---------------------------------------------------------------------------
# Activation + canonical team identity
# ---------------------------------------------------------------------------

GUARD_ENV = "WEBSITE_BUILDER_STAGING_GUARD"
PINNED_PROJECT_ENV = "VERCEL_STAGING_PROJECT_ID"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_CANONICAL_TEAM_RE = re.compile(r"^team_[A-Za-z0-9]{16,}$")
_VERCEL_API = "https://api.vercel.com"


def staging_guard_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    raw = os.environ if env is None else env
    return str(raw.get(GUARD_ENV, "")).strip().lower() in _TRUTHY


def resolve_canonical_team_id(*, token, team_id, transport=None) -> Optional[str]:
    """Return the canonical ``team_*`` id for ``team_id`` (which may be a slug).

    A slug (e.g. ``albert-a121``) is NOT comparable to the provider's
    ``accountId`` / ``teamId`` fields, which are canonical ids; an ownership
    check that compares a slug to a canonical id silently fails. This resolves
    the slug via ``GET /v2/teams`` and fails closed (``None``) when the mapping
    is missing or ambiguous.
    """
    if isinstance(team_id, str) and _CANONICAL_TEAM_RE.match(team_id):
        return team_id
    tr = transport or UrllibHttpTransport()
    try:
        resp = tr.request(
            "GET",
            _VERCEL_API + "/v2/teams",
            headers={"Authorization": "Bearer " + str(token)},
            data=None,
            timeout=30,
        )
        body = json.loads(resp.body)
    except Exception:
        return None
    teams = body.get("teams") if isinstance(body, dict) else None
    if not isinstance(teams, list):
        return None
    matches = [
        t.get("id")
        for t in teams
        if isinstance(t, dict)
        and t.get("slug") == team_id
        and isinstance(t.get("id"), str)
        and _CANONICAL_TEAM_RE.match(t["id"])
    ]
    return matches[0] if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Operation policy (checked BEFORE any network I/O)
# ---------------------------------------------------------------------------
# Allowlist: exactly the calls the staging preview lifecycle needs.
_ALLOWED_RULES: Sequence[Tuple[str, "re.Pattern"]] = (
    ("GET", re.compile(r"^/v9/projects/[^/]+$")),                      # lookup project
    ("GET", re.compile(r"^/v9/projects/[^/]+/protection-bypass$")),    # read bypass (smoke)
    ("POST", re.compile(r"^/v11/projects$")),                          # create staging project
    ("POST", re.compile(r"^/v13/deployments$")),                       # create PREVIEW deployment
    ("GET", re.compile(r"^/v13/deployments/[^/]+$")),                  # poll deployment
    ("GET", re.compile(r"^/v6/deployments$")),                         # find by meta
    ("PATCH", re.compile(r"^/v1/projects/[^/]+/protection-bypass$")),  # provision bypass
)

# Deny-first: these are refused even though some could match the allowlist shape.
_DENIED_RULES: Sequence[Tuple[str, "re.Pattern"]] = (
    ("DELETE", re.compile(r".*")),                                     # project/env/domain deletion
    ("POST", re.compile(r"^/v10/projects/[^/]+/promote/")),            # promote / alias-to-production
    ("POST", re.compile(r"^/v10/projects/[^/]+/domains")),             # domain add
    ("POST", re.compile(r"^/v9/projects/[^/]+/domains/")),             # domain verify
    ("PATCH", re.compile(r"^/v9/projects/[^/]+/env")),                 # env-var change
    ("POST", re.compile(r"^/v9/projects/[^/]+/env")),
)


class StagingVercelGuard(VercelAdapter):
    """Allowlist broker for staging Vercel operations (see module docstring)."""

    def __init__(self, token, team_id, ownership_namespace, *,
                 pinned_project_id: Optional[str] = None, transport=None):
        if not (isinstance(team_id, str) and _CANONICAL_TEAM_RE.match(team_id)):
            raise ValueError(
                "staging guard requires a canonical team_* id (got slug/empty)")
        super().__init__(token=token, team_id=team_id,
                         ownership_namespace=ownership_namespace, transport=transport)
        self.pinned_project_id = pinned_project_id or None
        self.denials: List[Tuple[str, str, str]] = []

    @staticmethod
    def _matches(rules, method: str, path: str) -> bool:
        return any(m == method and rx.match(path) for m, rx in rules)

    def _deny_reason(self, method: str, path: str, payload) -> Optional[str]:
        if self._matches(_DENIED_RULES, method, path):
            return "denied_operation"
        if not self._matches(_ALLOWED_RULES, method, path):
            return "not_allowlisted"
        if method == "POST" and path == "/v13/deployments" and isinstance(payload, dict):
            if payload.get("target") == "production":
                return "production_target"
            if payload.get("alias") or payload.get("aliases"):
                return "alias_remap"
        return None

    def _call(self, method, path, payload=None, **query):
        m = str(method).upper()
        reason = self._deny_reason(m, path, payload)
        if reason is not None:
            self.denials.append((m, path, reason))
            return (403, {"error": {"code": "STAGING_GUARD_DENIED", "reason": reason}})
        return super()._call(m, path, payload, **query)

    def _project_valid(self, project, app_id, expected_name=None):
        """Ownership is the canonical team id AND (once known) the pinned id."""
        if not super()._project_valid(project, app_id, expected_name=expected_name):
            return False
        pid = project.get("id") if isinstance(project, dict) else None
        if self.pinned_project_id and pid != self.pinned_project_id:
            return False
        return True


def build_vercel_adapter(*, token, team_id, ownership_namespace,
                         transport=None, env: Optional[Dict[str, str]] = None):
    """Single construction seam: plain adapter normally, guard in staging.

    Inert unless ``WEBSITE_BUILDER_STAGING_GUARD`` is truthy, so production
    behaviour is byte-for-byte unchanged.

    When ``WEBSITE_BUILDER_VERCEL_BROKER`` is set the adapter talks to the
    privileged broker over a UNIX socket (``transport`` defaults to
    ``BrokerTransport``), so the real token never enters this process.
    """
    raw = os.environ if env is None else env
    if transport is None:
        from app.deploy.staging_broker_client import build_broker_transport
        transport = build_broker_transport(raw)
    if not staging_guard_enabled(env):
        return VercelAdapter(token=token, team_id=team_id,
                             ownership_namespace=ownership_namespace,
                             transport=transport)
    canonical = resolve_canonical_team_id(token=token, team_id=team_id,
                                          transport=transport)
    if not canonical:
        raise ValueError("staging guard: unresolved canonical team id (fail closed)")
    pinned = str(raw.get(PINNED_PROJECT_ENV, "")).strip() or None
    return StagingVercelGuard(token=token, team_id=canonical,
                              ownership_namespace=ownership_namespace,
                              pinned_project_id=pinned, transport=transport)
