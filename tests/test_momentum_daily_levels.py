from app.services.trading.momentum_neural.daily_levels import (
    DailyContext,
    _round_number_near,
    compute_daily_context,
    entry_is_clear_sky,
    overhead_supply_atr,
)
from app.services.trading.momentum_neural.market_profile import market_session_elapsed_fraction
from app.services.trading.momentum_neural.risk_policy import daily_room_size_down_multiplier
from app.services.trading.momentum_neural.risk_policy import float_turnover_size_down_multiplier
from datetime import datetime, timezone


def _bars(closes, *, spread=0.5):
    rows = []
    for close in closes:
        rows.append(
            {
                "High": float(close) + float(spread),
                "Low": float(close) - float(spread),
                "Close": float(close),
            }
        )
    return rows


def test_daily_context_uses_signed_200sma_distance_below_is_overhead():
    ctx = compute_daily_context(_bars([10.0] * 205), price=9.0, entry_context=True)

    assert ctx is not None
    assert ctx.sma_200 == 10.0
    assert ctx.dist_to_sma_200_atr is not None
    assert ctx.dist_to_sma_200_atr < 0.0

    mult, meta = daily_room_size_down_multiplier(
        ctx.dist_to_sma_200_atr,
        None,
    )
    assert 0.0 < mult < 1.0
    assert meta["room_atr"] > 0.0


def test_daily_room_does_not_shrink_when_price_is_above_200sma_without_resistance():
    ctx = compute_daily_context(_bars([10.0] * 205), price=11.5, entry_context=True)

    assert ctx is not None
    assert ctx.dist_to_sma_200_atr is not None
    assert ctx.dist_to_sma_200_atr > 0.0

    mult, meta = daily_room_size_down_multiplier(
        ctx.dist_to_sma_200_atr,
        None,
    )
    assert mult == 1.0
    assert meta["reason"] == "no_overhead_distance"


def test_overhead_supply_uses_nearest_atr_normalized_wall():
    ctx = DailyContext(
        price=5.0,
        atr=1.0,
        sma_200=7.0,
        dist_to_sma_200_atr=-2.0,
        dist_to_resistance_atr=0.75,
        swing_high_nd=5.75,
        nearest_unfilled_gap_bottom=6.5,
        rejection_count=2,
        is_blue_sky=False,
    )

    assert overhead_supply_atr(ctx, entry=5.0) == 0.75


def test_clear_sky_requires_prior_high_break_and_enough_room():
    clear_ctx = compute_daily_context(
        _bars([4.0, 4.5, 4.8, 5.0, 6.0]),
        price=6.0,
        entry_context=True,
    )
    blocked_ctx = DailyContext(
        price=5.0,
        atr=1.0,
        sma_200=None,
        dist_to_sma_200_atr=None,
        dist_to_resistance_atr=0.25,
        swing_high_nd=5.25,
        nearest_unfilled_gap_bottom=None,
        rejection_count=1,
        is_blue_sky=False,
    )

    assert clear_ctx is not None
    assert entry_is_clear_sky(clear_ctx, entry=6.0, min_room_atr=1.5)
    assert not entry_is_clear_sky(blocked_ctx, entry=5.0, min_room_atr=1.5)


def test_round_number_near_uses_half_dollar_grid_for_small_caps():
    assert _round_number_near(9.92, 0.02) == 10.0
    assert _round_number_near(10.01, 0.02) == 10.5
    assert _round_number_near(0.91, 0.02) == 0.95


def test_float_turnover_missing_basis_is_neutral():
    mult, meta = float_turnover_size_down_multiplier(
        None,
        2_000_000,
        0.75,
        rvol_pace=8.0,
    )

    assert mult == 1.0
    assert meta["reason"] == "insufficient_basis"


def test_float_turnover_derates_late_churn_more_than_early_ignition():
    early, early_meta = float_turnover_size_down_multiplier(
        8_000_000,
        2_000_000,
        0.10,
        rvol_pace=16.0,
        floor=0.55,
    )
    late, late_meta = float_turnover_size_down_multiplier(
        8_000_000,
        2_000_000,
        0.90,
        rvol_pace=1.0,
        floor=0.55,
    )

    assert early > late
    assert early_meta["rotations"] == late_meta["rotations"] == 4.0
    assert late_meta["pressure"] > early_meta["pressure"]


def test_float_turnover_rvol_pace_mitigates_rotation_pressure():
    hot, _ = float_turnover_size_down_multiplier(
        10_000_000,
        2_000_000,
        0.75,
        rvol_pace=25.0,
        floor=0.55,
    )
    stale, _ = float_turnover_size_down_multiplier(
        10_000_000,
        2_000_000,
        0.75,
        rvol_pace=1.0,
        floor=0.55,
    )

    assert hot > stale


def test_market_session_elapsed_fraction_uses_regular_session_clock():
    frac = market_session_elapsed_fraction(
        "JEM",
        now=datetime(2026, 7, 1, 17, 15, tzinfo=timezone.utc),  # 13:15 ET
    )

    assert frac is not None
    assert 0.55 < frac < 0.60
