"""D4a: OpenViking context-library foundation -- schema, ingestion, retrieval.

OpenViking (``volcengine/OpenViking``, PyPI ``openviking``) is a context
database for agents. It stores resources under a ``viking://`` virtual
filesystem and exposes them through a small HTTP API (default
``http://localhost:1933``) plus a Python SDK (``openviking_sdk.SyncHTTPClient``).

This module is the **application-owned foundation** that a later batch (D4b,
Laya) will consume. It is deliberately bounded and read-first. It does three
things and nothing else:

1. **Schema** -- a versioned, project-scoped library layout expressed in
   OpenViking's *native* semantics (``viking://resources/...`` URIs, ``ls`` /
   ``find`` / ``read`` / ``abstract`` / ``overview``), with a trust
   classification, provenance record, and digest per resource.
2. **Ingestion** -- an explicit, allowlisted, idempotent, bounded ingestion
   process. It never crawls, never executes retrieved instructions, and never
   indexes secrets, logs, or generated dependency folders.
3. **Retrieval adapter** -- a narrow, application-owned interface
   (:meth:`OpenVikingRetrievalAdapter.retrieve_context`) over a *backend*
   protocol, with a deterministic in-memory fake backend used by the offline
   test suite and an HTTP backend for a real server.

WHAT THIS MODULE IS NOT
-----------------------
OpenViking is a **context provider, not an authority**. Nothing here can:

* make or change requirements decisions;
* select or install dependencies;
* override Design DNA;
* modify LIVE/project state;
* execute a command, generate code, or deploy;
* promote retrieved text into a system instruction.

The module imports no network client at import time and performs no I/O at
import time. It makes **no model calls** and installs nothing.

ISOLATION IS ENFORCED HERE, NOT TRUSTED FROM THE SERVER
-------------------------------------------------------
Every retrieval is scoped by a canonical ``viking://resources/projects/<id>/``
prefix and every returned URI is re-validated against that prefix before it is
allowed into a result (:func:`is_uri_within_scope`). A backend that returns a
foreign project's URI -- through a bug or a malicious server -- cannot leak it:
the item is dropped and a hard error is recorded. The same fail-closed rule
applies to credential-shaped content and to cross-tenant look-alike ids.

FAIL OPEN ON AVAILABILITY, FAIL CLOSED ON ISOLATION
---------------------------------------------------
A disabled feature, a timeout, or a service outage returns an explicit
``unavailable``/``error`` status with **zero items** -- it never fabricates
context and never raises a security check's outcome. But an isolation,
provenance, or credential violation is a hard, non-recoverable error: it is
reported, the offending item is dropped, and the result is marked failed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Pinned upstream contract
# ---------------------------------------------------------------------------

#: The EXACT OpenViking release this batch was designed and reviewed against.
#: Recorded as a fact, never auto-installed: nothing in this repository installs
#: or upgrades it. A future operator provisions exactly this version.
OPENVIKING_PINNED_VERSION = "0.4.23"

#: The Python SDK package name and the HTTP default, recorded for provenance.
OPENVIKING_SDK_PACKAGE = "openviking-sdk"
OPENVIKING_DEFAULT_BASE_URL = "http://localhost:1933"

#: The URI scheme OpenViking uses. Anything else is not an OpenViking URI.
VIKING_SCHEME = "viking"

#: The shared resource scope. Resources are account-global in OpenViking; the
#: application adds its OWN project-scoping segment beneath it.
RESOURCES_SCOPE = "resources"

#: The fixed, application-owned prefix every project's library lives under.
LIBRARY_ROOT = "viking://resources/website-builder"

#: Project ids are used VERBATIM as a URI path segment, so they are constrained
#: to a conservative, path-safe vocabulary. An id that could introduce ``/``,
#: ``..``, or a scheme change is refused before any URI is built -- this is the
#: syntactic half of cross-project isolation.
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: The context-library schema version. Bumped only when the SHAPE a consumer
#: must understand changes (categories, record fields, URI layout).
LIBRARY_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Categories, trust, layers
# ---------------------------------------------------------------------------

#: The CLOSED set of logical categories a project library is partitioned into.
#: Each is a directory under the project root, so OpenViking's native directory
#: semantics carry the category without any extra metadata store.
CATEGORIES: Tuple[str, ...] = (
    "design_dna",        # Design DNA and style references
    "components",        # component reference documentation
    "motion",            # motion and interaction guidance
    "briefs",            # project briefs and accepted requirements
    "decisions",         # revision decisions and approved preferences
)

#: Trust classifications. A CLOSED set, ordered from most to least trusted.
#:
#: * ``reviewed``   -- a Hermes-verified, application-owned artifact
#:   (a provisioned design skill's own files). Highest trust.
#: * ``internal``   -- application-generated, from accepted project state
#:   (a brief derived from confirmed requirements).
#: * ``external``   -- third-party reference content. LOWER trust by definition;
#:   it is data, never an instruction, and never outranks reviewed content.
TRUST_LEVELS: Tuple[str, ...] = ("reviewed", "internal", "external")

#: Trust ordering for comparisons (lower index == more trusted).
TRUST_RANK: Dict[str, int] = {level: index for index, level in enumerate(TRUST_LEVELS)}

#: The default trust for content whose origin is a third party.
DEFAULT_EXTERNAL_TRUST = "external"

#: Retrieval levels, mirroring OpenViking's L0/L1/L2 tiers.
#: L0 = abstract (relevance screening), L1 = overview (navigation),
#: L2 = full content (only when justified).
LEVEL_ABSTRACT = 0
LEVEL_OVERVIEW = 1
LEVEL_DETAIL = 2
LEVELS: Tuple[int, ...] = (LEVEL_ABSTRACT, LEVEL_OVERVIEW, LEVEL_DETAIL)

# ---------------------------------------------------------------------------
# Bounds -- every one is application-owned and enforced, never a server hint
# ---------------------------------------------------------------------------

#: Max resources one ingestion batch may declare.
MAX_INGEST_RESOURCES = 32

#: Max bytes read from ONE source file for ingestion.
MAX_SOURCE_BYTES = 256 * 1024

#: Max total bytes one ingestion batch may read.
MAX_INGEST_TOTAL_BYTES = 4 * 1024 * 1024

#: Max length of a canonical source locator string.
MAX_LOCATOR_CHARS = 512

#: Max entries a caller may request from one retrieval.
MAX_RETRIEVAL_RESULTS = 50

#: Max context bytes a single retrieval result may carry.
MAX_CONTEXT_BYTES = 64 * 1024

#: Max estimated tokens a single retrieval result may carry.
MAX_CONTEXT_TOKENS = 16_000

#: Default request timeout, in seconds.
DEFAULT_TIMEOUT_SECONDS = 10.0

#: Default bounded retry count (retries AFTER the first attempt).
DEFAULT_MAX_RETRIES = 1

#: The directory/filename segments that must never be indexed, whatever a
#: caller asks for. Checked against every source path AND every canonical URI.
FORBIDDEN_PATH_SEGMENTS: Tuple[str, ...] = (
    ".env",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
)

#: File suffixes that are never indexable (secret/credential/executable-shaped).
FORBIDDEN_SUFFIXES: Tuple[str, ...] = (
    ".env", ".key", ".pem", ".p12", ".pfx", ".crt",
    ".log", ".pyc", ".so", ".dll", ".dylib", ".exe",
)

#: Filenames that are never indexable even when the suffix would pass.
FORBIDDEN_FILENAMES: Tuple[str, ...] = (
    ".env", ".env.local", ".env.production", ".npmrc", ".netrc",
    "id_rsa", "id_ed25519", "credentials", "secrets", "app.env",
)

# ---------------------------------------------------------------------------
# Status + warning vocabulary (static, sanitized labels -- never file content)
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_ISOLATION_VIOLATION = "isolation_violation"

RETRIEVAL_STATUSES: Tuple[str, ...] = (
    STATUS_OK, STATUS_DISABLED, STATUS_UNAVAILABLE, STATUS_TIMEOUT,
    STATUS_ERROR, STATUS_ISOLATION_VIOLATION,
)

#: Statuses that mean "no context is available and none was invented".
UNAVAILABLE_STATUSES: Tuple[str, ...] = (
    STATUS_DISABLED, STATUS_UNAVAILABLE, STATUS_TIMEOUT, STATUS_ERROR,
    STATUS_ISOLATION_VIOLATION,
)

INGEST_STATUSES: Tuple[str, ...] = (
    "indexed", "skipped_duplicate", "skipped_forbidden",
    "rejected_allowlist", "rejected_unreadable", "rejected_oversize",
    "error",
)

WARNING_FEATURE_DISABLED = "openviking feature flag is disabled; no retrieval attempted"
WARNING_SERVICE_UNAVAILABLE = "openviking service is unreachable; no context retrieved"
WARNING_TIMEOUT = "openviking retrieval exceeded its timeout; no context retrieved"
WARNING_MALFORMED = "openviking response could not be parsed; no items accepted"
WARNING_MALFORMED_ITEM = "an openviking item was malformed and was dropped"
WARNING_ISOLATION = "openviking returned an item outside the requested project scope; it was dropped"
WARNING_CROSS_TENANT = "openviking returned an item for a different project; it was dropped"
WARNING_CREDENTIAL = "an openviking item contained credential-shaped content; it was dropped"
WARNING_PROVENANCE = "an openviking item carried no usable provenance; it was dropped"
WARNING_TRUNCATED = "openviking context was truncated to satisfy the byte/token budget"
WARNING_RESULT_LIMIT = "openviking returned more items than the requested maximum; the remainder was dropped"
WARNING_L0_L1_ONLY = "only L0/L1 summaries were loaded; L2 detail was not requested"

# ---------------------------------------------------------------------------
# Credential-shape detection (fail closed on ingestion AND retrieval)
# ---------------------------------------------------------------------------
#
# This is a defensive last line, not the primary control: the primary control is
# that secrets are never ingested in the first place (allowlist + forbidden
# names). It exists so that a secret that slips in through a compromised or
# buggy server is still refused at the retrieval boundary.

#: Substrings that, if present in an item's text, mark it credential-shaped.
#: Deliberately conservative and value-free: this module never records the
#: matched text, only that a match occurred.
_CREDENTIAL_MARKERS: Tuple[str, ...] = (
    "-----BEGIN",              # PEM private keys
    "AKIA",                    # AWS access key id
    "sk-ant-", "sk-proj-",     # Anthropic / OpenAI key prefixes
    "ghp_", "gho_", "github_pat_",
    "xoxb-", "xoxp-",          # Slack tokens
)

#: Assignment shapes that look like a live credential: ``<NAME>_KEY=``,
#: ``<NAME>_TOKEN=``, ``PASSWORD=``, ``SECRET=``. Matches the KEY, never a value.
_CREDENTIAL_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|PASSWORD|TOKEN|PRIVATE[_-]?KEY))"
    r"\s*[:=]\s*\S+"
)

#: A long high-entropy blob (>=32 base64/hex-ish chars) is treated as a
#: possible credential. Bounded and cheap.
_ENTROPY_BLOB_RE = re.compile(r"[A-Za-z0-9+/_\-]{40,}")


def text_looks_like_credential(text: str) -> bool:
    """True when ``text`` contains a credential-shaped substring.

    Value-free: the caller must never log the matched text, only the boolean.
    """
    if not isinstance(text, str) or not text:
        return False
    if any(marker in text for marker in _CREDENTIAL_MARKERS):
        return True
    if _CREDENTIAL_ASSIGN_RE.search(text):
        return True
    # A bare high-entropy blob alone is not proof (a base64 image or a hash can
    # match), so require it to co-occur with a key-ish word to reduce noise.
    if _ENTROPY_BLOB_RE.search(text) and re.search(
        r"(?i)\b(key|token|secret|password|bearer)\b", text
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Viking URI helpers
# ---------------------------------------------------------------------------


def is_valid_project_id(project_id: Any) -> bool:
    """True when ``project_id`` is safe to use verbatim as a URI segment."""
    return isinstance(project_id, str) and bool(PROJECT_ID_RE.match(project_id))


def project_root_uri(project_id: str) -> str:
    """The canonical root URI for one project's context library."""
    if not is_valid_project_id(project_id):
        raise ValueError("invalid project id")
    return f"{LIBRARY_ROOT}/projects/{project_id}"


