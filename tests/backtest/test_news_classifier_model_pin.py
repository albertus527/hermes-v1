"""R2.8.1 — canonical pin vs provider wire model ID.

The §11.5 canonical pin is ``<provider>/<vendor>/<model>[@<version>]`` and
is the PERSISTED model identity. The provider's wire model ID is that pin
with its leading provider segment removed, because ``call_llm(provider=...)``
already selects the endpoint. Forwarding the full provider-prefixed pin to
the wire makes OpenRouter answer ``400 ... is not a valid model ID``.

These tests are pure parser/contract tests: no LLM call, no DB, no network.
"""

from __future__ import annotations

import pytest

from backtest.news.classifier import (
    CLASSIFICATION_TEMPERATURE,
    NEWS_SCHEMA_V3_JSON_SCHEMA,
    NewsClassifierClient,
    PinnedModelMissing,
    build_messages,
    parse_pinned_model,
    parse_pinned_model_parts,
)

# Real-shaped canonical pins (names are TEST DATA for a generic parser —
# nothing about them is special-cased in the implementation).
GLM_PIN = "openrouter/z-ai/glm-5.3-flash"
QWEN_PIN = "openrouter/qwen/qwen3.7-flash"
LUNA_PIN = "openrouter/openai/gpt-6-luna"


# -- 1/2/4/5/6: provider-prefixed pin parses and strips the provider -------

@pytest.mark.parametrize(
    "canonical,wire",
    [
        pytest.param(GLM_PIN, "z-ai/glm-5.3-flash", id="glm-5.3-flash"),
        pytest.param(QWEN_PIN, "qwen/qwen3.7-flash", id="qwen3.7-flash"),
        pytest.param(LUNA_PIN, "openai/gpt-6-luna", id="gpt-6-luna-shaped"),
    ],
)
def test_openrouter_pin_splits_into_provider_and_wire_model(canonical, wire):
    canonical_version, provider, wire_model_id = parse_pinned_model_parts(
        canonical)
    assert provider == "openrouter"
    assert wire_model_id == wire
    # The provider prefix is stripped ONLY for the wire; it stays in identity.
    assert wire_model_id != canonical
    assert canonical.startswith(f"{provider}/")


@pytest.mark.parametrize(
    "canonical,wire",
    [
        (GLM_PIN, "z-ai/glm-5.3-flash"),
        (QWEN_PIN, "qwen/qwen3.7-flash"),
        (LUNA_PIN, "openai/gpt-6-luna"),
        ("openrouter/z-ai/glm-5.3-flash@v2026-10-03", "z-ai/glm-5.3-flash"),
        ("openrouter/anthropic/claude-test-model@2026-01-01",
         "anthropic/claude-test-model"),
    ],
)
def test_parse_pinned_model_returns_canonical_then_wire(canonical, wire):
    assert parse_pinned_model(canonical) == (canonical, wire)


# -- 3: canonical persisted pin remains unchanged ---------------------------

@pytest.mark.parametrize("canonical", [GLM_PIN, QWEN_PIN, LUNA_PIN])
def test_canonical_pin_is_returned_verbatim(canonical):
    canonical_version, _wire = parse_pinned_model(canonical)
    assert canonical_version == canonical


def test_client_separates_canonical_pin_from_wire_model_id():
    client = NewsClassifierClient(GLM_PIN)
    assert client.model_version == GLM_PIN          # persisted identity
    assert client.provider == "openrouter"          # wire endpoint
    assert client.model_id == "z-ai/glm-5.3-flash"  # wire model ID


def test_version_suffix_stays_on_canonical_only():
    canonical = "openrouter/z-ai/glm-5.3-flash@v3"
    client = NewsClassifierClient(canonical)
    assert client.model_version == canonical
    assert client.model_id == "z-ai/glm-5.3-flash"
    assert "@" not in client.model_id


