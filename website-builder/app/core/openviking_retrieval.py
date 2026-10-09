"""D4a: the OpenViking retrieval adapter -- narrow, bounded, fail-closed.

This is the application-owned boundary a later batch (D4b, Laya) consumes. It
wraps a :class:`~app.core.openviking_library.OpenVikingBackend` behind ONE
method, :meth:`OpenVikingRetrievalAdapter.retrieve_context`, and returns the
stable :class:`~app.core.openviking_library.ContextRetrievalResult`.

DESIGN COMMITMENTS
------------------
**Narrow.** One retrieval method, one result type. No write path is exposed to
a caller; ingestion is a separate, explicit application process.

**Optional.** The feature flag (:attr:`OpenVikingConfig.enabled`) is DISABLED by
default. When disabled the adapter makes no backend call and returns an explicit
``disabled`` result with zero items. Existing Design DNA retrieval is untouched
and FRONTEND/FAST receive no fabricated context.

**Bounded.** Result count, context bytes, estimated tokens, request timeout, and
retry count are all application-owned and enforced here -- never delegated to
the server. Retrieval is a single bounded query, never an unbounded traversal.

**Fail open on availability.** A disabled flag, a timeout, or an outage yields
``disabled``/``timeout``/``unavailable`` with ZERO items. It never invents
context and never changes a security outcome.

**Fail closed on isolation, provenance, or credentials.** A returned URI outside
the requested project scope, a cross-tenant record, an item with no usable
provenance, or credential-shaped content is a HARD failure: the whole result is
refused (zero items) and marked non-ok. A retrieval failure can never bypass a
security check.

**Data, never authority.** Items carry text with provenance; there is no field
capable of carrying an instruction into an authority position, and nothing here
promotes retrieved text into a prompt or a requirement.

The module imports no network client at import time and performs no I/O at
import time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from app.core.openviking_library import (
    CATEGORIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    LEVEL_ABSTRACT,
    LEVEL_DETAIL,
    LEVEL_OVERVIEW,
    LEVELS,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_TOKENS,
    MAX_RETRIEVAL_RESULTS,
    OPENVIKING_DEFAULT_BASE_URL,
    OPENVIKING_PINNED_VERSION,
    ContextItem,
    ContextRetrievalResult,
    OpenVikingBackend,
    RawMatch,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_ISOLATION_VIOLATION,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    TRUST_LEVELS,
    WARNING_CREDENTIAL,
    WARNING_CROSS_TENANT,
    WARNING_FEATURE_DISABLED,
    WARNING_ISOLATION,
    WARNING_L0_L1_ONLY,
    WARNING_MALFORMED,
    WARNING_MALFORMED_ITEM,
    WARNING_PROVENANCE,
    WARNING_RESULT_LIMIT,
    WARNING_SERVICE_UNAVAILABLE,
    WARNING_TIMEOUT,
    WARNING_TRUNCATED,
    estimate_tokens,
    is_uri_within_scope,
    is_valid_project_id,
    project_root_uri,
    text_looks_like_credential,
)

logger = logging.getLogger(__name__)

#: The retrieval contract version. Bumped when the SHAPE a consumer must
#: understand changes (result fields, status vocabulary).
RETRIEVAL_CONTRACT_VERSION = 1

#: A single logical scope selector may be one of these, or a sequence of
#: category names, or the literal ``"library"`` (the whole project library).
SCOPE_LIBRARY = "library"

#: Hard error reasons for the fail-closed violations. Static, value-free.
ERROR_ISOLATION_VIOLATION = "ISOLATION_VIOLATION"
ERROR_CROSS_TENANT = "CROSS_TENANT_VIOLATION"
ERROR_CREDENTIAL_LEAK = "CREDENTIAL_LEAK_VIOLATION"
ERROR_PROVENANCE_MISSING = "PROVENANCE_VIOLATION"
ERROR_MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
ERROR_BACKEND_FAILURE = "BACKEND_FAILURE"
ERROR_TIMEOUT = "RETRIEVAL_TIMEOUT"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenVikingConfig:
    """Application-owned OpenViking configuration.

    ``enabled`` defaults to **False**: OpenViking is optional and off until an
    operator turns it on. ``base_url`` is a localhost HTTP endpoint by default;
    the application never binds or exposes it publicly.
    """

    enabled: bool = False
    base_url: str = OPENVIKING_DEFAULT_BASE_URL
    #: Optional API key. NEVER logged, never serialized into a result.
    api_key: Optional[str] = field(default=None, repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    pinned_version: str = OPENVIKING_PINNED_VERSION

    def to_dict(self) -> Dict[str, Any]:
        """Serializable, SECRET-FREE view. The key is reported by PRESENCE only."""
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "api_key_configured": bool(self.api_key),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "pinned_version": self.pinned_version,
        }


@dataclass(frozen=True)
class RetrievalBudget:
    """Application-owned bounds for ONE retrieval.

    Every field has a hard ceiling the caller cannot raise past the module
    constants, so a caller cannot widen the boundary the application set.
    """

    max_items: int = 8
    max_bytes: int = 32 * 1024
    max_tokens: int = 8_000
    #: When False (the default), only L0/L1 summaries are loaded. L2 detail is
    #: fetched only when a caller explicitly justifies it.
    allow_detail: bool = False

    def normalized(self) -> "RetrievalBudget":
        """Clamp every field to the module ceiling (never widen)."""
        return RetrievalBudget(
            max_items=max(0, min(int(self.max_items), MAX_RETRIEVAL_RESULTS)),
            max_bytes=max(0, min(int(self.max_bytes), MAX_CONTEXT_BYTES)),
            max_tokens=max(0, min(int(self.max_tokens), MAX_CONTEXT_TOKENS)),
            allow_detail=bool(self.allow_detail),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "max_tokens": self.max_tokens,
            "allow_detail": self.allow_detail,
        }


DEFAULT_BUDGET = RetrievalBudget()


def config_from_mapping(data: Optional[Mapping[str, Any]]) -> OpenVikingConfig:
    """Build a config from a mapping (e.g. parsed YAML), fail-closed on shape.

    Only the documented keys are read. An unknown key is ignored rather than
    raising, so a config file may carry forward-compatible keys without
    breaking startup -- but the SECURITY-relevant defaults (disabled, localhost)
    are never weakened by a malformed value.
    """
    data = data or {}
    enabled = data.get("enabled", False)
    base_url = data.get("base_url", OPENVIKING_DEFAULT_BASE_URL)
    if not isinstance(base_url, str) or not base_url.strip():
        base_url = OPENVIKING_DEFAULT_BASE_URL
    timeout = data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    retries = data.get("max_retries", DEFAULT_MAX_RETRIES)
    try:
        retries = int(retries)
    except (TypeError, ValueError):
        retries = DEFAULT_MAX_RETRIES
    retries = max(0, min(retries, 5))
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        api_key = None
    return OpenVikingConfig(
        enabled=bool(enabled),
        base_url=base_url.strip(),
        api_key=api_key,
        timeout_seconds=timeout,
        max_retries=retries,
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def normalize_scope(scope: Any) -> Tuple[str, ...]:
    """Normalize a scope selector into a validated tuple of category names.

    ``"library"`` (or ``None``/empty) means the whole project library. A single
    category string or a sequence of them is validated against
    :data:`~app.core.openviking_library.CATEGORIES`; an unknown category is
    refused rather than silently ignored, so a typo cannot widen or narrow the
    search without the caller noticing.
    """
    if scope is None:
        return ()
    if isinstance(scope, str):
        value = scope.strip()
        if value in ("", SCOPE_LIBRARY, "all", "*"):
            return ()
        items: Sequence[Any] = (value,)
    elif isinstance(scope, (list, tuple, set, frozenset)):
        items = tuple(scope)
    else:
        raise ValueError("invalid scope selector")
    normalized: List[str] = []
    for item in items:
        if not isinstance(item, str) or item not in CATEGORIES:
            raise ValueError("unknown scope category")
        if item not in normalized:
            normalized.append(item)
    return tuple(normalized)


@dataclass
class _Violation:
    """A fail-closed violation observed while normalizing a response."""

    reason: str
    warning: str


class OpenVikingRetrievalAdapter:
    """The one narrow retrieval surface the application exposes.

    Construct with an :class:`OpenVikingConfig` and a backend. When the feature
    is disabled, no backend is required and none is called.
    """

    def __init__(
        self,
        config: Optional[OpenVikingConfig] = None,
        backend: Optional[OpenVikingBackend] = None,
    ) -> None:
        self._config = config or OpenVikingConfig()
        self._backend = backend

    # -- read-only properties ---------------------------------------------

    @property
    def config(self) -> OpenVikingConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    # -- the contract ------------------------------------------------------

    def retrieve_context(
        self,
        query: str,
        project_id: str,
        scope: Any = None,
        budget: Optional[RetrievalBudget] = None,
    ) -> ContextRetrievalResult:
        """Retrieve bounded context for ``project_id``.

        Never raises for an availability problem (disabled, timeout, outage):
        those return an explicit non-ok status with zero items. A programming
        error (invalid project id, unknown scope) raises ``ValueError``, because
        a caller that asked for the wrong thing must hear about it.

        Isolation, provenance, and credential violations FAIL CLOSED: the whole
        result is refused and marked non-ok.
        """
        if not is_valid_project_id(project_id):
            raise ValueError("invalid project id")
        categories = normalize_scope(scope)
        effective_budget = (budget or DEFAULT_BUDGET).normalized()
        scope_label = ",".join(categories) if categories else SCOPE_LIBRARY
        limits = {**effective_budget.to_dict(), "timeout_seconds": self._config.timeout_seconds}

        # --- Feature flag: disabled is an explicit, honest empty result. ---
        if not self.enabled:
            return self._empty(
                status=STATUS_DISABLED,
                project_id=project_id,
                scope_label=scope_label,
                limits=limits,
                error_reason="FEATURE_DISABLED",
                warnings=(WARNING_FEATURE_DISABLED,),
            )

        if self._backend is None:
            return self._empty(
                status=STATUS_UNAVAILABLE,
                project_id=project_id,
                scope_label=scope_label,
                limits=limits,
                error_reason=ERROR_BACKEND_FAILURE,
                warnings=(WARNING_SERVICE_UNAVAILABLE,),
            )

        # --- Bounded query with bounded retries and a wall-clock deadline. --
        target_uri = project_root_uri(project_id)
        requested = max(1, effective_budget.max_items)
        # Fetch a small multiple so category filtering still has enough to
        # choose from, but never unbounded.
        fetch_limit = min(requested * 2, MAX_RETRIEVAL_RESULTS)

        started = time.monotonic()
        matches: Optional[Sequence[RawMatch]] = None
        last_error: Optional[str] = None
        timed_out = False
        attempts = 1 + max(0, int(self._config.max_retries))
        for _attempt in range(attempts):
            if time.monotonic() - started > self._config.timeout_seconds:
                timed_out = True
                break
            try:
                matches = self._backend.find(
                    query=query or "", target_uri=target_uri, limit=fetch_limit
                )
                last_error = None
                break
            except TimeoutError as exc:  # a backend that reports a timeout
                last_error = str(exc) or "timeout"
                timed_out = True
            except Exception as exc:  # an outage or transport failure
                last_error = type(exc).__name__
                matches = None
        if time.monotonic() - started > self._config.timeout_seconds and matches is None:
            timed_out = True

        if matches is None:
            if timed_out:
                return self._empty(
                    status=STATUS_TIMEOUT, project_id=project_id,
                    scope_label=scope_label, limits=limits,
                    error_reason=ERROR_TIMEOUT, warnings=(WARNING_TIMEOUT,),
                )
            return self._empty(
                status=STATUS_UNAVAILABLE, project_id=project_id,
                scope_label=scope_label, limits=limits,
                error_reason=ERROR_BACKEND_FAILURE,
                warnings=(WARNING_SERVICE_UNAVAILABLE,),
            )

        return self._normalize(
            matches=matches,
            project_id=project_id,
            categories=categories,
            scope_label=scope_label,
            budget=effective_budget,
            limits=limits,
        )

    # -- normalization -----------------------------------------------------

    def _normalize(
        self,
        *,
        matches: Sequence[RawMatch],
        project_id: str,
        categories: Tuple[str, ...],
        scope_label: str,
        budget: RetrievalBudget,
        limits: Dict[str, Any],
    ) -> ContextRetrievalResult:
        warnings: List[str] = []
        violations: List[_Violation] = []

        try:
            materialized = list(matches)
        except TypeError:
            return self._empty(
                status=STATUS_ERROR, project_id=project_id,
                scope_label=scope_label, limits=limits,
                error_reason=ERROR_MALFORMED_RESPONSE, warnings=(WARNING_MALFORMED,),
            )

        scope_uri = project_root_uri(project_id)
        accepted: List[ContextItem] = []
        total_matches = 0

        for raw in materialized:
            total_matches += 1
            if not isinstance(raw, RawMatch) or not isinstance(raw.uri, str) or not raw.uri:
                warnings.append(WARNING_MALFORMED_ITEM)
                continue

            # (1) Isolation: the URI must be strictly inside the project scope.
            if not is_uri_within_scope(raw.uri, scope_uri):
                violations.append(_Violation(ERROR_ISOLATION_VIOLATION, WARNING_ISOLATION))
                continue

            # (2) Provenance + cross-tenant: the record must exist, be well
            #     formed, and name THIS project.
            record = raw.record if isinstance(raw.record, Mapping) else None
            if record is None:
                violations.append(_Violation(ERROR_PROVENANCE_MISSING, WARNING_PROVENANCE))
                continue
            record_project = str(record.get("project_id", ""))
            if record_project and record_project != project_id:
                violations.append(_Violation(ERROR_CROSS_TENANT, WARNING_CROSS_TENANT))
                continue
            source_id = str(record.get("source_id", "")).strip()
            source_revision = str(record.get("source_revision", "")).strip()
            if not source_id or not source_revision:
                violations.append(_Violation(ERROR_PROVENANCE_MISSING, WARNING_PROVENANCE))
                continue

            category = str(record.get("category", "") or raw.category or "")
            # (3) Category allowlist: a category outside the request is filtered.
            if categories and category not in categories:
                continue

            trust = str(record.get("trust", "")) or "external"
            if trust not in TRUST_LEVELS:
                trust = "external"

            # (4) Credential-shaped content fails closed.
            haystack = " ".join(
                part for part in (raw.abstract, raw.overview, raw.content) if part
            )
            if text_looks_like_credential(haystack):
                violations.append(_Violation(ERROR_CREDENTIAL_LEAK, WARNING_CREDENTIAL))
                continue

            level, body = self._select_body(raw, budget)
            title = str(record.get("source_id", "") or raw.uri).strip() or raw.uri
            accepted.append(
                ContextItem(
                    uri=raw.uri,
                    source_id=source_id,
                    source_revision=source_revision,
                    trust=trust,
                    category=category,
                    level=level,
                    score=float(raw.score or 0.0),
                    title=title[:200],
                    body=body,
                    summary=(raw.abstract or "")[:256],
                    estimated_tokens=estimate_tokens(body),
                )
            )

        # --- Fail closed: any hard violation refuses the whole result. ------
        if violations:
            reason = violations[0].reason
            status = (
                STATUS_ISOLATION_VIOLATION
                if reason in (ERROR_ISOLATION_VIOLATION, ERROR_CROSS_TENANT)
                else STATUS_ERROR
            )
            unique_warnings = tuple(dict.fromkeys(v.warning for v in violations))
            return self._empty(
                status=status, project_id=project_id, scope_label=scope_label,
                limits=limits, error_reason=reason, warnings=unique_warnings,
                total_items=total_matches,
            )

        # --- Deterministic ordering, then bounded selection. ----------------
        accepted.sort(key=lambda item: (-item.score, item.uri))

        kept: List[ContextItem] = []
        total_bytes = 0
        total_tokens = 0
        truncated = False
        for item in accepted:
            item_bytes = len(item.body) + len(item.title) + len(item.uri)
            if len(kept) >= budget.max_items:
                truncated = True
                warnings.append(WARNING_RESULT_LIMIT)
                break
            if total_bytes + item_bytes > budget.max_bytes:
                truncated = True
                warnings.append(WARNING_TRUNCATED)
                break
            if total_tokens + item.estimated_tokens > budget.max_tokens:
                truncated = True
                warnings.append(WARNING_TRUNCATED)
                break
            kept.append(item)
            total_bytes += item_bytes
            total_tokens += item.estimated_tokens

        if not budget.allow_detail:
            warnings.append(WARNING_L0_L1_ONLY)

        return ContextRetrievalResult(
            status=STATUS_OK,
            project_id=project_id,
            scope=scope_label,
            items=tuple(kept),
            total_items=total_matches,
            returned_items=len(kept),
            estimated_tokens=total_tokens,
            total_bytes=total_bytes,
            truncated=truncated,
            degraded=False,
            error_reason="",
            warnings=tuple(dict.fromkeys(warnings)),
            limits=dict(limits),
        )

    @staticmethod
    def _select_body(raw: RawMatch, budget: RetrievalBudget) -> Tuple[int, str]:
        """Choose the body + level for a match, honoring the L2 policy.

        L0/L1 summaries are preferred; L2 detail is used only when the budget
        explicitly allows it. A match with no content at its requested level
        falls back to the nearest available lower level rather than inventing
        text.
        """
        if budget.allow_detail and raw.content:
            return LEVEL_DETAIL, raw.content
        if raw.overview:
            return LEVEL_OVERVIEW, raw.overview
        if raw.abstract:
            return LEVEL_ABSTRACT, raw.abstract
        # Nothing usable: an empty body, at the lowest level. Never fabricated.
        return LEVEL_ABSTRACT, ""

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _empty(
        *,
        status: str,
        project_id: str,
        scope_label: str,
        limits: Dict[str, Any],
        error_reason: str,
        warnings: Tuple[str, ...],
        total_items: int = 0,
    ) -> ContextRetrievalResult:
        return ContextRetrievalResult(
            status=status,
            project_id=project_id,
            scope=scope_label,
            items=(),
            total_items=total_items,
            returned_items=0,
            estimated_tokens=0,
            total_bytes=0,
            truncated=False,
            degraded=status not in (STATUS_OK,),
            error_reason=error_reason,
            warnings=tuple(warnings),
            limits=dict(limits),
        )


# ---------------------------------------------------------------------------
# HTTP backend (thin, lazy, never imported at module load)
# ---------------------------------------------------------------------------


class OpenVikingLiveNotQualified(RuntimeError):
    """Raised when a live-only operation is attempted without qualification."""


class HttpOpenVikingBackend(OpenVikingBackend):
    """A thin HTTP client over a REAL OpenViking server.

    Only the documented retrieval route is implemented
    (``POST /api/v1/search/find``). Write/record operations raise
    :class:`OpenVikingLiveNotQualified` because live ingestion was NOT
    qualified in D4a -- the offline fake backend is the qualified ingestion
    path. Reporting a live write as working without having exercised it would
    be exactly the dishonest claim this batch forbids.

    ``httpx`` is imported lazily inside the call, so importing this module never
    requires a network client.
    """

    def __init__(self, config: OpenVikingConfig) -> None:
        self._config = config

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            # The key is sent as a header and NEVER logged.
            headers["X-API-Key"] = self._config.api_key
        return headers

    def find(
        self, *, query: str, target_uri: str, limit: int, level: Optional[int] = None
    ) -> Sequence[RawMatch]:
        import httpx  # lazy: never imported at module load

        payload: Dict[str, Any] = {
            "query": query,
            "target_uri": target_uri,
            "limit": max(0, int(limit)),
        }
        if level is not None:
            payload["level"] = level
        url = self._config.base_url.rstrip("/") + "/api/v1/search/find"
        try:
            response = httpx.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self._config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError("openviking request timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("openviking transport failure") from exc
        if response.status_code != 200:
            raise RuntimeError(f"openviking returned status {response.status_code}")
        try:
            body = response.json()
        except Exception as exc:
            raise RuntimeError("openviking response was not JSON") from exc
        return _parse_find_response(body)

    def get_record(self, project_id: str, uri: str):
        raise OpenVikingLiveNotQualified(
            "live ingestion is not qualified in D4a; use the offline backend"
        )

    def put_resource(self, project_id: str, uri: str, content: bytes, record) -> None:
        raise OpenVikingLiveNotQualified(
            "live ingestion is not qualified in D4a; use the offline backend"
        )


def _parse_find_response(body: Any) -> Sequence[RawMatch]:
    """Translate a real ``find`` response into :class:`RawMatch` records.

    Tolerant of the documented envelope shapes (``result`` / top-level, and
    ``resources``/``memories``/``skills`` buckets), but never inventive: an
    item that does not carry a URI is dropped, and a body that is not a mapping
    yields an empty list (the adapter then reports malformed).
    """
    if not isinstance(body, Mapping):
        raise RuntimeError("openviking response body was not an object")
    result = body.get("result", body)
    if not isinstance(result, Mapping):
        return []
    buckets: List[Mapping[str, Any]] = []
    for key in ("resources", "memories", "skills"):
        value = result.get(key)
        if isinstance(value, list):
            buckets.extend(v for v in value if isinstance(v, Mapping))
    # Some deployments return a flat ``results`` list.
    flat = result.get("results")
    if isinstance(flat, list):
        buckets.extend(v for v in flat if isinstance(v, Mapping))

    matches: List[RawMatch] = []
    for entry in buckets:
        uri = entry.get("uri")
        if not isinstance(uri, str) or not uri:
            continue
        matches.append(
            RawMatch(
                uri=uri,
                context_type=str(entry.get("context_type", "resource")),
                level=int(entry.get("level", LEVEL_ABSTRACT) or 0),
                abstract=str(entry.get("abstract", "") or ""),
                overview=str(entry.get("overview", "") or ""),
                content=str(entry.get("content", "") or ""),
                score=float(entry.get("score", 0.0) or 0.0),
                category=str(entry.get("category", "") or ""),
                match_reason=str(entry.get("match_reason", "") or ""),
                record=entry.get("record") if isinstance(entry.get("record"), Mapping) else None,
            )
        )
    return matches


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------


def build_adapter(
    config: Optional[OpenVikingConfig] = None,
    backend: Optional[OpenVikingBackend] = None,
) -> OpenVikingRetrievalAdapter:
    """Build an adapter, choosing the backend from the config.

    When a backend is supplied (the offline fake, in tests) it is used as-is.
    Otherwise a live backend is built -- but only lazily reached, so constructing
    the adapter touches no network.

    D4a.1: the enabled path prefers the *live* backend
    (:class:`~app.core.openviking_live.LiveOpenVikingBackend`), which implements
    retrieval AND qualified ingestion behind the same protocol. If that module
    is unavailable the retrieval-only D4a HTTP backend is used as a fallback, so
    enabling the flag never fails to construct. The import is lazy to avoid a
    circular import (the live module imports this one).
    """
    effective = config or OpenVikingConfig()
    if backend is None and effective.enabled:
        try:
            from app.core.openviking_live import LiveOpenVikingBackend

            backend = LiveOpenVikingBackend(effective)
        except Exception:
            backend = HttpOpenVikingBackend(effective)
    return OpenVikingRetrievalAdapter(effective, backend)


__all__ = [
    "DEFAULT_BUDGET",
    "ERROR_BACKEND_FAILURE",
    "ERROR_CREDENTIAL_LEAK",
    "ERROR_CROSS_TENANT",
    "ERROR_ISOLATION_VIOLATION",
    "ERROR_MALFORMED_RESPONSE",
    "ERROR_PROVENANCE_MISSING",
    "ERROR_TIMEOUT",
    "HttpOpenVikingBackend",
    "OpenVikingConfig",
    "OpenVikingLiveNotQualified",
    "OpenVikingRetrievalAdapter",
    "RETRIEVAL_CONTRACT_VERSION",
    "RetrievalBudget",
    "SCOPE_LIBRARY",
    "build_adapter",
    "config_from_mapping",
    "normalize_scope",
]