def category_uri(project_id: str, category: str) -> str:
    """The URI for one category directory inside a project library."""
    if category not in CATEGORIES:
        raise ValueError("unknown category")
    return f"{project_root_uri(project_id)}/{category}"


def resource_uri(project_id: str, category: str, slug: str) -> str:
    """The URI for one indexed resource.

    ``slug`` is a deterministic, path-safe name derived from the source
    identity (:func:`slugify`), never a caller-supplied raw path.
    """
    if category not in CATEGORIES:
        raise ValueError("unknown category")
    safe = slugify(slug)
    if not safe:
        raise ValueError("empty resource slug")
    return f"{category_uri(project_id, category)}/{safe}"


#: A URI path segment must be path-safe: no traversal, no scheme, no whitespace.
_SAFE_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _validated_viking_segments(uri: Any) -> Optional[List[str]]:
    """The validated path segments of a ``viking://`` URI, or ``None``.

    **The single URI-validation point.** A URI is accepted only when it is a
    ``viking://`` URI whose every path segment is path-safe (no ``..``/``.``, no
    scheme-looking segment, no whitespace). Returning ``None`` means "not a
    usable OpenViking URI". Both the scope check and the normalization delegate
    here so there is exactly one place the rule lives -- and one place a test can
    pin.
    """
    if not isinstance(uri, str) or not uri.startswith(f"{VIKING_SCHEME}://"):
        return None
    path = uri[len(f"{VIKING_SCHEME}://"):]
    segments = [s for s in path.split("/") if s != ""]
    if not segments:
        return None
    for segment in segments:
        if segment in (".", ".."):
            return None
        if not _SAFE_SEGMENT_RE.match(segment):
            return None
    return segments


