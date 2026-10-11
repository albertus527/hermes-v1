"""Four-field human-label validation (§11.4 / §11.1 conditional rules)."""

from __future__ import annotations

import pytest

from backtest.calibration_reviewer.labels import (
    ENUMS,
    InvalidHumanLabelError,
    is_complete_label,
    validate_human_label,
)
from trading_core.news_effects import CATEGORIES, DIRECTIONS, MA_ROLES, SEVERITIES


def test_enums_match_authoritative_sources():
    assert ENUMS["category"] == list(CATEGORIES)
    assert ENUMS["direction"] == list(DIRECTIONS)
    assert ENUMS["severity"] == list(SEVERITIES)
    assert ENUMS["ma_role"] == list(MA_ROLES)


def test_valid_non_ma_label():
    lab = validate_human_label({"category": "EARNINGS", "direction": "BULLISH",
                                "severity": "MEDIUM", "ma_role": "NEITHER"})
    assert lab.category == "EARNINGS" and lab.ma_role == "NEITHER"


def test_valid_ma_target_label():
    lab = validate_human_label({"category": "M&A", "direction": "BULLISH",
                                "severity": "HIGH", "ma_role": "TARGET"})
    assert lab.ma_role == "TARGET"


def test_ma_neither_is_legal():
    lab = validate_human_label({"category": "M&A", "direction": "NEUTRAL",
                                "severity": "LOW", "ma_role": "NEITHER"})
    assert lab.ma_role == "NEITHER"


def test_non_ma_requires_neither():
    with pytest.raises(InvalidHumanLabelError):
        validate_human_label({"category": "EARNINGS", "direction": "BULLISH",
                              "severity": "LOW", "ma_role": "TARGET"})


@pytest.mark.parametrize("field,value", [
    ("category", "NOT_A_CATEGORY"),
    ("direction", "UP"),
    ("severity", "EXTREME"),
    ("ma_role", "BUYER"),
])
def test_unknown_enum_rejected(field, value):
    label = {"category": "OTHER", "direction": "NEUTRAL", "severity": "LOW",
             "ma_role": "NEITHER"}
    label[field] = value
    with pytest.raises(InvalidHumanLabelError):
        validate_human_label(label)


def test_missing_field_rejected():
    with pytest.raises(InvalidHumanLabelError):
        validate_human_label({"category": "OTHER", "direction": "NEUTRAL",
                              "severity": "LOW"})


def test_extra_field_rejected():
    with pytest.raises(InvalidHumanLabelError):
        validate_human_label({"category": "OTHER", "direction": "NEUTRAL",
                              "severity": "LOW", "ma_role": "NEITHER",
                              "confidence": 0.9})


def test_empty_value_rejected():
    with pytest.raises(InvalidHumanLabelError):
        validate_human_label({"category": "", "direction": "NEUTRAL",
                              "severity": "LOW", "ma_role": "NEITHER"})


def test_non_dict_rejected():
    with pytest.raises(InvalidHumanLabelError):
        validate_human_label(["EARNINGS", "BULLISH", "LOW", "NEITHER"])  # type: ignore[arg-type]


def test_is_complete_label_truthy_and_falsy():
    assert is_complete_label({"category": "OTHER", "direction": "NEUTRAL",
                              "severity": "LOW", "ma_role": "NEITHER"})
    assert not is_complete_label({"category": "OTHER", "direction": "NEUTRAL",
                                  "severity": "LOW"})
    assert not is_complete_label(None)
    assert not is_complete_label({"category": "EARNINGS", "direction": "UP",
                                  "severity": "LOW", "ma_role": "NEITHER"})


def test_no_implicit_default_selection():
    """An empty object must NOT validate (no defaulted fields)."""
    with pytest.raises(InvalidHumanLabelError):
        validate_human_label({})
