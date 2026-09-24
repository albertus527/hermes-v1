"""PHASE F — smoke bypass integration + Vercel auth-wall detection.

The ``PreviewSmokeTester`` accepts a project-specific bypass secret by
dependency injection; it sends ``x-vercel-protection-bypass`` and
``x-vercel-set-bypass-cookie: true`` ONLY for ``*.vercel.app`` origins, and it
explicitly fails when the page it lands on is Vercel's auth wall (HTTP 200 is
NOT enough).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deploy.adapters import (  # noqa: E402
    PreviewSmokeTester,
    _is_same_origin_preview_redirect,
)

PNG = b"\x89PNG\r\n\x1a\nfake"


class RecordingBrowser:
    """Minimal Playwright-style Browser that records the extra HTTP headers and
    the final URL/title it 'navigates' to.

    ``redirect_to``/``redirected_from`` emulate a Playwright redirect chain: the
    FIRST request carries ``redirected_from``; the FOLLOW-UP request uses the
    destination URL. ``redirect_hops`` chains several hops; ``loop`` repeats a
    single hop forever (bounded by the tester's redirect counter).
    """

    def __init__(self, *, final_url=None, title="Home", status=200,
                 redirect_to=None, redirected_from=None, redirect_hops=None,
                 redirect_method="GET", loop=False, loop_hops=20):
        self.final_url = final_url
        self.title_text = title
        self.status = status
        self.redirect_to = redirect_to
        self.redirected_from = redirected_from
        self.redirect_hops = redirect_hops
        self.redirect_method = redirect_method
        self.loop = loop
        self.loop_hops = loop_hops
        self.extra_headers = None
        self.options = {}
        self.url = None
        self.blocked_count = 0
        self.allowed_count = 0
        self.methods = []

    # -- Playwright surface used by PreviewSmokeTester --------------------
    def new_context(self, **kwargs):
        self.options = kwargs
        return self

    def set_extra_http_headers(self, headers):
        self.extra_headers = dict(headers)

    def route(self, pattern, callback):
        self._route = callback

    def route_web_socket(self, pattern, callback):
        self._ws = callback

    def new_page(self):
        return self

    def on(self, event, callback):
        pass

    def _dispatch(self, url, method, redirected_from):
        request = SimpleNamespace(url=url, method=method,
                                  redirected_from=redirected_from)
        route = SimpleNamespace(
            request=request,
            continue_=lambda: setattr(self, 'allowed_count', self.allowed_count + 1),
            abort=lambda: setattr(self, 'blocked_count', self.blocked_count + 1),
        )
        self._route(route)

    def goto(self, url, **kwargs):
        if self.redirect_hops is not None:
            self._goto_chain(url, self.redirect_hops)
        elif self.redirect_to is not None:
            self._goto_chain(url, [(self.redirect_to, self.redirected_from)])
        elif self.loop:
            self._goto_chain(url, [(url, url)] * self.loop_hops)
        else:
            self._dispatch(url, "GET", None)
        self.url = self.final_url or (self.redirect_to or url)
        return SimpleNamespace(status=self.status)

    def _goto_chain(self, url, hops):
        # Initial navigation (never redirected).
        self._dispatch(url, "GET", None)
        previous = SimpleNamespace(url=url)
        for target, _explicit in hops:
            # A redirected request carries the request object it followed.
            self._dispatch(target, self.redirect_method, previous)
            previous = SimpleNamespace(url=target)

    def evaluate(self, script):
        return True

    def title(self):
        return self.title_text

    def screenshot(self, **kwargs):
        return PNG

    def close(self):
        pass


def _factory(browsers):
    def factory():
        b = RecordingBrowser()
        browsers.append(b)
        return b
    return factory


def test_bypass_headers_sent_to_vercel_preview(tmp_path):
    """Test 4/13: desktop + mobile both send the bypass headers for a
    protected *.vercel.app preview."""
    browsers = []
    tester = PreviewSmokeTester(_factory(browsers), lambda _: ["8.8.8.8"])
    result = tester.run("https://cozy.vercel.app/", tmp_path,
                        bypass_secret="test-secret-value")
    assert result.success
    assert len(browsers) == 2  # desktop + mobile
    for b in browsers:
        assert b.extra_headers == {
            "x-vercel-protection-bypass": "test-secret-value",
            "x-vercel-set-bypass-cookie": "true",
        }
    # The secret must not leak into the result data.
    assert "test-secret-value" not in str(result.data)


def test_bypass_not_sent_to_non_vercel_url(tmp_path):
    """Test 10: a non-Vercel URL never receives the bypass header, even when a
    secret is configured."""
    browsers = []
    tester = PreviewSmokeTester(_factory(browsers), lambda _: ["8.8.8.8"])
    result = tester.run("https://example.com/", tmp_path, bypass_secret="test-secret-value")
    # example.com passes the origin check (https, not vercel.app) -> smoke runs
    # but WITHOUT the bypass header.
    for b in browsers:
        assert b.extra_headers is None


def test_no_bypass_secret_no_headers(tmp_path):
    browsers = []
    tester = PreviewSmokeTester(_factory(browsers), lambda _: ["8.8.8.8"])
    result = tester.run("https://cozy.vercel.app/", tmp_path)
    assert result.success
    for b in browsers:
        assert b.extra_headers is None


def test_auth_wall_login_path_fails_closed(tmp_path):
    """Test 7: landing on /login is a Vercel auth wall -> sanitized failure."""
    browsers = []
    def factory():
        b = RecordingBrowser(final_url="https://vercel.com/login", title="Login – Vercel")
        browsers.append(b)
        return b
    tester = PreviewSmokeTester(factory, lambda _: ["8.8.8.8"])
    result = tester.run("https://cozy.vercel.app/", tmp_path, bypass_secret="wrong-secret")
    assert not result.success
    assert result.error_code == "VERCEL_BYPASS_AUTH_FAILED"
    assert "wrong-secret" not in str(result.data)


def test_auth_wall_api_sso_detected(tmp_path):
    browsers = []
    def factory():
        b = RecordingBrowser(final_url="https://cozy.vercel.app/api/sso", title="x")
        browsers.append(b)
        return b
    tester = PreviewSmokeTester(factory, lambda _: ["8.8.8.8"])
    result = tester.run("https://cozy.vercel.app/", tmp_path, bypass_secret="sec")
    assert not result.success
    assert result.error_code == "VERCEL_BYPASS_AUTH_FAILED"


def test_vercel_login_title_detected(tmp_path):
    browsers = []
    def factory():
        b = RecordingBrowser(final_url="https://cozy.vercel.app/", title="Login – Vercel")
        browsers.append(b)
        return b
    tester = PreviewSmokeTester(factory, lambda _: ["8.8.8.8"])
    result = tester.run("https://cozy.vercel.app/", tmp_path, bypass_secret="sec")
    assert not result.success
    assert result.error_code == "VERCEL_BYPASS_AUTH_FAILED"


def test_bypass_header_helpers_are_scoped():
    assert PreviewSmokeTester._bypass_headers("s", "https://x.vercel.app/") == {
        "x-vercel-protection-bypass": "s",
        "x-vercel-set-bypass-cookie": "true",
    }
    # Non-Vercel origin -> no headers.
    assert PreviewSmokeTester._bypass_headers("s", "https://example.com/") == {}
    # No secret -> no headers.
    assert PreviewSmokeTester._bypass_headers(None, "https://x.vercel.app/") == {}


# ===========================================================================
# Bounded SAME-ORIGIN redirect handling.
#
# LIVE EVIDENCE: a protected *.vercel.app preview answer, with a valid bypass,
# is ``HTTP/2 307  location: /`` for ``https://x.vercel.app`` -> canonical
# ``https://x.vercel.app/``. That safe hop must not be reported as a redirect
# / navigation failure, while every UNSAFE redirect stays blocked.
# ===========================================================================

def _run(url, tmp_path, *, resolver=None, browser_kwargs=None, **run_kwargs):
    browsers = []
    def factory():
        b = RecordingBrowser(**(browser_kwargs or {}))
        browsers.append(b)
        return b
    result = PreviewSmokeTester(
        factory, resolver or (lambda _: ["8.8.8.8"])
    ).run(url, tmp_path, **run_kwargs)
    return result, browsers


REDIRECT_307 = {"redirect_to": "https://test.vercel.app/",
                "redirected_from": "https://test.vercel.app"}


def test_root_to_slash_307_same_origin_redirect_succeeds(tmp_path):
    """The exact LIVE case: ``/`` root 307 with ``location: /`` succeeds and is
    NOT reported as a redirect/navigation failure."""
    result, browsers = _run("https://test.vercel.app", tmp_path,
                            browser_kwargs=REDIRECT_307)
    assert result.success, result.data.get("failures")
    assert len(browsers) == 2  # desktop + mobile both pass
    assert all(b.blocked_count == 0 for b in browsers)
    assert all(b.allowed_count == 2 for b in browsers)  # nav + redirected hop


def test_same_origin_redirected_request_is_allowed(tmp_path):
    """A same-origin redirected request is continued (not aborted) for both
    viewports. The final URL here differs by more than a trailing slash, so the
    smoke still flags it -- but the REDIRECTED REQUEST itself must be allowed,
    never counted as a blocked request."""
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"redirect_to": "https://test.vercel.app/index.html",
                        "redirected_from": "https://test.vercel.app",
                        "final_url": "https://test.vercel.app/index.html"},
    )
    assert len(browsers) == 2
    assert all(b.allowed_count == 2 for b in browsers)
    assert all(b.blocked_count == 0 for b in browsers)
    assert not any("blocked request" in f for f in result.data["failures"])


def test_cross_origin_redirect_blocked(tmp_path):
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"redirect_to": "https://evil.example.com/",
                        "redirected_from": "https://test.vercel.app",
                        "final_url": "https://evil.example.com/"},
    )
    assert not result.success
    assert any("blocked request" in f for f in result.data["failures"])
    assert any("navigation failed/redirected" in f for f in result.data["failures"])


def test_https_to_http_downgrade_redirect_blocked(tmp_path):
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"redirect_to": "http://test.vercel.app/",
                        "redirected_from": "https://test.vercel.app",
                        "final_url": "http://test.vercel.app/"},
    )
    assert not result.success
    assert any("blocked request" in f for f in result.data["failures"])


def test_redirect_to_private_ip_blocked(tmp_path):
    """The redirect destination resolves only to a private address -> blocked,
    even though scheme/host are otherwise identical."""
    def resolver(_host):
        return ["127.0.0.1"]
    result, browsers = _run("https://test.vercel.app", tmp_path,
                            resolver=resolver, browser_kwargs=REDIRECT_307)
    assert not result.success
    assert any("blocked request" in f for f in result.data["failures"])


def test_within_bound_same_origin_redirects_succeed(tmp_path):
    """A few same-origin hops (<= bound) are all allowed: only the trailing
    slash ever changes, so no hop is blocked and navigation is not flagged."""
    hops = [("https://test.vercel.app/", "https://test.vercel.app"),
            ("https://test.vercel.app/", "https://test.vercel.app/"),
            ("https://test.vercel.app/", "https://test.vercel.app/")]
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"redirect_hops": hops,
                        "final_url": "https://test.vercel.app/"},
    )
    assert result.success, result.data.get("failures")
    assert all(b.allowed_count == 4 for b in browsers)  # nav + 3 hops
    assert 3 <= PreviewSmokeTester.max_redirects <= 10


def test_excessive_redirect_depth_blocked(tmp_path):
    """A redirect LOOP (more hops than the explicit bound) is capped: the hop
    that exceeds ``max_redirects`` is blocked and the smoke fails closed."""
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"loop": True,
                        "loop_hops": PreviewSmokeTester.max_redirects + 5},
    )
    assert not result.success
    assert any("blocked request" in f for f in result.data["failures"])
    # Exactly the bounded number of hops were allowed; the rest were blocked.
    assert all(b.allowed_count == PreviewSmokeTester.max_redirects + 1 for b in browsers)
    assert PreviewSmokeTester.max_redirects <= 10


def test_redirect_with_unsupported_method_blocked(tmp_path):
    """POST redirects are never followed (only GET/HEAD)."""
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"redirect_to": "https://test.vercel.app/",
                        "redirected_from": "https://test.vercel.app",
                        "redirect_method": "POST"},
    )
    assert not result.success
    assert any("blocked request" in f for f in result.data["failures"])


def test_redirect_with_userinfo_blocked(tmp_path):
    """A credential-bearing redirect destination is never followed."""
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"redirect_to": "https://user:pw@test.vercel.app/",
                        "redirected_from": "https://test.vercel.app",
                        "final_url": "https://user:pw@test.vercel.app/"},
    )
    assert not result.success
    assert any("blocked request" in f for f in result.data["failures"])


def test_final_url_trailing_slash_only_accepted(tmp_path):
    """Canonical equivalence: final ``.../`` vs requested ``...`` is NOT a
    navigation failure (no redirect hop needed, just the canonical root)."""
    result, browsers = _run(
        "https://test.vercel.app",
        tmp_path,
        browser_kwargs={"final_url": "https://test.vercel.app/"},
    )
    assert result.success, result.data.get("failures")


def test_final_url_different_path_still_redirect_failure(tmp_path):
    """A final URL that differs by MORE than the trailing slash stays a
    navigation failure."""
    result, browsers = _run(
        "https://test.vercel.app/",
        tmp_path,
        browser_kwargs={"final_url": "https://test.vercel.app/other"},
    )
    assert not result.success
    assert any("navigation failed/redirected" in f for f in result.data["failures"])


def test_canonical_equivalence_helper():
    canon = PreviewSmokeTester._canonical
    assert canon("https://x.vercel.app") == canon("https://x.vercel.app/")
    assert canon("https://x.vercel.app/a/") == canon("https://x.vercel.app/a")
    # Query strings are significant.
    assert canon("https://x.vercel.app/?a=1") != canon("https://x.vercel.app/")
    # Cross-host / downgrade are never "equivalent".
    assert canon("https://x.vercel.app/") != canon("https://y.vercel.app/")
    assert canon("http://x.vercel.app/") is None


def test_same_origin_redirect_guards_are_fail_closed():
    """Unit-level: only a bounded, https, same-host, public-IP, credential-free
    GET/HEAD hop is sanctioned."""
    guard = _is_same_origin_preview_redirect
    public = lambda _h: ["8.8.8.8"]
    private = lambda _h: ["10.0.0.5"]
    resolver = lambda *_: public

    assert guard("https://x.vercel.app", "https://x.vercel.app/",
                 dest_resolver=public, max_redirects=5)
    assert guard("https://x.vercel.app", "https://x.vercel.app/a",
                 dest_resolver=public, max_redirects=5)
    # cross-origin host
    assert not guard("https://x.vercel.app/", "https://y.vercel.app/",
                     dest_resolver=public, max_redirects=5)
    # http downgrade
    assert not guard("https://x.vercel.app/", "http://x.vercel.app/",
                     dest_resolver=public, max_redirects=5)
    # non-default port
    assert not guard("https://x.vercel.app/", "https://x.vercel.app:444/",
                     dest_resolver=public, max_redirects=5)
    # credential-bearing destination
    assert not guard("https://x.vercel.app/", "https://u:p@x.vercel.app/",
                     dest_resolver=public, max_redirects=5)
    # fragment trickery
    assert not guard("https://x.vercel.app/", "https://x.vercel.app/#x",
                     dest_resolver=public, max_redirects=5)
    # private destination
    assert not guard("https://x.vercel.app/", "https://x.vercel.app/",
                     dest_resolver=private, max_redirects=5)
    # empty resolution fails closed
    assert not guard("https://x.vercel.app/", "https://x.vercel.app/",
                     dest_resolver=lambda _h: [], max_redirects=5)
    # depth bound exhausted
    assert not guard("https://x.vercel.app/", "https://x.vercel.app/",
                     dest_resolver=public, max_redirects=0)
    # control/space smuggling
    assert not guard("https://x.vercel.app/", "https://x.vercel.app/ x",
                     dest_resolver=public, max_redirects=5)