def is_uri_within_scope(uri: Any, scope_uri: str) -> bool:
    """True when ``uri`` is a ``viking://`` URI strictly inside ``scope_uri``.

    This is the **load-bearing isolation predicate**. It rejects, in order:

    * anything that is not a ``viking://`` URI (a foreign scheme whose path
      merely LOOKS in-scope is still refused);
    * any path segment that is ``..`` (traversal) or otherwise path-unsafe;
    * a URI that is not under ``scope_uri`` (the project prefix).

    The comparison is on the NORMALIZED path (single slashes, no trailing
    slash), so ``viking://resources/projects/a/`` and
    ``viking://resources/projects/a`` are the same scope, while
    ``viking://resources/projects/ab`` is NOT inside ``.../a``.
    """
    segments = _validated_viking_segments(uri)
    if segments is None:
        return False
    scope_segments = _validated_viking_segments(scope_uri)
    if scope_segments is None:
        return False
    candidate = "/".join(segments)
    scope = "/".join(scope_segments)
    # Strict containment: equal to the scope is inside it; a longer path that
    # starts with ``scope + "/"`` is inside it. A path that merely shares a
    # string prefix (``.../ab`` vs ``.../a``) is NOT.
    return candidate == scope or candidate.startswith(scope + "/")


def _normalized_viking_path(uri: str) -> Optional[str]:
    """The normalized path of a ``viking://`` URI, or ``None`` if malformed."""
    segments = _validated_viking_segments(uri)
    return None if segments is None else "/".join(segments)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: Any) -> str:
    """A deterministic, path-safe slug from arbitrary text.

    Lowercased, non-alphanumeric runs collapsed to ``_``. Used to derive a
    resource's URI name from its source identity so no caller-supplied path
    ever reaches a URI.
    """
    if not isinstance(value, str):
        return ""
    slug = _SLUG_RE.sub("_", value.strip().lower()).strip("_")
    return slug[:96]


