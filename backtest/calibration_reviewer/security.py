"""Local security: pairing bootstrap, sessions, CSRF, size limits, headers.

Threat model (single user, loopback only):

* The server binds strictly to ``127.0.0.1`` and has no public-bind option.
  Remote access is via SSH port forwarding:
  ``ssh -L 8765:127.0.0.1:8765 user@vps``.
* Localhost binding is NOT treated as authentication. A one-time **pairing
  code** is generated at startup and printed to the operator's terminal — it
  is never placed in a URL, never logged, and never written to the reviewer
  DB in plaintext. The browser operator enters it once; the server then
  issues an opaque, server-side-validated session.
* Session cookie is ``HttpOnly`` + ``SameSite=Strict``; the cookie value is a
  256-bit random token whose SHA-256 is the stored session id. A stolen
  cookie cannot be replayed cross-site.
* State-changing requests require a CSRF token delivered via the
  ``X-CSRF-Token`` header (never in a URL query string).
* Every response is ``no-store`` with a restrictive CSP and no CDN origins.

Limitations (documented): any process able to read the operator's terminal
stdout, or the same OS user's files, can bootstrap a session. Revocation and
re-initialization are provided (:meth:`SecurityManager.revoke_all` and
restarting the server regenerates the pairing code). This is acceptable only
because access is loopback + SSH tunnel.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

MAX_BODY_BYTES = 64 * 1024          # request body cap
MAX_NOTES_CHARS = 4000              # reviewer notes cap
PAIRING_MAX_ATTEMPTS = 8            # per-window brute-force guard
PAIRING_WINDOW_SECONDS = 300

SESSION_COOKIE = "r281_reviewer_session"


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SecurityError(Exception):
    """Authentication / authorization / request-shape failure."""

    def __init__(self, message: str, *, status: int = 403):
        super().__init__(message)
        self.status = status


class SecurityManager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._pairing_code = secrets.token_urlsafe(24)
        self._pairing_used = False
        self._attempts: list[float] = []

    # -- pairing ---------------------------------------------------------

    @property
    def pairing_code(self) -> str:
        return self._pairing_code

    def rotate_pairing_code(self) -> str:
        self._pairing_code = secrets.token_urlsafe(24)
        self._pairing_used = False
        self._attempts.clear()
        return self._pairing_code

    def _pairing_rate_ok(self) -> bool:
        now = time.monotonic()
        self._attempts = [t for t in self._attempts
                          if now - t < PAIRING_WINDOW_SECONDS]
        return len(self._attempts) < PAIRING_MAX_ATTEMPTS

    def pair(self, code: str) -> str:
        """Exchange a valid pairing code for a new session token.

        Returns the opaque cookie token (the caller sets it HttpOnly).
        The code is strictly one-time: the first successful use burns it and
        any later reuse fails until the operator explicitly rotates it.
        Existing (already-issued) sessions are unaffected either way.
        """
        if self._pairing_used:
            raise SecurityError(
                "pairing code already used; rotate the code to pair again",
                status=409)
        if not self._pairing_rate_ok():
            raise SecurityError("too many pairing attempts", status=429)
        self._attempts.append(time.monotonic())
        if not isinstance(code, str) or not hmac.compare_digest(
                code.strip(), self._pairing_code):
            raise SecurityError("invalid pairing code", status=401)
        self._pairing_used = True
        return self._create_session()

    def _create_session(self) -> str:
        token = secrets.token_urlsafe(32)          # 256-bit
        session_id = _sha256_hex(token)
        csrf = secrets.token_urlsafe(32)
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO session (session_id, token_hash, csrf_token, "
            "created_at, last_seen_at, revoked) VALUES (?,?,?,?,?,0)",
            (session_id, session_id, csrf, now, now))
        self._conn.commit()
        return token

    # -- session validation ----------------------------------------------

    def validate_session(self, cookie_token: str | None) -> dict:
        """Return the session row for a valid cookie token, else raise."""
        if not cookie_token:
            raise SecurityError("no session", status=401)
        session_id = _sha256_hex(cookie_token)
        row = self._conn.execute(
            "SELECT * FROM session WHERE session_id=? AND revoked=0",
            (session_id,)).fetchone()
        if row is None:
            raise SecurityError("invalid session", status=401)
        self._conn.execute(
            "UPDATE session SET last_seen_at=? WHERE session_id=?",
            (_now_iso(), session_id))
        self._conn.commit()
        return dict(row)

    def require_csrf(self, session: dict, presented: str | None) -> None:
        if not presented or not hmac.compare_digest(
                presented, session["csrf_token"]):
            raise SecurityError("CSRF token mismatch", status=403)

    def revoke_session(self, session: dict) -> None:
        self._conn.execute(
            "UPDATE session SET revoked=1 WHERE session_id=?",
            (session["session_id"],))
        self._conn.commit()

    def revoke_all(self) -> int:
        cur = self._conn.execute("UPDATE session SET revoked=1 WHERE revoked=0")
        self._conn.commit()
        return cur.rowcount


# -- request-shape helpers ------------------------------------------------

def check_origin(origin: str | None, host_header: str | None) -> None:
    """Reject cross-origin state-changing requests (defense in depth)."""
    if not origin:
        return  # same-origin fetch may omit Origin on some clients; cookie+
        # CSRF still gate the request.
    allowed_hosts = {"127.0.0.1", "localhost"}
    try:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
    except ValueError as exc:
        raise SecurityError("malformed Origin", status=403) from exc
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise SecurityError("cross-origin request rejected", status=403)


def safe_static_path(static_root: Path, rel: str) -> Path | None:
    """Resolve a static asset within ``static_root`` or return None.

    Rejects traversal, absolute paths, null bytes, and anything outside root.
    """
    if not rel or "\x00" in rel:
        return None
    rel = rel.lstrip("/")
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    candidate = (static_root / rel).resolve()
    root = static_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def validate_notes(notes) -> str:
    if notes is None:
        return ""
    if not isinstance(notes, str):
        raise SecurityError("notes must be a string", status=400)
    if len(notes) > MAX_NOTES_CHARS:
        raise SecurityError("notes too long", status=413)
    return notes


def security_headers() -> dict:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"),
    }
