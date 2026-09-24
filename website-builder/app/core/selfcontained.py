"""Deterministic self-contained artifact gate for Website Builder R1.

PRODUCT PRINCIPLE
-----------------
Design freedom upstream, deterministic self-contained artifact downstream.
FRONTEND may choose ANY typography, icons, imagery, layout, animation, CSS
or JavaScript. But the FINAL generated artifact must satisfy one invariant:

    Every render-critical runtime dependency is served from the generated
    project itself (same origin).

This module is the BUILD-TIME half of that invariant. ``PreviewSmokeTester``
remains the independent RUNTIME half (its same-origin rule is never
weakened, and this module never relaxes it).

Two layers, deliberately small:

  1. NORMALIZATION -- convert a SUPPORTED external dependency (for Phase 1,
     Google Fonts web fonts) into local assets inside the generated project.
     Only a deterministic, allowlisted, trusted source is ever fetched; the
     chosen font families are preserved exactly.

  2. VALIDATION -- reject every remaining unsupported external runtime
     dependency with a sanitized ``EXTERNAL_RUNTIME_DEPENDENCY`` failure.

LINKS ARE NOT DEPENDENCIES
--------------------------
``<a href="https://instagram.com/x">`` is ordinary navigation and PASSES.
``<script src="https://cdn.example.com/a.js">`` and
``<link rel="stylesheet" href="https://fonts.googleapis.com/...">`` FAIL.

No generic asset platform, no CDN proxy, no microservice. Detection is
deterministic string/regex scanning only -- never semantic JS analysis.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public error contract
# ---------------------------------------------------------------------------

EXTERNAL_RUNTIME_DEPENDENCY = "EXTERNAL_RUNTIME_DEPENDENCY"

# Diagnostic ``kind`` values. These are stable, sanitized identifiers that
# FRONTEND can repair against; they never contain provider bodies or secrets.
KIND_STYLESHEET = "external_stylesheet"
KIND_SCRIPT = "external_script"
KIND_IMAGE = "external_image"
KIND_CSS_IMPORT = "external_css_import"
KIND_CSS_URL = "external_css_url"
KIND_JS_IMPORT = "external_js_import"

# Per-check bound on reported findings so a pathological artifact cannot
# produce an unbounded diagnostic payload.
_MAX_FINDINGS = 25
_MAX_EVIDENCE_LEN = 300

_DEPLOYED_SUFFIXES = (".html", ".htm", ".css", ".js", ".mjs", ".cjs",
                      ".jsx", ".ts", ".tsx", ".svg")

_SKIP_DIR_NAMES = frozenset({
    "node_modules", "dist", "qa", ".git", ".hermes", ".browser", ".runtime",
    "coverage", "__pycache__",
})

# Content roots of the canonical Vite starter, in canonical pre-dist form.
# A render-critical reference is only rewritten when the target is provably a
# real local file (the dist copy of the same asset), never heuristically.
_ARTIFACT_CONTENT_ROOTS = ("public", "src", "assets")


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalDependencyFinding:
    """One sanitized unsupported external runtime dependency."""

    kind: str
    host: str
    file: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "host": self.host, "file": self.file,
                "detail": self.detail}

    def render(self) -> str:
        return (f"{self.kind} host={self.host or '(inline)'} "
                f"file={self.file} :: {self.detail}")


@dataclass(frozen=True)
class SelfContainedReport:
    """Deterministic result of the self-contained preflight."""

    findings: Tuple[ExternalDependencyFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def error_code(self) -> Optional[str]:
        return None if self.ok else EXTERNAL_RUNTIME_DEPENDENCY

    def error_text(self) -> Optional[str]:
        if self.ok:
            return None
        lines = "\n".join(f"- {f.render()}" for f in self.findings)
        return f"{EXTERNAL_RUNTIME_DEPENDENCY}\n{lines}"

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "error_code": self.error_code,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_HTML_ENTITY_RE = re.compile(r"&(?:amp|#0*38|#x0*26);", re.IGNORECASE)


def _decode_entities(text: str) -> str:
    """Decode the entity forms that can disguise an absolute URL."""
    return _HTML_ENTITY_RE.sub("&", text)


def is_external_runtime_url(value: str) -> bool:
    """True when a reference denotes an off-origin runtime dependency.

    Non-URL references are internal by definition. ``data:`` / ``blob:`` are
    inline artifacts and never a network dependency.
    """
    if not isinstance(value, str):
        return False
    stripped = _decode_entities(value).strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith(("data:", "blob:", "mailto:", "tel:", "javascript:")):
        return False
    if "//" in stripped:
        try:
            parts = urlsplit(stripped if ":" in stripped.split("//", 1)[0] or lowered.startswith("//")
                             else "//" + stripped)
            host = (parts.netloc or "").rsplit("@", 1)[-1].split(":", 1)[0]
            return bool(host)
        except ValueError:
            return True
    return lowered.startswith(("http:", "https:"))


def _host_of(value: str) -> str:
    """Best-effort sanitized hostname for diagnostics (never a path/query)."""
    try:
        raw = value.strip()
        if raw.startswith("//"):
            raw = "https:" + raw
        parts = urlsplit(raw)
        return (parts.hostname or "")[:253]
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Deterministic scanners
# ---------------------------------------------------------------------------


@dataclass
class _ScanContext:
    """Accumulates findings for one artifact-level scan."""

    findings: List[ExternalDependencyFinding] = field(default_factory=list)
    seen: set = field(default_factory=set)

    def add(self, kind: str, host: str, file: str, detail: str) -> None:
        if len(self.findings) >= _MAX_FINDINGS:
            return
        key = (kind, host, file, detail)
        if key in self.seen:
            return
        self.seen.add(key)
        self.findings.append(ExternalDependencyFinding(
            kind=kind, host=host[:253], file=file,
            detail=detail[:_MAX_EVIDENCE_LEN],
        ))


# Quoted attribute values (either quote style). Deliberately non-greedy and
# bounded so a malformed document cannot cause catastrophic backtracking.
def _attr_pattern(attr: str, name: str, tag: Optional[str] = None) -> re.Pattern:
    tag_part = f"{re.escape(tag)}\\b" if tag else r"[a-zA-Z][a-zA-Z0-9-]*"
    return re.compile(
        r"<" + tag_part + r"[^>]*?\b" + re.escape(attr) + r"\s*=\s*"
        r"(?:\"(?P<" + name + r"_dq>[^\"]*)\""
        r"|'(?P<" + name + r"_sq>[^']*)'"
        r"|(?P<" + name + r"_uq>[^\s>\"'`=]+))",
        re.IGNORECASE | re.DOTALL,
    )


_SCRIPT_SRC_RE = _attr_pattern("src", "src", tag="script")
_TAG_RE = re.compile(r"<(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)\b(?P<attrs>[^>]*)>", re.DOTALL)
_ATTR_IN_TAG_RE = re.compile(
    r"\b(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"
    r"(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<uq>[^\s>\"'`=]+))"
)

_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<uq>[^)\s]*))\s*\)"
    r"|(?:\"(?P<dq2>[^\"]*)\"|'(?P<sq2>[^']*)'))",
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(
    r"url\(\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<uq>[^)\s]*))\s*\)",
    re.IGNORECASE,
)

# Only unmistakable static remote ESM specifiers. Never semantic JS analysis.
_JS_IMPORT_RE = re.compile(
    r"""(?:\bimport\s*\(\s*|\bfrom\s*|\bimport\s*)(?P<q>["'])(?P<url>https?://[^"'\s]+)(?P=q)""",
    re.IGNORECASE,
)
_JS_FETCH_RE = re.compile(
    r"""\bfetch\s*\(\s*(?P<q>["'])(?P<url>https?://[^"'\s]+)(?P=q)""",
    re.IGNORECASE,
)


def _scan_html(text: str, rel_name: str, ctx: _ScanContext,
               links: Optional[List[Tuple[str, str]]] = None) -> None:
    """Scan one HTML document.

    ``links`` optionally collects ``(attribute, url)`` pairs so the
    normalizer can address the exact reference it needs to rewrite. The
    VALIDATOR never uses it.
    """
    # <script src="...">
    for match in _SCRIPT_SRC_RE.finditer(text):
        url = _group_value(match, "src")
        if links is not None:
            links.append(("script_src", url))
        if is_external_runtime_url(url):
            ctx.add(KIND_SCRIPT, _host_of(url), rel_name,
                    "render-critical external script")

    # Inline <script> static remote imports (client content origin).
    for match in _script_body_ranges(text):
        body = text[match[0]:match[1]]
        for url in _remote_imports_in_source(body):
            ctx.add(KIND_JS_IMPORT, _host_of(url), rel_name,
                    "static remote import in inline script")

    # Every tag: href / src / srcset / style attributes.
    for tag_match in _TAG_RE.finditer(text):
        tag = tag_match.group("tag").lower()
        attrs_raw = tag_match.group("attrs") or ""
        attrs = {m.group("name").lower(): _group_value(m, m.group("name") or "")
                 for m in _ATTR_IN_TAG_RE.finditer(attrs_raw)}
        rel = attrs.get("rel", "")
        href = attrs.get("href")
        if href is not None:
            if links is not None:
                links.append(("href", href))
            if is_external_runtime_url(href):
                rel_tokens = {t.lower() for t in rel.split()}
                if tag == "link" and "stylesheet" in rel_tokens:
                    ctx.add(KIND_STYLESHEET, _host_of(href), rel_name,
                            "render-critical external stylesheet")
                elif tag == "link" and "icon" in rel_tokens:
                    # Favicons are not render-critical: the smoke health check
                    # only asserts images/body text. A missing favicon must
                    # never block a deployment (and normalizing it is out of
                    # Phase 1 scope), so the preferred icon link is dropped.
                    pass
                elif tag == "link" and "preload" in rel_tokens:
                    as_attr = attrs.get("as", "").lower()
                    if as_attr not in ("image", "font"):
                        ctx.add(KIND_STYLESHEET, _host_of(href), rel_name,
                                f"render-critical external preload (as={as_attr or 'unknown'})")
                elif tag == "a":
                    # Ordinary user navigation is NOT a render dependency.
                    pass
                elif tag == "link":
                    # manifest / canonical / alternate / dns-prefetch: not
                    # fetch-on-load render dependencies.
                    pass
        src = attrs.get("src")
        if src is not None and tag in ("img", "iframe", "embed", "audio", "video",
                                       "source", "track", "input"):
            if links is not None:
                links.append(("src", src))
            if is_external_runtime_url(src):
                # Render-critical imagery only: img/embedded visual content.
                if tag in ("img", "iframe", "embed", "video", "source"):
                    ctx.add(KIND_IMAGE, _host_of(src), rel_name,
                            f"remote render-critical {tag} source")
        srcset = attrs.get("srcset")
        if srcset is not None:
            for candidate in srcset.split(","):
                token = candidate.strip().split(" ", 1)[0]
                if not token:
                    continue
                if links is not None:
                    links.append(("srcset", token))
                if is_external_runtime_url(token):
                    ctx.add(KIND_IMAGE, _host_of(token), rel_name,
                            "remote render-critical srcset candidate")
        style = attrs.get("style")
        if style:
            for match in _CSS_URL_RE.finditer(style):
                url = _css_group_value(match)
                if links is not None:
                    links.append(("style_url", url))
                if is_external_runtime_url(url):
                    ctx.add(KIND_CSS_URL, _host_of(url), rel_name,
                            "external url(...) in inline style attribute")


def _group_value(match: re.Match, base: str = "") -> str:
    """Read a quoted attribute value from either attribute-regex shape.

    ``_SCRIPT_SRC_RE`` names its groups ``<attr>_dq/_sq/_uq``; the generic
    ``_ATTR_IN_TAG_RE`` names them ``dq/_sq/_uq``. Both are supported so a
    single accessor works everywhere. Empty-string matches are ambiguous with
    an absent group, so ``None`` (absent) and ``""`` (present-but-empty) are
    distinguished through :meth:`re.Match.groupdict`.
    """
    groups = match.groupdict()
    if base:
        for suffix in ("_dq", "_sq", "_uq"):
            key = base + suffix
            if key in groups and groups[key] is not None:
                return groups[key]
    for key in ("dq", "sq", "uq"):
        if key in groups and groups[key] is not None:
            return groups[key]
    return ""


def _css_group_value(match: re.Match) -> str:
    for name in ("dq", "sq", "uq", "dq2", "sq2"):
        value = match.groupdict().get(name)
        if value is not None:
            return value
    return ""


def _script_body_ranges(text: str) -> List[Tuple[int, int]]:
    ranges = []
    lowered = text.lower()
    cursor = 0
    while True:
        start = lowered.find("<script", cursor)
        if start == -1:
            return ranges
        open_end = lowered.find(">", start)
        if open_end == -1:
            return ranges
        closing = lowered.find("</script", open_end)
        if closing == -1:
            return ranges
        ranges.append((open_end + 1, closing))
        cursor = closing + 8


def _remote_imports_in_source(source: str) -> List[str]:
    found = []
    for pattern in (_JS_IMPORT_RE, _JS_FETCH_RE):
        for match in pattern.finditer(source):
            found.append(match.group("url"))
    return found


def _scan_css(text: str, rel_name: str, ctx: _ScanContext,
              links: Optional[List[Tuple[str, str]]] = None) -> None:
    for match in _CSS_IMPORT_RE.finditer(text):
        url = _css_group_value(match)
        if links is not None:
            links.append(("css_import", url))
        if is_external_runtime_url(url):
            ctx.add(KIND_CSS_IMPORT, _host_of(url), rel_name,
                    "external @import render dependency")
    for match in _CSS_URL_RE.finditer(text):
        url = _css_group_value(match)
        if links is not None:
            links.append(("css_url", url))
        if is_external_runtime_url(url):
            ctx.add(KIND_CSS_URL, _host_of(url), rel_name,
                    "external url(...) render dependency")


def _scan_js(text: str, rel_name: str, ctx: _ScanContext) -> None:
    for url in _remote_imports_in_source(text):
        ctx.add(KIND_JS_IMPORT, _host_of(url), rel_name,
                "static remote import/fetch in bundled source")
    for match in _CSS_URL_RE.finditer(text):
        url = _css_group_value(match)
        if is_external_runtime_url(url):
            ctx.add(KIND_CSS_URL, _host_of(url), rel_name,
                    "external url(...) in bundled source")


def _scan_svg(text: str, rel_name: str, ctx: _ScanContext) -> None:
    for match in _TAG_RE.finditer(text):
        attrs = {m.group("name").lower(): _group_value(m, m.group("name") or "")
                 for m in _ATTR_IN_TAG_RE.finditer(match.group("attrs") or "")}
        for key in ("href", "xlink:href"):
            value = attrs.get(key)
            if value and is_external_runtime_url(value):
                ctx.add(KIND_IMAGE, _host_of(value), rel_name,
                        "remote SVG reference")
    for match in _CSS_URL_RE.finditer(text):
        url = _css_group_value(match)
        if is_external_runtime_url(url):
            ctx.add(KIND_CSS_URL, _host_of(url), rel_name,
                    "external url(...) in SVG style")


# ---------------------------------------------------------------------------
# Deployed-artifact iteration
# ---------------------------------------------------------------------------


def _deployed_files(workspace: Path) -> List[Tuple[str, Path]]:
    """Yield ``(deployed_relative_name, path)`` for scannable artifact files.

    Mirrors what Vercel's static builder actually serves: ``dist/`` contents
    are lifted to the site root, application/runtime output is never
    deployed. Deterministic sorted order.
    """
    workspace = Path(workspace)
    dist = workspace / "dist"
    results: List[Tuple[str, Path]] = []
    if dist.is_dir():
        for path in sorted(dist.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(dist).as_posix()
            if path.suffix.lower() not in _DEPLOYED_SUFFIXES:
                continue
            results.append((relative, path))
        return results
    # Pre-dist (source) fallback: only the public/ root documents are served.
    public = workspace / "public"
    if public.is_dir():
        for path in sorted(public.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() not in _DEPLOYED_SUFFIXES:
                continue
            results.append((path.relative_to(public).as_posix(), path))
    return results


def _source_scan_files(workspace: Path) -> List[Tuple[str, Path]]:
    """Source-level files scanned when ``dist/`` exists.

    ``dist/`` is authoritative for what ships, but bundled/transformed output
    can hide a remote reference (e.g. an inline CSS ``url(...)`` hoisted into
    the JS chunk). Scanning the generated source closes that gap without
    re-deriving the bundle.
    """
    workspace = Path(workspace)
    results: List[Tuple[str, Path]] = []
    index = workspace / "index.html"
    if index.is_file() and not index.is_symlink():
        results.append(("index.html", index))
    for root_name in ("src", "public"):
        root = workspace / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.relative_to(workspace).parts[:-1]):
                continue
            if path.suffix.lower() not in _DEPLOYED_SUFFIXES:
                continue
            results.append((path.relative_to(workspace).as_posix(), path))
    return results


def _read_text(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return None
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


def _dispatch_scan(rel_name: str, text: str, ctx: _ScanContext,
                   links: Optional[List[Tuple[str, str]]] = None) -> None:
    suffix = Path(rel_name).suffix.lower()
    if suffix in (".html", ".htm"):
        _scan_html(text, rel_name, ctx, links)
    elif suffix == ".css":
        _scan_css(text, rel_name, ctx, links)
    elif suffix == ".svg":
        _scan_svg(text, rel_name, ctx)
    elif suffix in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        _scan_js(text, rel_name, ctx)


# ---------------------------------------------------------------------------
# Layer 2 — VALIDATION (preflight)
# ---------------------------------------------------------------------------


def check_self_contained(workspace, *, scan_source: Optional[bool] = None) -> SelfContainedReport:
    """Deterministic preflight over the FINAL generated artifact.

    Returns a ``SelfContainedReport``; ``report.ok`` is False when any
    unsupported external runtime dependency remains.
    """
    workspace = Path(workspace)
    ctx = _ScanContext()
    if not workspace.is_dir():
        return SelfContainedReport()

    deployed = _deployed_files(workspace)
    if scan_source is None:
        scan_source = bool(deployed)
    scanned: List[Tuple[str, Path]] = list(deployed)
    if scan_source:
        seen_names = {name for name, _ in scanned}
        for name, path in _source_scan_files(workspace):
            if name not in seen_names:
                scanned.append((name, path))

    for rel_name, path in scanned:
        text = _read_text(path)
        if text is None:
            continue
        _dispatch_scan(rel_name, text, ctx)

    return SelfContainedReport(tuple(ctx.findings))


# ---------------------------------------------------------------------------
# Trusted source policy — Google Fonts ONLY
# ---------------------------------------------------------------------------

GOOGLE_FONTS_CSS_HOSTS = ("fonts.googleapis.com",)
GOOGLE_FONTS_ASSET_HOSTS = ("fonts.gstatic.com",)

# Bounded, deterministic fetch policy. No cookies, no auth forwarding, no
# arbitrary redirect hop, no executable content, bounded bytes and time.
_MAX_CSS_BYTES = 256 * 1024
_MAX_FONT_BYTES = 4 * 1024 * 1024
_MAX_FONTS_PER_FAMILY = 8
_FETCH_TIMEOUT = 15
_FAMILY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
_EXPECTED_CSS_TYPES = ("text/css",)
_EXPECTED_FONT_TYPES = ("font/woff2", "font/woff", "application/font-woff2",
                        "application/font-woff", "application/octet-stream",
                        "binary/octet-stream")
_FONT_MAGIC = {"woff2": b"wOF2", "woff": b"wOFF"}
_UA_FONT = "WebsiteBuilderFontVendor/1.0"

# Fixture seam (tests only): a pre-resolved stylesheet body + font payloads
# keyed by URL. ``None`` (production) always fetches from the trusted source.
_FIXTURE_CSS_KEY = "__css__"


class FontVendorError(Exception):
    """Deterministic, sanitized font-vendoring failure."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail[:200]


def _resolve_public_addresses(hostname: str, resolver) -> List[str]:
    addresses = list(dict.fromkeys(resolver(hostname)))
    if not addresses or len(addresses) > 32:
        raise FontVendorError("FONT_VENDOR_UNSAFE_DNS")
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise FontVendorError("FONT_VENDOR_UNSAFE_DNS")
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None)
                or getattr(ip, "teredo", None)):
            raise FontVendorError("FONT_VENDOR_UNSAFE_DNS")
    return addresses


class PinnedFontHTTPSConnection(http.client.HTTPSConnection):
    """One validated numeric destination; TLS authenticates the real hostname.

    No proxy, no environment credentials, no second DNS resolution.
    """

    def __init__(self, hostname, address, timeout=_FETCH_TIMEOUT):
        super().__init__(hostname, port=443, timeout=timeout,
                         context=ssl.create_default_context())
        self.address = address

    def connect(self):
        ip = ipaddress.ip_address(self.address)
        raw = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                            socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.connect((str(ip), 443))
            if ipaddress.ip_address(raw.getpeername()[0]) != ip:
                raise ValueError("Peer address mismatch")
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


@dataclass(frozen=True)
class VendoredFonts:
    """Pre-resolved fixture payloads (tests only).

    ``assets`` maps either a Google Fonts *stylesheet* URL to its CSS text
    (via the :data:`_FIXTURE_CSS_KEY` entry) or a Google Fonts *asset* URL to
    its raw WOFF/WOFF2 bytes. Production never constructs this: the real path
    fetches from the allowlisted trusted source with the bounded policy above.
    """

    assets: MappingProxyType


def fixture_payloads(css_text, font_files) -> VendoredFonts:
    """Build the fixture seam from in-memory CSS + font bytes (tests only).

    ``font_files`` maps a Google Fonts asset URL -> WOFF/WOFF2 bytes.
    """
    payloads = {_FIXTURE_CSS_KEY: css_text}
    payloads.update(dict(font_files))
    return VendoredFonts(MappingProxyType(payloads))


def _fetch(url: str, *, hosts: Iterable[str], allowed_types: Iterable[str],
           max_bytes: int, magic: Optional[bytes], resolver=None,
           connection_factory=None) -> bytes:
    """Bounded single-hop HTTPS GET from an allowlisted host.

    Rejects: non-HTTPS, non-allowlisted host, userinfo, non-443 port,
    fragments, encoded/control characters in the path, private DNS
    destinations, non-2xx status, non-identity content-encoding, unexpected
    content type, wrong magic bytes, oversized/unbounded bodies.

    Redirects are NEVER followed — a 3xx is a deterministic failure, which
    also means no arbitrary-hostname redirect can be requested by generated
    code. Cookies, Authorization and referer are never sent.
    """
    hosts = tuple(hosts)
    try:
        if not isinstance(url, str) or len(url) > 2048:
            raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
        if any(ord(c) <= 32 or ord(c) >= 127 for c in url):
            raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
        if "\\" in url or "%" in urlsplit(url).netloc:
            raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.fragment:
            raise FontVendorError("FONT_VENDOR_INSECURE_SOURCE")
        if parts.port not in (None, 443) or "@" in parts.netloc:
            raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
        host = parts.hostname or ""
        if host not in hosts:
            raise FontVendorError("FONT_VENDOR_UNTRUSTED_HOST")
        if any(ord(c) <= 32 for c in unquote(parts.path)):
            raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
        addresses = _resolve_public_addresses(host, resolver or _default_resolver)
    except FontVendorError:
        raise
    except Exception as exc:
        raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL", str(exc))
    conn = None
    try:
        conn = (connection_factory or PinnedFontHTTPSConnection)(
            host, addresses[0], timeout=_FETCH_TIMEOUT)
        deadline = time.monotonic() + _FETCH_TIMEOUT
        conn.request("GET", parts.path or "/", headers={
            "Accept": ",".join(allowed_types) if allowed_types else "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": _UA_FONT,
        })
        response = conn.getresponse()
        status = response.status
        if status in (301, 302, 303, 307, 308):
            raise FontVendorError("FONT_VENDOR_REDIRECT_BLOCKED")
        if status != 200:
            raise FontVendorError("FONT_VENDOR_HTTP_STATUS")
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise FontVendorError("FONT_VENDOR_UNEXPECTED_ENCODING")
        content_type = (response.getheader("Content-Type", "") or "").split(";", 1)[0].strip().lower()
        if content_type not in tuple(allowed_types):
            raise FontVendorError("FONT_VENDOR_UNEXPECTED_CONTENT_TYPE")
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                raise FontVendorError("FONT_VENDOR_UNEXPECTED_CONTENT_TYPE")
            if not 0 < declared <= max_bytes:
                raise FontVendorError("FONT_VENDOR_TOO_LARGE")
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FontVendorError("FONT_VENDOR_TIMEOUT")
            if conn.sock is not None:
                conn.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise FontVendorError("FONT_VENDOR_TOO_LARGE")
        payload = bytes(data)
        if not payload:
            raise FontVendorError("FONT_VENDOR_EMPTY_BODY")
        if magic is not None and not payload.startswith(magic):
            raise FontVendorError("FONT_VENDOR_INVALID_FORMAT")
        return payload
    except FontVendorError:
        raise
    except Exception as exc:
        raise FontVendorError("FONT_VENDOR_FETCH_FAILED", type(exc).__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _default_resolver(hostname):
    return [row[4][0] for row in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)]


# --- Google Fonts CSS parsing -------------------------------------------------

_GF_FAMILY_RE = re.compile(r"font-family\s*:\s*(?P<quote>['\"]?)(?P<name>[^;'\"]+)(?P=quote)\s*;",
                           re.IGNORECASE)
_GF_WEIGHT_RE = re.compile(r"font-weight\s*:\s*(?P<weight>[0-9]{3}|normal|bold)\s*;",
                           re.IGNORECASE)
_GF_STYLE_RE = re.compile(r"font-style\s*:\s*(?P<style>normal|italic|oblique)\s*;",
                          re.IGNORECASE)
_GF_DISPLAY_RE = re.compile(r"font-display\s*:\s*(?P<display>[a-z-]+)\s*;", re.IGNORECASE)
_GF_UNICODE_RE = re.compile(r"unicode-range\s*:\s*(?P<range>[^;}]+)\s*;", re.IGNORECASE)
_GF_SRC_RE = re.compile(r"src\s*:\s*(?P<src>[^;}]+)", re.IGNORECASE)
_GF_TECH_RE = re.compile(r"format\s*\(\s*['\"]?(?P<fmt>[a-z0-9-]+)['\"]?\s*\)", re.IGNORECASE)
_GF_URL_RE = re.compile(r"url\(\s*(?P<url>[^)]+?)\s*\)", re.IGNORECASE)
_GF_BLOCK_RE = re.compile(r"@font-face\s*\{(?P<body>[^}]*)\}", re.IGNORECASE | re.DOTALL)


def parse_google_font_faces(css: str) -> List[Dict[str, str]]:
    """Extract ``@font-face`` records from Google Fonts CSS.

    Only the deterministic subset Google actually emits is understood. One
    record is emitted per source URL so a single unavailable variant never
    discards the rest of a family.
    """
    faces: List[Dict[str, str]] = []
    for block in _GF_BLOCK_RE.finditer(css):
        body = block.group("body")
        family_match = _GF_FAMILY_RE.search(body)
        if not family_match:
            continue
        family = family_match.group("name").strip().strip("'\"")
        if not _FAMILY_SEGMENT_RE.match(family):
            continue
        weight_match = _GF_WEIGHT_RE.search(body)
        style_match = _GF_STYLE_RE.search(body)
        display_match = _GF_DISPLAY_RE.search(body)
        range_match = _GF_UNICODE_RE.search(body)
        src_match = _GF_SRC_RE.search(body)
        if not src_match:
            continue
        for url_match in _GF_URL_RE.finditer(src_match.group("src")):
            url = url_match.group("url").strip().strip("'\"")
            if not url:
                continue
            fmt = ""
            after = src_match.group("src")[url_match.end():]
            fmt_match = _GF_TECH_RE.match(after.strip()) or _GF_TECH_RE.search(after[:40])
            if fmt_match:
                fmt = fmt_match.group("fmt").lower()
            faces.append({
                "family": family,
                "weight": (weight_match.group("weight") if weight_match else "400"),
                "style": (style_match.group("style") if style_match else "normal"),
                "display": (display_match.group("display") if display_match else "swap"),
                "unicode_range": (range_match.group("range").strip() if range_match else ""),
                "url": url,
                "format": fmt,
            })
    return faces


def _font_extension(face: Dict[str, str], url: str) -> Optional[str]:
    fmt = (face.get("format") or "").lower()
    if fmt in ("woff2", "woff"):
        return fmt
    lowered = urlsplit(url).path.lower()
    if lowered.endswith(".woff2"):
        return "woff2"
    if lowered.endswith(".woff"):
        return "woff"
    if fmt in ("truetype", "opentype", "embedded-opentype"):
        # Convertible-by-nothing: we only vendor WOFF/WOFF2 verbatim because
        # those are what Google Fonts serves for every modern browser.
        return None
    return None


def _family_slug(family: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", family.strip().lower()).strip("-")
    return slug or "font"


def _font_face_css(face: Dict[str, str], local_path: str) -> str:
    fmt = (face.get("format") or "").lower()
    css_format = "woff2" if fmt == "woff2" else ("woff" if fmt == "woff" else None)
    src = f'url("{local_path}")'
    if css_format:
        src += f' format("{css_format}")'
    lines = [
        "@font-face {",
        f'  font-family: "{face["family"]}";',
        f'  font-style: {face.get("style") or "normal"};',
        f'  font-weight: {face.get("weight") or "400"};',
        f'  font-display: {face.get("display") or "swap"};',
        f"  src: {src};",
    ]
    if face.get("unicode_range"):
        lines.append(f'  unicode-range: {face["unicode_range"]};')
    lines.append("}")
    return "\n".join(lines)


def _validate_google_fonts_url(url: str) -> str:
    """Strict shape check for a Google Fonts CSS2 stylesheet URL.

    Any deviation (query parameters other than ``family``/``display``,
    fragments, userinfo, non-443 ports, unknown hosts) is unsupported and
    therefore never fetched.
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
    if any(ord(c) <= 32 or ord(c) >= 127 for c in url) or "\\" in url:
        raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FontVendorError("FONT_VENDOR_INSECURE_SOURCE")
    if parts.hostname not in GOOGLE_FONTS_CSS_HOSTS:
        raise FontVendorError("FONT_VENDOR_UNTRUSTED_HOST")
    if parts.port not in (None, 443) or "@" in parts.netloc or parts.fragment:
        raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
    if parts.path not in ("/css", "/css2", "/css2/"):
        raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
    params = [segment for segment in (parts.query or "").split("&") if segment]
    for segment in params:
        key = segment.split("=", 1)[0]
        if key not in ("family", "display"):
            raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
    if not any(segment.startswith("family=") for segment in params):
        raise FontVendorError("FONT_VENDOR_UNSUPPORTED_URL")
    return url


def _extract_families(url: str) -> List[str]:
    families = []
    for segment in (urlsplit(url).query or "").split("&"):
        if not segment.startswith("family="):
            continue
        raw = unquote(segment[len("family="):])
        name = raw.split(":", 1)[0]
        name = name.replace("+", " ").strip()
        if not name:
            continue
        for part in name.split("|"):
            part = part.strip()
            if not part:
                continue
            if not _FAMILY_SEGMENT_RE.match(part):
                raise FontVendorError("FONT_VENDOR_UNSUPPORTED_FAMILY")
            families.append(part)
    return families


# ---------------------------------------------------------------------------
# Layer 1 — NORMALIZATION
# ---------------------------------------------------------------------------


@dataclass
class NormalizationResult:
    changed: bool
    assets: Dict[str, bytes] = field(default_factory=dict)
    stylesheets: List[Tuple[str, str]] = field(default_factory=list)
    removed: List[Tuple[str, str]] = field(default_factory=list)
    families: List[str] = field(default_factory=list)
    diagnostics: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "changed": self.changed,
            "stylesheets": list(self.stylesheets),
            "removed": list(self.removed),
            "families": list(self.families),
            "font_assets": sorted(self.assets),
            "diagnostics": dict(self.diagnostics),
        }


def normalize_artifact(workspace: Path, *, vendored: Optional[VendoredFonts] = None,
                       resolver=None, connection_factory=None) -> NormalizationResult:
    """Layers 1: convert supported external dependencies into local assets.

    Phase 1 support: Google Fonts web fonts. Every Google Fonts stylesheet
    ``<link>`` found in a DEPLOYED artifact file is (a) parsed for the chosen
    families and (b) replaced by local ``@font-face`` rules backed by local
    font bytes, preserving the exact font families and weights.

    Deterministic: the same input always produces the same asset names,
    the same bytes, and the same file contents.
    """
    workspace = Path(workspace)
    assets: Dict[str, bytes] = {}
    stylesheets: List[Tuple[str, str]] = []
    removed: List[Tuple[str, str]] = []
    families: List[str] = []
    families_seen = set()
    diagnostics = {"stylesheets": 0, "font_files": 0,
                   "unsupported_stylesheets": 0, "vendor_failures": 0}

    if not workspace.is_dir():
        return NormalizationResult(False, {}, [], [], [], diagnostics)

    # Normalization must rewrite EVERY document that can reintroduce the
    # external reference: the deployed ``dist/`` artifact, the Vite template
    # ``index.html`` (which regenerates dist/index.html on the next build) and
    # any ``public/`` root document (copied verbatim into dist/).
    targets: List[Tuple[str, Path]] = []
    deployed = _deployed_files(workspace)
    if deployed:
        targets.extend(deployed)
    root_index = workspace / "index.html"
    if root_index.is_file() and not root_index.is_symlink():
        targets.append(("index.html", root_index))
    public = workspace / "public"
    if public.is_dir():
        for path in sorted(public.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() not in (".html", ".htm"):
                continue
            targets.append((path.relative_to(public).as_posix(), path))

    changed = False
    for rel_name, path in targets:
        if Path(rel_name).suffix.lower() not in (".html", ".htm"):
            continue
        text = _read_text(path)
        if text is None or "fonts.googleapis.com" not in text:
            continue
        updated, result = _normalize_html_font_links(
            text, rel_name, vendored=vendored, resolver=resolver,
            connection_factory=connection_factory or PinnedFontHTTPSConnection,
        )
        diagnostics["stylesheets"] += result["stylesheets"]
        diagnostics["unsupported_stylesheets"] += result["unsupported_stylesheets"]
        diagnostics["vendor_failures"] += result["vendor_failures"]
        for family in result["families"]:
            if family not in families_seen:
                families_seen.add(family)
                families.append(family)
        for name, data in result["assets"].items():
            assets[name] = data
            diagnostics["font_files"] += 1
        stylesheets.extend(result["stylesheets_list"])
        removed.extend(result["removed_list"])
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed = True

    return NormalizationResult(changed, assets, stylesheets, removed, families,
                               diagnostics)


def _normalize_html_font_links(text: str, rel_name: str, *, vendored,
                               resolver, connection_factory) -> Tuple[str, Dict]:
    """Rewrite Google Fonts ``<link>`` tags in one HTML document."""
    result = {
        "stylesheets": 0, "unsupported_stylesheets": 0, "vendor_failures": 0,
        "families": [], "assets": {}, "stylesheets_list": [], "removed_list": [],
    }
    css_blocks: List[str] = []
    family_order: List[str] = []

    def _replace_tag(match: re.Match) -> str:
        tag_text = match.group(0)
        attrs = {}
        for attr_match in re.finditer(
            r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*\"([^\"]*)\"", tag_text
        ):
            attrs[attr_match.group(1).lower()] = attr_match.group(2)
        for attr_match in re.finditer(
            r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*'([^']*)'", tag_text
        ):
            attrs.setdefault(attr_match.group(1).lower(), attr_match.group(2))
        rel_tokens = {t.lower() for t in (attrs.get("rel") or "").split()}
        if attrs.get("href") is None:
            return tag_text
        kind = tag_text[1:].split(None, 1)[0].split(">", 1)[0].lower()
        href = attrs["href"]
        if kind == "link" and "stylesheet" in rel_tokens:
            try:
                _validate_google_fonts_url(href)
            except FontVendorError:
                return tag_text
        elif kind == "link" and "preconnect" in rel_tokens:
            host = _host_of(href)
            if host not in GOOGLE_FONTS_CSS_HOSTS + GOOGLE_FONTS_ASSET_HOSTS:
                return tag_text
        else:
            return tag_text

        result["stylesheets"] += 1
        if kind == "link" and "preconnect" in rel_tokens:
            result["removed_list"].append((rel_name, "preconnect"))
            return ""

        try:
            faces, asset_payload, unsupported = _vendor_google_fonts(
                href, vendored=vendored, resolver=resolver,
                connection_factory=connection_factory,
            )
        except FontVendorError as exc:
            result["vendor_failures"] += 1
            result["unsupported_stylesheets"] += 1
            logger.warning(
                "Font vendoring skipped for %s (%s): %s", rel_name, exc.code, exc.detail
            )
            # Keep the ORIGINAL external reference so the deterministic
            # preflight surfaces it as an explicit failure rather than
            # silently shipping a font-less artifact.
            return tag_text

        for face, asset_name in faces:
            css_blocks.append(_font_face_css(face, "/" + asset_name))
            result["assets"][asset_name] = asset_payload[asset_name]
        for family in _extract_families(href):
            if family not in family_order:
                family_order.append(family)
        result["stylesheets_list"].append((href, rel_name))
        result["removed_list"].append((rel_name, "stylesheet"))
        logger.info(
            "Vendored Google Fonts stylesheet for %s (%d files, %d unsupported variants)",
            rel_name, len(result["assets"]), unsupported,
        )
        return ""

    updated = re.sub(
        r"<link\b[^>]*>", _replace_tag, text, flags=re.IGNORECASE | re.DOTALL,
    )
    result["families"] = family_order
    if not css_blocks:
        return updated, result

    style_block = ("<style data-website-builder-fonts=\"vendored\">\n"
                   + "\n".join(css_blocks) + "\n</style>\n")
    if "</head>" in updated:
        head_index = updated.rindex("</head>")
        updated = updated[:head_index] + style_block + updated[head_index:]
    else:
        updated = style_block + updated
    return updated, result


def _vendor_google_fonts(css_url: str, *, vendored: Optional[VendoredFonts],
                         resolver, connection_factory
                         ) -> Tuple[List[Tuple[Dict[str, str], str]], Dict[str, bytes], int]:
    """Fetch + parse a Google Fonts stylesheet and produce local font assets.

    Returns ``(faces_with_asset_names, assets, unsupported_variant_count)``.
    """
    _validate_google_fonts_url(css_url)
    if vendored is not None:
        css_text = vendored.assets.get(_FIXTURE_CSS_KEY)
        if isinstance(css_text, (bytes, bytearray)):
            css_text = bytes(css_text).decode("utf-8", errors="strict")
        if not isinstance(css_text, str):
            raise FontVendorError("FONT_VENDOR_FIXTURE_MISSING")
    else:
        css_bytes = _fetch(css_url, hosts=GOOGLE_FONTS_CSS_HOSTS,
                           allowed_types=_EXPECTED_CSS_TYPES,
                           max_bytes=_MAX_CSS_BYTES, magic=None,
                           resolver=resolver, connection_factory=connection_factory)
        css_text = css_bytes.decode("utf-8", errors="strict")

    faces = parse_google_font_faces(css_text)
    if not faces:
        raise FontVendorError("FONT_VENDOR_NO_FONT_FACES")

    assets: Dict[str, bytes] = {}
    resolved: List[Tuple[Dict[str, str], str]] = []
    unsupported = 0
    per_family: Dict[str, int] = {}
    for face in faces:
        family = face["family"]
        extension = _font_extension(face, face["url"])
        if extension is None:
            unsupported += 1
            continue
        if per_family.get(family, 0) >= _MAX_FONTS_PER_FAMILY:
            unsupported += 1
            continue
        asset_name = _font_asset_name(face, extension)
        if asset_name in assets:
            resolved.append((face, asset_name))
            per_family[family] = per_family.get(family, 0) + 1
            continue
        try:
            payload = _font_bytes(face, vendored=vendored, resolver=resolver,
                                 connection_factory=connection_factory,
                                 max_bytes=_MAX_FONT_BYTES)
        except FontVendorError:
            unsupported += 1
            continue
        assets[asset_name] = payload
        per_family[family] = per_family.get(family, 0) + 1
        resolved.append((face, asset_name))

    if not resolved:
        raise FontVendorError("FONT_VENDOR_NO_USABLE_FONT_FILES")
    return resolved, assets, unsupported


def _font_asset_name(face: Dict[str, str], extension: str) -> str:
    digest = hashlib.sha256(
        (face["family"] + "|" + face["weight"] + "|" + face["style"] + "|"
         + (face.get("unicode_range") or "") + "|" + extension).encode("utf-8")
    ).hexdigest()[:16]
    return f"fonts/{_family_slug(face['family'])}-{face['weight']}-{face['style']}-{digest}.{extension}"


def _font_bytes(face: Dict[str, str], *, vendored, resolver, connection_factory,
                max_bytes: int) -> bytes:
    extension = _font_extension(face, face["url"])
    magic = _FONT_MAGIC.get(extension or "")
    if vendored is not None:
        payload = vendored.assets.get(face["url"])
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
    else:
        payload = None
    if payload is None and vendored is None:
        payload = _fetch(face["url"], hosts=GOOGLE_FONTS_ASSET_HOSTS,
                         allowed_types=_EXPECTED_FONT_TYPES,
                         max_bytes=max_bytes, magic=magic, resolver=resolver,
                         connection_factory=connection_factory)
    if payload is None:
        # Fixture mode without this exact URL: synthesize nothing.
        raise FontVendorError("FONT_VENDOR_FIXTURE_MISSING")
    if not isinstance(payload, bytes) or not payload:
        raise FontVendorError("FONT_VENDOR_EMPTY_BODY")
    if len(payload) > max_bytes:
        raise FontVendorError("FONT_VENDOR_TOO_LARGE")
    if magic is not None and not payload.startswith(magic):
        raise FontVendorError("FONT_VENDOR_INVALID_FORMAT")
    return payload


def font_asset_names(result: NormalizationResult) -> List[str]:
    """Deterministic sorted list of the local font files normalization produced."""
    return sorted(name for name in result.assets if name.startswith("fonts/"))


# ---------------------------------------------------------------------------
# Workspace-level orchestration (shared by Phase 7 build, revision, and the
# QA post-repair rebuild boundary)
# ---------------------------------------------------------------------------
#
# Lives here, NOT in app.projects.build, so the QA orchestrator can enforce the
# same invariant without importing the build module (which imports the QA
# orchestrator). No new subsystem: two thin wrappers over the two layers above.

# Attributes written INTO the generated project rather than fetched at runtime
# are staged into the Vite ``public/`` directory so Vite copies them into
# ``dist/`` with their byte identity preserved. The local directory name is
# ``fonts/`` (see ``_font_asset_name``) and matches the emitted ``url()`` paths.
def sync_vendored_assets(workspace: Path, assets: Dict[str, bytes]) -> List[str]:
    """Write normalization assets into the deterministic local location.

    Windows-safe: ``dist/fonts/*`` can be materialized read-only by a bare
    checkout, so only byte-identical files are skipped; anything else is
    overwritten, and a locked destination fails loudly with a sanitized code
    instead of silently shipping a broken artifact. Source-level ``public/``
    copies are always written (they are generated-source state).
    """
    written: List[str] = []
    workspace = Path(workspace)
    for name, data in sorted(assets.items()):
        relative = str(name).replace("\\", "/")
        if relative.startswith("/") or ".." in relative.split("/") or ":" in relative:
            raise ValueError(f"Unsafe vendored asset name: {name}")
        targets = [workspace / "public" / relative]
        dist_root = workspace / "dist"
        if dist_root.is_dir():
            targets.append(dist_root / relative)
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                try:
                    if target.read_bytes() == data:
                        continue
                except OSError:
                    pass
            try:
                target.write_bytes(data)
            except PermissionError:
                target.chmod(0o644)
                target.write_bytes(data)
            written.append(target.relative_to(workspace).as_posix())
    return written


def normalize_self_contained(project_id: str, workspace: Path) -> Dict[str, Any]:
    """Layer 1: normalize supported external dependencies into local assets.

    Deterministic and idempotent: an already self-contained artifact is a
    strict no-op (``changed`` False, no assets), so its source/artifact
    fingerprint does not move and no unnecessary revision/deployment follows.
    """
    result = normalize_artifact(workspace)
    payload = result.to_dict()
    if not result.changed and not result.assets:
        payload["written"] = []
        return payload
    payload["written"] = sync_vendored_assets(workspace, result.assets)
    logger.info(
        "Self-contained normalization for %s: families=%s stylesheets=%d files=%d",
        project_id, result.families, len(result.stylesheets), len(result.assets),
    )
    return payload


def self_contained_preflight(project_id: str, workspace: Path) -> SelfContainedReport:
    """Layer 2: deterministic preflight over the final generated artifact."""
    report = check_self_contained(workspace)
    if not report.ok:
        logger.warning("Self-contained preflight FAILED for %s:\n%s",
                       project_id, report.error_text())
    return report


def normalize_and_check_self_contained(project_id: str, workspace: Path) -> SelfContainedReport:
    """Normalize, then preflight. Never raises for content-level findings.

    A normalization failure is NOT an infrastructure crash: the unsupported
    external reference is deliberately left in place so the deterministic
    preflight below turns it into a stable ``EXTERNAL_RUNTIME_DEPENDENCY``
    failure that FRONTEND can repair.
    """
    try:
        normalize_self_contained(project_id, workspace)
    except Exception:
        logger.exception("Self-contained normalization raised for %s", project_id)
    return self_contained_preflight(project_id, workspace)