# ---------------------------------------------------------------------------
# Source / resource records
# ---------------------------------------------------------------------------


def canonical_source_locator(path: Any) -> str:
    """A canonical, absolute-locator string for a source file.

    Bounded, and never a ``..`` path. This is the identity that ingestion
    dedupes on: the same canonical locator always maps to the same resource
    URI, so re-ingesting an unchanged source is idempotent.
    """
    text = str(path or "").strip()
    if not text:
        raise ValueError("empty source locator")
    # Collapse to a forward-slash form and drop any traversal segment.
    parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("source locator names no file")
    canonical = "/".join(parts)
    return canonical[:MAX_LOCATOR_CHARS]


def source_digest(content: bytes) -> str:
    """The SHA-256 hex digest of a source's bytes (content identity)."""
    return hashlib.sha256(content).hexdigest()


def source_revision(content: bytes) -> str:
    """A short, deterministic source revision string (first 12 hex of digest)."""
    return source_digest(content)[:12]


@dataclass(frozen=True)
class SourceSpec:
    """One approved source in the ingestion allowlist.

    An ingestion run may ONLY read sources declared here. ``source_id`` is a
    stable, application-owned identity; ``project_id`` scopes it; ``trust`` is
    its classification; ``locator`` is the canonical source location.
    """

    source_id: str
    project_id: str
    category: str
    trust: str
    locator: str
    content_type: str = "text/markdown"

    def __post_init__(self) -> None:
        if not is_valid_project_id(self.project_id):
            raise ValueError("SourceSpec: invalid project_id")
        if self.category not in CATEGORIES:
            raise ValueError("SourceSpec: unknown category")
        if self.trust not in TRUST_LEVELS:
            raise ValueError("SourceSpec: unknown trust level")
        if not self.source_id or not str(self.source_id).strip():
            raise ValueError("SourceSpec: empty source_id")
        # Normalize the locator once, at construction.
        object.__setattr__(self, "locator", canonical_source_locator(self.locator))

    @property
    def uri(self) -> str:
        """The resource URI this source indexes to (deterministic)."""
        return resource_uri(self.project_id, self.category, self.source_id)


