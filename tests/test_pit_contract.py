"""Unit tests for app.services.trading.pit_contract."""
from __future__ import annotations

import json
import pytest

from app.services.trading import pit_contract as pit


class TestClassify:
    def test_allowed_indicator(self):
        assert pit.classify("rsi_14") == "pit"
        assert pit.classify("macd_histogram") == "pit"
        assert pit.classify("ema_stack") == "pit"
        assert pit.classify("news_sentiment") == "pit"
        assert pit.classify("regime") == "pit"
        assert pit.classify("predicted_score") == "pit"

    def test_forbidden_indicator(self):
        assert pit.classify("future_return_5d") == "non_pit"
        assert pit.classify("future_return_10d") == "non_pit"
        assert pit.classify("tp_hit") == "non_pit"
        assert pit.classify("realized_pnl") == "non_pit"
        assert pit.classify("expected_return") == "non_pit"

    def test_unknown_indicator(self):
        assert pit.classify("some_secret_feature") == "unknown"
        assert pit.classify("my_neural_output") == "unknown"

    def test_empty_or_none(self):
        assert pit.classify(None) == "unknown"
        assert pit.classify("") == "unknown"
        assert pit.classify("   ") == "unknown"

    def test_whitespace_strip(self):
        assert pit.classify("  rsi_14  ") == "pit"

    def test_case_sensitive(self):
        # Deliberately case-sensitive — upper-case variants must be added
        # explicitly if the miner ever emits them.
        assert pit.classify("RSI_14") == "unknown"


class TestClassifyRules:
    def test_all_pit_conditions(self):
        rules = {
            "conditions": [
                {"indicator": "rsi_14", "op": "<", "value": 30},
                {"indicator": "macd_histogram", "op": ">", "value": 0},
                {"indicator": "adx", "op": ">", "value": 20},
            ]
        }
        result = pit.classify_rules(rules)
        assert result["pit"] == ["rsi_14", "macd_histogram", "adx"]
        assert result["non_pit"] == []
        assert result["unknown"] == []

    def test_contains_forbidden(self):
        rules = {
            "conditions": [
                {"indicator": "rsi_14", "op": "<", "value": 30},
                {"indicator": "future_return_5d", "op": ">", "value": 0.02},
            ]
        }
        result = pit.classify_rules(rules)
        assert result["pit"] == ["rsi_14"]
        assert result["non_pit"] == ["future_return_5d"]
        assert result["unknown"] == []

    def test_contains_unknown(self):
        rules = {
            "conditions": [
                {"indicator": "rsi_14", "op": "<", "value": 30},
                {"indicator": "my_secret_feature", "op": ">", "value": 0.5},
            ]
        }
        result = pit.classify_rules(rules)
        assert result["pit"] == ["rsi_14"]
        assert result["non_pit"] == []
        assert result["unknown"] == ["my_secret_feature"]

    def test_ref_field_also_classified(self):
        rules = {
            "conditions": [
                {"indicator": "macd", "op": ">", "ref": "macd_signal"},
                {"indicator": "close", "op": ">", "ref": "future_return_5d"},
            ]
        }
        result = pit.classify_rules(rules)
        assert "macd" in result["pit"]
        assert "macd_signal" in result["pit"]
        assert "close" in result["pit"]
        assert "future_return_5d" in result["non_pit"]

    def test_scanner_style_field_key(self):
        rules = {
            "conditions": [
                {"field": "rsi_14", "op": "lt", "value": 30},
                {"field": "future_return_5d", "op": "gt", "value": 0.02},
            ]
        }
        result = pit.classify_rules(rules)
        assert result["pit"] == ["rsi_14"]
        assert result["non_pit"] == ["future_return_5d"]

    def test_string_json_input(self):
        rules_str = json.dumps(
            {"conditions": [{"indicator": "rsi_14", "op": "<", "value": 30}]}
        )
        result = pit.classify_rules(rules_str)
        assert result["pit"] == ["rsi_14"]

    def test_malformed_json_string(self):
        result = pit.classify_rules("{broken json")
        assert result == {"pit": [], "non_pit": [], "unknown": []}

    def test_none_input(self):
        assert pit.classify_rules(None) == {"pit": [], "non_pit": [], "unknown": []}

    def test_empty_dict(self):
        assert pit.classify_rules({}) == {"pit": [], "non_pit": [], "unknown": []}

    def test_missing_conditions_key(self):
        assert pit.classify_rules({"name": "foo"}) == {"pit": [], "non_pit": [], "unknown": []}

    def test_conditions_not_a_list(self):
        assert pit.classify_rules({"conditions": "not a list"}) == {
            "pit": [],
            "non_pit": [],
            "unknown": [],
        }

    def test_deduplicates_same_field(self):
        rules = {
            "conditions": [
                {"indicator": "rsi_14", "op": "<", "value": 30},
                {"indicator": "rsi_14", "op": ">", "value": 20},
            ]
        }
        result = pit.classify_rules(rules)
        assert result["pit"] == ["rsi_14"]

    def test_preserves_first_seen_order(self):
        rules = {
            "conditions": [
                {"indicator": "macd_histogram", "op": ">", "value": 0},
                {"indicator": "rsi_14", "op": "<", "value": 30},
                {"indicator": "adx", "op": ">", "value": 20},
            ]
        }
        result = pit.classify_rules(rules)
        assert result["pit"] == ["macd_histogram", "rsi_14", "adx"]

    def test_skips_non_dict_condition_entries(self):
        rules = {
            "conditions": [
                {"indicator": "rsi_14", "op": "<", "value": 30},
                "not a dict",
                None,
                42,
            ]
        }
        result = pit.classify_rules(rules)
        assert result["pit"] == ["rsi_14"]

    def test_skips_missing_indicator_field(self):
        rules = {"conditions": [{"op": "<", "value": 30}]}
        result = pit.classify_rules(rules)
        assert result == {"pit": [], "non_pit": [], "unknown": []}


