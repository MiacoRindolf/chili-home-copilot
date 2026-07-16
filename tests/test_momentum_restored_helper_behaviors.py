from __future__ import annotations

import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import auto_arm
from app.services.trading.momentum_neural.candles import (
    _ema,
    bounce_curl_from_df,
    is_bounce_curl_candle,
    macd_hist_rollover_from_df,
)
from app.services.trading.momentum_neural.ross_momentum import (
    compute_is_ssr,
    front_side_size_tilt,
    front_side_strength_score,
    squeeze_entry_size_multiplier,
    squeeze_exit_band_widen,
    squeeze_fuel_signal,
)
from app.services.trading.momentum_neural.paper_execution import (
    flag_breakout_add_decision,
    pullback_add_decision,
    pyramid_add_decision,
)


def test_restored_ssr_helper_fails_neutral_and_uses_ten_pct_boundary() -> None:
    assert compute_is_ssr(None, 100.0) is False
    assert compute_is_ssr(90.01, 100.0) is False
    assert compute_is_ssr(90.00, 100.0) is True


def test_squeeze_size_up_requires_all_confirming_legs_and_is_bounded() -> None:
    assert squeeze_entry_size_multiplier(None, ofi=1.0, news_agrees=True)[0] == 1.0
    assert squeeze_entry_size_multiplier(0.99, ofi=None, news_agrees=True)[0] == 1.0
    assert squeeze_entry_size_multiplier(0.99, ofi=1.0, news_agrees=False)[0] == 1.0

    mult, meta = squeeze_entry_size_multiplier(
        1.0,
        ofi=1.0,
        news_agrees=True,
        top_pctl=0.80,
        max_mult=1.50,
    )
    assert mult == pytest.approx(1.50)
    assert meta["reason"] == "squeeze_size_up"


def test_squeeze_exit_widen_is_tail_only_and_bounded() -> None:
    assert squeeze_exit_band_widen(None)[0] == 1.0
    assert squeeze_exit_band_widen(0.89, tail_pctl=0.90, max_widen=1.50)[0] == 1.0

    factor, meta = squeeze_exit_band_widen(1.0, tail_pctl=0.90, max_widen=1.50)
    assert factor == pytest.approx(1.50)
    assert meta["reason"] == "squeeze_exit_widen"


def test_squeeze_fuel_signal_self_normalizes_present_legs() -> None:
    assert squeeze_fuel_signal().squeeze_pct is None

    sig = squeeze_fuel_signal(
        short_interest_pct=50.0,
        cost_to_borrow=100.0,
        utilization=80.0,
        is_easy_to_borrow=False,
    )
    assert sig.squeeze_pct == pytest.approx((0.50 + 1.00 + 0.80 + 1.00) / 4.0)
    assert sig.components["legs"] == 4


def test_front_side_strength_and_size_tilt_are_missing_data_neutral() -> None:
    assert front_side_strength_score() is None
    assert front_side_size_tilt(None)[0] == 1.0
    assert front_side_size_tilt(0.9, stale_tape=True)[0] == 1.0

    weak_mult, weak_meta = front_side_size_tilt(0.10, size_floor=0.40, s_lo=0.25, s_hi=0.75)
    mid_mult, _ = front_side_size_tilt(0.50, size_floor=0.40, s_lo=0.25, s_hi=0.75)
    strong_mult, strong_meta = front_side_size_tilt(0.90, size_floor=0.40, s_lo=0.25, s_hi=0.75)

    assert weak_mult == pytest.approx(0.40)
    assert 0.40 < mid_mult < 1.0
    assert strong_mult == pytest.approx(1.0)
    assert weak_meta["reason"] == strong_meta["reason"] == "frontside_size_tilt"


def test_front_side_strength_uses_present_terms_without_requiring_every_feed() -> None:
    score = front_side_strength_score(
        closes=[1.0, 1.05, 1.10, 1.20],
        vwap_dist_sigma=0.50,
        day_range_pos=0.90,
        ofi_level=1.0,
    )
    assert score is not None
    assert 0.0 <= score <= 1.0
    assert score > 0.60


def test_restored_candle_helpers_fail_safe_and_detect_curl_shape() -> None:
    assert _ema([], 9) == []
    assert _ema([1.0, 2.0, 3.0], 2)[0] == pytest.approx(1.0)

    assert is_bounce_curl_candle(1.0, 1.20, 0.95, 1.17) is True
    assert is_bounce_curl_candle(1.17, 1.20, 0.95, 1.00) is False
    assert bounce_curl_from_df(None) is False

    df = pd.DataFrame(
        {
            "Open": [1.0, 1.05],
            "High": [1.1, 1.2],
            "Low": [0.95, 1.04],
            "Close": [1.05, 1.18],
        }
    )
    assert bounce_curl_from_df(df) is True
    assert macd_hist_rollover_from_df(None) is False