@dataclass(frozen=True)
class ResourceRecord:
    """The provenance record stored ALONGSIDE every indexed resource.

    OpenViking stores content; this record is what makes that content
    *traceable*. It is written as a sidecar under the resource and is what the
    retrieval adapter surfaces as provenance.
    """

    source_id: str
    canonical_locator: str
    source_revision: str
    project_id: str
    category: str
    content_type: str
    trust: str
    ingested_at: str
    digest: str
    byte_size: int
    uri: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "canonical_locator": self.canonical_locator,
            "source_revision": self.source_revision,
            "project_id": self.project_id,
            "category": self.category,
            "content_type": self.content_type,
            "trust": self.trust,
            "ingested_at": self.ingested_at,
            "digest": self.digest,
            "byte_size": self.byte_size,
            "uri": self.uri,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceRecord":
        return cls(
            source_id=str(data.get("source_id", "")),
            canonical_locator=str(data.get("canonical_locator", "")),
            source_revision=str(data.get("source_revision", "")),
            project_id=str(data.get("project_id", "")),
            category=str(data.get("category", "")),
            content_type=str(data.get("content_type", "")),
            trust=str(data.get("trust", "")),
            ingested_at=str(data.get("ingested_at", "")),
            digest=str(data.get("digest", "")),
            byte_size=int(data.get("byte_size", 0) or 0),
            uri=str(data.get("uri", "")),
        )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def is_forbidden_source(locator: str) -> bool:
    """True when ``locator`` names a path that must never be indexed.

    Covers secret files, private logs, generated dependency folders, and
    executable content. Checked on the canonical locator BEFORE any read, and
    again on the resolved URI, so a source cannot reach the library under a
    forbidden name.
    """
    text = str(locator or "").replace("\\", "/").lower()
    if not text:
        return True
    segments = [s for s in text.split("/") if s]
    for segment in segments:
        if segment in FORBIDDEN_PATH_SEGMENTS:
            return True
        if segment in FORBIDDEN_FILENAMES:
            return True
        for suffix in FORBIDDEN_SUFFIXES:
            if segment.endswith(suffix):
                return True
    return False


