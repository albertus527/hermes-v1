"""D4a.1: the LIVE OpenViking backend -- real ingestion + retrieval.

D4a shipped an application-owned retrieval adapter with a deterministic fake
backend and a *retrieval-only* HTTP backend whose write path deliberately
refused (`OpenVikingLiveNotQualified`). D4a.1 adds the **qualified live
backend**: it speaks to a real OpenViking server over localhost HTTP for BOTH
retrieval and ingestion, and it reuses D4a's application-owned ingestion policy
verbatim -- it does NOT invent a second policy.

WHAT IS PRESERVED (unchanged, enforced by ``app.core.openviking_library``)
--------------------------------------------------------------------------
Live ingestion is still :func:`~app.core.openviking_library.ingest_sources`:
the explicit source allowlist, source-revision pinning, content digest,
provenance, idempotent re-ingestion, project isolation, category allowlist,
per-source and per-batch size limits, forbidden-source rejection, credential
detection, and trust classification all run in the D4a library module. This
module only supplies a *backend* (the three calls the library makes) and a
bounded local-file reader. A caller cannot widen the policy by using the live
backend: the same ``ingest_sources`` gates every write.

WHY A SEPARATE BACKEND (and not a rewrite of the D4a one)
---------------------------------------------------------
``HttpOpenVikingBackend`` (D4a) is a *retrieval-only* client that refuses
writes; it is a D4a artifact and stays exactly as it is. ``LiveOpenVikingBackend``
subclasses it and adds:

* a real ingestion path (``temp_upload`` -> ``POST /api/v1/resources`` ->
  bounded task polling -> record sidecar write);
* provenance attachment on retrieval (the real server does not return the
  application's ``ResourceRecord``, so the backend resolves it from the
  application-owned sidecar the ingestion path wrote).

PROVENANCE IS RESOLVED, NOT TRUSTED FROM THE SERVER
---------------------------------------------------
The server is never asked to carry the application's provenance. The live
backend reads it from an application-owned sidecar (``.openviking-record.json``)
written under each resource directory. A match whose provenance cannot be
resolved is **dropped at the backend layer** so it never reaches the adapter's
fail-closed provenance check -- the adapter's isolation/provenance/credential
guarantees are therefore untouched and still enforced in one place.

ONE RESOURCE = ONE DIRECTORY
----------------------------
Each ingested source becomes a directory ``viking://…/projects/<id>/<category>/<slug>/``
holding ``content.md`` (L2) and ``.openviking-record.json`` (provenance). This
maps 1:1 onto OpenViking's *directory-level* L0/L1 sidecar model: the server
generates ``.abstract.md``/``.overview.md`` for the directory, and ``find``
returns that directory, whose record the backend reads back.

FAIL OPEN ON AVAILABILITY, FAIL CLOSED ON POLICY
------------------------------------------------
A transport failure/timeout returns a value to the caller (the adapter reports
``unavailable``/``timeout``); it never fabricates context. An ingestion failure
is recorded per-source by ``ingest_sources`` (``error``) and never marks a
resource indexed. A partial write cannot leave a resource "fully indexed"
without provenance: the record sidecar is written LAST, and retrieval drops any
resource without a resolvable record.

No ``subprocess``, no shell, no installer: ``httpx`` is imported lazily inside
the transport call, exactly as the D4a HTTP backend does.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.openviking_library import (
    MAX_SOURCE_BYTES,
    OpenVikingBackend,
    RawMatch,
    ResourceRecord,
    canonical_source_locator,
    is_forbidden_source,
    is_uri_within_scope,
    project_root_uri,
)
from app.core.openviking_retrieval import (
    HttpOpenVikingBackend,
    OpenVikingConfig,
)

# ---------------------------------------------------------------------------
# Live layout constants
# ---------------------------------------------------------------------------

#: The provenance sidecar written under each resource directory. A leading dot
#: keeps it out of normal ``ls`` output and out of retrieval matches.
LIVE_RECORD_FILENAME = ".openviking-record.json"

#: The single content file written for one ingested source.
LIVE_CONTENT_FILENAME = "content.md"

#: HARD CEILING on paid-VLM source ingestions in ONE backend instance / run.
#: ``semantic_and_vectors`` calls the paid VLM to generate L0/L1 for every
#: resource directory AND refreshes every ancestor directory, so the number of
#: paid LLM calls grows with the tree, not just the source count. This ceiling
#: is a fail-closed backstop: the backend refuses the (N+1)th paid ingestion
#: rather than silently spending past it. It is deliberately small; raise it
#: explicitly per run only with an approved budget.
MAX_PAID_VLM_SOURCES_PER_RUN = 12

#: The application-owned top-level directory name used for a resource. Each
#: ingested source owns ``<resource_uri>/`` (the D4a ``resource_uri``).
#:
#: The server-side write path names the resource; the ``to`` target is the
#: directory URI itself, so a single ``temp_upload``ed ``content.md`` lands
#: inside it and the directory gets L0/L1 sidecars.

#: Transport + polling bounds (application-owned, never server hints).
DEFAULT_LIVE_TIMEOUT_SECONDS = 60.0
DEFAULT_TASK_POLL_INTERVAL_SECONDS = 1.0
MAX_TASK_POLL_SECONDS = 600.0
MAX_TASK_POLLS = 600

#: Terminal OpenViking task states.
_TASK_TERMINAL_OK = frozenset({"completed", "success", "succeeded", "done"})
_TASK_TERMINAL_FAIL = frozenset({"failed", "cancelled", "canceled", "error"})

#: Retrieval tag namespaces written with each resource so a caller can filter by
#: application-owned provenance without trusting server metadata.
TAG_PROJECT_PREFIX = "wb_project="
TAG_CATEGORY_PREFIX = "wb_category="
TAG_TRUST_PREFIX = "wb_trust="
TAG_REVISION_PREFIX = "wb_revision="


class LiveBackendError(RuntimeError):
    """A live transport/policy failure. Never carries a secret value."""


# ---------------------------------------------------------------------------
# Record <-> URI mapping
# ---------------------------------------------------------------------------


def resource_directory_uri(record_uri: str) -> str:
    """The directory URI that owns a resource's record + content.

    The D4a ``resource_uri`` already IS the resource directory (``…/<slug>``),
    so this is the identity for a directory URI and strips a trailing
    ``/content.md`` leaf back to its directory.
    """
    uri = str(record_uri or "").rstrip("/")
    suffix = "/" + LIVE_CONTENT_FILENAME
    if uri.endswith(suffix):
        uri = uri[: -len(suffix)]
    return uri


def record_uri_for(uri: str) -> str:
    """The sidecar URI that holds the provenance record for ``uri``."""
    return resource_directory_uri(uri).rstrip("/") + "/" + LIVE_RECORD_FILENAME


def content_uri_for(uri: str) -> str:
    """The content (L2) URI for ``uri``."""
    return resource_directory_uri(uri).rstrip("/") + "/" + LIVE_CONTENT_FILENAME


def _encode_record(record: ResourceRecord) -> str:
    return json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))


def _decode_record(text: str) -> Optional[ResourceRecord]:
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, Mapping):
        return None
    try:
        return ResourceRecord.from_dict(data)
    except Exception:
        return None


def retrieval_tags_for(record: ResourceRecord) -> List[str]:
    """Explicit ``k=v`` retrieval tags derived from an application record.

    Written with the resource so a caller may filter by application-owned
    provenance without trusting server metadata. Tags are non-secret.
    """
    return [
        f"{TAG_PROJECT_PREFIX}{record.project_id}",
        f"{TAG_CATEGORY_PREFIX}{record.category}",
        f"{TAG_TRUST_PREFIX}{record.trust}",
        f"{TAG_REVISION_PREFIX}{record.source_revision}",
    ]


# ---------------------------------------------------------------------------
# Bounded local-file reader (the ONLY read path for live ingestion)
# ---------------------------------------------------------------------------


def make_source_reader(root: Path):
    """Return a bounded ``reader(locator) -> Optional[bytes]`` under ``root``.

    The reader is the *only* read path live ingestion uses, and it refuses,
    before any read:

    * a locator that escapes ``root`` (no traversal);
    * a forbidden path (secrets, logs, dependency dirs, executables);
    * a file larger than :data:`MAX_SOURCE_BYTES`.

    Returning ``None`` maps to the library's ``rejected_unreadable`` status, so
    an unreadable source is reported, never invented.
    """
    base = Path(root).resolve()

    def reader(locator: str) -> Optional[bytes]:
        text = str(locator or "")
        if is_forbidden_source(text):
            return None
        try:
            candidate = (base / text).resolve()
        except Exception:
            return None
        if base != candidate and base not in candidate.parents:
            return None
        try:
            if not candidate.is_file():
                return None
            if candidate.stat().st_size > MAX_SOURCE_BYTES:
                return None
            return candidate.read_bytes()
        except Exception:
            return None

    return reader


# ---------------------------------------------------------------------------
# The live backend
# ---------------------------------------------------------------------------


class LiveOpenVikingBackend(HttpOpenVikingBackend):
    """A real OpenViking server backend: qualified retrieval AND ingestion.

    Subclasses the D4a retrieval-only HTTP backend (reusing its ``find``
    transport and header logic) and adds a real write path plus provenance
    resolution. Construct it lazily -- importing this module touches no network.
    """

    def __init__(self, config: OpenVikingConfig, *, client: Any = None) -> None:
        super().__init__(config)
        #: Optional injected httpx-like client (used by the offline tests via a
        #: MockTransport). ``None`` means "build one per call".
        self._client = client
        #: When True, ``find`` asks the server for full L2 content per match so
        #: the adapter's credential check sees the REAL body (a security control
        #: must not be blind to L2), and its ``allow_detail`` policy can surface
        #: it. Default True; the adapter still bounds how much is kept and only
        #: surfaces L2 when the caller asked for detail.
        self.read_content = True
        #: Application-owned ingestion cost control. ``semantic_and_vectors``
        #: calls the paid VLM to generate L0/L1 AND refreshes every ancestor
        #: directory, so N sources cost many paid LLM calls. ``vectors_only``
        #: skips the VLM (embeddings only; no L0/L1).
        #:
        #: FAIL-CLOSED DEFAULT: the FREE path is the default. Selecting the paid
        #: path requires an explicit ``allow_paid_vlm=True`` opt-in, and it is
        #: bounded by :data:`MAX_PAID_VLM_SOURCES_PER_RUN` per run. This is the
        #: preventive control for the D4a.1 budget overrun: no paid call happens
        #: unless a caller deliberately asks for it AND stays under the ceiling.
        self.processing_mode = "vectors_only"
        self._allow_paid_vlm = False
        self._paid_vlm_sources = 0

    def enable_paid_vlm(
        self, *, processing_mode: str = "semantic_and_vectors", ceiling: int = MAX_PAID_VLM_SOURCES_PER_RUN
    ) -> None:
        """Explicitly opt in to paid-VLM ingestion, bounded by ``ceiling``.

        This is the ONLY way to reach the paid path. Calling it records the
        caller's intent and the hard per-run ceiling; ingestion then refuses
        past the ceiling instead of spending unboundedly.
        """
        if processing_mode != "semantic_and_vectors":
            raise ValueError("enable_paid_vlm requires processing_mode=semantic_and_vectors")
        self._allow_paid_vlm = True
        self.processing_mode = processing_mode
        self._paid_vlm_ceiling = max(1, int(ceiling))

    # -- transport ---------------------------------------------------------

    def _make_client(self):
        if self._client is not None:
            return self._client
        import httpx  # lazy: never imported at module load

        # Do NOT set a default Content-Type: it breaks the multipart
        # ``temp_upload`` (httpx would otherwise send application/json on a
        # multipart body). JSON requests set their own Content-Type; uploads set
        # a multipart one.
        headers = {
            k: v for k, v in self._headers().items() if k.lower() != "content-type"
        }
        return httpx.Client(
            base_url=self._config.base_url.rstrip("/"),
            headers=headers,
            timeout=self._config.timeout_seconds,
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """One bounded JSON request. Raises :class:`LiveBackendError`/``TimeoutError``."""
        import httpx

        client = self._make_client()
        try:
            response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise TimeoutError("openviking request timed out") from exc
        except httpx.HTTPError as exc:
            raise LiveBackendError("openviking transport failure") from exc
        if response.status_code >= 400:
            raise LiveBackendError(
                f"openviking returned status {response.status_code}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except Exception as exc:
            raise LiveBackendError("openviking response was not JSON") from exc

    @staticmethod
    def _result_of(body: Any) -> Any:
        if isinstance(body, Mapping) and "result" in body:
            return body["result"]
        return body

    # -- retrieval ---------------------------------------------------------

    def _read_text(self, uri: str) -> Optional[str]:
        """Read one file's content, or ``None`` if it does not exist."""
        try:
            body = self._request(
                "GET", "/api/v1/content/read", params={"uri": uri, "raw": True}
            )
        except (LiveBackendError, TimeoutError):
            return None
        result = self._result_of(body)
        if isinstance(result, str):
            return result
        if isinstance(result, Mapping):
            content = result.get("content")
            if isinstance(content, str):
                return content
        return None

    def _find_record_dir(self, uri: str) -> Tuple[Optional[str], Optional[ResourceRecord]]:
        """Walk UP from ``uri`` to the resource directory that holds a record.

        The real server returns matches at the FILE level -- e.g. the L1 sidecar
        ``…/<slug>/.overview.md`` -- while the application-owned record lives at
        the resource directory ``…/<slug>/.openviking-record.json``. So we try
        the URI itself, then each ancestor, and return the first directory whose
        record resolves. The walk is bounded by the ``viking://`` path and never
        leaves the project scope (the caller still validates containment).

        Returns ``(directory, record)`` or ``(None, None)``.
        """
        candidate = str(uri or "").rstrip("/")
        if not candidate.startswith("viking://"):
            return None, None
        while True:
            text = self._read_text(f"{candidate}/{LIVE_RECORD_FILENAME}")
            if text is not None:
                record = _decode_record(text)
                if record is not None:
                    return candidate, record
            parent = candidate.rsplit("/", 1)[0]
            if parent == candidate or not parent.startswith("viking://"):
                return None, None
            candidate = parent

    def _read_record(self, uri: str) -> Optional[ResourceRecord]:
        """The application-owned provenance record for ``uri``, or ``None``."""
        _, record = self._find_record_dir(uri)
        return record

    def find(
        self, *, query: str, target_uri: str, limit: int, level: Optional[int] = None
    ) -> Sequence[RawMatch]:
        """Server ``find`` with application provenance attached.

        Calls the real route, then attaches each match's application record and
        DROPS any match whose provenance cannot be resolved. Dropping here keeps
        the adapter's fail-closed provenance check untouched: an unprovenanced
        match never reaches it.
        """
        from app.core.openviking_retrieval import _parse_find_response

        payload: Dict[str, Any] = {
            "query": query,
            "target_uri": target_uri,
            "limit": max(0, int(limit)),
        }
        if level is not None:
            payload["level"] = level
        # Ask for full content only when the caller's policy can use it. This is
        # what lets the adapter's L2 policy AND its credential check see the real
        # body; the adapter still bounds how much is kept.
        if self.read_content:
            payload["read_content"] = True
        body = self._request("POST", "/api/v1/search/find", json=payload)
        if not isinstance(body, Mapping):
            raise LiveBackendError("openviking response body was not an object")
        matches = list(_parse_find_response(body))
        # The server returns the SAME resource at multiple levels (an L0/L1
        # directory sidecar AND the L2 body), which resolve to the same
        # application resource directory. Present each resource ONCE, keeping its
        # highest-scoring match, so a result is a set of distinct resources and
        # never spends the context budget on duplicates.
        best: Dict[str, RawMatch] = {}
        for match in matches:
            record_dir, record = self._find_record_dir(match.uri)
            if record is None or record_dir is None:
                # Not application-provenanced content (e.g. a server-generated
                # directory node with no record). Never surfaced.
                continue
            # Present the match at its RESOURCE DIRECTORY, not the server's
            # file-level leaf (which may be a hidden `.overview.md` sidecar whose
            # dot-prefixed segment is not a path-safe URI segment). The directory
            # is the application-owned resource identity and is path-safe. Scope
            # is NOT filtered here: the adapter remains the single fail-closed
            # isolation boundary, so a foreign match reaches it and is refused
            # there rather than being silently dropped.
            candidate = RawMatch(
                uri=record_dir,
                context_type=match.context_type,
                level=match.level,
                abstract=match.abstract,
                overview=match.overview,
                content=match.content,
                score=match.score,
                category=record.category or match.category,
                match_reason=match.match_reason,
                record=record.to_dict(),
            )
            existing = best.get(record_dir)
            if existing is None or candidate.score > existing.score:
                best[record_dir] = candidate
        return sorted(best.values(), key=lambda m: (-m.score, m.uri))

    # -- ingestion ---------------------------------------------------------

    def get_record(self, project_id: str, uri: str) -> Optional[ResourceRecord]:  # type: ignore[override]
        """The stored provenance record for ``uri``, scoped to ``project_id``."""
        record = self._read_record(uri)
        if record is None:
            return None
        if record.project_id != project_id:
            return None
        return record

    def _temp_upload(self, filename: str, content: bytes) -> str:
        """Upload bytes to the server's temporary area; return ``temp_file_id``."""
        body = self._request(
            "POST",
            "/api/v1/resources/temp_upload",
            files={"file": (filename, content, "text/markdown")},
        )
        result = self._result_of(body)
        temp_id = None
        if isinstance(result, Mapping):
            temp_id = result.get("temp_file_id") or result.get("file_id")
        if not temp_id and isinstance(body, Mapping):
            temp_id = body.get("temp_file_id")
        if not temp_id or not isinstance(temp_id, str):
            raise LiveBackendError("openviking temp_upload returned no temp_file_id")
        return temp_id

    def _await_task(self, task_id: str) -> None:
        """Poll a task to a terminal state, bounded. Raises on failure/timeout."""
        if not task_id:
            return
        deadline = time.monotonic() + MAX_TASK_POLL_SECONDS
        polls = 0
        while True:
            polls += 1
            if polls > MAX_TASK_POLLS or time.monotonic() > deadline:
                raise LiveBackendError("openviking task did not finish within the bound")
            body = self._request("GET", f"/api/v1/tasks/{task_id}")
            result = self._result_of(body)
            status = ""
            if isinstance(result, Mapping):
                status = str(result.get("status", "")).lower()
            elif isinstance(body, Mapping):
                status = str(body.get("status", "")).lower()
            if status in _TASK_TERMINAL_OK:
                return
            if status in _TASK_TERMINAL_FAIL:
                raise LiveBackendError(f"openviking task ended as {status!r}")
            time.sleep(DEFAULT_TASK_POLL_INTERVAL_SECONDS)

    def _write_text(self, uri: str, content: str) -> None:
        self._request(
            "POST",
            "/api/v1/content/write",
            json={"uri": uri, "content": content, "mode": "replace"},
        )

    def put_resource(
        self, project_id: str, uri: str, content: bytes, record: ResourceRecord
    ) -> None:
        """Write one resource to the real server, then its provenance record.

        Order matters for consistency: content is committed and its task is
        awaited FIRST; the provenance sidecar is written LAST. A resource is
        therefore "fully indexed" only once its record exists -- a failed
        content write, a failed task, or a failed record write leaves the
        resource without resolvable provenance, so retrieval drops it instead of
        presenting it as current.
        """
        if record.project_id != project_id:
            raise LiveBackendError("record project does not match write scope")
        directory = resource_directory_uri(uri)
        if not is_uri_within_scope(directory, project_root_uri(project_id)):
            raise LiveBackendError("resource uri escapes the project scope")

        # COST GUARD (fail closed): a paid-VLM ingestion must be explicitly
        # opted in AND stay under the per-run ceiling. This is the preventive
        # control for the D4a.1 budget overrun -- the backend refuses the
        # over-ceiling write instead of silently spending.
        if self.processing_mode == "semantic_and_vectors":
            if not self._allow_paid_vlm:
                raise LiveBackendError(
                    "paid VLM ingestion is not enabled; call enable_paid_vlm() "
                    "with an approved budget, or use vectors_only"
                )
            ceiling = getattr(self, "_paid_vlm_ceiling", MAX_PAID_VLM_SOURCES_PER_RUN)
            if self._paid_vlm_sources >= ceiling:
                raise LiveBackendError(
                    f"paid VLM ingestion ceiling reached ({ceiling} sources this run); "
                    "refusing further paid writes"
                )

        # 1. Stage the content and commit it as a resource directory.
        temp_id = self._temp_upload(LIVE_CONTENT_FILENAME, bytes(content))
        payload: Dict[str, Any] = {
            "temp_file_id": temp_id,
            "to": directory,
            "source_name": record.source_id,
            # The processing mode is an APPLICATION-OWNED cost control. The
            # default ``semantic_and_vectors`` calls the paid VLM to generate
            # L0/L1 for the resource directory AND refreshes every ancestor
            # directory (each refresh is another paid LLM call), so ingesting N
            # sources costs far more than N VLM calls. ``vectors_only`` skips
            # the VLM entirely (embeddings only) at the cost of no L0/L1
            # summaries. Callers choose via ``self.processing_mode``.
            "processing_mode": self.processing_mode,
            "wait": True,
            "timeout": self._config.timeout_seconds,
            "tags": retrieval_tags_for(record),
            "tag_mode": "replace",
            # Keep the uploaded markdown as ONE file (no heading/size splitting)
            # so the layout is deterministic: the resource directory holds
            # exactly `content.md` and the server's L0/L1 sidecars.
            "args": {"parse_mode": "no_split"},
        }
        body = self._request("POST", "/api/v1/resources", json=payload)
        result = self._result_of(body)
        task_id = None
        if isinstance(result, Mapping):
            task_id = result.get("task_id")
        if not task_id and isinstance(body, Mapping):
            task_id = body.get("task_id")

        # 2. Await semantic + vector processing so the resource is searchable.
        if task_id:
            self._await_task(str(task_id))

        # A paid-VLM write succeeded: count it against the per-run ceiling.
        if self.processing_mode == "semantic_and_vectors":
            self._paid_vlm_sources += 1

        # 3. Write provenance LAST: this is what marks the resource indexed.
        self._write_text(f"{directory}/{LIVE_RECORD_FILENAME}", _encode_record(record))

    # -- verification ------------------------------------------------------

    def _resolve_content_uri(self, uri: str) -> Optional[str]:
        """The actual L2 content URI for a resource directory.

        Tries the deterministic ``content.md`` first; if the server laid the
        file out differently (or renamed it), falls back to a bounded ``ls`` of
        the resource directory and picks the first non-hidden file. Returns
        ``None`` when no content file exists.
        """
        directory = resource_directory_uri(uri)
        expected = f"{directory}/{LIVE_CONTENT_FILENAME}"
        if self._read_text(expected) is not None:
            return expected
        try:
            body = self._request(
                "GET", "/api/v1/fs/ls", params={"uri": directory, "recursive": True}
            )
        except (LiveBackendError, TimeoutError):
            return None
        result = self._result_of(body)
        entries = result if isinstance(result, list) else None
        if entries is None and isinstance(result, Mapping):
            for key in ("entries", "nodes", "items"):
                if isinstance(result.get(key), list):
                    entries = result[key]
                    break
        if not entries:
            return None
        for entry in entries:
            if isinstance(entry, str):
                name = entry
            elif isinstance(entry, Mapping):
                name = str(entry.get("uri") or entry.get("name") or "")
            else:
                continue
            if not name or Path(name).name.startswith("."):
                continue
            if name.startswith("viking://"):
                return name
            return f"{directory}/{Path(name).name}"
        return None

    def verify_record(self, project_id: str, uri: str) -> Tuple[bool, str]:
        """Consistency check: the stored record matches the stored content.

        Returns ``(ok, reason)``. Checks that a record exists, belongs to
        ``project_id``, and that its digest still matches the content the server
        holds -- so a drifted or partially-written resource is detected rather
        than trusted.
        """
        record = self.get_record(project_id, uri)
        if record is None:
            return False, "no record"
        from app.core.openviking_library import source_digest

        content_uri = self._resolve_content_uri(uri)
        if content_uri is None:
            return False, "no content"
        text = self._read_text(content_uri)
        if text is None:
            return False, "no content"
        if source_digest(text.encode("utf-8")) != record.digest:
            return False, "digest mismatch"
        return True, "ok"


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------


def build_live_backend(config: Optional[OpenVikingConfig] = None, **kwargs: Any) -> LiveOpenVikingBackend:
    """Build a live backend from config (no network at construction)."""
    return LiveOpenVikingBackend(config or OpenVikingConfig(), **kwargs)


__all__ = [
    "DEFAULT_LIVE_TIMEOUT_SECONDS",
    "DEFAULT_TASK_POLL_INTERVAL_SECONDS",
    "LIVE_CONTENT_FILENAME",
    "LIVE_RECORD_FILENAME",
    "LiveBackendError",
    "LiveOpenVikingBackend",
    "MAX_TASK_POLL_SECONDS",
    "build_live_backend",
    "content_uri_for",
    "make_source_reader",
    "record_uri_for",
    "resource_directory_uri",
    "retrieval_tags_for",
]
