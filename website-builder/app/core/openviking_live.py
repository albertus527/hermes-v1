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

    # -- transport ---------------------------------------------------------

    def _make_client(self):
        if self._client is not None:
            return self._client
        import httpx  # lazy: never imported at module load

        return httpx.Client(
            base_url=self._config.base_url.rstrip("/"),
            headers=self._headers(),
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

    def _read_record(self, uri: str) -> Optional[ResourceRecord]:
        """Resolve the application-owned provenance record for ``uri``.

        Tries the resource directory that owns ``uri``; a trailing content leaf
        resolves to its parent directory. Returns ``None`` when no record exists
        (so the caller drops the match rather than surfacing an unprovenanced
        item).
        """
        directory = resource_directory_uri(uri)
        for candidate in (f"{directory}/{LIVE_RECORD_FILENAME}",):
            text = self._read_text(candidate)
            if text is None:
                continue
            record = _decode_record(text)
            if record is not None:
                return record
        return None

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
        body = self._request("POST", "/api/v1/search/find", json=payload)
        if not isinstance(body, Mapping):
            raise LiveBackendError("openviking response body was not an object")
        matches = list(_parse_find_response(body))
        resolved: List[RawMatch] = []
        for match in matches:
            record = self._read_record(match.uri)
            if record is None:
                # Not application-provenanced content (e.g. a server-generated
                # directory node with no record). Never surfaced.
                continue
            resolved.append(
                RawMatch(
                    uri=match.uri,
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
            )
        return resolved

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

        # 1. Stage the content and commit it as a resource directory.
        temp_id = self._temp_upload(LIVE_CONTENT_FILENAME, bytes(content))
        payload: Dict[str, Any] = {
            "temp_file_id": temp_id,
            "to": directory,
            "source_name": record.source_id,
            "processing_mode": "semantic_and_vectors",
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
