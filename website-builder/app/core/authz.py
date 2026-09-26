"""Local project ACL. Principals must come from trusted authentication, not payloads.

Bearer references grant viewer access only. This is not design-reference Phase 12.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Optional

from .state import ProjectState

ROLE_OWNER = "owner"
ROLE_REVIEWER = "reviewer"
ROLE_VIEWER = "viewer"


class AuthzError(Exception):
    def __init__(self, error_code="UNAUTHORIZED_ROLE", message=None):
        super().__init__(message or error_code)
        self.error_code = error_code


def valid_principal(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _valid_acl(state):
    roles = state.roles
    if not isinstance(roles, dict) or set(roles) != {"owner", "reviewers", "viewers"}:
        return False
    if roles["owner"] is not None and not valid_principal(roles["owner"]):
        return False
    for key in ("reviewers", "viewers"):
        if not isinstance(roles[key], list) or not all(valid_principal(x) for x in roles[key]):
            return False
    tokens = state.reference_tokens
    if not isinstance(tokens, dict):
        return False
    for digest, meta in tokens.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
        if not isinstance(meta, dict) or meta.get("role") != ROLE_VIEWER:
            return False
        if type(meta.get("created_at")) not in (int, float):
            return False
        if meta.get("label") is not None and not isinstance(meta["label"], str):
            return False
    return True


def _hash_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def resolve_role(state, principal_id=None, reference_token=None):
    if not _valid_acl(state):
        return None
    if principal_id is not None and not valid_principal(principal_id):
        return None
    if reference_token is not None and (
        not isinstance(reference_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", reference_token)
    ):
        return None
    if principal_id is not None:
        if principal_id == state.roles["owner"]:
            return ROLE_OWNER
        if principal_id in state.roles["reviewers"]:
            return ROLE_REVIEWER
        if principal_id in state.roles["viewers"]:
            return ROLE_VIEWER
    if reference_token:
        digest = _hash_token(reference_token)
        matched = False
        for stored in state.reference_tokens:
            matched |= hmac.compare_digest(stored, digest)
        if matched:
            return ROLE_VIEWER
    return None


def require_mutating_role(state, principal_id=None, reference_token=None):
    role = resolve_role(state, principal_id, reference_token)
    if not valid_principal(principal_id) or role not in (ROLE_OWNER, ROLE_REVIEWER):
        raise AuthzError()
    return role


def require_owner_role(state, principal_id=None, reference_token=None):
    if not valid_principal(principal_id) or resolve_role(state, principal_id, reference_token) != ROLE_OWNER:
        raise AuthzError()
    return ROLE_OWNER


def can_view(state, principal_id=None, reference_token=None):
    return resolve_role(state, principal_id, reference_token) is not None


def generate_reference_token(state, role=ROLE_VIEWER, label=None, *, principal_id=None):
    """Caller must hold writer and save. Return raw 32-byte token once."""
    require_owner_role(state, principal_id)
    if role != ROLE_VIEWER or (label is not None and not isinstance(label, str)):
        raise AuthzError()
    token = secrets.token_urlsafe(32)
    state.reference_tokens[_hash_token(token)] = {
        "role": ROLE_VIEWER, "created_at": time.time(), "label": label,
    }
    return token


def revoke_reference_token(state, raw_token, *, principal_id=None):
    require_owner_role(state, principal_id)
    if not isinstance(raw_token, str):
        raise AuthzError()
    return state.reference_tokens.pop(_hash_token(raw_token), None) is not None


class ProjectAccess:
    """Locked persisted ACL operations and deliberately minimal public read."""
    def __init__(self, store):
        self.store = store

    def create(self, project_id, principal_id, *, channel=None, conversation_id=None):
        if not valid_principal(principal_id):
            raise AuthzError()
        with self.store.acquire_writer(project_id) as state:
            if self.store.load(project_id) is not None:
                raise AuthzError("PROJECT_EXISTS")
            state.owner_id = principal_id
            state.roles["owner"] = principal_id
            state.channel = channel
            state.conversation_id = conversation_id
            self.store.save(state)
        return state

    def set_roles(self, project_id, principal_id, roles):
        with self.store.acquire_writer(project_id) as state:
            require_owner_role(state, principal_id)
            state.roles = roles
            if not _valid_acl(state) or not valid_principal(roles["owner"]):
                raise AuthzError()
            state.owner_id = roles["owner"]
            self.store.save(state)

    def issue(self, project_id, principal_id, *, label=None):
        with self.store.acquire_writer(project_id) as state:
            token = generate_reference_token(state, label=label, principal_id=principal_id)
            self.store.save(state)
            return token

    def revoke(self, project_id, principal_id, token):
        with self.store.acquire_writer(project_id) as state:
            found = revoke_reference_token(state, token, principal_id=principal_id)
            if found:
                self.store.save(state)
            return found

    def read(self, project_id, principal_id=None, reference_token=None):
        """Read the public view of a project.

        Deliberately lock-FREE. This is a pure read of three fields and it is
        reachable on the ``read`` action ahead of the dispatcher claim block;
        taking the exclusive writer lock made a status query block every
        build/revise/preview writer on that project and could exceed the 30s
        lock timeout. ``load`` reads a document that ``save`` replaces
        atomically, so a lock-free read observes either the old or the new
        state, never a partial one.
        """
        state = self.store.load(project_id)
        if state is None:
            raise AuthzError("NO_PROJECT_STATE")
        if not can_view(state, principal_id, reference_token):
            raise AuthzError()
        # No internal paths, ACLs, identities, tokens, briefs or adapter metadata.
        return {"project_id": state.project_id, "lifecycle": state.lifecycle,
                "source_revision": state.revisions.source_revision}
