"""Regression tests for live Trade stop/target field normalization."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from app.models.trading import Trade
from app.services.trading import live_exit_engine as lee
from app.services.trading.exit_config_defaults import classify_exit_params


def _live_trade(
    *,
    direction: str = "long",
    entry_price: float = 100.0,
    stop_loss: float | None = 95.0,
    take_profit: float | None = 110.0,
) -> Trade:
    return Trade(
        ticker="TEST",
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        quantity=1.0,
        status="open",
        entry_date=datetime.now(UTC).replace(tzinfo=None),
    )


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _FakeQuery(self._rows)


def _quiet_exit_engine(monkeypatch) -> None:
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": None,
            "use_bos": False,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        lambda *_args, **_kwargs: None,
    )


def _stub_bos_market_data(monkeypatch) -> None:
    import numpy as np
    import pandas as pd

    def _fake_fetch_ohlcv_df(_ticker, period=None, interval=None, start=None, end=None):
        idx = pd.date_range("2026-01-01", periods=30, freq="D")
        highs = [100.0] * 25 + [101.0, 102.0, 103.0, 104.0, 104.0]
        lows = [99.0] * 25 + [98.0, 97.0, 96.0, 95.0, 95.0]
        return pd.DataFrame(
            {
                "Open": 100.0,
                "High": highs,
                "Low": lows,
                "Close": 100.0,
                "Volume": 1_000_000,
            },
            index=idx,
        )

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        _fake_fetch_ohlcv_df,
    )
    monkeypatch.setattr(
        "app.services.trading.indicator_core.compute_atr",
        lambda _highs, _lows, closes, period=14: np.array([1.0] * len(closes)),
    )


def _stub_atr_market_data(monkeypatch, atr_value) -> None:
    import numpy as np
    import pandas as pd

    fetch_calls = {"count": 0}

    def _fake_fetch_ohlcv_df(_ticker, period=None, interval=None, start=None, end=None):
        fetch_calls["count"] += 1
        idx = pd.date_range("2026-01-01", periods=30, freq="D")
        return pd.DataFrame(
            {
                "Open": 100.0,
                "High": 101.0,
                "Low": 99.0,
                "Close": 100.0,
                "Volume": 1_000_000,
            },
            index=idx,
        )

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        _fake_fetch_ohlcv_df,
    )
    monkeypatch.setattr(
        "app.services.trading.indicator_core.compute_atr",
        lambda _highs, _lows, closes, period=14: np.array([atr_value] * len(closes)),
    )
    return fetch_calls


@pytest.mark.parametrize("zero_value", [0, 0.0, "0"])
def test_exit_parity_sample_pct_preserves_explicit_zero(zero_value):
    assert lee._exit_parity_sample_pct(zero_value) == 0.0


@pytest.mark.parametrize("bad_value", [None, "", "NaN", math.nan, math.inf, True, False])
def test_exit_parity_sample_pct_defaults_unproven_values(bad_value):
    assert lee._exit_parity_sample_pct(bad_value) == 1.0


def test_phase_b_shadow_parity_preserves_zero_sample_setting(monkeypatch):
    from app import config as app_config
    from app.services.trading import exit_parity_metric

    captured: dict[str, float] = {}

    def _capture_sample_pct(**kwargs):
        captured["sample_pct"] = kwargs["sample_pct"]
        return False

    monkeypatch.setattr(app_config.settings, "brain_exit_engine_mode", "shadow")
    monkeypatch.setattr(app_config.settings, "brain_exit_engine_parity_sample_pct", 0.0)
    monkeypatch.setattr(app_config.settings, "brain_exit_engine_ops_log_enabled", False)
    monkeypatch.setattr(
        exit_parity_metric,
        "should_persist_parity_row",
        _capture_sample_pct,
    )

    trade = _live_trade(stop_loss=95.0, take_profit=110.0)

    lee._phase_b_shadow_parity(
        db=object(),
        trade=trade,
        exit_cfg={
            "trailing_enabled": False,
            "max_bars": None,
            "use_bos": False,
            "partial_at_1r": False,
        },
        current_price=100.0,
        atr=None,
        swing_low_val=None,
        swing_high_val=None,
        legacy_result={"action": "hold"},
    )

    assert captured["sample_pct"] == 0.0


@pytest.mark.parametrize("bad_price", [0.0, -1.0, math.nan, math.inf, "not-a-price"])
def test_invalid_current_price_is_non_actionable(monkeypatch, bad_price):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": True,
            "max_bars": 1,
            "use_bos": True,
            "partial_at_1r": True,
        },
    )

    def _must_not_fetch(*_args, **_kwargs):
        raise AssertionError("invalid quote should not fetch market data")

    def _must_not_log_parity(**_kwargs):
        raise AssertionError("invalid quote should not enter parity logging")

    monkeypatch.setattr("app.services.trading.market_data.fetch_ohlcv_df", _must_not_fetch)
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", _must_not_log_parity)
    trade = _live_trade(stop_loss=95.0, take_profit=110.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=bad_price)

    assert result["action"] == "hold"
    assert result["skip_reason"] == "invalid_current_price"


@pytest.mark.parametrize("bad_atr", [0.0, -1.0, math.nan, math.inf])
def test_invalid_atr_does_not_drive_trailing_or_bos(monkeypatch, bad_atr):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": True,
            "trailing_atr_mult": 2.0,
            "max_bars": None,
            "use_bos": True,
            "bos_buffer_pct": 0.5,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    fetch_calls = _stub_atr_market_data(monkeypatch, bad_atr)
    trade = _live_trade(stop_loss=95.0, take_profit=110.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=100.0)

    assert result["action"] == "hold"
    assert result["atr"] is None
    assert "trailing_stop" not in result
    assert "bos_level" not in result
    assert fetch_calls["count"] == 1


def test_invalid_trailing_atr_mult_does_not_invert_trailing_stop(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": True,
            "trailing_atr_mult": -2.0,
            "max_bars": None,
            "use_bos": False,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    _stub_atr_market_data(monkeypatch, 1.0)
    trade = _live_trade(stop_loss=95.0, take_profit=110.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=100.0)

    assert result["action"] == "hold"
    assert result["atr"] == 1.0
    assert "trailing_atr_mult_used" not in result
    assert "trailing_stop" not in result


@pytest.mark.parametrize(
    "quote",
    [
        None,
        {},
        {"price": 0.0},
        {"price": -1.0},
        {"price": math.nan},
        {"price": math.inf},
        {"price": "not-a-price"},
    ],
)
def test_run_exit_engine_skips_invalid_quotes_before_evaluation(monkeypatch, quote):
    trade = _live_trade()
    db = _FakeDb([trade])
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda _ticker: quote,
    )

    def _must_not_evaluate(*_args, **_kwargs):
        raise AssertionError("invalid quote should not reach exit evaluation")

    monkeypatch.setattr(lee, "compute_live_exit_levels", _must_not_evaluate)

    result = lee.run_exit_engine(db)

    assert result["evaluated"] == 0
    assert result["all"] == []
    assert result["actions"] == []
    assert result["partial_actions"] == []
    assert result["skipped_invalid_quotes"] == 1


def test_run_exit_engine_normalizes_valid_quote_before_evaluation(monkeypatch):
    trade = _live_trade()
    db = _FakeDb([trade])
    seen = {}
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda _ticker: {"price": "101.25"},
    )

    def _fake_evaluate(_db, _trade, price):
        seen["price"] = price
        return {"action": "hold"}

    monkeypatch.setattr(lee, "compute_live_exit_levels", _fake_evaluate)

    result = lee.run_exit_engine(db)

    assert seen["price"] == 101.25
    assert result["evaluated"] == 1
    assert result["all"][0]["current_price"] == 101.25
    assert result["skipped_invalid_quotes"] == 0


def test_live_trade_uses_stop_loss_column_for_hard_stop(monkeypatch):
    _quiet_exit_engine(monkeypatch)
    trade = _live_trade(stop_loss=95.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=94.0)

    assert result["action"] == "exit_stop"
    assert result["exit_price"] == 95.0


def test_live_trade_uses_take_profit_column_for_hard_target(monkeypatch):
    _quiet_exit_engine(monkeypatch)
    trade = _live_trade(stop_loss=95.0, take_profit=110.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=111.0)

    assert result["action"] == "exit_target"
    assert result["exit_price"] == 110.0


def test_short_missing_stop_fallback_is_above_entry(monkeypatch):
    _quiet_exit_engine(monkeypatch)
    trade = _live_trade(direction="short", stop_loss=None, take_profit=None)

    result = lee.compute_live_exit_levels(object(), trade, current_price=100.0)

    assert result["action"] == "hold"


def test_short_bos_uses_recent_swing_high(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": None,
            "use_bos": True,
            "bos_buffer_pct": 0.5,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    _stub_bos_market_data(monkeypatch)
    trade = _live_trade(
        direction="short",
        entry_price=100.0,
        stop_loss=110.0,
        take_profit=90.0,
    )

    result = lee.compute_live_exit_levels(object(), trade, current_price=105.0)

    assert result["action"] == "exit_bos"
    assert result["exit_price"] == 105.0
    assert result["bos_level"] == 104.52


def test_hard_stop_wins_when_stop_and_bos_fire_same_tick(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": None,
            "use_bos": True,
            "bos_buffer_pct": 0.5,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    _stub_bos_market_data(monkeypatch)
    trade = _live_trade(stop_loss=95.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=94.0)

    assert result["action"] == "exit_stop"
    assert result["exit_price"] == 95.0
    assert result["bos_level"] == 94.525


def test_bos_wins_over_time_decay_when_both_fire(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": 1,
            "use_bos": True,
            "bos_buffer_pct": 0.5,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    monkeypatch.setattr(lee, "_compute_bars_held", lambda _db, _trade: 99)
    _stub_bos_market_data(monkeypatch)
    trade = _live_trade(stop_loss=80.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=94.0)

    assert result["action"] == "exit_bos"
    assert result["exit_price"] == 94.0
    assert "bars_held" not in result


def test_malformed_bos_buffer_falls_back_instead_of_suppressing_bos(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": None,
            "use_bos": True,
            "bos_buffer_pct": "not-a-number",
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    _stub_bos_market_data(monkeypatch)
    trade = _live_trade(stop_loss=80.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=94.0)

    assert result["action"] == "exit_bos"
    assert result["bos_level"] == 94.525


def test_malformed_max_bars_does_not_crash_or_time_decay(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": "not-a-number",
            "use_bos": False,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    monkeypatch.setattr(lee, "_compute_bars_held", lambda _db, _trade: 99)
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        lambda *_args, **_kwargs: None,
    )
    trade = _live_trade(stop_loss=80.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=100.0)

    assert result["action"] == "hold"
    assert "bars_held" not in result


def test_integer_string_max_bars_still_time_decays(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": "20",
            "use_bos": False,
            "partial_at_1r": False,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    monkeypatch.setattr(lee, "_compute_bars_held", lambda _db, _trade: 21)
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        lambda *_args, **_kwargs: None,
    )
    trade = _live_trade(stop_loss=80.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=100.0)

    assert result["action"] == "exit_time_decay"
    assert result["bars_held"] == 21


def test_load_exit_config_infers_backtest_classifier_defaults_for_missing_config():
    from app.models.trading import ScanPattern

    rules_json = {"conditions": [{"indicator": "rsi_14", "op": "<=", "value": 40}]}
    pat = ScanPattern(
        id=42,
        name="1m mean reversion",
        rules_json=rules_json,
        timeframe="1m",
        active=True,
    )

    cfg = lee._load_exit_config(_FakeDb([pat]), pat.id)

    _atr_mult, max_bars, use_bos = classify_exit_params(
        rules_json["conditions"],
        timeframe="1m",
    )
    assert cfg["max_bars"] == max_bars == 30
    assert cfg["use_bos"] is use_bos is True
    assert cfg["exit_defaults_source"] == "backtest_classifier"


def test_load_exit_config_infers_defaults_from_legacy_rules_json_string():
    from app.models.trading import ScanPattern

    pat = ScanPattern(
        id=44,
        name="legacy string rules",
        rules_json='{"conditions": [{"indicator": "bb_squeeze", "op": ">", "value": 0}]}',
        timeframe="1m",
        active=True,
    )

    cfg = lee._load_exit_config(_FakeDb([pat]), pat.id)

    assert cfg["max_bars"] == 120
    assert cfg["use_bos"] is False
    assert cfg["exit_defaults_source"] == "backtest_classifier"


def test_load_exit_config_explicit_config_wins_over_classifier_defaults():
    from app.models.trading import ScanPattern

    pat = ScanPattern(
        id=43,
        name="explicit exit config",
        rules_json={"conditions": [{"indicator": "rsi_14", "op": "<=", "value": 40}]},
        timeframe="1m",
        exit_config={"max_bars": 7, "use_bos": False},
        active=True,
    )

    cfg = lee._load_exit_config(_FakeDb([pat]), pat.id)

    assert cfg["max_bars"] == 7
    assert cfg["use_bos"] is False
    assert "exit_defaults_source" not in cfg


@pytest.mark.parametrize("bad_fraction", [0.0, -0.1, 1.01, math.nan, math.inf, "bad"])
def test_invalid_partial_fraction_does_not_emit_partial(monkeypatch, bad_fraction):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": None,
            "use_bos": False,
            "partial_at_1r": True,
            "partial_close_fraction": bad_fraction,
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        lambda *_args, **_kwargs: None,
    )
    trade = _live_trade(stop_loss=95.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=105.0)

    assert result["action"] == "hold"
    assert result["partial_skip_reason"] == "invalid_partial_close_fraction"
    assert "partial_close_fraction" not in result


def test_valid_string_partial_fraction_emits_partial(monkeypatch):
    monkeypatch.setattr(
        lee,
        "_load_exit_config",
        lambda _db, _sp_id: {
            "trailing_enabled": False,
            "max_bars": None,
            "use_bos": False,
            "partial_at_1r": True,
            "partial_close_fraction": "0.25",
        },
    )
    monkeypatch.setattr(lee, "_phase_b_shadow_parity", lambda **_kwargs: None)
    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        lambda *_args, **_kwargs: None,
    )
    trade = _live_trade(stop_loss=95.0, take_profit=120.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=105.0)

    assert result["action"] == "partial"
    assert result["partial_close_fraction"] == 0.25


def test_live_trade_ignores_wrong_side_stop_and_target(monkeypatch):
    _quiet_exit_engine(monkeypatch)
    trade = _live_trade(stop_loss=105.0, take_profit=90.0)

    result = lee.compute_live_exit_levels(object(), trade, current_price=100.0)

    assert result["action"] == "hold"
