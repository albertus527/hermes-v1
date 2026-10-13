"""N6-A — Decision Engine modes and configuration.

Proves the N6 requirement that the Decision Engine has two independent modes
(``disabled``/``shadow``/``active``) and that **no configuration error can turn
the engine on**. The production default must be ``disabled`` in both modes.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.decision_modes import (  # noqa: E402
    DECISION_ENGINE_MODES,
    DEFAULT_DECISION_ENGINE_MODE,
    MAX_CONFIGURED_CANDIDATES,
    DecisionEngineConfig,
    DecisionEngineMode,
    config_from_mapping,
    parse_mode,
)


class ModeSetTests(unittest.TestCase):
    def test_mode_set_is_exactly_three(self):
        self.assertEqual(DECISION_ENGINE_MODES, ("disabled", "shadow", "active"))

    def test_default_mode_is_disabled(self):
        self.assertIs(DEFAULT_DECISION_ENGINE_MODE, DecisionEngineMode.DISABLED)

    def test_default_config_is_disabled_in_both_modes(self):
        cfg = DecisionEngineConfig()
        self.assertIs(cfg.intake_mode, DecisionEngineMode.DISABLED)
        self.assertIs(cfg.design_mode, DecisionEngineMode.DISABLED)
        self.assertFalse(cfg.intake_enabled)
        self.assertFalse(cfg.design_enabled)
        self.assertFalse(cfg.intake_active)
        self.assertFalse(cfg.design_active)


class ParseModeTests(unittest.TestCase):
    def test_accepts_every_valid_mode(self):
        for mode in DECISION_ENGINE_MODES:
            self.assertEqual(parse_mode(mode).value, mode)

    def test_is_case_and_whitespace_insensitive(self):
        self.assertIs(parse_mode("  ACTIVE "), DecisionEngineMode.ACTIVE)
        self.assertIs(parse_mode("Shadow"), DecisionEngineMode.SHADOW)

    def test_unknown_mode_falls_back_to_disabled(self):
        for bad in ("enabled", "on", "true", "ACTIVE!", "disabled ", "1", ""):
            self.assertIs(parse_mode(bad), DecisionEngineMode.DISABLED, bad)

    def test_non_string_falls_back_to_disabled(self):
        for bad in (None, 1, True, [], {}, object()):
            self.assertIs(parse_mode(bad), DecisionEngineMode.DISABLED)

    def test_accepts_an_enum_instance(self):
        self.assertIs(parse_mode(DecisionEngineMode.ACTIVE), DecisionEngineMode.ACTIVE)


class ConfigFromMappingTests(unittest.TestCase):
    def test_missing_block_is_disabled(self):
        cfg = config_from_mapping(None)
        self.assertFalse(cfg.intake_enabled)
        self.assertFalse(cfg.design_enabled)

    def test_empty_block_is_disabled(self):
        cfg = config_from_mapping({})
        self.assertFalse(cfg.intake_enabled)
        self.assertFalse(cfg.design_enabled)

    def test_malformed_mode_never_enables(self):
        cfg = config_from_mapping({"intake_mode": "yes-please", "design_mode": 7})
        self.assertFalse(cfg.intake_enabled)
        self.assertFalse(cfg.design_enabled)

    def test_valid_modes_are_read(self):
        cfg = config_from_mapping({"intake_mode": "shadow", "design_mode": "active"})
        self.assertIs(cfg.intake_mode, DecisionEngineMode.SHADOW)
        self.assertIs(cfg.design_mode, DecisionEngineMode.ACTIVE)
        self.assertTrue(cfg.intake_enabled)
        self.assertFalse(cfg.intake_active)
        self.assertTrue(cfg.design_active)

    def test_out_of_range_score_floor_falls_back(self):
        self.assertEqual(config_from_mapping({"score_floor": 5}).score_floor, 0.5)
        self.assertEqual(config_from_mapping({"score_floor": -1}).score_floor, 0.5)
        self.assertEqual(config_from_mapping({"score_floor": "x"}).score_floor, 0.5)

    def test_valid_score_floor_is_read(self):
        self.assertEqual(config_from_mapping({"score_floor": 0.7}).score_floor, 0.7)

    def test_max_candidates_is_clamped_and_never_widened(self):
        self.assertEqual(
            config_from_mapping({"max_candidates": 999}).max_candidates,
            MAX_CONFIGURED_CANDIDATES,
        )
        self.assertEqual(config_from_mapping({"max_candidates": -3}).max_candidates, 0)
        self.assertEqual(config_from_mapping({"max_candidates": "x"}).max_candidates, 4)

    def test_provider_names_are_trimmed_or_none(self):
        cfg = config_from_mapping({"intake_provider": "  openviking ", "design_provider": ""})
        self.assertEqual(cfg.intake_provider, "openviking")
        self.assertIsNone(cfg.design_provider)


class DefaultYamlTests(unittest.TestCase):
    """The shipped config must default both modes to disabled."""

    def test_shipped_config_is_disabled(self):
        import yaml

        path = Path(__file__).resolve().parents[1] / "config" / "default.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        block = (raw.get("website_builder") or {}).get("decision_engine")
        self.assertIsInstance(block, dict, "decision_engine block must exist")
        cfg = config_from_mapping(block)
        self.assertFalse(cfg.intake_enabled, "intake_mode must default to disabled")
        self.assertFalse(cfg.design_enabled, "design_mode must default to disabled")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
