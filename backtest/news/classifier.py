"""R2.7 §11.5/§20 Phase 2 — the news-classifier client (POPULATION TIME ONLY).

This module is the ONLY place in the backtest tree that may reach an LLM,
and it does so exclusively through ``agent.auxiliary_client.call_llm`` with
a pinned single-provider route (§11.5; plan §14 item 9): explicit
``provider=``/``model=``/``temperature=0`` and a verified resolved route —
never ``auto`` resolution, never the agent's main model.

The deterministic core and the replay path NEVER import this module's
call path. Tests inject a fake ``llm_call`` callable — no test ever
performs a live LLM call, and no backtest replay does either (§21 item 18).

The exact pinned model identifier is an EXTERNAL BLOCKER (§11.5 [MISSING
SOURCE CONTENT]): it must arrive via config (``backtest.pinned_model``)
as ``openrouter/<provider>/<model>@<version>``. Nothing here invents or
defaults to a concrete identifier; an empty pin fails closed with
:class:`PinnedModelMissing`.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable

from trading_core.news_effects import (
    SCHEMA_VERSION_V3,
    Classification,
    classify_with_keyword_fallback,
    headline_hash as compute_headline_hash,
)

from backtest.news.cache import (
    MalformedClassificationError,
    validate_classification_payload,
)

# §11.5: temperature is pinned at 0 for reproducibility.
CLASSIFICATION_TEMPERATURE = 0.0

# The strict news_schema_v3 JSON schema handed to the model via
# response_format (structured output). Effect fields only — the model must
# never emit free-form prose that reaches trading logic.
NEWS_SCHEMA_V3_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string",
                     "enum": ["EARNINGS", "GUIDANCE", "ANALYST", "M&A",
                              "REGULATORY", "LEGAL", "PRODUCT", "MACRO",
                              "INSIDER", "CAPITAL_RETURN", "OTHER"]},
        "direction": {"type": "string",
                      "enum": ["BULLISH", "BEARISH", "NEUTRAL"]},
        "severity": {"type": "string",
                     "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
        "ma_role": {"type": "string",
                    "enum": ["TARGET", "ACQUIRER", "NEITHER"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["category", "direction", "severity", "ma_role",
                 "confidence"],
    "additionalProperties": False,
}


class PinnedModelMissing(Exception):
    """The exact pinned ``openrouter/<provider>/<model>@<version>``
    identifier is not set in config (§11.5 blocker). Raised fail-closed —
    the population job refuses to run rather than guessing a model."""


class RouteMismatchError(Exception):
    """The resolved auxiliary route did not match the pinned identifier
    (plan §14 item 9: never rely on auto resolution)."""


def parse_pinned_model(pinned: str) -> tuple[str, str]:
    """Split ``openrouter/<provider>/<model>@<version>`` into
    ``(model_version, model_id)`` where model_version is the full pinned
    identifier (persisted as ``model_version`` in the cache) and model_id
    is what is passed to ``call_llm(model=...)``.

    ``<version>`` may be empty (identifier without an @version suffix) —
    the whole string is still the cache's model_version.
    """
    pinned = (pinned or "").strip()
    if not pinned:
        raise PinnedModelMissing(
            "backtest.pinned_model is not set — the exact "
            "openrouter/<provider>/<model>@<version> identifier is an "
            "unresolved external prerequisite (§11.5)")
    if not pinned.startswith("openrouter/"):
        raise PinnedModelMissing(
            f"pinned model {pinned!r} must follow "
            "'openrouter/<provider>/<model>@<version>'")
    model_id = pinned.split("@", 1)[0]
    if not model_id or model_id.endswith("/"):
        raise PinnedModelMissing(
            f"pinned model {pinned!r} has no <provider>/<model> part")
    return pinned, model_id


def build_messages(ticker: str, headline_text: str) -> list[dict[str, str]]:
    """The classification prompt (headline classification ONLY — the one
    task the LLM is permitted by R2.7). Source is deliberately absent:
    ``source`` is never a classification input (P-4)."""
    system = (
        "You are a deterministic financial-news classifier. Classify the "
        "given headline for the given ticker into exactly the fields of "
        "the provided JSON schema. Base every field on the headline text "
        "and the ticker ONLY. Never invent facts. Output strict JSON only."
    )
    user = (
        f"Ticker: {ticker}\n"
        f"Headline: {headline_text}\n"
        "Classify this headline. Respond with JSON matching the schema."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _extract_content(response: Any) -> str:
    """Pull the text content out of an OpenAI-shaped response object."""
    try:
        return (response.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError) as exc:
        raise MalformedClassificationError(
            f"LLM response has no text content: {exc!r}") from exc


class NewsClassifierClient:
    """Population-time classifier client.

    Parameters
    ----------
    pinned_model:
        The full pinned identifier (validated by :func:`parse_pinned_model`).
    llm_call:
        The callable used to reach the LLM. Defaults to a lazy wrapper over
        ``agent.auxiliary_client.call_llm`` — imported ONLY inside a
        classification call so importing this module (or the whole
        backtest.news package) never drags the LLM stack in. Tests and
        offline population inject their own callable.
    """

    def __init__(self, pinned_model: str,
                 llm_call: Callable[..., Any] | None = None):
        self.model_version, self.model_id = parse_pinned_model(pinned_model)
        self.schema_version = SCHEMA_VERSION_V3
        self._llm_call = llm_call

    def _call(self, messages: list[dict[str, str]]) -> Any:
        if self._llm_call is not None:
            return self._llm_call(messages=messages)
        # The one authorized live-LLM reach (§20 Phase 2). Single-provider
        # pinned routing per §11.5 / plan §14 item 9: explicit provider and
        # model, temperature 0, structured output, no auto fallback.
        from agent.auxiliary_client import call_llm  # lazy: population only
        route_info: dict[str, str] = {}
        response = call_llm(
            provider="openrouter",
            model=self.model_id,
            messages=messages,
            temperature=CLASSIFICATION_TEMPERATURE,
            extra_body={"response_format": {
                "type": "json_schema",
                "json_schema": {"name": "news_schema_v3",
                                "schema": NEWS_SCHEMA_V3_JSON_SCHEMA,
                                "strict": True},
            }},
            route_info=route_info,
        )
        # Verify the resolved route matched the pin (never trust auto).
        if route_info.get("model") not in (None, self.model_id,
                                           f"openrouter/{self.model_id}"):
            raise RouteMismatchError(
                f"resolved route {route_info!r} does not match pinned "
                f"model {self.model_id!r}")
        return response

    def classify(self, *, ticker: str, headline_text: str,
                 source: str, published_at: _dt.datetime) -> tuple[Classification, dict]:
        """Classify one timed headline.

        Returns ``(classification, payload)`` where ``payload`` is the
        full strict news_schema_v3 JSON object (including ``schema_version``
        and ``model_version`` for reproducibility) ready for
        :meth:`NewsClassificationCache.insert`.

        Deterministic post-processing:
        - the model's JSON is validated strictly (fail-closed
          :class:`MalformedClassificationError` on any deviation);
        - §11.3 keyword fallback is applied (BEFORE the §11.2 mapping,
          persisted as ``keyword_override``) so cache-only replay
          reproduces it without headline-time LLM calls;
        - the FP-4 ``headline_hash`` is recomputed from the headline text —
          the model never supplies identity fields.
        """
        response = self._call(build_messages(ticker, headline_text))
        import json
        try:
            raw = json.loads(_extract_content(response))
        except (ValueError, TypeError) as exc:
            raise MalformedClassificationError(
                f"model output is not valid JSON: {exc}") from exc
        # The model supplies effect fields only; identity, clock, source,
        # schema/model provenance are stamped deterministically here.
        payload = {
            "ticker": ticker,
            "category": raw.get("category"),
            "direction": raw.get("direction"),
            "severity": raw.get("severity"),
            "ma_role": raw.get("ma_role"),
            "confidence": raw.get("confidence"),
            "published_at": published_at.isoformat(),
            "headline_hash": compute_headline_hash(headline_text),
            "source": source,
            "keyword_override": False,
            "schema_version": self.schema_version,
            "model_version": self.model_version,
        }
        # Validate the LLM-shaped payload BEFORE the keyword override so a
        # malformed model answer fails closed here.
        validated = validate_classification_payload(payload)
        # §11.3 deterministic keyword fallback at classification time.
        final = classify_with_keyword_fallback(
            ticker=validated.ticker,
            headline_text=headline_text,
            category=validated.category,
            direction=validated.direction,
            severity=validated.severity,
            ma_role=validated.ma_role,
            confidence=validated.confidence,
            published_at=validated.published_at,
            source=validated.source,
        )
        payload["keyword_override"] = final.keyword_override
        payload["direction"] = final.direction
        payload["severity"] = final.severity
        return final, payload
