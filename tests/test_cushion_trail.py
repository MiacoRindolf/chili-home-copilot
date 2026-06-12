"""Cushion-adaptive runner trail (Ross day-4): patience scales with cushion —
no cushion hugs the floor width, a banked 2R plan earns the ceiling."""

from __future__ import annotations

from app.services.trading.momentum_neural.paper_execution import cushion_adaptive_trail_stop


_BASE = dict(
    entry_price=10.0, atr_pct=0.05, stop_atr_mult=0.60,  # risk_dist = 0.30 (3%)
    position_risk_usd=300.0, breakeven_floor=0.0, current_stop=0.0, side_long=True,
)


def test_no_cushion_uses_floor_width() -> None:
    # hwm == entry: zero unrealized R, zero day R -> 500bps below hwm
    out = cushion_adaptive_trail_stop(high_water_mark=10.0, day_realized_usd=0.0, **_BASE)
    assert abs(out - 10.0 * 0.95) < 1e-9


def test_full_cushion_earns_ceiling_width(monkeypatch) -> None:
    # band knobs honored: with a widened ceiling configured, +2R cushion
    # (entry + 2*0.30 = 10.60) earns the ceiling width (defaults ship FLAT 500
    # per the 2026-06-11 sweep; the band machinery stays for weekly refits)
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_trail_ceiling_bps", 1000.0, raising=False)
    out = cushion_adaptive_trail_stop(high_water_mark=10.6, day_realized_usd=0.0, **_BASE)
    assert abs(out - 10.6 * 0.90) < 1e-9


def test_day_pnl_contributes_cushion(monkeypatch) -> None:
    # flat position but the day already banked +1R ($300) -> halfway patience (750bps)
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_trail_ceiling_bps", 1000.0, raising=False)
    out = cushion_adaptive_trail_stop(high_water_mark=10.0, day_realized_usd=300.0, **_BASE)
    assert abs(out - 10.0 * 0.925) < 1e-9


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