@dataclass(frozen=True)
class IngestItemResult:
    """The outcome of ingesting one source."""

    source_id: str
    uri: str
    status: str
    record: Optional[ResourceRecord] = None
    reason: str = ""

    @property
    def indexed(self) -> bool:
        return self.status == "indexed"


@dataclass(frozen=True)
class IngestReport:
    """The bounded, deterministic result of one ingestion run."""

    project_id: str
    statuses: Dict[str, int]
    items: Tuple[IngestItemResult, ...]
    total_bytes: int
    truncated: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "statuses": dict(self.statuses),
            "items": [
                {
                    "source_id": i.source_id,
                    "uri": i.uri,
                    "status": i.status,
                    "reason": i.reason,
                    "record": i.record.to_dict() if i.record else None,
                }
                for i in self.items
            ],
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
        }

    def summary(self) -> str:
        """One bounded, payload-free line. The only string safe to log."""
        counts = " ".join(f"{k}={v}" for k, v in sorted(self.statuses.items()))
        return f"project={self.project_id} {counts} bytes={self.total_bytes}"


def _now_iso(clock: Optional[Any]) -> str:
    """An ISO-8601 UTC timestamp from ``clock``, or the real clock.

    ``clock`` is injected so ingestion is DETERMINISTIC in tests: the same
    clock value yields the same record bytes.
    """
    if clock is not None:
        return str(clock)
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest_sources(
    backend: "OpenVikingBackend",
    sources: Sequence[SourceSpec],
    *,
    reader: Any,
    project_id: str,
    clock: Optional[Any] = None,
) -> IngestReport:
    """Ingest an allowlisted set of sources into a project's library.

    Bounded and idempotent:

    * ``sources`` MUST all belong to ``project_id`` -- a source for another
      project is rejected, never silently re-scoped;
    * at most :data:`MAX_INGEST_RESOURCES` sources are processed;
    * each source is read through ``reader`` (an application-owned callable
      ``reader(locator) -> bytes``); nothing else is ever read;
    * a forbidden path, an oversize file, an unreadable file, and an
      already-present identical resource each have their own explicit status;
    * re-ingesting an UNCHANGED source is a ``skipped_duplicate`` (idempotent);
      re-ingesting a CHANGED source overwrites with the new revision.
    """
    if not is_valid_project_id(project_id):
        raise ValueError("invalid project id")

    statuses: Dict[str, int] = {}
    items: List[IngestItemResult] = []
    total_bytes = 0
    truncated = len(sources) > MAX_INGEST_RESOURCES

    for spec in list(sources)[:MAX_INGEST_RESOURCES]:
        status: str
        reason = ""
        record: Optional[ResourceRecord] = None

        # 1. Scope: a source for a different project is never ingested here.
        if spec.project_id != project_id:
            status, reason = "rejected_allowlist", "source belongs to another project"
        # 2. Forbidden path: secrets, logs, deps, executables.
        elif is_forbidden_source(spec.locator):
            status, reason = "skipped_forbidden", "source path is on the never-index list"
        else:
            # 3. Read ONLY through the application reader, bounded.
            try:
                content = reader(spec.locator)
            except Exception:
                content = None
            if content is None:
                status, reason = "rejected_unreadable", "source could not be read"
            elif not isinstance(content, (bytes, bytearray)):
                status, reason = "rejected_unreadable", "reader did not return bytes"
            elif len(content) > MAX_SOURCE_BYTES:
                status, reason = "rejected_oversize", "source exceeds the per-source byte limit"
            elif total_bytes + len(content) > MAX_INGEST_TOTAL_BYTES:
                status, reason = "rejected_oversize", "batch exceeds the total byte limit"
            else:
                digest = source_digest(bytes(content))
                revision = source_revision(bytes(content))
                uri = spec.uri
                # 4. Duplicate detection by (canonical locator, digest).
                existing = backend.get_record(project_id, uri)
                if existing is not None and existing.digest == digest:
                    status, reason = "skipped_duplicate", "unchanged source already indexed"
                else:
                    total_bytes += len(content)
                    record = ResourceRecord(
                        source_id=spec.source_id,
                        canonical_locator=spec.locator,
                        source_revision=revision,
                        project_id=project_id,
                        category=spec.category,
                        content_type=spec.content_type,
                        trust=spec.trust,
                        ingested_at=_now_iso(clock),
                        digest=digest,
                        byte_size=len(content),
                        uri=uri,
                    )
                    try:
                        backend.put_resource(project_id, uri, bytes(content), record)
                        status = "indexed"
                    except Exception:
                        status, reason, record = "error", "backend rejected the write", None

        statuses[status] = statuses.get(status, 0) + 1
        items.append(
            IngestItemResult(
                source_id=spec.source_id, uri=spec.uri, status=status,
                record=record, reason=reason,
            )
        )

    return IngestReport(
        project_id=project_id,
        statuses=statuses,
        items=tuple(items),
        total_bytes=total_bytes,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Backend protocol + deterministic fake
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextItem:
    """One retrieved context item, normalized and bounded.

    ``body`` and ``summary`` are DATA strings. There is deliberately NO field
    capable of carrying an instruction into an authority position, exactly as
    :class:`~app.core.design_retrieval.DesignEntry` has none.
    """

    uri: str
    source_id: str
    source_revision: str
    trust: str
    category: str
    level: int
    score: float
    title: str
    body: str
    summary: str
    estimated_tokens: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uri": self.uri,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "trust": self.trust,
            "category": self.category,
            "level": self.level,
            "score": self.score,
            "title": self.title,
            "body": self.body,
            "summary": self.summary,
            "estimated_tokens": self.estimated_tokens,
        }


