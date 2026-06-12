"""Evening batch 2 (2026-06-12): exit ladder + 5m-EMA runner trail + repeg."""

import math

from app.services.trading.momentum_neural.paper_execution import cushion_adaptive_trail_stop


BASE = dict(
    high_water_mark=10.0, entry_price=8.0, atr_pct=0.02, stop_atr_mult=0.6,
    day_realized_usd=0.0, position_risk_usd=50.0, breakeven_floor=8.0,
    current_stop=8.0, side_long=True,
)


def test_ema5m_anchor_replaces_the_band_when_in_profit():
    # hwm 10 vs entry 8 => unrealized > 1R. The STRUCTURE replaces the band:
    # ema 9.5 -> anchor 9.46 (buffer 8*0.02*0.25) — LOOSER than the 9.5 band
    # (breathing room, the BATL fix); ema 9.8 -> anchor 9.76 — TIGHTER.
    band_only = cushion_adaptive_trail_stop(**BASE)
    loose = cushion_adaptive_trail_stop(**BASE, ema_5m=9.5)
    tight = cushion_adaptive_trail_stop(**BASE, ema_5m=9.8)
    assert abs(band_only - 9.5) < 1e-9
    assert abs(loose - 9.46) < 1e-9   # structure governs below the band
    assert abs(tight - 9.76) < 1e-9   # structure governs above the band
    assert loose >= BASE["breakeven_floor"]


def test_ema5m_never_loosens_the_ratchet():
    tight = dict(BASE, current_stop=9.8)
    out = cushion_adaptive_trail_stop(**tight, ema_5m=9.0)
    assert out >= 9.8  # ratchet-only: an EMA below the current stop never lowers it


def test_ema5m_ignored_before_one_r():
    early = dict(BASE, high_water_mark=8.05)  # barely in profit, < 1R
    a = cushion_adaptive_trail_stop(**early)
    b = cushion_adaptive_trail_stop(**early, ema_5m=8.04)
    assert a == b


def test_exit_ladder_structure():
    src = open("app/services/trading/momentum_neural/live_runner.py", encoding="utf-8").read()
    i = src.index("EXIT LADDER (2026-06-12")
    block = src[i:i + 2500]
    assert 'str(reason or "") in ("kill_switch_flatten", "operator_flatten")' in block
    assert "place_limit_order_gtc" in block
    assert "place_market_order" in block  # the floor remains
    assert "live_exit_limit_repegged" in src
