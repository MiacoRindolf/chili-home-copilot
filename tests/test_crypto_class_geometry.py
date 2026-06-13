"""Per-asset-class geometry (2026-06-13 crypto-live plan, A4).

Equity keeps 2:1 / 0.33; crypto's fatter tails take a wider target (3:1) and a
heavier first de-risk (0.5) via overrides that NEVER touch the equity lane.
"""

import app.services.trading.momentum_neural.paper_execution as pe
from app.services.trading.momentum_neural.paper_execution import (
    class_aware_reward_risk,
    scale_out_fraction,
    stop_target_prices,
)


class _S:
    """Minimal settings stand-in."""

    chili_momentum_risk_reward_risk_ratio = 2.0
    chili_momentum_scale_out_fraction = 0.33
    chili_momentum_crypto_reward_risk_ratio = 3.0
    chili_momentum_crypto_scale_out_fraction = 0.5


def _patch(monkeypatch, **over):
    s = _S()
    for k, v in over.items():
        setattr(s, k, v)
    monkeypatch.setattr(pe, "settings", s)


def test_equity_uses_global_geometry(monkeypatch):
    _patch(monkeypatch)
    assert class_aware_reward_risk("AAPL") == 2.0
    assert abs(scale_out_fraction(symbol="AAPL") - 0.33) < 1e-9
    assert abs(scale_out_fraction(symbol=None) - 0.33) < 1e-9


def test_crypto_uses_overrides(monkeypatch):
    _patch(monkeypatch)
    assert class_aware_reward_risk("ORCA-USD") == 3.0
    assert abs(scale_out_fraction(symbol="DOGE-USD") - 0.5) < 1e-9


def test_crypto_override_none_falls_back_to_global(monkeypatch):
    _patch(monkeypatch, chili_momentum_crypto_reward_risk_ratio=None,
           chili_momentum_crypto_scale_out_fraction=None)
    assert class_aware_reward_risk("ORCA-USD") == 2.0
    assert abs(scale_out_fraction(symbol="ORCA-USD") - 0.33) < 1e-9


def test_crypto_rr_clamped_up_to_equity_floor(monkeypatch):
    # An override below the 2:1 Ross floor is clamped up — R:R is a floor.
    _patch(monkeypatch, chili_momentum_crypto_reward_risk_ratio=1.2)
    assert class_aware_reward_risk("ETH-USD") == 2.0


def test_target_widens_for_crypto(monkeypatch):
    _patch(monkeypatch)
    # entry 100, atr 2%, stop_atr 0.6 -> stop = 100*(1-0.012)=98.8, risk=1.2.
    eq_stop, eq_tgt = stop_target_prices(100.0, atr_pct=0.02, stop_atr_mult=0.6,
                                         reward_risk=class_aware_reward_risk("AAPL"))
    cx_stop, cx_tgt = stop_target_prices(100.0, atr_pct=0.02, stop_atr_mult=0.6,
                                         reward_risk=class_aware_reward_risk("ORCA-USD"))
    assert abs(eq_stop - cx_stop) < 1e-9          # same stop
    assert abs(eq_tgt - (100.0 + 2.0 * 1.2)) < 1e-9   # 2:1 -> 102.4
    assert abs(cx_tgt - (100.0 + 3.0 * 1.2)) < 1e-9   # 3:1 -> 103.6
    assert cx_tgt > eq_tgt