@dataclass(frozen=True)
class RawMatch:
    """An un-normalized match as a backend returns it.

    Kept deliberately close to OpenViking's own ``MatchedContext`` shape so the
    HTTP backend is a thin translation and the fake backend cannot invent a
    contract the real server does not have.
    """

    uri: str
    context_type: str = "resource"
    level: int = LEVEL_ABSTRACT
    abstract: str = ""
    overview: str = ""
    content: str = ""
    score: float = 0.0
    category: str = ""
    match_reason: str = ""
    record: Optional[Dict[str, Any]] = None


class OpenVikingBackend:
    """The narrow surface the adapter needs from a context store.

    Two implementations ship: :class:`FakeOpenVikingBackend` (deterministic,
    offline, the one the test suite uses) and, for a real server, a thin HTTP
    client behind the same three calls. Keeping the surface this small is what
    makes the adapter testable without a live server -- and what keeps a real
    deployment from leaking a second, unbounded API surface into the app.
    """

    def find(
        self, *, query: str, target_uri: str, limit: int, level: Optional[int] = None
    ) -> Sequence[RawMatch]:
        raise NotImplementedError

    def get_record(self, project_id: str, uri: str) -> Optional[ResourceRecord]:
        raise NotImplementedError

    def put_resource(
        self, project_id: str, uri: str, content: bytes, record: ResourceRecord
    ) -> None:
        raise NotImplementedError


