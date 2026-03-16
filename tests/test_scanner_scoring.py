"""Tests for trading scanner scoring and thesis generation."""
import pytest

from app.services.trading.thesis import (
    _SIGNAL_TRANSLATIONS,
    build_conversational_thesis,
    make_plain_english,
)


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