class TestBuiltinPatternCoverage:
    """Every field the built-in and community-seed patterns emit must be
    allowlisted. This is a regression guard: if someone adds a new seed
    pattern referencing an unknown field without updating ALLOWED_INDICATORS,
    this test fails."""

    def test_builtin_patterns_classify_pit(self):
        from app.services.trading.pattern_engine import (
            _BUILTIN_INTRADAY_PATTERNS,
            _BUILTIN_PATTERNS,
        )
        for bp in (*_BUILTIN_PATTERNS, *_BUILTIN_INTRADAY_PATTERNS):
            result = pit.classify_rules(bp.get("rules_json"))
            assert result["non_pit"] == [], (
                f"pattern {bp.get('name')!r} has non_pit fields: {result['non_pit']}"
            )
            assert result["unknown"] == [], (
                f"pattern {bp.get('name')!r} has unknown fields: {result['unknown']}"
            )

    def test_community_seed_patterns_classify_pit(self):
        from app.services.trading.pattern_engine import _COMMUNITY_SEED_PATTERNS
        for bp in _COMMUNITY_SEED_PATTERNS:
            result = pit.classify_rules(bp.get("rules_json"))
            assert result["non_pit"] == [], (
                f"pattern {bp.get('name')!r} has non_pit fields: {result['non_pit']}"
            )
            assert result["unknown"] == [], (
                f"pattern {bp.get('name')!r} has unknown fields: {result['unknown']}"
            )

    def test_cross_tf_prefix_classification(self):
        assert pit.classify("1d:rsi_14") == "pit"
        assert pit.classify("1h:rsi_14") == "pit"
        assert pit.classify("5m:macd") == "pit"
        assert pit.classify("1d:future_return_5d") == "non_pit"
        assert pit.classify("bogus_tf:rsi_14") == "unknown"

    def test_forbidden_set_is_disjoint_from_allowed(self):
        assert pit.ALLOWED_INDICATORS.isdisjoint(pit.FORBIDDEN_INDICATORS)
