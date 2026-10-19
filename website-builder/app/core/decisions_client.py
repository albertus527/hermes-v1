"""Shared OpenRouter Decisions API transport for the N8 Decision roles.

ONE transport, two roles. The HTTP contract is identical for Intake and Design;
only the ``questions`` payload differs, so the transport stays role-agnostic.

Design constraints (owner-mandated):

* **A real bounded operational deadline.** ``urllib`` timeouts apply per socket
  operation, so a slow-drip peer can exceed a nominal timeout. This module runs
  the call on a worker thread and joins with the deadline, which makes the
  wall-clock bound real rather than nominal. Cancellation is therefore
  cooperative: the worker is abandoned (daemon), its result discarded, and the
  caller returns ``TIMEOUT`` immediately.
* **No retries, no substitutions.** Exactly one HTTP request per call.
* **No secrets in any returned value.** The ``Authorization`` header is never
  echoed, and every error string is redacted before it leaves this module.
* **Fail closed.** Every non-200, unparseable body, or over-deadline call returns
  a structured failure. Callers translate that into "use FAST" -- never into an
  acting label.
* **Text and multimodal, one contract.** ``state`` may be a string, a JSON
  object, or an array of parts carrying ``image_url`` media. Every payload is
  validated locally before transmission, so an unsupported or oversized payload
  never reaches the provider and never becomes an opaque 400.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

#: Default endpoint. Overridable by configuration; never user input.
DEFAULT_BASE_URL = "https://openrouter.ai/api/alpha/decisions"

#: The Decisions ``state`` payload.
#:
#: The documented contract accepts a plain string, a JSON object, or an ARRAY of
#: parts. Media parts are::
#:
#:     {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
#:
#: Remote ``http(s)`` URLs are NOT fetched by the API, so a non-``data:`` url is
#: rejected locally rather than surfacing as an opaque 400.
StatePayload = Union[str, Mapping[str, Any], Sequence[Any]]

#: Bounded resource limits for multimodal payloads.
#:
#: Deliberately BELOW the documented provider ceilings (200 questions per
#: request, 128 images for GPT-6 Luna Decisions) so a regression cannot silently
#: walk into the provider's own limit.
MAX_QUESTION_COUNT = 64
MAX_IMAGES_PER_REQUEST = 32
#: One image, measured as DECODED bytes (not base64 length).
MAX_IMAGE_BYTES = 2 * 1024 * 1024
#: Total decoded state: text bytes plus decoded image bytes.
MAX_STATE_BYTES = 8 * 1024 * 1024
#: The serialised request body, which base64 inflation can push past the
#: decoded-size checks above.
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024

#: Image MIME types the Decisions contract accepts.
ALLOWED_IMAGE_MIME: Tuple[str, ...] = ("image/png", "image/jpeg", "image/webp")

#: ``data:`` URL prefixes that correspond to the allowed MIME types.
_ALLOWED_DATA_PREFIXES: Tuple[str, ...] = tuple(
    f"data:{mime};base64," for mime in ALLOWED_IMAGE_MIME
)

#: Total wall-clock deadline for one Decisions call, in seconds.
#:
#: p95 observed in the Stage 3 confirmation run was 705 ms (Intake) and 734 ms
#: (Design); the slowest single response was 1,782 ms. 20 s is ~11x the worst
#: observed and still far below any interactive timeout.
DEFAULT_DEADLINE_SECONDS = 20.0

#: Outcome codes. Stable strings -- telemetry and tests key off them.
OK = "ok"
TIMEOUT = "timeout"
HTTP_ERROR = "http_error"
MALFORMED = "malformed"
TRANSPORT_ERROR = "transport_error"

#: Payload-validation codes. These are returned BEFORE any network call, so an
#: unacceptable payload can never reach the provider.
UNSUPPORTED_STATE = "unsupported_state"
TOO_MANY_QUESTIONS = "too_many_questions"
TOO_MANY_IMAGES = "too_many_images"
PAYLOAD_TOO_LARGE = "payload_too_large"
INVALID_IMAGE_PART = "invalid_image_part"

#: Redaction marker.
_REDACTED = "[REDACTED]"

#: Substrings that must never survive into a returned error string.
_SECRET_MARKERS = ("sk-or-", "sk-", "Bearer ")


def redact(value: Any) -> str:
    """Return *value* as a string with credential-shaped substrings removed.

    Defensive: error bodies can echo request headers on some gateways, so every
    string that leaves this module passes through here.
    """
    if value is None:
        return ""
    out = str(value)
    for marker in _SECRET_MARKERS:
        start = 0
        while True:
            idx = out.find(marker, start)
            if idx < 0:
                break
            head = idx + len(marker)
            end = head
            while end < len(out) and (out[end].isalnum() or out[end] in "-_"):
                end += 1
            out = out[:head] + _REDACTED + out[end:]
            start = head + len(_REDACTED)
    return out


class DecisionsResult:
    """The outcome of one Decisions call.

    ``ok`` is the only success state. ``payload`` is the decoded JSON body on
    success and ``None`` otherwise. ``elapsed_ms`` is always populated so latency
    telemetry works for failures too.
    """

    __slots__ = ("status", "payload", "error_code", "detail", "elapsed_ms", "http_status")

    def __init__(
        self,
        status: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        error_code: str = "",
        detail: str = "",
        elapsed_ms: int = 0,
        http_status: Optional[int] = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.error_code = error_code
        self.detail = detail
        self.elapsed_ms = elapsed_ms
        self.http_status = http_status

    @property
    def ok(self) -> bool:
        return self.status == OK

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"DecisionsResult(status={self.status!r}, http={self.http_status!r}, "
            f"error={self.error_code!r}, elapsed_ms={self.elapsed_ms})"
        )


def _http_status_class(code: Optional[int]) -> str:
    """Map an HTTP status to a stable error class for telemetry."""
    if code is None:
        return "no_status"
    if code == 429:
        return "http_429"
    if code == 529:
        return "http_529"
    if 500 <= code < 600:
        return "http_5xx"
    if 400 <= code < 500:
        return "http_4xx"
    return f"http_{code}"


def _post_once(
    url: str,
    body: bytes,
    api_key: str,
    socket_timeout: float,
) -> Tuple[Optional[int], bytes, str]:
    """One HTTP POST. Returns (status, raw body, error detail).

    ``socket_timeout`` bounds each individual socket operation; the *total*
    deadline is enforced by the caller via thread join.
    """
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=socket_timeout) as response:
            return response.status, response.read(), ""
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:  # pragma: no cover - body already consumed
            payload = b""
        return exc.code, payload, ""
    except Exception as exc:  # noqa: BLE001 - every transport fault is a failure
        return None, b"", f"{type(exc).__name__}: {exc}"


def validate_state_payload(state: StatePayload, questions: Mapping[str, Any]) -> str:
    """Return a stable error code, or ``""`` when the payload is sendable.

    Runs BEFORE the request is issued, so an unacceptable payload can never
    reach the network. Every branch is bounded by an application-owned
    constant -- no server hint is trusted.

    The accepted shapes follow the documented Decisions contract: a plain
    string, a JSON object, or an array of parts where media parts are
    ``{"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}``.
    """
    if not isinstance(questions, Mapping):
        return UNSUPPORTED_STATE
    if len(questions) > MAX_QUESTION_COUNT:
        return TOO_MANY_QUESTIONS

    if isinstance(state, str):
        return PAYLOAD_TOO_LARGE if len(state.encode("utf-8")) > MAX_STATE_BYTES else ""

    if isinstance(state, Mapping):
        parts: Sequence[Any] = (state,)
    elif isinstance(state, (list, tuple)):
        parts = state
    else:
        # ``bytes``, ``set``, ``None``, numbers: not a documented state shape.
        return UNSUPPORTED_STATE

    images = 0
    decoded_total = 0
    for part in parts:
        if isinstance(part, str):
            decoded_total += len(part.encode("utf-8"))
            continue
        if not isinstance(part, Mapping):
            return UNSUPPORTED_STATE
        if part.get("type") != "image_url":
            # A non-image mapping is legitimate JSON context, not a media part.
            continue

        images += 1
        if images > MAX_IMAGES_PER_REQUEST:
            return TOO_MANY_IMAGES

        container = part.get("image_url")
        if not isinstance(container, Mapping):
            return INVALID_IMAGE_PART
        url = container.get("url")
        if not isinstance(url, str):
            return INVALID_IMAGE_PART
        prefix = next((p for p in _ALLOWED_DATA_PREFIXES if url.startswith(p)), None)
        if prefix is None:
            # Rejects remote http(s) urls, bare base64, and unsupported MIME.
            return INVALID_IMAGE_PART
        try:
            raw = base64.b64decode(url[len(prefix):], validate=True)
        except Exception:  # noqa: BLE001 - any decode fault is a malformed part
            return INVALID_IMAGE_PART
        if len(raw) > MAX_IMAGE_BYTES:
            return PAYLOAD_TOO_LARGE
        decoded_total += len(raw)

    if decoded_total > MAX_STATE_BYTES:
        return PAYLOAD_TOO_LARGE
    return ""


def post_decision(
    *,
    model: str,
    state: StatePayload,
    questions: Mapping[str, Any],
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> DecisionsResult:
    """POST one Decisions request and return a structured result.

    Exactly one HTTP request is issued. There is no retry path and no fallback
    model: a failure returns a non-ok result and the caller must degrade to FAST.

    The total wall-clock deadline is enforced by joining a worker thread, so a
    peer that stalls mid-read cannot exceed it.

    ``state`` may be text or a multimodal array. It is validated locally first;
    an unacceptable payload returns a validation code and never touches the
    network.
    """
    if not model or not isinstance(model, str):
        return DecisionsResult(TRANSPORT_ERROR, error_code="invalid_model")
    if not api_key:
        return DecisionsResult(TRANSPORT_ERROR, error_code="missing_credential")

    invalid = validate_state_payload(state, questions)
    if invalid:
        return DecisionsResult(TRANSPORT_ERROR, error_code=invalid)

    body = json.dumps(
        {"model": model, "state": state, "questions": dict(questions)}
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BODY_BYTES:
        # Base64 inflation can exceed the decoded-size checks above.
        return DecisionsResult(TRANSPORT_ERROR, error_code=PAYLOAD_TOO_LARGE)

    box: dict = {}
    started = time.monotonic()

    def _worker() -> None:
        try:
            box["value"] = _post_once(base_url, body, api_key, deadline_seconds)
        except BaseException as exc:  # noqa: BLE001 - never escape the thread
            box["value"] = (None, b"", f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(
        target=_worker, name="n8-decisions-call", daemon=True
    )
    worker.start()
    worker.join(deadline_seconds)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if worker.is_alive():
        # Deadline exceeded. The worker is a daemon and is abandoned; its result,
        # should it ever arrive, is discarded because nothing reads the box again.
        return DecisionsResult(
            TIMEOUT,
            error_code="deadline_exceeded",
            detail=f"exceeded {deadline_seconds:.1f}s",
            elapsed_ms=elapsed_ms,
        )

    status, raw, detail = box.get("value", (None, b"", "no_result"))

    if status is None:
        return DecisionsResult(
            TRANSPORT_ERROR,
            error_code="transport",
            detail=redact(detail)[:300],
            elapsed_ms=elapsed_ms,
        )

    if status != 200:
        return DecisionsResult(
            HTTP_ERROR,
            error_code=_http_status_class(status),
            detail=redact(raw.decode("utf-8", errors="replace"))[:300],
            elapsed_ms=elapsed_ms,
            http_status=status,
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return DecisionsResult(
            MALFORMED,
            error_code="unparseable_body",
            detail=f"{type(exc).__name__}",
            elapsed_ms=elapsed_ms,
            http_status=status,
        )

    if not isinstance(payload, Mapping):
        return DecisionsResult(
            MALFORMED,
            error_code="non_object_body",
            elapsed_ms=elapsed_ms,
            http_status=status,
        )

    return DecisionsResult(
        OK, payload=payload, elapsed_ms=elapsed_ms, http_status=status
    )


def extract_choice(
    payload: Mapping[str, Any], question_name: str
) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """Pull ``choice`` / ``confidence`` / ``cost`` out of a Decisions payload.

    Returns ``(choice, confidence, cost)``. Any element may be ``None``. The
    real envelope (verified across 288 benchmark requests) nests answers under
    ``answers.<question_name>`` and reports cost as ``usage.cost``.
    """
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        return None, None, None
    answer = answers.get(question_name)
    if not isinstance(answer, Mapping):
        return None, None, None

    choice = answer.get("choice")
    if not isinstance(choice, str) or not choice.strip():
        choice = None
    else:
        choice = choice.strip()

    confidence = answer.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    usage = payload.get("usage")
    cost = None
    if isinstance(usage, Mapping):
        raw_cost = usage.get("cost")
        try:
            if raw_cost is not None:
                cost = float(raw_cost)
        except (TypeError, ValueError):
            cost = None

    return choice, confidence, cost


def resolved_model_id(payload: Mapping[str, Any]) -> str:
    """Return the provider-reported model id, or ``""`` when absent."""
    value = payload.get("model")
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_DEADLINE_SECONDS",
    "StatePayload",
    "MAX_QUESTION_COUNT",
    "MAX_IMAGES_PER_REQUEST",
    "MAX_IMAGE_BYTES",
    "MAX_STATE_BYTES",
    "MAX_REQUEST_BODY_BYTES",
    "ALLOWED_IMAGE_MIME",
    "OK",
    "TIMEOUT",
    "HTTP_ERROR",
    "MALFORMED",
    "TRANSPORT_ERROR",
    "UNSUPPORTED_STATE",
    "TOO_MANY_QUESTIONS",
    "TOO_MANY_IMAGES",
    "PAYLOAD_TOO_LARGE",
    "INVALID_IMAGE_PART",
    "DecisionsResult",
    "validate_state_payload",
    "post_decision",
    "extract_choice",
    "resolved_model_id",
    "redact",
]
