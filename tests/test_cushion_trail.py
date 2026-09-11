"""Cushion-adaptive runner trail (Ross day-4): patience scales with cushion —
no cushion hugs the floor width, a banked FULL plan (``plan_reward_risk``, 2.5R since
A/B #1271) earns the ceiling.

[37] 2026-09-11: two of these tests hardcoded a 2R plan (hwm 10.60 = +2R; day +1R =
"halfway" 750 bps) and had been RED on main since #1271 moved the plan R:R to 2.5 — the
trail divides the cushion by the plan, so +2R is 80% patience, not 100%. They now read the
plan from the ONE source instead of restating a number that already moved once."""

from __future__ import annotations

from app.services.trading.momentum_neural.paper_execution import (
    cushion_adaptive_trail_stop,
    plan_reward_risk,
)


_BASE = dict(
    entry_price=10.0, atr_pct=0.05, stop_atr_mult=0.60,  # risk_dist = 0.30 (3%)
    position_risk_usd=300.0, breakeven_floor=0.0, current_stop=0.0, side_long=True,
)


def test_no_cushion_uses_floor_width() -> None:
    # hwm == entry: zero unrealized R, zero day R -> 500bps below hwm
    out = cushion_adaptive_trail_stop(high_water_mark=10.0, day_realized_usd=0.0, **_BASE)
    assert abs(out - 10.0 * 0.95) < 1e-9


def test_full_cushion_earns_ceiling_width(monkeypatch) -> None:
    # band knobs honored: with a widened ceiling configured, a cushion of the FULL plan
    # (entry + plan_rr x 0.30) earns the ceiling width (defaults ship FLAT 500 per the
    # 2026-06-11 sweep; the band machinery stays for weekly refits)
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_trail_ceiling_bps", 1000.0, raising=False)
    hwm = 10.0 + plan_reward_risk() * 0.30
    out = cushion_adaptive_trail_stop(high_water_mark=hwm, day_realized_usd=0.0, **_BASE)
    assert abs(out - hwm * 0.90) < 1e-9


def test_day_pnl_contributes_cushion(monkeypatch) -> None:
    # flat position but the day already banked +1R ($300) -> patience 1/plan_rr
    # (2.5 plan => 0.4 => 500 + 0.4 x 500 = 700 bps)
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_trail_ceiling_bps", 1000.0, raising=False)
    out = cushion_adaptive_trail_stop(high_water_mark=10.0, day_realized_usd=300.0, **_BASE)
    trail_bps = 500.0 + 500.0 * min(1.0, 1.0 / plan_reward_risk())
    assert abs(out - 10.0 * (1.0 - trail_bps / 10_000.0)) < 1e-9


def test_a_two_r_cushion_is_no_longer_the_full_plan(monkeypatch) -> None:
    # the old premise, stated as a fact that must stay false while the plan is 2.5:
    # +2R is 80% patience (900 bps), not the ceiling
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_trail_ceiling_bps", 1000.0, raising=False)
    out = cushion_adaptive_trail_stop(high_water_mark=10.6, day_realized_usd=0.0, **_BASE)
    assert plan_reward_risk() == 2.5
    assert abs(out - 10.6 * (1.0 - (500.0 + 500.0 * (2.0 / 2.5)) / 10_000.0)) < 1e-9
    assert abs(out - 10.6 * 0.90) > 1e-6


def test_losing_day_never_tightens_below_floor_nor_widens() -> None:
    # negative day = zero cushion contribution (never widen on losses)
    out = cushion_adaptive_trail_stop(high_water_mark=10.0, day_realized_usd=-500.0, **_BASE)
    assert abs(out - 10.0 * 0.95) < 1e-9


def test_ratchet_only_and_breakeven_floor() -> None:
    kw = dict(_BASE)
    kw.update(breakeven_floor=10.0, current_stop=9.9)
    out = cushion_adaptive_trail_stop(high_water_mark=10.2, day_realized_usd=0.0, **kw)
    assert out >= 10.0  # never below breakeven
    kw.update(current_stop=10.5)
    out2 = cushion_adaptive_trail_stop(high_water_mark=10.2, day_realized_usd=0.0, **kw)
    assert out2 == 10.5  # never loosens
