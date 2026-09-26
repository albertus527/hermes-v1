"""Self-contained preview gate tests (Website Builder R1).

Covers the required matrix A-J:

A. Google Fonts normalization (Cormorant Garamond + Crimson Pro)
B. Self-contained HTML checks (script / stylesheet / anchor)
C. CSS checks (@import / url(...) / local)
D. Image checks
E. Font checks
F. Redirect + security policy for font vendoring
G. Artifact identity / deterministic fingerprint
H. QA integration (gate blocks the ``checked`` binding)
I. P7-style recovery end to end through the normal pipeline
J. No regression (normalization is a no-op on a local-only artifact)

No real network, no real Vercel, no real Telegram. Font payloads come from the
deterministic fixture seam (``VendoredFonts``), and the security tests use
injected fake HTTPS connections.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The R1 simulation harness lives beside this module; mirror the sibling R1
# test modules' sys.path bootstrap so ``from r1_harness import ...`` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.selfcontained import (
    EXTERNAL_RUNTIME_DEPENDENCY,
    GOOGLE_FONTS_ASSET_HOSTS,
    GOOGLE_FONTS_CSS_HOSTS,
    FontVendorError,
    VendoredFonts,
    _fetch,
    check_self_contained,
    fixture_payloads,
    normalize_and_check_self_contained,
    normalize_artifact,
    parse_google_font_faces,
)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Route every font vendoring fetch through the deterministic fixture.

    The production wrapper (``normalize_and_check_self_contained``) does not
    accept a fixture argument, so the seam is patched at the layer boundary.
    """
    from app.core import selfcontained as module

    original = module.normalize_artifact

    def with_fixture(workspace, **kwargs):
        kwargs.setdefault("vendored", _fixture())
        return original(workspace, **kwargs)

    monkeypatch.setattr(module, "normalize_artifact", with_fixture)
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Google-identifier families used by the real p7 artifact.
FAMILY_HEADING = "Cormorant Garamond"
FAMILY_BODY = "Crimson Pro"

_GOOGLE_CSS_URL = (
    "https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,700"
    "&family=Crimson+Pro:wght@400;600&display=swap"
)
_GF_ASSET_1 = "https://fonts.gstatic.com/s/cormorantgaramond/v16/co3bmX5slCNuHLi8bLeY9MK7whWMhyjYqQkL.woff2"
_GF_ASSET_2 = "https://fonts.gstatic.com/s/cormorantgaramond/v16/co3bmX5slCNuHLi8bLeY9MK7whWMhyjYqgsL.woff2"
_GF_ASSET_3 = "https://fonts.gstatic.com/s/crimsonpro/v23/q5uDsoa5M_tv7IihmnkabARuoZg.woff2"