# -- 7: malformed pins fail closed -----------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param(None, id="none"),
        pytest.param("gpt-4o", id="bare-model-no-provider"),
        pytest.param("openrouter", id="provider-only"),
        pytest.param("openrouter/", id="provider-trailing-slash"),
        pytest.param("openrouter/z-ai/", id="no-model"),
        pytest.param("openrouter//glm-5.3-flash", id="empty-vendor"),
        pytest.param("openrouter/z-ai/glm-5.3-flash/", id="trailing-slash"),
        pytest.param("openrouter/ z-ai/glm-5.3-flash", id="leading-space-seg"),
    ],
)
def test_malformed_pin_fails_closed(bad):
    with pytest.raises(PinnedModelMissing):
        parse_pinned_model_parts(bad)


def test_malformed_pin_fails_closed_in_client():
    with pytest.raises(PinnedModelMissing):
        NewsClassifierClient("openrouter/z-ai")


# -- 8: unsupported provider stays explicit ---------------------------------

@pytest.mark.parametrize(
    "pin",
    [
        pytest.param("anthropic/claude-sonnet/x", id="anthropic-direct"),
        pytest.param("Openrouter/z-ai/glm-5.3-flash", id="wrong-case"),
        pytest.param("bogusprovider/vendor/model", id="unknown-provider"),
    ],
)
def test_unsupported_provider_is_not_silently_reinterpreted(pin):
    """An unknown provider fails closed; it is NEVER coerced to openrouter."""
    with pytest.raises(PinnedModelMissing) as exc:
        parse_pinned_model_parts(pin)
    assert "cannot wire to" in str(exc.value)


def test_non_provider_style_string_fails_closed():
    """A colon-URL style value is malformed, not reinterpreted as a pin."""
    with pytest.raises(PinnedModelMissing):
        parse_pinned_model_parts(
            "custom:https://example.invalid/v1/vendor/model")


def test_supported_provider_constant_names_endpoints_not_models():
    from backtest.news.classifier import SUPPORTED_WIRE_PROVIDERS
    assert "openrouter" in SUPPORTED_WIRE_PROVIDERS
    # No model/vendor name may leak into the endpoint allowlist.
    for name in SUPPORTED_WIRE_PROVIDERS:
        assert "/" not in name
        assert "glm" not in name and "qwen" not in name and "luna" not in name


# -- 9: no change to prompt / schema / classification semantics -------------

def test_prompt_is_unchanged_and_ticker_headline_only():
    messages = build_messages("AAPL", "Apple beats Q4 estimates")
    assert [m["role"] for m in messages] == ["system", "user"]
    # Source is never a classification input (P-4).
    assert not any("source" in m["content"].lower() for m in messages)
    assert "Apple beats Q4 estimates" in messages[1]["content"]


def test_schema_and_temperature_unchanged():
    assert CLASSIFICATION_TEMPERATURE == 0.0
    schema = NEWS_SCHEMA_V3_JSON_SCHEMA
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "category", "direction", "severity", "ma_role", "confidence"}
    assert schema["properties"]["ma_role"]["enum"] == [
        "TARGET", "ACQUIRER", "NEITHER"]


def test_classified_payload_records_full_canonical_pin():
    """Classification semantics unchanged AND provenance keeps the full pin."""

    class _FakeResponse:
        def __init__(self, content):
            self.choices = [type("C", (), {
                "message": type("M", (), {"content": content})()})()]

    answer = ('{"category": "EARNINGS", "direction": "BULLISH", '
              '"severity": "HIGH", "ma_role": "NEITHER", "confidence": 0.8}')
    client = NewsClassifierClient(
        GLM_PIN, llm_call=lambda messages: _FakeResponse(answer))
    import datetime as dt
    _classification, payload = client.classify(
        ticker="AAPL",
        headline_text="Apple beats Q4 estimates",
        source="finnhub",
        published_at=dt.datetime(2026, 1, 5, 9, 0,
                                 tzinfo=dt.timezone.utc))
    assert payload["model_version"] == GLM_PIN
    assert payload["category"] == "EARNINGS"
    assert payload["ma_role"] == "NEITHER"
    assert payload["schema_version"] == "news_schema_v3"