class FakeOpenVikingBackend(OpenVikingBackend):
    """A deterministic, in-memory OpenViking stand-in.

    This is the backend the offline suite runs against. It implements ONLY the
    documented behaviour the adapter relies on: keyword matching for ``find``,
    per-resource provenance records, and a write path for ingestion. It performs
    no network I/O, spawns nothing, and is fully deterministic.

    A backend fault can be injected (``fail_with``) so timeout/outage/malformed
    behaviour is testable without a real server.
    """

    def __init__(self) -> None:
        # uri -> (content bytes, record)
        self._store: Dict[str, Tuple[bytes, ResourceRecord]] = {}
        #: When set, ``find`` raises this exception (simulating an outage).
        self.fail_with: Optional[Exception] = None
        #: When set, ``find`` returns these matches verbatim (simulating a
        #: hostile/buggy server), bypassing the normal scoping.
        self.override_matches: Optional[Sequence[RawMatch]] = None

    # -- ingestion side ----------------------------------------------------

    def put_resource(
        self, project_id: str, uri: str, content: bytes, record: ResourceRecord
    ) -> None:
        if record.project_id != project_id:
            raise ValueError("record project does not match write scope")
        if not is_uri_within_scope(uri, project_root_uri(project_id)):
            raise ValueError("resource uri escapes the project scope")
        self._store[uri] = (bytes(content), record)

    def get_record(self, project_id: str, uri: str) -> Optional[ResourceRecord]:
        entry = self._store.get(uri)
        if entry is None:
            return None
        _, record = entry
        if record.project_id != project_id:
            return None
        return record

    # -- retrieval side ----------------------------------------------------

    def find(
        self, *, query: str, target_uri: str, limit: int, level: Optional[int] = None
    ) -> Sequence[RawMatch]:
        if self.fail_with is not None:
            raise self.fail_with
        if self.override_matches is not None:
            return list(self.override_matches)

        tokens = [t for t in re.split(r"[^0-9a-z]+", (query or "").lower()) if t]
        matches: List[RawMatch] = []
        for uri in sorted(self._store):
            if not is_uri_within_scope(uri, target_uri):
                continue
            content, record = self._store[uri]
            text = content.decode("utf-8", errors="replace")
            haystack = (record.source_id + " " + text).lower()
            score = 0.0
            if tokens:
                hits = sum(1 for t in tokens if t in haystack)
                if hits == 0:
                    continue
                score = hits / len(tokens)
            else:
                score = 1.0
            # L0 abstract is the first line; L1 overview is the first ~400 chars.
            first_line = text.strip().splitlines()[0] if text.strip() else record.source_id
            matches.append(
                RawMatch(
                    uri=uri,
                    context_type="resource",
                    level=LEVEL_OVERVIEW if level is None else level,
                    abstract=first_line[:256],
                    overview=text[:400],
                    content=text,
                    score=round(score, 4),
                    category=record.category,
                    match_reason="keyword",
                    record=record.to_dict(),
                )
            )
        matches.sort(key=lambda m: (-m.score, m.uri))
        return matches[: max(0, limit)]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextRetrievalResult:
    """The bounded, deterministic result of one retrieval.

    The stable contract D4b (Laya) consumes. Every field is explicit so a
    consumer never has to guess whether "empty" meant "no matches" or "the
    service was down".
    """

    status: str
    project_id: str
    scope: str
    items: Tuple[ContextItem, ...]
    total_items: int
    returned_items: int
    estimated_tokens: int
    total_bytes: int
    truncated: bool
    degraded: bool
    error_reason: str
    warnings: Tuple[str, ...]
    limits: Dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def available(self) -> bool:
        return self.status not in UNAVAILABLE_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "project_id": self.project_id,
            "scope": self.scope,
            "items": [item.to_dict() for item in self.items],
            "total_items": self.total_items,
            "returned_items": self.returned_items,
            "estimated_tokens": self.estimated_tokens,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "degraded": self.degraded,
            "error_reason": self.error_reason,
            "warnings": list(self.warnings),
            "limits": dict(self.limits),
        }

    def summary(self) -> str:
        """One bounded, payload-free line. The only string safe to log."""
        return (
            f"status={self.status} project={self.project_id} "
            f"returned={self.returned_items}/{self.total_items} "
            f"bytes={self.total_bytes} tokens={self.estimated_tokens} "
            f"truncated={self.truncated} degraded={self.degraded}"
        )


def estimate_tokens(text: str) -> int:
    """A cheap, deterministic token estimate (~4 chars/token), bounded.

    Deliberately NOT a real tokenizer: a real one is a dependency, and the
    budget is a safety bound, not a billing figure.
    """
    if not text:
        return 0
    return (len(text) + 3) // 4