def _woff2(seed: bytes, size: int = 512) -> bytes:
    """Deterministic minimal WOFF2-shaped payload (magic + padding)."""
    return b"wOF2" + bytes(8) + (seed * (size // len(seed) + 1))[:size]


_FAKE_FONT_BYTES = {
    _GF_ASSET_1: _woff2(b"\x01"),
    _GF_ASSET_2: _woff2(b"\x02"),
    _GF_ASSET_3: _woff2(b"\x03"),
}

# Shaped exactly like the Google Fonts CSS2 response: per-subset @font-face
# blocks with unicode-range.
_FAKE_GOOGLE_CSS = f"""
/* cyrillic-ext */
@font-face {{
  font-family: 'Cormorant Garamond';
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url({_GF_ASSET_1}) format('woff2');
  unicode-range: U+0460-052F, U+1C80-1C88;
}}
/* latin */
@font-face {{
  font-family: 'Cormorant Garamond';
  font-style: normal;
  font-weight: 700;
  font-display: swap;
  src: url({_GF_ASSET_2}) format('woff2');
  unicode-range: U+0000-00FF, U+0131;
}}
/* latin */
@font-face {{
  font-family: 'Crimson Pro';
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url({_GF_ASSET_3}) format('woff2');
  unicode-range: U+0000-00FF;
}}
"""


def _fixture() -> VendoredFonts:
    return fixture_payloads(_FAKE_GOOGLE_CSS, _FAKE_FONT_BYTES)


def _p7_index_html() -> str:
    """The exact shape the real p7 FRONTEND emitted."""
    return f"""<!doctype html>
<html lang="id">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="{_GOOGLE_CSS_URL}" rel="stylesheet" />
    <title>Rumah Baca</title>
  </head>
  <body>
    <div id="root"></div>
    <a href="https://instagram.com/rumahbaca">Instagram</a>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""


def _make_p7_workspace(root: Path) -> Path:
    """Materialize a p7-shaped artifact: dist present, external Google Fonts."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "dist").mkdir(parents=True, exist_ok=True)
    (root / "src" / "App.tsx").write_text(
        "export default () => null\n", encoding="utf-8")
    (root / "index.html").write_text(_p7_index_html(), encoding="utf-8")
    (root / "dist" / "index.html").write_text(_p7_index_html(), encoding="utf-8")
    (root / "dist" / "assets").mkdir(parents=True, exist_ok=True)
    (root / "dist" / "assets" / "index.js").write_text(
        "console.log('ok')\n", encoding="utf-8")
    (root / "design-dna.json").write_text(json.dumps({
        "version": 1,
        "typography": {"heading_font": FAMILY_HEADING, "body_font": FAMILY_BODY},
    }), encoding="utf-8")
    return root


@pytest.fixture
def p7_workspace(tmp_path: Path) -> Path:
    return _make_p7_workspace(tmp_path / "proj")


def _write_p7_site(workspace: Path) -> None:
    """Harness materialization seam: FRONTEND's generated output is a
    p7-shaped artifact whose typography is loaded from Google Fonts."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "dist").mkdir(exist_ok=True)
    (workspace / "src" / "App.tsx").write_bytes(b"export default () => null\n")
    (workspace / "index.html").write_text(_p7_index_html(), encoding="utf-8")
    (workspace / "dist" / "index.html").write_text(_p7_index_html(),
                                                   encoding="utf-8")
    (workspace / "design-dna.json").write_text(json.dumps({
        "version": 1,
        "brand_personality": "soft, bright, calm",
        "typography": {"heading_font": FAMILY_HEADING, "body_font": FAMILY_BODY},
    }, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# A. Google Fonts normalization
# ---------------------------------------------------------------------------

class TestGoogleFontsNormalization:
    def test_external_reference_fails_preflight_before_normalization(self, p7_workspace):
        report = check_self_contained(p7_workspace)
        assert report.ok is False
        assert report.error_code == EXTERNAL_RUNTIME_DEPENDENCY
        hosts = {f.host for f in report.findings}
        assert "fonts.googleapis.com" in hosts

    def test_normalization_vendors_fonts_and_preserves_families(self, p7_workspace):
        result = normalize_artifact(p7_workspace, vendored=_fixture())

        assert result.changed is True
        assert FAMILY_HEADING in result.families
        assert FAMILY_BODY in result.families

        font_files = [n for n in result.assets if n.startswith("fonts/")]
        assert font_files, "expected local font files"
        assert all(name.endswith(".woff2") for name in font_files)
        # Every payload must be the real WOFF2 magic bytes.
        for name in font_files:
            assert result.assets[name].startswith(b"wOF2")

    def test_external_stylesheet_and_gstatic_removed_from_emitted_html(self, p7_workspace):
        normalize_artifact(p7_workspace, vendored=_fixture())
        emitted = (p7_workspace / "dist" / "index.html").read_text(encoding="utf-8")

        assert "fonts.googleapis.com" not in emitted
        assert "fonts.gstatic.com" not in emitted
        assert "@font-face" in emitted
        assert 'font-family: "Cormorant Garamond"' in emitted
        assert 'font-family: "Crimson Pro"' in emitted
        assert 'format("woff2")' in emitted
        assert "font-display: swap" in emitted
        # Local, root-absolute asset paths (what the deployed site serves).
        assert 'url("/fonts/cormorant-garamond-400-normal-' in emitted

    def test_normalization_keeps_ordinary_navigation_links(self, p7_workspace):
        normalize_artifact(p7_workspace, vendored=_fixture())
        emitted = (p7_workspace / "dist" / "index.html").read_text(encoding="utf-8")
        assert 'href="https://instagram.com/rumahbaca"' in emitted

    def test_normalized_artifact_passes_preflight(self, p7_workspace):
        report = normalize_and_check_self_contained("proj", p7_workspace)
        assert report.ok is True, report.error_text()

    def test_html_entity_obfuscated_url_is_detected(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "dist").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(
            '<link rel="stylesheet" '
            'href="https://fonts.googleapis.com/css2?family=X&amp;display=swap">',
            encoding="utf-8",
        )
        report = check_self_contained(ws)
        assert report.ok is False
        assert report.findings[0].kind == "external_stylesheet"
        assert report.findings[0].host == "fonts.googleapis.com"


# ---------------------------------------------------------------------------
# B. Self-contained HTML checks
# ---------------------------------------------------------------------------

class TestHtmlChecks:
    def _ws(self, tmp_path, body: str) -> Path:
        ws = tmp_path / f"ws-{abs(hash(body)):x}"
        (ws / "dist").mkdir(parents=True, exist_ok=True)
        (ws / "dist" / "index.html").write_text(
            f"<!doctype html><html><head></head><body>{body}</body></html>",
            encoding="utf-8",
        )
        return ws

    def test_external_script_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '<script src="https://cdn.example.com/a.js"></script>'))
        assert report.ok is False
        assert report.findings[0].kind == "external_script"
        assert report.findings[0].host == "cdn.example.com"
        assert report.findings[0].file == "index.html"

    def test_external_stylesheet_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '<link rel="stylesheet" href="https://cdn.example.com/a.css">'))
        assert report.ok is False
        assert report.findings[0].kind == "external_stylesheet"
        assert report.findings[0].host == "cdn.example.com"

    def test_protocol_relative_stylesheet_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '<link rel="stylesheet" href="//cdn.example.com/a.css">'))
        assert report.ok is False
        assert report.findings[0].host == "cdn.example.com"

    def test_normal_anchor_passes(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '<a href="https://example.com">Visit</a>'))
        assert report.ok is True, report.error_text()

    def test_mailto_and_tel_pass(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '<a href="mailto:hi@example.com">mail</a>'
            '<a href="tel:+628123">call</a>'))
        assert report.ok is True

    def test_canonical_and_manifest_links_pass(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '<link rel="canonical" href="https://example.com/">'
            '<link rel="manifest" href="/manifest.webmanifest">'))
        assert report.ok is True

    def test_local_assets_pass(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '<script src="/assets/app.js"></script>'
            '<link rel="stylesheet" href="/assets/app.css">'
            '<img src="/images/hero.webp" alt="hero">'
            '<link rel="icon" href="/favicon.svg">'))
        assert report.ok is True, report.error_text()

    def test_inline_script_remote_import_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '<script type="module">'
            'import { x } from "https://cdn.skypack.dev/pkg";'
            '</script>'))
        assert report.ok is False
        assert report.findings[0].kind == "external_js_import"

    def test_preload_render_critical_fails_but_image_preload_passes(self, tmp_path):
        fail = check_self_contained(self._ws(
            tmp_path, '<link rel="preload" as="script" href="https://cdn.example.com/a.js">'))
        assert fail.ok is False

        ok = check_self_contained(self._ws(
            tmp_path, '<link rel="preload" as="image" href="https://cdn.example.com/a.webp">'))
        assert ok.ok is True


# ---------------------------------------------------------------------------
# C. CSS checks
# ---------------------------------------------------------------------------

class TestCssChecks:
    def _ws(self, tmp_path, css: str) -> Path:
        ws = tmp_path / "ws"
        (ws / "dist" / "assets").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text("<!doctype html><html></html>",
                                                encoding="utf-8")
        (ws / "dist" / "assets" / "index.css").write_text(css, encoding="utf-8")
        return ws

    def test_external_at_import_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '@import url("https://fonts.googleapis.com/css2?family=X");'))
        assert report.ok is False
        assert report.findings[0].kind == "external_css_import"

    def test_bare_string_at_import_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '@import "https://cdn.example.com/a.css";'))
        assert report.ok is False
        assert report.findings[0].kind == "external_css_import"

    def test_external_background_image_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '.hero { background-image: url("https://cdn.example.com/hero.avif"); }'))
        assert report.ok is False
        assert report.findings[0].kind == "external_css_url"
        assert report.findings[0].host == "cdn.example.com"

    def test_local_css_url_passes(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '.hero { background-image: url("/assets/a.webp"); }'
            '@import "/assets/base.css";'))
        assert report.ok is True, report.error_text()

    def test_data_uri_and_fragment_pass(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '.a { background: url("data:image/svg+xml;base64,AAAA"); }'
            '.b { fill: url(#gradient); }'))
        assert report.ok is True

    def test_inline_style_attribute_external_url_fails(self, tmp_path):
        ws = tmp_path / "ws-inline-style"
        (ws / "dist").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(
            '<div style="background:url(https://cdn.example.com/a.png)"></div>',
            encoding="utf-8")
        report = check_self_contained(ws)
        assert report.ok is False
        assert report.findings[0].kind == "external_css_url"


# ---------------------------------------------------------------------------
# D. Image checks
# ---------------------------------------------------------------------------

class TestImageChecks:
    def _ws(self, tmp_path, body: str) -> Path:
        ws = tmp_path / f"ws-{abs(hash(body)):x}"
        (ws / "dist").mkdir(parents=True, exist_ok=True)
        (ws / "dist" / "index.html").write_text(
            f"<!doctype html><html><body>{body}</body></html>", encoding="utf-8")
        return ws

    def test_remote_img_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '<img src="https://images.unsplash.com/photo-1" alt="x">'))
        assert report.ok is False
        assert report.findings[0].kind == "external_image"
        assert report.findings[0].host == "images.unsplash.com"

    def test_local_img_passes(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '<img src="/images/a.webp" alt="x">'))
        assert report.ok is True

    def test_remote_srcset_candidate_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '<img src="/a.webp" srcset="/a.webp 1x, https://cdn.example.com/a2.webp 2x" alt="x">'))
        assert report.ok is False
        assert report.findings[0].kind == "external_image"

    def test_remote_source_tag_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '<picture><source srcset="https://cdn.example.com/h.webp" type="image/webp">'
            '<img src="/h.png" alt="h"></picture>'))
        assert report.ok is False

    def test_remote_favicon_is_not_render_critical(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path, '<link rel="icon" href="https://cdn.example.com/fav.svg">'))
        assert report.ok is True


# ---------------------------------------------------------------------------
# E. Font checks
# ---------------------------------------------------------------------------

class TestFontChecks:
    def _ws(self, tmp_path, css: str) -> Path:
        ws = tmp_path / "ws"
        (ws / "dist" / "assets").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text("<!doctype html><html></html>",
                                                encoding="utf-8")
        (ws / "dist" / "assets" / "fonts.css").write_text(css, encoding="utf-8")
        return ws

    def test_external_font_url_source_fails(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '@font-face { font-family: "X"; src: url("https://fonts.gstatic.com/s/x.woff2") format("woff2"); }'))
        assert report.ok is False
        assert report.findings[0].kind == "external_css_url"
        assert report.findings[0].host == "fonts.gstatic.com"

    def test_local_font_face_passes(self, tmp_path):
        report = check_self_contained(self._ws(
            tmp_path,
            '@font-face { font-family: "Cormorant Garamond"; src: url("/fonts/cg.woff2") format("woff2"); }'))
        assert report.ok is True, report.error_text()

    def test_remote_svg_reference_fails(self, tmp_path):
        ws = tmp_path / "ws-svg"
        (ws / "dist" / "assets").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(
            "<!doctype html><html></html>", encoding="utf-8")
        (ws / "dist" / "assets" / "logo.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<image href="https://cdn.example.com/logo.png"/>'
            '<rect style="fill:url(https://cdn.example.com/p.svg#g)"/>'
            "</svg>", encoding="utf-8")
        report = check_self_contained(ws)
        assert report.ok is False
        assert report.findings[0].file == "assets/logo.svg"

    def test_local_svg_reference_passes(self, tmp_path):
        ws = tmp_path / "ws-svg-ok"
        (ws / "dist" / "assets").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(
            "<!doctype html><html></html>", encoding="utf-8")
        (ws / "dist" / "assets" / "logo.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg">'
            '<image href="/images/logo.png"/>'
            '<rect style="fill:url(#gradient)"/>'
            "</svg>", encoding="utf-8")
        report = check_self_contained(ws)
        assert report.ok is True, report.error_text()

    def test_font_preload_is_allowed_but_script_preload_is_not(self, tmp_path):
        ws = tmp_path / "ws-preload-font"
        (ws / "dist").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(
            '<link rel="preload" as="font" href="https://cdn.example.com/f.woff2"'
            ' crossorigin>', encoding="utf-8")
        assert check_self_contained(ws).ok is True

        ws2 = tmp_path / "ws-preload-script"
        (ws2 / "dist").mkdir(parents=True)
        (ws2 / "dist" / "index.html").write_text(
            '<link rel="preload" as="script" href="https://cdn.example.com/a.js">',
            encoding="utf-8")
        report = check_self_contained(ws2)
        assert report.ok is False
        assert report.findings[0].kind == "external_stylesheet"

    def test_vendoring_removes_all_gstatic_dependency(self, p7_workspace):
        # After normalization NO @font-face may reference the CDN origin.
        normalize_artifact(p7_workspace, vendored=_fixture())
        report = check_self_contained(p7_workspace)
        assert report.ok is True, report.error_text()
        for path in (p7_workspace / "dist").rglob("*"):
            if path.is_file() and path.suffix in (".html", ".css"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert "fonts.gstatic.com" not in text


# ---------------------------------------------------------------------------
# F. Redirect / security
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, body=b"wOF2" + bytes(64),
                 headers=None, read1_chunks=None):
        self.status = status
        self._body = body
        self._headers = headers or {"Content-Type": "font/woff2",
                                    "Content-Encoding": "identity"}
        self._chunks = read1_chunks

    def getheader(self, name, default=None):
        for key, value in self._headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def read1(self, _n):
        if self._chunks is None:
            chunk, self._body = self._body, b""
            return chunk
        return self._chunks.pop(0) if self._chunks else b""


class _FakeConn:
    def __init__(self, response, recorder=None):
        self._response = response
        self.sock = None
        self.recorder = recorder

    def request(self, method, path, headers=None):
        if self.recorder is not None:
            self.recorder.append((method, path, dict(headers or {})))

    def getresponse(self):
        return self._response

    def close(self):
        pass


def _fetch_font(url, response, recorder=None):
    return _fetch(
        url,
        hosts=GOOGLE_FONTS_ASSET_HOSTS,
        allowed_types=("font/woff2",),
        max_bytes=1024,
        magic=b"wOF2",
        resolver=lambda h: ["93.184.216.34"],
        connection_factory=lambda host, addr, timeout: _FakeConn(response, recorder),
    )


class TestFontVendoringSecurity:
    def test_non_https_source_rejected(self):
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("http://fonts.gstatic.com/s/x.woff2", _FakeResponse())
        assert exc.value.code == "FONT_VENDOR_INSECURE_SOURCE"

    def test_unapproved_host_rejected(self):
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("https://evil.example.com/s/x.woff2", _FakeResponse())
        assert exc.value.code == "FONT_VENDOR_UNTRUSTED_HOST"

    def test_redirect_to_unapproved_host_is_blocked_not_followed(self):
        response = _FakeResponse(status=302, headers={
            "Content-Type": "font/woff2",
            "Location": "https://evil.example.com/s/x.woff2",
        })
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("https://fonts.gstatic.com/s/x.woff2", response)
        assert exc.value.code == "FONT_VENDOR_REDIRECT_BLOCKED"

    def test_oversized_declared_response_rejected(self):
        response = _FakeResponse(headers={
            "Content-Type": "font/woff2", "Content-Encoding": "identity",
            "Content-Length": str(10 * 1024 * 1024),
        })
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("https://fonts.gstatic.com/s/x.woff2", response)
        assert exc.value.code == "FONT_VENDOR_TOO_LARGE"

    def test_oversized_streamed_response_rejected(self):
        response = _FakeResponse(read1_chunks=[b"wOF2" + bytes(2048)])
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("https://fonts.gstatic.com/s/x.woff2", response)
        assert exc.value.code == "FONT_VENDOR_TOO_LARGE"

    def test_invalid_content_type_rejected(self):
        response = _FakeResponse(headers={
            "Content-Type": "text/html", "Content-Encoding": "identity"})
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("https://fonts.gstatic.com/s/x.woff2", response)
        assert exc.value.code == "FONT_VENDOR_UNEXPECTED_CONTENT_TYPE"

    def test_executable_content_rejected_by_magic_bytes(self):
        response = _FakeResponse(body=b"#!/bin/sh\nrm -rf /\n")
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("https://fonts.gstatic.com/s/x.woff2", response)
        assert exc.value.code == "FONT_VENDOR_INVALID_FORMAT"

    def test_encoded_response_rejected(self):
        response = _FakeResponse(headers={
            "Content-Type": "font/woff2", "Content-Encoding": "gzip"})
        with pytest.raises(FontVendorError) as exc:
            _fetch_font("https://fonts.gstatic.com/s/x.woff2", response)
        assert exc.value.code == "FONT_VENDOR_UNEXPECTED_ENCODING"

    def test_private_dns_destination_rejected(self):
        with pytest.raises(FontVendorError) as exc:
            _fetch("https://fonts.gstatic.com/s/x.woff2",
                   hosts=GOOGLE_FONTS_ASSET_HOSTS,
                   allowed_types=("font/woff2",), max_bytes=1024, magic=b"wOF2",
                   resolver=lambda h: ["127.0.0.1"],
                   connection_factory=lambda *a, **k: _FakeConn(_FakeResponse()))
        assert exc.value.code == "FONT_VENDOR_UNSAFE_DNS"

    def test_no_cookies_or_authorization_are_sent(self):
        recorder = []
        _fetch_font("https://fonts.gstatic.com/s/x.woff2", _FakeResponse(), recorder)
        sent = recorder[0][2]
        joined = " ".join(f"{k}:{v}" for k, v in sent.items()).lower()
        assert "cookie" not in joined
        assert "authorization" not in joined
        assert "referer" not in joined

    def test_unsupported_css_url_shape_rejected_for_vendoring(self):
        # Query params other than family/display are never fetched.
        with pytest.raises(FontVendorError):
            _fetch("https://fonts.googleapis.com/css2?family=X&evil=1",
                   hosts=GOOGLE_FONTS_CSS_HOSTS, allowed_types=("text/css",),
                   max_bytes=1024, magic=None, resolver=lambda h: ["93.184.216.34"],
                   connection_factory=lambda *a, **k: _FakeConn(_FakeResponse()))

    def test_vendor_failure_leaves_reference_for_preflight_to_reject(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "dist").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(_p7_index_html(), encoding="utf-8")

        from app.core import selfcontained as module

        def _boom(*args, **kwargs):
            raise FontVendorError("FONT_VENDOR_FETCH_FAILED")

        with patch.object(module, "_vendor_google_fonts", side_effect=_boom):
            report = normalize_and_check_self_contained("proj", ws)

        assert report.ok is False
        assert report.error_code == EXTERNAL_RUNTIME_DEPENDENCY
        assert any(f.host == "fonts.googleapis.com" for f in report.findings)

    def test_parse_google_font_faces_is_deterministic(self):
        first = parse_google_font_faces(_FAKE_GOOGLE_CSS)
        second = parse_google_font_faces(_FAKE_GOOGLE_CSS)
        assert first == second
        assert [f["family"] for f in first].count(FAMILY_HEADING) == 2
        assert all(f["url"].startswith("https://fonts.gstatic.com/") for f in first)


# ---------------------------------------------------------------------------
# G. Artifact identity
# ---------------------------------------------------------------------------

class TestArtifactIdentity:
    def test_normalization_changes_fingerprint_deterministically(self, tmp_path):
        from app.deploy.snapshot import source_fingerprint

        ws_a = _make_p7_workspace(tmp_path / "a")
        ws_b = _make_p7_workspace(tmp_path / "b")

        before = source_fingerprint(ws_a)
        normalize_artifact(ws_a, vendored=_fixture())
        after = source_fingerprint(ws_a)

        assert before != after, "normalization must move the fingerprint"

        normalize_artifact(ws_b, vendored=_fixture())
        assert source_fingerprint(ws_b) == after, "must be deterministic"

    def test_repeated_normalization_is_idempotent(self, p7_workspace):
        from app.deploy.snapshot import source_fingerprint

        normalize_and_check_self_contained("proj", p7_workspace)
        once = source_fingerprint(p7_workspace)
        second = normal_and = normalize_artifact(p7_workspace, vendored=_fixture())
        assert second.changed is False
        assert normal_and.assets == {}
        assert source_fingerprint(p7_workspace) == once

    def test_stale_tested_snapshot_is_invalidated(self, p7_workspace):
        """A fingerprint change must make the stored tested snapshot stale."""
        from app.deploy.snapshot import TestedSnapshot

        snapshot = TestedSnapshot.capture(p7_workspace)
        normalize_artifact(p7_workspace, vendored=_fixture())
        with pytest.raises(ValueError) as exc:
            snapshot.verify(p7_workspace)
        assert "STALE_" in str(exc.value)


# ---------------------------------------------------------------------------
# J. No regression — local-only artifact
# ---------------------------------------------------------------------------

class TestNoRegressionLocalArtifact:
    def test_local_only_site_is_a_strict_noop(self, tmp_path):
        from app.deploy.snapshot import source_fingerprint

        ws = tmp_path / "ws"
        (ws / "dist" / "assets").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(
            '<!doctype html><html><head>'
            '<link rel="stylesheet" href="/assets/app.css">'
            '<link rel="icon" href="/favicon.svg">'
            '</head><body><img src="/images/hero.webp" alt="h">'
            '<a href="https://instagram.com/x">ig</a></body></html>',
            encoding="utf-8")
        (ws / "dist" / "assets" / "app.css").write_text(
            '@font-face { font-family: "Local"; src: url("/fonts/l.woff2") format("woff2"); }',
            encoding="utf-8")

        before = source_fingerprint(ws)
        result = normalize_artifact(ws)
        report = check_self_contained(ws)

        assert result.changed is False
        assert result.assets == {}
        assert report.ok is True
        assert source_fingerprint(ws) == before

    def test_empty_workspace_passes(self, tmp_path):
        ws = tmp_path / "empty"
        ws.mkdir()
        assert check_self_contained(ws).ok is True

    def test_missing_workspace_passes(self, tmp_path):
        assert check_self_contained(tmp_path / "nope").ok is True


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------

class TestErrorContract:
    def test_diagnostics_carry_kind_host_file_and_no_secrets(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "dist").mkdir(parents=True)
        (ws / "dist" / "index.html").write_text(
            '<script src="https://cdn.example.com/a.js?token=SUPERSECRET"></script>',
            encoding="utf-8")

        report = check_self_contained(ws)
        assert report.error_code == EXTERNAL_RUNTIME_DEPENDENCY
        payload = json.dumps(report.to_dict())
        assert "SUPERSECRET" not in payload
        finding = report.to_dict()["findings"][0]
        assert set(finding) == {"kind", "host", "file", "detail"}
        assert finding["kind"] == "external_script"
        assert finding["host"] == "cdn.example.com"
        assert finding["file"] == "index.html"

    def test_finding_list_is_bounded(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "dist").mkdir(parents=True)
        body = "".join(
            f'<script src="https://cdn{i}.example.com/a.js"></script>'
            for i in range(60)
        )
        (ws / "dist" / "index.html").write_text(
            f"<!doctype html><html><body>{body}</body></html>", encoding="utf-8")
        report = check_self_contained(ws)
        assert report.ok is False
        assert len(report.findings) == 25


# ---------------------------------------------------------------------------
# H. QA integration — the gate blocks the ``checked`` binding
# ---------------------------------------------------------------------------

class TestQaIntegration:
    def _orchestrator(self, tmp_path, dist_html: str):
        from app.core.state import ProjectStateStore
        from app.qa.orchestrator import QAOrchestrator

        class _StubRunner:
            """Deterministic stand-in: no process is ever spawned, but the real
            single-worker slot + workspace layout semantics are preserved."""

            def __init__(self, root: Path):
                self.workspace_root = Path(root)
                self.commands: list = []

            def create_workspace(self, project_id: str) -> Path:
                path = self.workspace_root / project_id
                (path / "dist").mkdir(parents=True, exist_ok=True)
                (path / "src").mkdir(parents=True, exist_ok=True)
                (path / "src" / "App.tsx").write_text(
                    "export default () => null\n", encoding="utf-8")
                return path

            def acquire_project(self, project_id: str) -> bool:
                return True

            def release_project(self, project_id: str) -> None:
                return None

            def run_command(self, project_id, command, cwd=None, **kwargs):
                import subprocess

                self.commands.append(list(command))
                return subprocess.CompletedProcess(
                    args=list(command), returncode=0, stdout="", stderr="")

        store = ProjectStateStore(tmp_path / "state")
        runner = _StubRunner(tmp_path / "work")
        workspace = runner.create_workspace("proj")
        (workspace / "dist" / "index.html").write_text(dist_html, encoding="utf-8")
        (workspace / "dist" / "assets").mkdir(exist_ok=True)
        (workspace / "dist" / "assets" / "app.js").write_text(
            "console.log('ok')\n", encoding="utf-8")
        with store.acquire_writer("proj") as state:
            state.lifecycle = "RUNNING"
            state.revisions.source_revision = 1
            state.revisions.qa_revision = 0
            store.save(state)
        return QAOrchestrator(runner, store, hermes_adapter=MagicMock()), store, workspace

    def test_rebuild_gate_blocks_record_checks_on_external_dependency(self, tmp_path):
        orchestrator, store, workspace = self._orchestrator(
            tmp_path, '<script src="https://cdn.example.com/a.js"></script>')

        build_ok, typecheck_ok, self_contained_ok = orchestrator._run_rebuild_checks(
            "proj", workspace)

        assert (build_ok, typecheck_ok) == (True, True)
        assert self_contained_ok is False
        state = store.load("proj")
        assert not (state.deployment or {}).get("checked")

    def test_rebuild_gate_allows_record_checks_when_clean(self, tmp_path):
        orchestrator, store, workspace = self._orchestrator(
            tmp_path, '<link rel="stylesheet" href="/assets/a.css">')

        build_ok, typecheck_ok, self_contained_ok = orchestrator._run_rebuild_checks(
            "proj", workspace)

        assert (build_ok, typecheck_ok, self_contained_ok) == (True, True, True)
        assert (store.load("proj").deployment or {}).get("checked")

    def test_repair_that_reintroduces_cdn_script_blocks_the_binding(self, tmp_path):
        """A repair may fix a blocking finding any way it likes, but it must
        not leave an external runtime dependency behind: the recorded
        ``checked`` binding (which QA/preview trust) must stay absent."""
        orchestrator, store, workspace = self._orchestrator(
            tmp_path, '<link rel="stylesheet" href="/assets/a.css">')

        # Simulate the repair rewriting the artifact with a CDN dependency.
        (workspace / "dist" / "index.html").write_text(
            '<script src="https://cdn.example.com/analytics.js"></script>',
            encoding="utf-8")

        build_ok, typecheck_ok, self_contained_ok = orchestrator._run_rebuild_checks(
            "proj", workspace)

        assert (build_ok, typecheck_ok) == (True, True)
        assert self_contained_ok is False
        assert not (store.load("proj").deployment or {}).get("checked")


def test_font_vendor_transport_failure_is_infrastructure(tmp_path, monkeypatch):
    from app.core import selfcontained as module

    def fail_normalization(project_id, workspace):
        raise FontVendorError("FONT_VENDOR_TIMEOUT")

    monkeypatch.setattr(module, "normalize_self_contained", fail_normalization)
    report = normalize_and_check_self_contained("proj", tmp_path)
    assert not report.ok
    assert report.infrastructure_error == "FONT_VENDOR_TIMEOUT"
    assert report.error_code == "PREVIEW_NORMALIZATION_INFRASTRUCTURE"


# ---------------------------------------------------------------------------
# I. P7-style recovery through the real pipeline
# ---------------------------------------------------------------------------

class TestP7RecoveryFlow:
    """An existing project whose artifact carries Google Fonts flows through the
    normal Website Builder pipeline: normalize → rerun checks → deploy only the
    required next preview → smoke → Telegram. No manual Vercel edits."""

    def _scenario(self, tmp_path, monkeypatch, *, materialize=None):
        from r1_harness import LocalR1Scenario, patch_qa_boundaries

        patch_qa_boundaries(monkeypatch)
        if materialize is not None:
            # FRONTEND "writes" the p7-shaped artifact through the harness's
            # real materialization seam, so the pipeline sees it exactly as it
            # would see generated output.
            import r1_harness

            monkeypatch.setattr(r1_harness, "_materialize_site", materialize)
        scenario = LocalR1Scenario(tmp_path)
        # Every font vendoring fetch is served from the deterministic fixture.
        from app.core import selfcontained as module

        original = module.normalize_artifact

        def with_fixture(workspace, **kwargs):
            kwargs.setdefault("vendored", _fixture())
            return original(workspace, **kwargs)

        monkeypatch.setattr(module, "normalize_artifact", with_fixture)
        return scenario

    def test_initial_build_preserves_families_and_removes_cdn_dependency(
            self, tmp_path, monkeypatch):
        """The INITIAL build of a p7-shaped artifact (Google Fonts for
        Cormorant Garamond + Crimson Pro) vendors the fonts, preserves the
        exact families and lets the build reach PREVIEW_READY."""
        from app.core.lifecycle import ProjectLifecycle
        from app.deploy.snapshot import TestedSnapshot

        scenario = self._scenario(tmp_path, monkeypatch, materialize=_write_p7_site)
        pid = scenario.seed_project()
        scenario.store.transition_lifecycle(pid, ProjectLifecycle.READY)
        scenario.store.transition_lifecycle(pid, ProjectLifecycle.QUEUED)
        scenario.set_frontend_result(
            {"success": True, "design_dna": {"version": 1}})

        result = scenario.builder.build(pid, dict(scenario.store.load(pid).brief))
        assert result.success is True, result.error

        workspace = scenario.runner.create_workspace(pid)
        emitted = (workspace / "dist" / "index.html").read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in emitted
        assert "fonts.gstatic.com" not in emitted
        assert 'font-family: "Cormorant Garamond"' in emitted
        assert 'font-family: "Crimson Pro"' in emitted
        assert 'format("woff2")' in emitted
        # The Vite root template was normalized too, so the NEXT build cannot
        # reintroduce the external reference.
        root = (workspace / "index.html").read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in root

        font_files = sorted((workspace / "dist" / "fonts").glob("*.woff2"))
        assert font_files, "expected local .woff2 assets in dist/fonts"
        for path in font_files:
            assert path.read_bytes().startswith(b"wOF2")

        state = scenario.store.load(pid)
        snapshot = TestedSnapshot.from_dict(state.deployment["tested_snapshot"])
        snapshot.verify(workspace)
        assert snapshot.source_sha256 == state.deployment["checked"]["source_sha256"]
        assert "index.html" in state.deployment["tested_snapshot"]["source"]

        # The DEPLOYED file map (what Vercel's static builder serves) contains
        # the local fonts at exactly the root-absolute paths the emitted
        # @font-face rules reference, and NO external font origin anywhere.
        deployed = dict(snapshot.dist)
        font_map = {n: b for n, b in deployed.items() if n.startswith("fonts/")}
        assert font_map, f"expected fonts/ entries in the deployed map: {sorted(deployed)}"
        for name, payload in font_map.items():
            assert payload.startswith(b"wOF2")
            assert f'url("/{name}")' in emitted
        assert not any("fonts.googleapis.com" in n for n in deployed)
        assert not any("fonts.gstatic.com" in n for n in deployed)
        joined = b"\n".join(deployed.values())
        assert b"fonts.googleapis.com" not in joined
        assert b"fonts.gstatic.com" not in joined

    def test_p7_recovery_revision_removes_cdn_dependency_and_repreviews(
            self, tmp_path, monkeypatch):
        """The p7 RECOVERY path: an existing project with an existing preview
        whose artifact carries Google Fonts runs a normal revision. The
        revision normalizes the fonts, re-runs the required checks/QA and
        delivers the next preview through smoke + Telegram."""
        from app.core.lifecycle import ProjectLifecycle

        scenario = self._scenario(tmp_path, monkeypatch, materialize=_write_p7_site)
        pid = scenario.seed_project()
        scenario.store.transition_lifecycle(pid, ProjectLifecycle.READY)
        scenario.store.transition_lifecycle(pid, ProjectLifecycle.QUEUED)
        scenario.set_frontend_result(
            {"success": True, "design_dna": {"version": 1}})
        # First build establishes the existing project + preview + bypass.
        first = scenario.builder.build(pid, dict(scenario.store.load(pid).brief))
        assert first.success is True, first.error
        assert scenario.telegram.photo_calls, "expected the first preview delivery"
        photos_after_first = len(scenario.telegram.photo_calls)

        # The user asks for a change; the revision regenerates the artifact
        # (again p7-shaped) and the pipeline must normalize it again.
        scenario.set_frontend_result(
            {"success": True, "design_dna": {"version": 2}})
        revision = scenario.run_revision("ganti tipografi sedikit")

        assert revision.success, getattr(revision, "error", None)
        workspace = scenario.runner.create_workspace(pid)
        emitted = (workspace / "dist" / "index.html").read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in emitted
        assert 'font-family: "Cormorant Garamond"' in emitted
        state = scenario.store.load(pid)
        assert state.revisions.source_revision == 2
        assert state.lifecycle == "PREVIEW_READY"
        assert len(scenario.telegram.photo_calls) == photos_after_first + 2

    def test_unsupported_external_dependency_blocks_qa_and_preview(
            self, tmp_path, monkeypatch):
        """An UNSUPPORTED external dependency (a CDN script, which Phase 1
        normalization must NOT download) fails the fixed checks, so QA and
        preview are never reached."""
        from app.core.lifecycle import ProjectLifecycle

        def materialize(workspace: Path) -> None:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "src").mkdir(exist_ok=True)
            (workspace / "dist").mkdir(exist_ok=True)
            (workspace / "src" / "App.tsx").write_bytes(
                b"export default () => null\n")
            (workspace / "dist" / "index.html").write_text(
                "<!doctype html><html><body>"
                '<script src="https://cdn.example.com/app.js"></script>'
                "</body></html>", encoding="utf-8")
            (workspace / "design-dna.json").write_text(
                json.dumps({"version": 1}), encoding="utf-8")

        scenario = self._scenario(tmp_path, monkeypatch, materialize=materialize)
        pid = scenario.seed_project()
        scenario.store.transition_lifecycle(pid, ProjectLifecycle.READY)
        scenario.store.transition_lifecycle(pid, ProjectLifecycle.QUEUED)
        scenario.set_frontend_result(
            {"success": True, "design_dna": {"version": 1}})

        result = scenario.builder.build(pid, dict(scenario.store.load(pid).brief))

        assert result.success is False
        assert "CHEAP_CHECKS_FAILED:self_contained" in (result.error or "")
        assert EXTERNAL_RUNTIME_DEPENDENCY in (result.error or "")
        state = scenario.store.load(pid)
        assert state.lifecycle == "FAILED"
        assert not (state.deployment or {}).get("checked")
        assert not (state.deployment or {}).get("tested_snapshot")
        assert not scenario.smoke.calls
        assert not scenario.telegram.photo_calls
