"""Loopback-only HTTP server for the §11.4 human calibration reviewer.

Security invariants enforced here (not in CSS/JS):

* Bind strictly to 127.0.0.1 (no public-bind option exists).
* Every data route requires a valid server-side session; state-changing
  routes additionally require a CSRF header.
* BLIND REVIEW: a sample is serialized with blind fields only until its
  reviewer row is LOCKED. Consensus label, notes, selection cues, and model
  prediction are NEVER attached to a pre-lock response, and never to any
  export the reviewer can reach pre-lock.
* GET exports stream deterministic bytes and never mutate state or write
  files.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backtest.calibration_reviewer import exports as exports_mod
from backtest.calibration_reviewer import rubric as rubric_mod
from backtest.calibration_reviewer.datasets import DATASET_SPECS, load_all
from backtest.calibration_reviewer.labels import ENUMS, InvalidHumanLabelError
from backtest.calibration_reviewer.reveal import consensus_reveal, model_reveal
from backtest.calibration_reviewer.schema import open_reviewer_db
from backtest.calibration_reviewer.security import (
    MAX_BODY_BYTES,
    SESSION_COOKIE,
    SecurityError,
    SecurityManager,
    check_origin,
    safe_static_path,
    security_headers,
    validate_notes,
)
from backtest.calibration_reviewer.store import (
    LockedLabelError,
    NotLockedError,
    StaleWriteError,
    Store,
    StoreError,
)

STATIC_DIR = Path(__file__).parent / "static"
# Blind fields exposed before lock.
_BLIND_FIELDS = ("sample_id", "ord", "ticker", "published_at", "source",
                 "headline_text", "headline_hash")


class ReviewerApp:
    """Application logic, independent of the HTTP handler (unit-testable)."""

    def __init__(self, db_path, *, cache_db_path=None, specs=DATASET_SPECS):
        self.conn = open_reviewer_db(db_path)
        self.store = Store(self.conn)
        self.security = SecurityManager(self.conn)
        self.loaded = load_all(specs)
        self.store.initialize_from_datasets(self.loaded)
        self.cache_db_path = cache_db_path
        # One connection is shared by the threaded server; serialize requests.
        import threading as _threading
        self.lock = _threading.RLock()

    # -- blind-safe sample serialization ---------------------------------

    def sample_payload(self, dataset_id: str, sample_id: str) -> dict:
        rec = self.store.get_sample(dataset_id, sample_id)
        if rec is None:
            raise SecurityError("unknown sample", status=404)
        status = rec.get("status") or "UNREVIEWED"
        payload = {
            "dataset_id": dataset_id,
            "status": status,
            "version": rec.get("version") or 0,
            "blind": {f: rec.get(f) for f in _BLIND_FIELDS},
        }
        # The reviewer's OWN in-progress draft is theirs to see; it is not the
        # answer key. Pre-lock we expose only the reviewer's own selections.
        payload["draft"] = {
            "category": rec.get("category"),
            "direction": rec.get("direction"),
            "severity": rec.get("severity"),
            "ma_role": rec.get("ma_role"),
            "certainty": rec.get("certainty"),
            "notes": rec.get("notes") or "",
            "flagged": bool(rec.get("flagged")),
        }
        payload["rubric_approved"] = bool(rec.get("rubric_approved"))
        payload["revealed"] = {
            "consensus": bool(rec.get("revealed_consensus")),
            "model": bool(rec.get("revealed_model")),
        }
        # Reveal content ONLY when locked AND the specific reveal was recorded.
        if status == "LOCKED" and rec.get("revealed_consensus"):
            payload["consensus"] = consensus_reveal(self.loaded[dataset_id],
                                                    sample_id)
        if status == "LOCKED" and rec.get("revealed_model") and self.cache_db_path:
            payload["model"] = model_reveal(
                self.cache_db_path,
                headline_hash=rec.get("headline_hash") or "",
                ticker=rec.get("ticker") or "")
        return payload


def _read_cookie(header: str | None) -> str | None:
    if not header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(header)
    except Exception:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None


class Handler(BaseHTTPRequestHandler):
    server_version = "R281Reviewer/1.0"

    @property
    def app(self) -> ReviewerApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: A002 - base signature
        # Redact: never log query strings, cookies, or headers.
        sys.stderr.write("[reviewer] %s\n" % (fmt % args).split("?")[0])

    # -- low-level helpers ----------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str,
              extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in security_headers().items():
            self.send_header(k, v)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, doc, extra_headers=None):
        body = json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   extra_headers)

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise SecurityError("request body too large", status=413)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if len(raw) > MAX_BODY_BYTES:
            raise SecurityError("request body too large", status=413)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecurityError("malformed JSON body", status=400) from exc
        if not isinstance(doc, dict):
            raise SecurityError("body must be a JSON object", status=400)
        return doc

    def _session(self) -> dict:
        token = _read_cookie(self.headers.get("Cookie"))
        return self.app.security.validate_session(token)

    def _require_csrf(self, session: dict, body: dict):
        # CSRF token from header only — never from a URL query string.
        presented = self.headers.get("X-CSRF-Token")
        if presented is None:
            presented = body.get("csrf_token")
        self.app.security.require_csrf(session, presented)

    # -- routing ---------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            with self.app.lock:
                if path == "/":
                    return self._serve_static("index.html")
                if path.startswith("/static/"):
                    return self._serve_static(path[len("/static/"):])
                if path == "/api/health":
                    return self._json(200, {"ok": True})
                if path == "/api/session":
                    return self._api_session()
                if path == "/api/rubric":
                    return self._api_rubric()
                if path == "/api/datasets":
                    return self._authed(lambda s: self._api_datasets())
                if path == "/api/progress":
                    return self._authed(lambda s: self._api_progress(parsed))
                if path == "/api/resume":
                    return self._authed(lambda s: self._api_resume(parsed))
                if path == "/api/sample":
                    return self._authed(lambda s: self._api_sample(parsed))
                if path == "/api/order":
                    return self._authed(lambda s: self._api_order(parsed))
                if path == "/api/export":
                    return self._authed(lambda s: self._api_export(parsed))
                return self._error(404, "not found")
        except SecurityError as exc:
            return self._error(exc.status, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return self._error(500, f"internal error: {type(exc).__name__}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            with self.app.lock:
                if path == "/api/pair":
                    return self._api_pair()
                session = self._session()
                check_origin(self.headers.get("Origin"),
                             self.headers.get("Host"))
                body = self._body()
                self._require_csrf(session, body)
                routes = {
                    "/api/draft": self._api_draft,
                    "/api/lock": self._api_lock,
                    "/api/flag": self._api_flag,
                    "/api/reveal": self._api_reveal,
                    "/api/revise": self._api_revise,
                    "/api/rubric/approve": self._api_rubric_approve,
                    "/api/logout": self._api_logout,
                }
                handler = routes.get(path)
                if handler is None:
                    return self._error(404, "not found")
                return handler(session, body)
        except SecurityError as exc:
            return self._error(exc.status, str(exc))
        except (StaleWriteError,) as exc:
            return self._error(409, str(exc))
        except (LockedLabelError, NotLockedError) as exc:
            return self._error(409, str(exc))
        except InvalidHumanLabelError as exc:
            return self._error(422, str(exc))
        except StoreError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return self._error(500, f"internal error: {type(exc).__name__}")

    # -- static ----------------------------------------------------------

    def _serve_static(self, rel: str):
        resolved = safe_static_path(STATIC_DIR, rel)
        if resolved is None:
            return self._error(404, "not found")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(resolved.suffix, "application/octet-stream")
        self._send(200, resolved.read_bytes(), ctype)

    # -- auth ------------------------------------------------------------

    def _authed(self, fn):
        session = self._session()
        return fn(session)

    def _api_session(self):
        token = _read_cookie(self.headers.get("Cookie"))
        try:
            session = self.app.security.validate_session(token)
        except SecurityError:
            return self._json(200, {"paired": False})
        return self._json(200, {
            "paired": True,
            "csrf_token": session["csrf_token"],
        })

    def _api_pair(self):
        body = self._body()
        token = self.app.security.pair(body.get("code", ""))
        cookie = (
            f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")
        self._json(200, {"paired": True}, {"Set-Cookie": cookie})

    def _api_logout(self, session, body):
        self.app.security.revoke_session(session)
        self._json(200, {"ok": True})

    # -- read APIs -------------------------------------------------------

    def _api_rubric(self):
        self._json(200, rubric_mod.rubric_help())

    def _api_datasets(self):
        self._json(200, {
            "datasets": self.app.store.list_datasets(),
            "enums": ENUMS,
        })

    def _q(self, parsed, key, default=None):
        return parse_qs(parsed.query).get(key, [default])[0]

    def _api_progress(self, parsed):
        dataset_id = self._q(parsed, "dataset")
        if dataset_id not in self.app.loaded:
            return self._error(400, "unknown dataset")
        return self._json(200, self.app.store.progress(dataset_id))

    def _api_resume(self, parsed):
        dataset_id = self._q(parsed, "dataset")
        if dataset_id not in self.app.loaded:
            return self._error(400, "unknown dataset")
        return self._json(200, {
            "sample_id": self.app.store.resume_sample_id(dataset_id)})

    def _api_sample(self, parsed):
        dataset_id = self._q(parsed, "dataset")
        sample_id = self._q(parsed, "sample_id")
        if dataset_id not in self.app.loaded:
            return self._error(400, "unknown dataset")
        return self._json(200, self.app.sample_payload(dataset_id,
                                                       sample_id))

    def _api_order(self, parsed):
        dataset_id = self._q(parsed, "dataset")
        if dataset_id not in self.app.loaded:
            return self._error(400, "unknown dataset")
        return self._json(200, {
            "dataset_id": dataset_id,
            "sample_ids": self.app.store.ordered_sample_ids(dataset_id)})

    def _api_export(self, parsed):
        dataset_id = self._q(parsed, "dataset")
        kind = self._q(parsed, "kind")
        store = self.app.store
        # integrity_json is dataset-independent (covers all datasets).
        if kind == "integrity_json":
            body = exports_mod.export_integrity_json(store, self.app.loaded)
            return self._send(
                200, body, "application/json; charset=utf-8",
                {"Content-Disposition":
                 'attachment; filename="reviewer_integrity.json"'})
        if dataset_id not in self.app.loaded:
            return self._error(400, "unknown dataset")
        loaded = self.app.loaded[dataset_id]
        if kind == "labels_csv":
            body = exports_mod.export_labels_csv(store, dataset_id, loaded)
            fname = f"{dataset_id}_human_labels.csv"
            ctype = "text/csv; charset=utf-8"
        elif kind == "progress_json":
            body = exports_mod.export_progress_json(store, dataset_id, loaded)
            fname = f"{dataset_id}_progress.json"
            ctype = "application/json; charset=utf-8"
        elif kind == "provenance_json":
            body = exports_mod.export_provenance_json(store, dataset_id, loaded)
            fname = f"{dataset_id}_provenance.json"
            ctype = "application/json; charset=utf-8"
        elif kind == "integrity_json":
            body = exports_mod.export_integrity_json(store, self.app.loaded)
            fname = "reviewer_integrity.json"
            ctype = "application/json; charset=utf-8"
        else:
            return self._error(400, "unknown export kind")
        self._send(200, body, ctype,
                   {"Content-Disposition": f'attachment; filename="{fname}"'})

    # -- write APIs ------------------------------------------------------

    def _expected_version(self, body):
        ev = body.get("expected_version")
        if not isinstance(ev, int) or isinstance(ev, bool) or ev < 0:
            raise SecurityError("expected_version must be a non-negative int",
                                status=400)
        return ev

    def _api_draft(self, session, body):
        dataset_id = body.get("dataset_id")
        sample_id = body.get("sample_id")
        self._check_dataset(dataset_id)
        notes = validate_notes(body.get("notes"))
        label = body.get("label") if isinstance(body.get("label"), dict) else None
        flagged = body.get("flagged")
        v = self.app.store.save_draft(
            dataset_id, sample_id, label=label,
            certainty=body.get("certainty"), notes=notes,
            flagged=bool(flagged) if flagged is not None else None,
            expected_version=self._expected_version(body))
        return self._json(200, {"version": v, "status": "DRAFT"})

    def _api_lock(self, session, body):
        dataset_id = body.get("dataset_id")
        sample_id = body.get("sample_id")
        self._check_dataset(dataset_id)
        notes = validate_notes(body.get("notes"))
        label = body.get("label")
        if not isinstance(label, dict):
            return self._error(422, "a complete four-field label is required")
        rubric_approved = self.app.store.is_rubric_approved(
            rubric_mod.RUBRIC_VERSION)
        v = self.app.store.lock(
            dataset_id, sample_id, label=label,
            certainty=body.get("certainty"), notes=notes,
            expected_version=self._expected_version(body),
            rubric_version=rubric_mod.RUBRIC_VERSION,
            rubric_approved=rubric_approved)
        return self._json(200, {
            "version": v, "status": "LOCKED",
            "rubric_approved": rubric_approved})

    def _api_flag(self, session, body):
        dataset_id = body.get("dataset_id")
        sample_id = body.get("sample_id")
        self._check_dataset(dataset_id)
        flagged = body.get("flagged")
        if not isinstance(flagged, bool):
            return self._error(400, "flagged must be a boolean")
        v = self.app.store.set_flag(
            dataset_id, sample_id, flagged=flagged,
            expected_version=self._expected_version(body))
        return self._json(200, {"version": v})

    def _api_reveal(self, session, body):
        dataset_id = body.get("dataset_id")
        sample_id = body.get("sample_id")
        self._check_dataset(dataset_id)
        kind = body.get("kind")
        v = self.app.store.reveal(
            dataset_id, sample_id, kind=kind,
            expected_version=self._expected_version(body))
        payload = self.app.sample_payload(dataset_id, sample_id)
        return self._json(200, {"version": v, "sample": payload})

    def _api_revise(self, session, body):
        dataset_id = body.get("dataset_id")
        sample_id = body.get("sample_id")
        self._check_dataset(dataset_id)
        notes = validate_notes(body.get("notes"))
        label = body.get("label")
        if not isinstance(label, dict):
            return self._error(422, "a complete four-field label is required")
        rubric_approved = self.app.store.is_rubric_approved(
            rubric_mod.RUBRIC_VERSION)
        v = self.app.store.revise(
            dataset_id, sample_id, label=label,
            certainty=body.get("certainty"), notes=notes,
            reason=body.get("reason", ""),
            expected_version=self._expected_version(body),
            rubric_version=rubric_mod.RUBRIC_VERSION,
            rubric_approved=rubric_approved)
        return self._json(200, {"version": v, "status": "LOCKED"})

    def _api_rubric_approve(self, session, body):
        version = body.get("rubric_version")
        if version != rubric_mod.RUBRIC_VERSION:
            return self._error(400, "unknown rubric_version")
        self.app.store.approve_rubric(version,
                                             note=body.get("note", ""))
        return self._json(200, {"approved": version})

    def _check_dataset(self, dataset_id):
        if dataset_id not in self.app.loaded:
            raise SecurityError("unknown dataset", status=400)


class ReviewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: ReviewerApp):
        super().__init__(addr, Handler)
        self.app = app


def build_server(app: ReviewerApp, *, host="127.0.0.1", port=8765) -> ReviewerServer:
    if host != "127.0.0.1":
        raise ValueError("reviewer must bind to 127.0.0.1 only")
    return ReviewerServer((host, port), app)
