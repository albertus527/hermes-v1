"""SCRATCH reproduction (not a committed test): simulate the p7 smoke failure.

Models a realistic generated artifact that pulls a render-critical external
asset at runtime, and a browser that (like real Playwright) fires
requestfailed + console error as CONSEQUENCES of the aborted route.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deploy.adapters import PreviewSmokeTester  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"


class ConsequenceBrowser:
    """Browser that emulates Playwright event ordering when a route is aborted:

    * route handler aborts the external request -> route.abort()
    * Playwright then fires `requestfailed` for that request
    * the page JS (font/stylesheet consumer) logs a console error
    """

    def __init__(self, *, external_urls, final_url=None):
        self.external_urls = external_urls
        self.final_url = final_url
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
        # main document (same-origin) - allowed
        doc = SimpleNamespace(url=url, method="GET", redirected_from=None)
        self._route(SimpleNamespace(
            request=doc,
            continue_=lambda: None,
            abort=lambda: None,
        ))
        # external subresources -> route aborts, then requestfailed + console error
        for ext in self.external_urls:
            ext_req = SimpleNamespace(url=ext, method="GET", redirected_from=None)
            aborted = {"v": False}

            def _abort(a=aborted):
                a["v"] = True

            self._route(SimpleNamespace(
                request=ext_req,
                continue_=lambda: None,
                abort=_abort,
            ))
            if aborted["v"]:
                self.aborted.append(ext)
                self._emit("requestfailed", SimpleNamespace(url=ext))
                self._emit("console", SimpleNamespace(type="error", text="net::ERR_FAILED"))
        self.url = self.final_url or url
        return SimpleNamespace(status=200)

    def evaluate(self, script):
        return True

    def title(self):
        return "Home"

    def screenshot(self, **kwargs):
        return PNG

    def close(self):
        pass


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_scratch_out")
    browsers = []

    def factory():
        b = ConsequenceBrowser(external_urls=[
            "https://fonts.googleapis.com/css2?family=Inter",
            "https://fonts.gstatic.com/s/inter/x.woff2",
        ])
        browsers.append(b)
        return b

    tester = PreviewSmokeTester(factory, lambda _h: ["76.76.21.21"])
    result = tester.run("https://cozy.vercel.app/", out, bypass_secret="sec")
    print("success:", result.success)
    print("error_code:", result.error_code)
    print("failures:")
    for f in result.data["failures"]:
        print("  -", f)
    print("blocked hosts:", [u.split('/')[2] for u in browsers[0].aborted] if browsers else [])
    import json
    print("classification:", result.data.get("failure_classification"))
    print("records:")
    for r in result.data.get("failure_records", []):
        print("  ", json.dumps(r))
    print("summary:", result.data.get("failure_summary"))
    print("query leak?", any("?" in str(r.get("path", "")) for r in result.data.get("failure_records", [])))


if __name__ == "__main__":
    main()