def test_auto_arm_restored_contracts_are_operator_visible_and_neutral(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_24h_eligible_symbols", "", raising=False)
    assert auto_arm.known_24h_eligible_symbols() == set()
    assert auto_arm._is_24h_eligible("BTC-USD") is True
    assert auto_arm._is_24h_eligible("AAPL") is False

    monkeypatch.setattr(settings, "chili_momentum_24h_eligible_symbols", "JEM, TC", raising=False)
    assert auto_arm.known_24h_eligible_symbols() == {"JEM", "TC"}
    assert auto_arm._is_24h_eligible("TC") is True
    assert auto_arm._is_24h_eligible("BTC-USD") is False

    assert auto_arm.is_agentic_unauthorized_reject("not available for agentic trading") is True
    assert auto_arm.is_agentic_unauthorized_reject("temporary rate limit") is False

    auto_arm._AGENTIC_NON_TRADEABLE_SYMBOLS.clear()
    auto_arm._ENTRY_REJECT_COOLDOWNS.clear()
    auto_arm._write_entry_reject_cooldown("jem", reason="not available for agentic trading")
    mult, meta = auto_arm.per_symbol_fatigue_size_multiplier(None, "JEM")
    assert mult == 1.0
    assert meta["reason"] == "agentic_non_tradeable_recorded"

    assert auto_arm.win_cycle_yellow_size_multiplier(None)[0] == 1.0
    assert auto_arm.hot_cold_tape_size_multiplier(atr_pct=0.10, rvol=20.0)[0] == 1.0
    assert auto_arm.prime_window_size_multiplier()[0] == 1.0


def test_time_fatigue_derate_is_bounded_soft_sizing_only() -> None:
    from app.services.trading.momentum_neural.risk_policy import fatigue_derate_multiplier

    neutral, neutral_meta = fatigue_derate_multiplier(
        trade_count_today=0,
        max_trades_per_day=5,
        minutes_since_open=0,
        is_crypto=False,
    )
    tired, tired_meta = fatigue_derate_multiplier(
        trade_count_today=5,
        max_trades_per_day=5,
        minutes_since_open=240,
        is_crypto=False,
    )
    crypto, crypto_meta = fatigue_derate_multiplier(
        trade_count_today=5,
        max_trades_per_day=5,
        minutes_since_open=None,
        is_crypto=True,
    )

    assert neutral == 1.0
    assert neutral_meta["fatigue_mult"] == 1.0
    assert 0.0 < tired <= 1.0
    assert tired_meta["time_frac"] == 1.0
    assert tired_meta["trade_frac"] == 1.0
    assert tired_meta["fatigue_mult"] == tired
    assert 0.0 < crypto < 1.0
    assert crypto_meta["time_frac"] == 0.0
    assert crypto_meta["trade_frac"] == 1.0


def test_restored_ofi_helpers_cannot_confirm_adds_when_missing() -> None:
    """Missing OFI/L2 compatibility restores must not become fake add confirmation."""
    pyramid = pyramid_add_decision(
        enabled=True,
        is_equity=True,
        add_count=0,
        max_adds=1,
        in_flight=False,
        a0=10.0,
        q0=100.0,
        d0=0.20,
        bid=10.40,
        stop_px=10.0,
        entry_stop_ref=10.0,
        high_water_mark=10.30,
        ofi=None,
        ofi_threshold=0.25,
        min_cushion_r=1.0,
        midday_lull=False,
    )
    assert pyramid["fire"] is False
    assert pyramid["reason"] == "ofi_below_threshold"

    pullback = pullback_add_decision(
        enabled=True,
        is_equity=True,
        add_count=0,
        max_adds=1,
        in_flight=False,
        other_add_in_flight=False,
        a0=10.0,
        q0=100.0,
        d0=0.20,
        bid=10.40,
        stop_px=9.95,
        high_water_mark=10.60,
        support_level=10.20,
        pullback_low=10.22,
        prior_pullback_low=10.10,
        move_range=1.0,
        pullback_depth_lo_frac=0.20,
        pullback_depth_hi_frac=0.62,
        bounced=True,
        front_side_strength=0.80,
        strength_floor=0.50,
        above_vwap_or_reclaiming=True,
        ofi_level=None,
        ofi_slope=None,
        midday_lull=False,
        cooldown_active=False,
    )
    assert pullback["fire"] is False
    assert pullback["reason"] == "ofi_unknown"

    flag = flag_breakout_add_decision(
        enabled=True,
        is_equity=True,
        add_count=0,
        max_adds=1,
        in_flight=False,
        other_add_in_flight=False,
        a0=10.0,
        q0=100.0,
        d0=0.20,
        bid=10.61,
        stop_px=9.95,
        flag_confirmed=True,
        flag_high=10.50,
        flag_low=10.00,
        prior_flag_high=10.20,
        breakout_margin_frac=0.10,
        front_side_strength=0.80,
        strength_floor=0.50,
        above_vwap_or_reclaiming=True,
        ofi_level=None,
        ofi_slope=None,
        midday_lull=False,
        cooldown_active=False,
    )
    assert flag["fire"] is False
    assert flag["reason"] == "ofi_unknown"
