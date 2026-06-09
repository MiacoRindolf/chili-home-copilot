"""Liquidity-ceiling sizing (docs/DESIGN/SCALING_ENGINE.md) — cap the per-trade notional at
a fraction of the NAME's dollar-volume so a COMPOUNDING account never sizes beyond what it can
EXIT cleanly (Ross's "can't move 500k shares in 1-2 min"). Pure tests; no DB / network."""

from __future__ import annotations

from app.config import settings
from app.services.trading.momentum_neural.risk_policy import liquidity_capped_notional


def test_small_account_equity_cap_binds_unchanged():
    # $22.5k account -> 15% notional = $3,383. Liquid name ($50M $-vol) -> 1% = $500k.
    # The equity cap is far smaller, so behavior is UNCHANGED (liquidity cap doesn't bind).
    out = liquidity_capped_notional(3_383.0, 50_000_000.0, fraction=0.01)
    assert out == 3_383.0


def test_large_account_liquidity_cap_binds_on_thin_name():
    # Compounded $1M account -> 15% notional = $150k. Thin name ($5M $-vol) -> 1% = $50k.
    # The LIQUIDITY cap binds: CHILI scales only as far as the name can absorb.
    out = liquidity_capped_notional(150_000.0, 5_000_000.0, fraction=0.01)
    assert out == 50_000.0


def test_liquidity_cap_does_not_bind_on_liquid_name_even_at_large_account():
    # $1M account, $150k notional; a deep $30M-$-vol name -> 1% = $300k > $150k -> equity binds.
    out = liquidity_capped_notional(150_000.0, 30_000_000.0, fraction=0.01)
    assert out == 150_000.0


def test_fail_open_when_no_dollar_volume():
    assert liquidity_capped_notional(150_000.0, None, fraction=0.01) == 150_000.0
    assert liquidity_capped_notional(150_000.0, 0.0, fraction=0.01) == 150_000.0
    assert liquidity_capped_notional(150_000.0, float("nan"), fraction=0.01) == 150_000.0
    assert liquidity_capped_notional(150_000.0, "bad", fraction=0.01) == 150_000.0


def test_disabled_fraction_is_no_op():
    assert liquidity_capped_notional(150_000.0, 5_000_000.0, fraction=0.0) == 150_000.0
    assert liquidity_capped_notional(150_000.0, 5_000_000.0, fraction=-1.0) == 150_000.0


def test_nonpositive_equity_cap_preserved():
    assert liquidity_capped_notional(0.0, 5_000_000.0, fraction=0.01) == 0.0
    assert liquidity_capped_notional(-10.0, 5_000_000.0, fraction=0.01) == -10.0


def test_uses_settings_fraction_when_not_passed(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_risk_liquidity_participation_fraction", 0.02, raising=False)
    # 2% of $5M = $100k < $150k -> liquidity binds at the settings fraction.
    assert liquidity_capped_notional(150_000.0, 5_000_000.0) == 100_000.0


def test_config_default_fraction_is_one_percent():
    assert settings.chili_momentum_risk_liquidity_participation_fraction == 0.01
