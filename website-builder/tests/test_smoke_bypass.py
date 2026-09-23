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

from app.deploy.adapters import PreviewSmokeTester  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"


class RecordingBrowser:
    """Minimal Playwright-style Browser that records the extra HTTP headers and
    the final URL/title it 'navigates' to."""

    def __init__(self, *, final_url=None, title="Home", status=200):
        self.final_url = final_url
        self.title_text = title
        self.status = status
        self.extra_headers = None
        self.options = {}
        self.url = None

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

    def goto(self, url, **kwargs):
        self.url = self.final_url or url
        request = SimpleNamespace(url=url, method="GET", redirected_from=None)
        route = SimpleNamespace(request=request, continue_=lambda: None, abort=lambda: None)
        self._route(route)
        return SimpleNamespace(status=self.status)

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
