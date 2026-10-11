"""HTTP integration: auth, CSRF, blinding, locking, reveal, exports.

Runs the real stdlib server on an ephemeral loopback port in a background
thread and drives it with http.client. No network, no LLM.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from backtest.calibration_reviewer.server import ReviewerApp, build_server

from .conftest import make_dataset


class Client:
    def __init__(self, host, port, pairing_code):
        self.host = host
        self.port = port
        self.cookie = None
        self.csrf = None
        self._pairing = pairing_code

    def _conn(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=10)

    def request(self, method, path, body=None, headers=None, use_csrf=True):
        conn = self._conn()
        hdrs = dict(headers or {})
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            if use_csrf and self.csrf:
                hdrs.setdefault("X-CSRF-Token", self.csrf)
            data = json.dumps(body).encode()
        else:
            data = None
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        parsed = None
        if "json" in ctype:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        return resp.status, parsed, raw, dict(resp.getheaders())

    def pair(self):
        status, body, _, _ = self.request(
            "POST", "/api/pair", {"code": self._pairing})
        assert status == 200, body
        s, sb, _, _ = self.request("GET", "/api/session")
        self.csrf = sb["csrf_token"]
        return self


@pytest.fixture
def live(tmp_path):
    specs = (make_dataset(tmp_path, "A_CANONICAL_240", n=6, role="CANONICAL"),
             make_dataset(tmp_path, "B_SAFETY_99", n=3,
                          role="SAFETY_CHALLENGE", with_cues=True))
    app = ReviewerApp(tmp_path / "reviewer.sqlite3", cache_db_path=None,
                      specs=specs)
    server = build_server(app, host="127.0.0.1", port=0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _client(live):
    app, host, port = live
    return Client(host, port, app.security.pairing_code)


# ---- auth ---------------------------------------------------------------

def test_unauthenticated_read_rejected(live):
    app, host, port = live
    c = Client(host, port, app.security.pairing_code)
    status, _, _, _ = c.request("GET", "/api/datasets")
    assert status == 401


def test_bad_pairing_code_rejected(live):
    app, host, port = live
    c = Client(host, port, "wrong-code")
    status, body, _, _ = c.request("POST", "/api/pair", {"code": "wrong-code"})
    assert status == 401


def test_pair_then_read_ok(live):
    c = _client(live).pair()
    status, body, _, _ = c.request("GET", "/api/datasets")
    assert status == 200
    assert "enums" in body


def test_csrf_missing_rejected(live):
    c = _client(live).pair()
    status, _, _, _ = c.request(
        "POST", "/api/draft",
        {"dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
         "expected_version": 0, "label": {"category": "EARNINGS"}},
        use_csrf=False)
    assert status == 403


def test_csrf_wrong_rejected(live):
    c = _client(live).pair()
    status, _, _, _ = c.request(
        "POST", "/api/draft",
        {"dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
         "expected_version": 0, "label": {"category": "EARNINGS"}},
        headers={"X-CSRF-Token": "not-the-token"})
    assert status == 403


def test_cross_origin_rejected(live):
    c = _client(live).pair()
    status, _, _, _ = c.request(
        "POST", "/api/draft",
        {"dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
         "expected_version": 0, "label": {"category": "EARNINGS"}},
        headers={"Origin": "http://evil.example"})
    assert status == 403


def test_oversized_body_rejected(live):
    c = _client(live).pair()
    big = "x" * (70 * 1024)
    status, _, _, _ = c.request(
        "POST", "/api/draft",
        {"dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
         "expected_version": 0, "notes": big})
    assert status == 413


def test_path_traversal_rejected(live):
    c = _client(live).pair()
    status, _, _, _ = c.request("GET", "/static/../../server.py")
    assert status == 404


def test_security_headers_present(live):
    c = _client(live).pair()
    _, _, _, headers = c.request("GET", "/api/datasets")
    assert headers.get("Cache-Control", "").startswith("no-store")
    assert "default-src 'none'" in headers.get("Content-Security-Policy", "")
    assert headers.get("X-Content-Type-Options") == "nosniff"


# ---- blinding -----------------------------------------------------------

def test_prelock_sample_has_no_secrets(live):
    c = _client(live).pair()
    status, body, raw, _ = c.request(
        "GET", "/api/sample?dataset=A_CANONICAL_240&sample_id=A_CANONICAL_240-0000")
    assert status == 200
    text = raw.decode()
    # The headline text IS shown (it is the review subject)...
    assert "Secret consensus headline number 0" in text
    # ...but the consensus label and notes must NOT appear anywhere.
    assert "SECRET_NOTES_0" not in text
    assert '"human_label"' not in text
    assert "human_notes" not in text
    assert body["status"] == "UNREVIEWED"
    assert "consensus" not in body
    assert "model" not in body


def test_prelock_sample_hides_consensus_label_values(live):
    """The consensus label JSON from the source must not leak pre-lock."""
    c = _client(live).pair()
    _, _, raw, _ = c.request(
        "GET", "/api/sample?dataset=A_CANONICAL_240&sample_id=A_CANONICAL_240-0001")
    text = raw.decode()
    # Row 1 consensus label is EARNINGS/BULLISH/LOW/NEITHER; ensure the raw
    # serialized consensus object isn't embedded.
    assert '"human_label"' not in text
    assert "SECRET_NOTES_1" not in text


def test_prelock_challenge_cues_hidden(live):
    c = _client(live).pair()
    _, _, raw, _ = c.request(
        "GET", "/api/sample?dataset=B_SAFETY_99&sample_id=B_SAFETY_99-0000")
    text = raw.decode()
    assert "SECRET_CUE_0" not in text
    assert "SECRET_DIM_0" not in text
    assert "SECRET_EVIDENCE_0" not in text


def test_prelock_export_has_no_answer_key(live):
    c = _client(live).pair()
    _, _, raw, _ = c.request(
        "GET", "/api/export?dataset=A_CANONICAL_240&kind=labels_csv")
    text = raw.decode()
    assert "SECRET_NOTES" not in text
    # CSV has label columns but they are empty pre-lock.
    assert "review_state" in text


def test_refresh_does_not_bypass_blinding(live):
    """A brand-new session still sees a blind sample."""
    c1 = _client(live).pair()
    c1.request("GET", "/api/sample?dataset=A_CANONICAL_240&sample_id=A_CANONICAL_240-0000")
    # New session (fresh client, same server).
    app, host, port = live
    c2 = Client(host, port, app.security.pairing_code)
    # rotate pairing so the old code no longer works, then pair fresh
    app.security.rotate_pairing_code()
    c2 = Client(host, port, app.security.pairing_code).pair()
    _, _, raw, _ = c2.request(
        "GET", "/api/sample?dataset=A_CANONICAL_240&sample_id=A_CANONICAL_240-0000")
    assert "SECRET_NOTES_0" not in raw.decode()


# ---- locking / reveal ---------------------------------------------------

def _lock(c, dataset="A_CANONICAL_240", sid="A_CANONICAL_240-0000"):
    label = {"category": "EARNINGS", "direction": "BULLISH",
             "severity": "MEDIUM", "ma_role": "NEITHER"}
    return c.request("POST", "/api/lock", {
        "dataset_id": dataset, "sample_id": sid, "expected_version": 0,
        "label": label, "certainty": "CERTAIN", "notes": "n"})


def test_lock_requires_four_fields(live):
    c = _client(live).pair()
    status, _, _, _ = c.request("POST", "/api/lock", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": 0,
        "label": {"category": "EARNINGS", "direction": "BULLISH",
                  "severity": "MEDIUM"}})  # missing ma_role
    assert status == 422


def test_lock_then_reveal_consensus(live):
    c = _client(live).pair()
    status, body, _, _ = _lock(c)
    assert status == 200 and body["status"] == "LOCKED"
    v = body["version"]
    status, body, _, _ = c.request("POST", "/api/reveal", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": v, "kind": "consensus"})
    assert status == 200
    assert body["sample"]["consensus"]["provenance"] == "LLM_CONSENSUS_SPOT_CHECKED"
    assert "SECRET_NOTES_0" in json.dumps(body["sample"]["consensus"])


def test_reveal_before_lock_rejected(live):
    c = _client(live).pair()
    status, _, _, _ = c.request("POST", "/api/reveal", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": 0, "kind": "consensus"})
    assert status == 409


def test_duplicate_lock_409(live):
    c = _client(live).pair()
    _, body, _, _ = _lock(c)
    status, _, _, _ = c.request("POST", "/api/lock", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": body["version"],
        "label": {"category": "EARNINGS", "direction": "BULLISH",
                  "severity": "MEDIUM", "ma_role": "NEITHER"}})
    assert status == 409


def test_stale_autosave_409(live):
    c = _client(live).pair()
    # Draft at version 0 -> 1.
    c.request("POST", "/api/draft", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": 0, "label": {"category": "EARNINGS"}})
    # Delayed autosave still claiming version 0.
    status, _, _, _ = c.request("POST", "/api/draft", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": 0, "label": {"category": "GUIDANCE"}})
    assert status == 409


def test_stale_autosave_cannot_overwrite_lock(live):
    c = _client(live).pair()
    _, body, _, _ = _lock(c)  # version 0 -> 1
    # Delayed draft from a stale tab claiming version 0.
    status, _, _, _ = c.request("POST", "/api/draft", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": 0, "label": {"category": "GUIDANCE"}})
    assert status == 409
    # Label remains LOCKED.
    _, s, _, _ = c.request(
        "GET", "/api/sample?dataset=A_CANONICAL_240&sample_id=A_CANONICAL_240-0000")
    assert s["status"] == "LOCKED"


def test_revise_requires_reason(live):
    c = _client(live).pair()
    _, body, _, _ = _lock(c)
    status, _, _, _ = c.request("POST", "/api/revise", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
        "expected_version": body["version"], "reason": "",
        "label": {"category": "EARNINGS", "direction": "BEARISH",
                  "severity": "HIGH", "ma_role": "NEITHER"}})
    assert status == 400


def test_rubric_approve_gate(live):
    c = _client(live).pair()
    # Lock before approval -> not canonical.
    _, body, _, _ = _lock(c)
    _, prog, _, _ = c.request("GET", "/api/progress?dataset=A_CANONICAL_240")
    assert prog["locked"] == 1 and prog["canonical_locked"] == 0
    # Approve rubric (digest comes from the served rubric document), then lock
    # a second sample -> canonical.
    _, rub, _, _ = c.request("GET", "/api/rubric")
    status, _, _, _ = c.request("POST", "/api/rubric/approve", {
        "rubric_version": "r281-reviewer-rubric-0.1",
        "rubric_sha256": rub["sha256"], "note": "approved"})
    assert status == 200
    _lock(c, sid="A_CANONICAL_240-0001")
    _, prog2, _, _ = c.request("GET", "/api/progress?dataset=A_CANONICAL_240")
    assert prog2["canonical_locked"] == 1


# ---- exports ------------------------------------------------------------

def test_export_deterministic(live):
    c = _client(live).pair()
    _lock(c)
    _, _, a, _ = c.request("GET", "/api/export?dataset=A_CANONICAL_240&kind=labels_csv")
    _, _, b, _ = c.request("GET", "/api/export?dataset=A_CANONICAL_240&kind=labels_csv")
    assert a == b


def test_export_does_not_mutate_state(live):
    c = _client(live).pair()
    _, before, _, _ = c.request("GET", "/api/progress?dataset=A_CANONICAL_240")
    c.request("GET", "/api/export?dataset=A_CANONICAL_240&kind=labels_csv")
    c.request("GET", "/api/export?dataset=A_CANONICAL_240&kind=progress_json")
    _, after, _, _ = c.request("GET", "/api/progress?dataset=A_CANONICAL_240")
    assert before == after


def test_export_requires_auth(live):
    app, host, port = live
    c = Client(host, port, app.security.pairing_code)
    status, _, _, _ = c.request(
        "GET", "/api/export?dataset=A_CANONICAL_240&kind=labels_csv")
    assert status == 401


def test_export_distinguishes_states(live):
    c = _client(live).pair()
    _lock(c, sid="A_CANONICAL_240-0000")
    c.request("POST", "/api/draft", {
        "dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0001",
        "expected_version": 0, "label": {"category": "EARNINGS"}})
    _, _, raw, _ = c.request(
        "GET", "/api/export?dataset=A_CANONICAL_240&kind=labels_csv")
    text = raw.decode()
    assert "LOCKED" in text and "DRAFT" in text and "UNREVIEWED" in text


def test_health_no_auth(live):
    app, host, port = live
    c = Client(host, port, app.security.pairing_code)
    status, body, _, _ = c.request("GET", "/api/health")
    assert status == 200 and body["ok"] is True


def test_integrity_export_all_datasets(live):
    c = _client(live).pair()
    status, body, raw, _ = c.request("GET", "/api/export?kind=integrity_json")
    assert status == 200
    doc = json.loads(raw)
    assert len(doc["datasets"]) == 2  # A and B fixtures
    assert "combined_note" in doc


# ---- pairing one-time reuse (regression) ---------------------------------

def test_pairing_code_single_use_http(live):
    app, host, port = live
    code = app.security.pairing_code
    c1 = Client(host, port, code)
    status, body, _, _ = c1.request("POST", "/api/pair", {"code": code})
    assert status == 200 and body["paired"] is True
    # Second use of the SAME code must fail even though it is correct.
    c2 = Client(host, port, code)
    status, _, _, _ = c2.request("POST", "/api/pair", {"code": code})
    assert status == 409
    # Rotation re-opens pairing for a new client.
    app.security.rotate_pairing_code()
    c3 = Client(host, port, app.security.pairing_code)
    status, _, _, _ = c3.request("POST", "/api/pair",
                                 {"code": app.security.pairing_code})
    assert status == 200


def test_first_session_survives_pairing_reuse_attempt(live):
    app, host, port = live
    code = app.security.pairing_code
    c1 = Client(host, port, code).pair()
    Client(host, port, code).request("POST", "/api/pair", {"code": code})
    status, body, _, _ = c1.request("GET", "/api/datasets")
    assert status == 200 and "enums" in body


# ---- CSRF is header-only (regression) -------------------------------------

def test_csrf_token_in_body_rejected(live):
    """The old JSON-body fallback must stay dead: header or nothing."""
    c = _client(live).pair()
    status, _, _, _ = c.request(
        "POST", "/api/draft",
        {"dataset_id": "A_CANONICAL_240", "sample_id": "A_CANONICAL_240-0000",
         "expected_version": 0, "label": {"category": "EARNINGS"},
         "csrf_token": c.csrf},
        use_csrf=False)
    assert status == 403


# ---- rubric digest-bound approval (regression) -----------------------------

def test_rubric_payload_carries_sha256(live):
    c = _client(live).pair()
    _, rub, _, _ = c.request("GET", "/api/rubric")
    import hashlib
    assert rub["sha256"] == hashlib.sha256(
        repr(sorted(
            __import__("backtest.calibration_reviewer.rubric",
                       fromlist=["RUBRIC"]).RUBRIC.items())).encode()
    ).hexdigest()


def test_rubric_approve_requires_matching_digest(live):
    c = _client(live).pair()
    status, _, _, _ = c.request("POST", "/api/rubric/approve", {
        "rubric_version": "r281-reviewer-rubric-0.1",
        "rubric_sha256": "0" * 64, "note": "blind guess"})
    assert status == 400


def test_rubric_approve_without_digest_rejected(live):
    c = _client(live).pair()
    status, _, _, _ = c.request("POST", "/api/rubric/approve", {
        "rubric_version": "r281-reviewer-rubric-0.1", "note": "no digest"})
    assert status == 400


def test_locked_row_stores_rubric_digest(live):
    c = _client(live).pair()
    _, rub, _, _ = c.request("GET", "/api/rubric")
    c.request("POST", "/api/rubric/approve", {
        "rubric_version": "r281-reviewer-rubric-0.1",
        "rubric_sha256": rub["sha256"], "note": ""})
    _, body, _, _ = _lock(c)
    assert body["status"] == "LOCKED"
    app, _, _ = live
    row = app.store.get_sample("A_CANONICAL_240", "A_CANONICAL_240-0000")
    assert row["rubric_sha256"] == rub["sha256"]
    assert row["rubric_approved"] == 1


def test_lock_after_rubric_content_change_is_provisional(live):
    """Content drift without renewed approval must fail closed."""
    c = _client(live).pair()
    _, rub, _, _ = c.request("GET", "/api/rubric")
    c.request("POST", "/api/rubric/approve", {
        "rubric_version": "r281-reviewer-rubric-0.1",
        "rubric_sha256": rub["sha256"], "note": ""})
    # Simulate an edit of the rubric content (digest changes, version string
    # unchanged) after approval.
    from backtest.calibration_reviewer import rubric as rubric_mod
    original = rubric_mod.RUBRIC["severity"]
    rubric_mod.RUBRIC["severity"] = original + " EDITED"
    try:
        # The server computes the gate digest from the EDITED content; the
        # recorded approval no longer matches, so the lock is refused outright
        # rather than silently counting as canonical.
        status, body, _, _ = _lock(c)
        assert status == 400, body
        assert "rubric gate" in body["error"]
        app, _, _ = live
        _, prog, _, _ = c.request("GET", "/api/progress?dataset=A_CANONICAL_240")
        assert prog["canonical_locked"] == 0
    finally:
        rubric_mod.RUBRIC["severity"] = original


def test_lock_claiming_canonical_without_approval_is_refused(live):
    """A client cannot assert rubric_approved through /api/lock pre-approval."""
    c = _client(live).pair()
    status, body, _, _ = _lock(c)
    assert status == 200 and body["status"] == "LOCKED"
    app, _, _ = live
    row = app.store.get_sample("A_CANONICAL_240", "A_CANONICAL_240-0000")
    assert row["rubric_approved"] == 0
    _, prog, _, _ = c.request("GET", "/api/progress?dataset=A_CANONICAL_240")
    assert prog["canonical_locked"] == 0
