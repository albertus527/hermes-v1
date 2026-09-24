"""R1 regression tests for preview smoke diagnostics and recovery.

These offline tests pin a self-contained-gate regression for external runtime
assets inside inline style blocks and the subsequent mandatory-preview
recovery loop. They do not claim that the synthetic fixture is the exact p7
artifact; that requires captured VPS evidence.

Tests use real imports, real state I/O in ``tmp_path``, and injected
browser/provider/smoke faults. No network, no Vercel, no Telegram.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.contracts import OperationResult  # noqa: E402
from app.core.selfcontained import (  # noqa: E402
    check_self_contained,
    normalize_and_check_self_contained,
    normalize_self_contained,
)
from app.deploy.adapters import (  # noqa: E402
    PreviewSmokeTester,
    _smoke_classification,
    _smoke_failure_record,
    _smoke_failure_summary,
    _sanitize_smoke_url,
)
from tests.r1_harness import (  # noqa: E402
    LocalR1Scenario,
    make_preview_ready,
)

PNG = b"\x89PNG\r\n\x1a\nfake"


# ===========================================================================
# A. External runtime asset in an inline style block
# ===========================================================================

STYLE_BLOCK_CASES = {
    # (name, html) -> must be REJECTED by the gate now.
    "style_block_url": (
        "<html><head><style>body{background:url(https://cdn.example.com/b.png)}"
        "</style></head><body>hi</body></html>"
    ),
    "style_block_at_import": (
        "<html><head><style>@import "
        "url('https://fonts.googleapis.com/css2?family=Inter');</style></head>"
        "<body>hi</body></html>"
    ),
    "style_block_font_face": (
        "<html><head><style>@font-face{font-family:X;"
        "src:url(https://cdn.example.com/x.woff2)}</style></head>"
        "<body>hi</body></html>"
    ),
    "style_block_srcset_style_block": (
        "<html><head><style>.hero{background-image:"
        "url(\"https://images.example.com/h.jpg\")}</style></head>"
        "<body>hi</body></html>"
    ),
}


@pytest.mark.parametrize("name,html", sorted(STYLE_BLOCK_CASES.items()))
def test_gate_rejects_external_asset_in_style_block(tmp_path, name, html):
    """The external-style scanner regression: an external runtime asset inside an inline
    ``<style>`` block must fail the self-contained gate (it is fetch-on-load
    render-critical, but lives in element TEXT, not a tag attribute)."""
    ws = tmp_path / name
    (ws / "dist").mkdir(parents=True)
    (ws / "dist" / "index.html").write_text(html, encoding="utf-8")

    report = check_self_contained(ws)
    assert not report.ok, f"{name}: external <style> dependency was NOT caught"
    kinds = {f.kind for f in report.findings}
    assert kinds & {
        "external_css_url", "external_css_import",
    }, f"{name}: unexpected finding kinds {kinds}"


def test_gate_still_allows_local_style_block(tmp_path):
    """A purely LOCAL inline style block must NOT be rejected (no over-block)."""
    ws = tmp_path / "ok"
    (ws / "dist").mkdir(parents=True)
    (ws / "dist" / "index.html").write_text(
        "<html><head><style>body{background:url(/local/bg.png)}"
        "@import url('/local/site.css');</style></head><body>hi</body></html>",
        encoding="utf-8",
    )
    assert check_self_contained(ws).ok


def test_gate_still_allows_external_link_and_favicon(tmp_path):
    """Non-render-critical external references (ordinary <a>, favicon,
    canonical) stay allowed -- the fix must not widen the block."""
    ws = tmp_path / "links"
    (ws / "dist").mkdir(parents=True)
    (ws / "dist" / "index.html").write_text(
        "<html><head>"
        "<link rel='icon' href='https://cdn.example.com/favicon.ico'>"
        "<link rel='canonical' href='https://example.com/'>"
        "</head><body><a href='https://example.com/x'>x</a></body></html>",
        encoding="utf-8",
    )
    assert check_self_contained(ws).ok


def test_normalize_then_check_still_gates_style_block(tmp_path):
    """The workspace orchestration (normalize THEN validate) used by build.py
    and revise.py must also reject the <style> block case end-to-end."""
    ws = tmp_path / "pipeline"
    (ws / "dist").mkdir(parents=True)
    (ws / "dist" / "index.html").write_text(
        "<html><head><style>body{background:"
        "url(https://cdn.example.com/b.png)}</style></head>"
        "<body>hi</body></html>",
        encoding="utf-8",
    )
    report = normalize_and_check_self_contained("p1", ws)
    assert not report.ok
    assert any(f.kind == "external_css_url" for f in report.findings)


def test_normalize_never_leaves_style_block_dependency(tmp_path):
    """A no-op normalize (nothing supported to vendor) must leave the external
    reference in place so the deterministic preflight rejects it (never a
    silently font-less/asset-less artifact)."""
    ws = tmp_path / "noop"
    (ws / "dist").mkdir(parents=True)
    html = ("<html><head><style>body{background:"
            "url(https://cdn.example.com/b.png)}</style></head><body>hi</body></html>")
    (ws / "dist" / "index.html").write_text(html, encoding="utf-8")
    normalize_self_contained("p1", ws)
    assert "cdn.example.com" in (ws / "dist" / "index.html").read_text(encoding="utf-8")


# ===========================================================================
# B. Runtime smoke observability — sanitized, actionable failure records
# ===========================================================================

class _ConsequenceBrowser:
    """Playwright-style browser: aborts off-origin subresources, then fires
    requestfailed + console error as CONSEQUENCES (order matters)."""

    def __init__(self, *, external_urls, final_url=None, status=200):
        self.external_urls = external_urls
        self.final_url = final_url
        self.status = status
        self.handlers = {}
        self.url = None
        self.aborted = []

    def new_context(self, **kwargs):
        return self

    def set_extra_http_headers(self, headers):
        pass

    def route(self, pattern, callback):
        self._route = callback

    def route_web_socket(self, pattern, callback):
        self._ws = callback

    def new_page(self):
        return self

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def _emit(self, event, *args):
        for cb in self.handlers.get(event, []):
            cb(*args)

    def goto(self, url, **kwargs):
        self._route(SimpleNamespace(
            request=SimpleNamespace(url=url, method="GET", redirected_from=None),
            continue_=lambda: None, abort=lambda: None))
        for ext in self.external_urls:
            aborted = {"v": False}
            self._route(SimpleNamespace(
                request=SimpleNamespace(url=ext, method="GET",
                                        redirected_from=None, resource_type="font"),
                continue_=lambda: None,
                abort=lambda a=aborted: a.__setitem__("v", True)))
            if aborted["v"]:
                self.aborted.append(ext)
                self._emit("requestfailed", SimpleNamespace(url=ext))
                self._emit("console", SimpleNamespace(type="error", text="net::ERR"))
        self.url = self.final_url or url
        return SimpleNamespace(status=self.status)

    def evaluate(self, script):
        return True

    def title(self):
        return "Home"

    def screenshot(self, **kwargs):
        return PNG

    def close(self):
        pass


def _run_consequence(urls, tmp_path, **browser_kw):
    holders = []

    def factory():
        b = _ConsequenceBrowser(external_urls=urls, **browser_kw)
        holders.append(b)
        return b

    return PreviewSmokeTester(factory, lambda _h: ["76.76.21.21"]).run(
        "https://cozy.vercel.app/", tmp_path, bypass_secret="SECRET-BYPASS"), holders


def test_smoke_records_actionable_sanitized_blocked_request(tmp_path):
    """The blocked request must be recorded with category/host/path/viewport,
    and the consequent request-failed + console errors must also be recorded."""
    result, _ = _run_consequence(
        ["https://fonts.googleapis.com/css2?family=Inter&token=leaky",
         "https://fonts.gstatic.com/s/inter/x.woff2"], tmp_path)
    assert not result.success
    assert result.error_code == "SMOKE_FAILED"

    records = result.data["failure_records"]
    blocked = [r for r in records if r["category"] == "blocked_request"]
    assert {r["viewport"] for r in blocked} == {"desktop", "mobile"}
    assert any(r["host"] == "fonts.googleapis.com" for r in blocked)
    assert any(r["host"] == "fonts.gstatic.com" for r in blocked)
    # The consequence failures are recorded too (so an operator sees the chain).
    assert {r["category"] for r in records} >= {
        "blocked_request", "request_failed", "console_error"}

    # Rolled-up classification tells the caller this is a deterministic defect.
    assert result.data["failure_classification"] == "artifact_defect"


def test_smoke_records_never_leak_query_or_secret(tmp_path):
    """HARD RULE: never persist/log query strings, tokens, or the bypass
    secret. The blocked asset host+path is kept; the query is NOT."""
    secret = "SECRET-BYPASS"
    result, _ = _run_consequence(
        ["https://cdn.example.com/a.css?token=SUPERSECRET&sig=abc"], tmp_path,
    )
    blob = json.dumps(result.data)
    assert "SUPERSECRET" not in blob
    assert "token=" not in blob
    assert "sig=" not in blob
    assert secret not in blob

    blocked = [r for r in result.data["failure_records"]
               if r["category"] == "blocked_request"]
    assert blocked and blocked[0]["host"] == "cdn.example.com"
    assert blocked[0]["path"] == "/a.css"
    assert "?" not in blocked[0]["path"]


def test_smoke_records_classify_transient_timeout(tmp_path):
    """A networkidle timeout is classified transient (bounded-retry eligible),
    distinct from a deterministic artifact defect."""
    class _TimeoutBrowser(_ConsequenceBrowser):
        def goto(self, url, **kwargs):
            raise TimeoutError("networkidle timeout")

    def factory():
        return _TimeoutBrowser(external_urls=[])
    result = PreviewSmokeTester(factory, lambda _h: ["76.76.21.21"]).run(
        "https://cozy.vercel.app/", tmp_path)
    assert not result.success
    cats = {r["category"] for r in result.data["failure_records"]}
    assert "networkidle_timeout" in cats
    # The exception TYPE is still persisted on the legacy line; the message is not.
    assert any("browser smoke failed: TimeoutError" in f
               for f in result.data["failures"])
    assert not any("networkidle timeout" in f for f in result.data["failures"])


def test_smoke_classification_precedence_never_downgrades_defect():
    """A transient error must never downgrade a deterministic artifact defect."""
    defect = _smoke_failure_record("blocked_request", viewport="desktop",
                                   host="cdn.example.com", path="/a.css")
    transient = _smoke_failure_record("networkidle_timeout", viewport="mobile")
    assert _smoke_classification([]) is None
    assert _smoke_classification([transient]) == "transient"
    assert _smoke_classification([defect, transient]) == "artifact_defect"
    assert _smoke_classification([defect, defect]) == "artifact_defect"


def test_sanitize_smoke_url_strips_query_and_credentials():
    host, path = _sanitize_smoke_url("https://user:pw@cdn.example.com/a/b.css?t=1#f")
    assert host == "cdn.example.com"
    assert path == "/a/b.css"
    assert _sanitize_smoke_url("not a url") == ("", "")
    assert _sanitize_smoke_url(None) == ("", "")


def test_smoke_failure_summary_is_bounded_and_secret_free():
    records = [_smoke_failure_record("blocked_request", viewport="desktop",
                                     host="h.example.com", path="/p.css")]
    lines = _smoke_failure_summary(records)
    assert len(lines) == 1 and "h.example.com" in lines[0]
    assert "?" not in lines[0]


# ===========================================================================
# C. Recovery loop — two consecutive Telegram turns after a smoke failure
# ===========================================================================

def _preview_ready_smoke_failing(tmp_path, *, classification,
                                 records=None, chat_id="555"):
    h = LocalR1Scenario(tmp_path, chat_id=chat_id)
    pid = h.seed_project("tokobunga")
    ws = h.runner.create_workspace(pid)
    make_preview_ready(h.store, pid, ws)
    h.set_smoke_failure(classification=classification, records=records)
    h.set_router_decision("PROJECT_TURN", target_project_name="tokobunga")
    return h, pid


def test_two_consecutive_turns_after_artifact_smoke_failure_make_progress(
        tmp_path, monkeypatch):
    """THE reported loop. Turn 1: smoke fails (artifact defect) -> ONE bounded
    meaningful status, preview never shown, turn still completes. Turn 2: the
    mandatory reconcile is NOT re-run; the revision actually runs a new build
    (frontend called, source_revision advanced)."""
    from tests.r1_harness import patch_qa_boundaries
    patch_qa_boundaries(monkeypatch)

    h, pid = _preview_ready_smoke_failing(
        tmp_path, classification="artifact_defect",
        records=[{"category": "blocked_request", "classification": "artifact_defect",
                  "viewport": "desktop", "host": "fonts.googleapis.com",
                  "path": "/css2"}])
    h.set_intent_response("INTAKE")

    # ---- Turn 1: greeting ----
    h.send_user_message("halo, gimana websitenya?")
    intent = h.preview_intent()
    assert h.current_lifecycle == "PREVIEW_READY", "preview must stay unshown"
    assert not h.latest_shown_preview(), "failed preview must NEVER be shown"
    assert intent.get("smoke_blocked") is True
    assert intent.get("smoke_attempts") == 1
    assert intent.get("failure_status_outcome") == "SENT"
    recon_after_turn1 = sum(1 for v in h.dispatch_events().values()
                            if v.get("action") == "reconcile_preview")
    assert recon_after_turn1 == 1
    # A bounded, meaningful status was sent (not the endless generic error).
    texts = [t for _, t in h.telegram.text_calls]
    assert any("Preview-nya belum bisa ditampilkan" in t for t in texts), texts

    # ---- Turn 2: a real revision request ----
    h.set_smoke_result(True)
    h.set_intent_response("REVISE")
    h.send_user_message("tolong ubah warnanya jadi hijau")

    # The mandatory reconcile did NOT run again for the dead operation.
    recon_after_turn2 = sum(1 for v in h.dispatch_events().values()
                            if v.get("action") == "reconcile_preview")
    assert recon_after_turn2 == 1, "reconcile must not re-run for a blocked op"

    # The repair path actually ran: a new source revision + a real frontend call.
    assert h.hermes.frontend_calls >= 1
    counters = h.revision_counters(pid)
    assert counters["source_revision"] >= 2
    assert counters["queued_revision_seq"] == 1
    # The repaired revision is a fresh tested PREVIEW_READY operation.
    assert h.current_lifecycle == "PREVIEW_READY"
    # A new revision must have CLEARED the blocked marker for the new operation.
    assert h.preview_intent(pid).get("smoke_blocked") is not True


def test_legacy_two_turn_loop_also_recovers(tmp_path, monkeypatch):
    """Same wedge/recovery contract on the LEGACY (no-router) turn path.

    The legacy path derives its project id as ``tg-<conversation_id>``; we put
    the project state under exactly that id (no registry entry needed) and
    detach the router so the loop takes the legacy branch.
    """
    from tests.r1_harness import patch_qa_boundaries
    patch_qa_boundaries(monkeypatch)

    h = LocalR1Scenario(tmp_path)
    legacy_pid = "tg-555"
    with h.store.acquire_writer(legacy_pid) as state:
        state.owner_id = h.principal_id
        state.roles["owner"] = h.principal_id
        state.channel = "telegram"
        state.conversation_id = h.chat_id
        state.brief = {"name": "tokobunga", "what": "toko bunga", "why": "jualan"}
        h.store.save(state)
    ws = h.runner.create_workspace(legacy_pid)
    make_preview_ready(h.store, legacy_pid, ws)
    h.set_smoke_failure(classification="artifact_defect")

    # Force the legacy path by detaching the conversation router.
    h._loop.conversations = None

    h.send_user_message("halo")
    state = h.store.load(legacy_pid)
    assert state.lifecycle == "PREVIEW_READY"
    assert state.deployment["preview_intent"].get("smoke_blocked") is True
    recon_after1 = sum(1 for v in state.dispatch_events.values()
                       if v.get("action") == "reconcile_preview")
    assert recon_after1 == 1

    h.send_user_message("tolong ganti warna")
    state2 = h.store.load(legacy_pid)
    recon_after2 = sum(1 for v in state2.dispatch_events.values()
                       if v.get("action") == "reconcile_preview")
    assert recon_after2 == 1, "legacy path must not re-run reconcile for a blocked op"


def test_transient_failure_gets_bounded_retry_then_stops_blocking(tmp_path):
    """A TRANSIENT smoke failure (no deterministic classification) is retried
    up to the bound; after the bound the gate stops blocking the turn."""
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="transient")
    h.set_router_decision("PROJECT_TURN")
    h.set_intent_response("INTAKE")
    h.set_intake_response(name="tokobunga")

    h.send_user_message("pesan 1")
    assert h.preview_intent().get("smoke_attempts") == 3
    assert h.preview_intent().get("smoke_blocked") is True
    assert not h.latest_shown_preview()

    recon_before = sum(1 for v in h.dispatch_events().values()
                       if v.get("action") == "reconcile_preview")
    assert recon_before == 1
    h.send_user_message("pesan setelah bound")
    recon_after = sum(1 for v in h.dispatch_events().values()
                      if v.get("action") == "reconcile_preview")
    assert recon_after == recon_before, "retry budget must be bounded"


def test_same_operation_retry_does_not_duplicate_remote_side_effects(tmp_path):
    """Bounded retry of the SAME (unshippable) operation must not create a
    SECOND Vercel project/deployment for the retry: identity is preserved and
    the retry reconciles by operation id rather than re-deploying."""
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="transient")
    h.set_router_decision("PROJECT_TURN")
    h.set_intent_response("INTAKE")

    # Turn 1 performs the ONE preview attempt for this operation.
    h.send_user_message("pesan 0")
    baseline = h.vercel_calls
    assert baseline["create_post"] >= 1

    # Subsequent retries of the SAME dead operation must NOT add remote creates.
    for i in range(1, 3):
        h.send_user_message(f"pesan {i}")
    after = h.vercel_calls
    assert after["create_post"] == baseline["create_post"], (
        "a retry must reconcile by identity, not create a second project")
    # And no preview was ever delivered.
    assert not h.latest_shown_preview()


def test_transient_smoke_retries_same_operation_without_duplicate_deploy(tmp_path):
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="transient")
    h.set_smoke_sequence([False, True])
    h.set_intent_response("INTAKE")
    h.send_user_message("retry the same preview")
    assert len(h.smoke.calls) == 2
    assert h.vercel_calls["deploy_post"] == 1
    assert h.vercel_calls["create_post"] == 1
    assert h.latest_shown_preview(pid)
    assert len(h.telegram.photo_calls) == 1


def test_hard_reconciliation_error_never_becomes_smoke_retry(tmp_path):
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="transient")
    h.preview.run_owned = lambda *args, **kwargs: OperationResult.fail(
        "PROJECT_RECONCILIATION_REQUIRED",
        error_code="PROJECT_RECONCILIATION_REQUIRED",
    )
    h.set_intent_response("INTAKE")
    h.send_user_message("first")
    assert not h.latest_shown_preview(pid)
    assert h.preview_intent(pid).get("smoke_attempts") is None
    assert h.hermes.frontend_calls == 0
    h.send_user_message("second")
    assert h.preview_intent(pid).get("smoke_attempts") is None
    assert h.hermes.frontend_calls == 0


def test_revision_smoke_failure_remains_recoverable_for_next_revision(tmp_path, monkeypatch):
    from tests.r1_harness import patch_qa_boundaries

    patch_qa_boundaries(monkeypatch)
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="artifact_defect")
    h.set_smoke_result(False)

    failed_revision = h.run_revision("repair the artifact")
    assert not failed_revision.success
    failed_state = h.store.load(pid)
    assert failed_state.lifecycle == "PREVIEW_READY"
    assert failed_state.revisions.source_revision == 2
    assert failed_state.revisions.qa_revision == 2
    assert failed_state.revisions.revision_seq == 1
    assert failed_state.deployment["preview_intent"]["smoke_blocked"] is True
    assert not h.latest_shown_preview(pid)

    h.set_smoke_result(True)
    successful_revision = h.run_revision("repair it again")
    assert successful_revision.success, successful_revision.error
    assert h.current_lifecycle == "PREVIEW_READY"
    assert h.store.load(pid).revisions.source_revision == 3
    assert len(h.telegram.photo_calls) == 1


def test_process_restart_preserves_blocked_intent(tmp_path):
    """A persisted rejected preview intent survives a full component restart."""
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="artifact_defect")
    h.set_router_decision("PROJECT_TURN")
    h.set_intent_response("INTAKE")
    h.set_intake_response(name="tokobunga")

    h.send_user_message("halo")
    assert h.preview_intent().get("smoke_blocked") is True

    h.restart_components()
    recon_before = sum(1 for v in h.dispatch_events().values()
                       if v.get("action") == "reconcile_preview")
    h.set_intent_response("REVISE")
    h.send_user_message("benerin dong warnanya")
    recon_after = sum(1 for v in h.dispatch_events().values()
                      if v.get("action") == "reconcile_preview")
    # Persisted smoke_blocked survived the restart -> no reconcile re-run.
    assert recon_after == recon_before


def test_failed_preview_can_never_be_approved_or_published(tmp_path):
    """A smoke-blocked preview has no latest_shown_preview, so approval (and
    therefore publication) must fail closed."""
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="artifact_defect")
    h.set_router_decision("PROJECT_TURN")
    h.set_intent_response("INTAKE")
    h.send_user_message("halo")
    assert not h.latest_shown_preview()
    result = h.approve()
    assert not result.success
    assert result.error_code == "NO_SHOWN_PREVIEW"


def test_no_duplicate_status_message_between_turns(tmp_path):
    """The bounded recovery status is sent AT MOST ONCE per blocked operation
    (not repeated on every subsequent turn)."""
    h, pid = _preview_ready_smoke_failing(tmp_path, classification="artifact_defect")
    h.set_router_decision("PROJECT_TURN")
    h.set_intent_response("INTAKE")
    h.set_intake_response(name="tokobunga")
    h.send_user_message("halo", event_id=900)
    marker = "Preview-nya belum bisa ditampilkan"
    first = sum(1 for _, t in h.telegram.text_calls if marker in t)
    assert first == 1
    h.send_user_message("halo", event_id=900)
    second = sum(1 for _, t in h.telegram.text_calls if marker in t)
    assert second == 1, "the bounded status must not be re-sent for the same op"
    assert len(h.telegram.photo_calls) == 0


def test_repair_revision_creates_new_snapshot_and_delivers(tmp_path, monkeypatch):
    """The safe repair path: after an artifact smoke failure, a revision
    rebuilds, re-runs QA, creates a NEW tested snapshot/operation, and smokes
    the NEW deployment. When that smoke passes, the preview is delivered
    exactly once -- proving the project is not permanently wedged and that the
    failed preview is never the one shown."""
    from tests.r1_harness import patch_qa_boundaries
    patch_qa_boundaries(monkeypatch)

    h, pid = _preview_ready_smoke_failing(tmp_path, classification="artifact_defect")
    h.set_intent_response("INTAKE")
    h.send_user_message("halo")
    blocked_intent = dict(h.preview_intent(pid))
    assert blocked_intent.get("smoke_blocked") is True
    assert not h.latest_shown_preview(pid)

    # The repair revision now produces an artifact that smokes clean.
    h.set_smoke_result(True)
    h.set_intent_response("REVISE")
    h.send_user_message("tolong benerin asetnya biar lokal")

    counters = h.revision_counters(pid)
    assert counters["source_revision"] >= 2, "repair must create new source bytes"
    assert counters["queued_revision_seq"] == 1
    # The NEW operation was smoked and delivered exactly once.
    shown = h.latest_shown_preview(pid)
    assert shown, "a passing repaired revision must be delivered"
    assert len(h.telegram.photo_calls) == 1, "exactly one preview photo per delivery"
    # The delivered preview is for the NEW operation, not the blocked one.
    assert shown.get("operation_id") != blocked_intent.get("operation_id")
    # The stale blocked marker is gone (new intent/operation).
    assert h.preview_intent(pid).get("smoke_blocked") is not True
