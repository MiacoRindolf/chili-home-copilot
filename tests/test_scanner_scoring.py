"""Tests for trading scanner scoring and thesis generation."""
import json
from types import SimpleNamespace

import pytest

from app.services.trading.thesis import (
    _SIGNAL_TRANSLATIONS,
    build_conversational_thesis,
    make_plain_english,
)


class _RowsQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class _ScannerWeightDb:
    def __init__(self, patterns):
        self._patterns = list(patterns)

    def query(self, model):
        if getattr(model, "__name__", "") == "ScanPattern":
            return _RowsQuery(self._patterns)
        return _RowsQuery([])


def _scan_pattern(*, win_rate, confidence):
    return SimpleNamespace(
        rules_json=json.dumps({"conditions": [{"indicator": "macd"}]}),
        win_rate=win_rate,
        confidence=confidence,
    )


def test_pattern_weight_evidence_score_normalizes_probability_scales():
    from app.services.trading import scanner

    assert scanner._pattern_weight_evidence_score(
        _scan_pattern(win_rate=0.6, confidence=75.0),
    ) == pytest.approx(0.45)
    assert scanner._pattern_weight_evidence_score(
        _scan_pattern(win_rate=60.0, confidence=0.75),
    ) == pytest.approx(0.45)
    assert scanner._pattern_weight_evidence_score(
        _scan_pattern(win_rate=0.0, confidence=1.0),
    ) == 0.0


def test_evolve_strategy_weights_preserves_zero_pattern_win_rate(monkeypatch):
    from app.services.trading import scanner

    monkeypatch.setattr(scanner, "_adaptive_weights", dict(scanner._DEFAULT_WEIGHTS))
    monkeypatch.setattr(
        "app.services.trading.portfolio.get_insights",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                pattern_description="unrelated",
                confidence=0.5,
                evidence_count=0,
            ),
        ],
    )
    monkeypatch.setattr(
        "app.services.trading.learning.log_learning_event",
        lambda *_args, **_kwargs: None,
    )

    out = scanner.evolve_strategy_weights(
        _ScannerWeightDb(
            [
                _scan_pattern(win_rate=0.0, confidence=1.0),
                _scan_pattern(win_rate=0.0, confidence=1.0),
            ],
        ),
    )

    default = scanner._DEFAULT_WEIGHTS["macd_positive_bonus"]
    assert out["current_weights"]["macd_positive_bonus"] < default
    assert any("pattern_score=0.00" in detail for detail in out["details"])


class TestBuildConversationalThesis:
    def test_bullish_basic(self):
        pick = {
            "ticker": "AAPL",
            "signal": "buy",
            "signals": ["EMA stacking bullish", "Volume surge"],
            "indicators": {"rsi": 45},
            "risk_reward": 2.0,
        }
        thesis = build_conversational_thesis(pick)
        assert "AAPL" in thesis
        assert "bullish" in thesis.lower()
        assert "2.0:1" in thesis

    def test_bullish_oversold_rsi(self):
        pick = {
            "ticker": "TSLA",
            "signal": "buy",
            "signals": ["RSI oversold"],
            "indicators": {"rsi": 28},
        }
        thesis = build_conversational_thesis(pick)
        assert "28" in thesis
        assert "TSLA" in thesis

    def test_bearish(self):
        pick = {
            "ticker": "GME",
            "signal": "sell",
            "signals": ["MACD bearish cross"],
            "indicators": {},
        }
        thesis = build_conversational_thesis(pick)
        assert "bearish" in thesis.lower()

    def test_with_backtest(self):
        pick = {
            "ticker": "SPY",
            "signal": "buy",
            "signals": [],
            "indicators": {},
            "best_strategy": "EMA Cross",
            "backtest_return": 18.3,
            "backtest_win_rate": 62,
        }
        thesis = build_conversational_thesis(pick)
        assert "EMA Cross" in thesis
        assert "62%" in thesis

    def test_empty_signals(self):
        pick = {"ticker": "XYZ", "signal": "watch", "signals": [], "indicators": {}}
        thesis = build_conversational_thesis(pick)
        assert "XYZ" in thesis
        assert len(thesis) > 10

    def test_unmatched_signals_pass_through(self):
        pick = {
            "ticker": "ABC",
            "signal": "buy",
            "signals": ["Some very custom signal that is long enough"],
            "indicators": {},
        }
        thesis = build_conversational_thesis(pick)
        assert "custom signal" in thesis.lower()


class TestMakePlainEnglish:
    def test_buy_signal(self):
        scored = {
            "signal": "buy",
            "signals": ["oversold bounce", "volume surge"],
            "risk_level": "medium",
        }
        result = make_plain_english(scored, "")
        assert "buying opportunity" in result.lower()
        assert "bounce" in result.lower()

    def test_sell_signal(self):
        scored = {
            "signal": "sell",
            "signals": ["overbought"],
            "risk_level": "high",
        }
        result = make_plain_english(scored, "")
        assert "overpriced" in result.lower() or "profits" in result.lower()
        assert "HIGH" in result

    def test_crypto_asset_label(self):
        scored = {
            "ticker": "BTC-USD",
            "signal": "buy",
            "signals": [],
            "risk_level": "medium",
        }
        result = make_plain_english(scored, "")
        assert "coin" in result.lower()

    def test_watch_signal(self):
        scored = {
            "signal": "watch",
            "signals": ["squeeze firing"],
            "risk_level": "low",
        }
        result = make_plain_english(scored, "")
        assert "watching" in result.lower() or "watch" in result.lower()
        assert "breaking out" in result.lower()


class TestSignalTranslations:
    def test_all_keys_produce_sentences(self):
        for key, sentence in _SIGNAL_TRANSLATIONS.items():
            assert len(sentence) > 10, f"Translation for '{key}' is too short"
            assert sentence.endswith("."), f"Translation for '{key}' should end with period"

    def test_known_signals_covered(self):
        expected = {"rsi oversold", "macd bullish cross", "ema stacking bullish", "volume surge", "breakout"}
        assert expected.issubset(set(_SIGNAL_TRANSLATIONS.keys()))
