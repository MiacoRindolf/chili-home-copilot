"""Phase E4: venue-aware equity-relative sizing (RH equity for stocks, Coinbase for crypto)."""
from __future__ import annotations

import pytest

import app.services.trading.momentum_neural.risk_policy as rp
from app.services import broker_service, coinbase_service


def _patch(monkeypatch):
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {"equity": 50000.0, "buying_power": 50000.0})
    monkeypatch.setattr(coinbase_service, "get_portfolio", lambda: {"equity": 2000.0})


def test_account_equity_is_venue_aware(monkeypatch):
    _patch(monkeypatch)
    assert rp._account_equity_usd("robinhood_spot") == 50000.0  # RH equity for stocks
    assert rp._account_equity_usd("coinbase_spot") == 2000.0    # Coinbase for crypto
    assert rp._account_equity_usd(None) == 2000.0               # default coinbase


def test_caps_scale_to_the_right_venue_equity(monkeypatch):
    """Both ceiling paths stay venue-aware ([27], 2026-09-10). DERIVED (fraction 0, the new
    default): on a non-Alpaca venue ``_account_equity_usd`` already IS that venue's
    buying-power truth, so the ceiling is that basis (multiplier 1.0) unless the loss budget
    at the stop floor is smaller. NAMED OVERRIDE (fraction > 0): the pre-[27] equity x
    fraction, still computed off the right venue's equity."""
    _patch(monkeypatch)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01)
    # DERIVED: min(equity x 1.0, loss / RISK_FIRST_STOP_FLOOR_PCT)
    # RH   -> min(50,000, 500 / 0.003 = 166,667) = 50,000   (buying power binds)
    # CB   -> min( 2,000,  20 / 0.003 =   6,667) =  2,000   (buying power binds)
    rh_usd, rh_meta = rp.equity_relative_notional_cap_with_meta(500.0, "robinhood_spot")
    cb_usd, cb_meta = rp.equity_relative_notional_cap_with_meta(500.0, "coinbase_spot")
    assert rh_usd == pytest.approx(50_000.0)
    assert cb_usd == pytest.approx(2_000.0)
    assert rh_meta["source"] == cb_meta["source"] == "sizing_basis_is_buying_power"
    assert rh_meta["binding"] == cb_meta["binding"] == "buying_power"
    # NAMED OVERRIDE: the legacy fraction arithmetic, unchanged, on the same venue equity
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15)
    assert rp.equity_relative_notional_cap(500.0, "robinhood_spot") == 7500.0   # 0.15 * 50000
    assert rp.equity_relative_notional_cap(500.0, "coinbase_spot") == 300.0     # 0.15 * 2000
    # loss fraction default 0.01 (unchanged by [27])
    assert rp.equity_relative_loss_cap(50.0, "robinhood_spot") == 500.0         # 0.01 * 50000
    assert rp.equity_relative_loss_cap(50.0, "coinbase_spot") == 20.0           # 0.01 * 2000


def test_falls_back_to_fixed_when_equity_unavailable(monkeypatch):
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {})
    assert rp.equity_relative_notional_cap(500.0, "robinhood_spot") == 500.0  # fixed fallback


def test_sizing_basis_uses_buying_power(monkeypatch):
    # Default: the sizing basis is BUYING POWER (margin-inclusive), not settled equity.
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {"equity": 10000.0, "buying_power": 21000.0})
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_size_use_buying_power", True, raising=False)
    assert rp._account_equity_usd("robinhood_spot") == 21000.0  # buying power utilized
    # Opt-out -> settled equity only.
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_size_use_buying_power", False, raising=False)
    assert rp._account_equity_usd("robinhood_spot") == 10000.0
    # Buying power missing -> fall back to equity even when the flag is on.
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_size_use_buying_power", True, raising=False)
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {"equity": 10000.0})
    assert rp._account_equity_usd("robinhood_spot") == 10000.0


def test_buying_power_margin_multiple(monkeypatch):
    # The margin multiple recovers the displayed margin BP (the API under-reports it:
    # returns the ~1x base; the 2x Gold margin shown in the app = base * 2).
    monkeypatch.setattr(broker_service, "get_portfolio", lambda: {"equity": 10000.0, "buying_power": 11275.69})
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_size_use_buying_power", True, raising=False)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_buying_power_margin_multiple", 2.0, raising=False)
    assert abs(rp._account_equity_usd("robinhood_spot") - 22551.38) < 0.5  # 2x Gold margin
    # multiple 1.0 -> just the API buying power (no extra leverage)
    monkeypatch.setattr(rp.settings, "chili_momentum_risk_buying_power_margin_multiple", 1.0, raising=False)
    assert abs(rp._account_equity_usd("robinhood_spot") - 11275.69) < 0.5


def test_alpaca_defaults_to_equity_even_when_global_buying_power_is_on(monkeypatch):
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (75_000.0, 300_000.0))
    monkeypatch.setattr(
        rp, "_alpaca_cached_account_generation", lambda: "acct-sizing-test"
    )
    monkeypatch.setattr(
        rp.settings, "chili_momentum_risk_size_use_buying_power", True, raising=False
    )
    monkeypatch.setattr(
        rp.settings, "chili_momentum_alpaca_size_use_buying_power", False, raising=False
    )
    assert rp._account_equity_usd("alpaca_spot") == 75_000.0
    assert rp._account_equity_usd("alpaca_spot", prefer_equity=True) == 75_000.0
    # The per-trade loss budget is equity x the documented loss fraction (the fixed $50 is
    # only the fallback when equity is unavailable). The old assertion here recorded a belief
    # ("paper stays at the explicit $ ceiling") that the code had not held since the
    # equity-relative caps landed; it was failing on origin/main (2026-09-10, [27]).
    monkeypatch.setattr(
        rp.settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01, raising=False
    )
    assert rp.equity_relative_loss_cap(50.0, "alpaca_spot") == pytest.approx(750.0)
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (None, None))
    assert rp.equity_relative_loss_cap(50.0, "alpaca_spot") == 50.0


def test_alpaca_leveraged_sizing_requires_explicit_venue_opt_in(monkeypatch):
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: (75_000.0, 300_000.0))
    monkeypatch.setattr(
        rp, "_alpaca_cached_account_generation", lambda: "acct-sizing-test"
    )
    monkeypatch.setattr(
        rp.settings, "chili_momentum_risk_size_use_buying_power", True, raising=False
    )
    monkeypatch.setattr(
        rp.settings, "chili_momentum_alpaca_size_use_buying_power", True, raising=False
    )
    assert rp._account_equity_usd("alpaca_spot") == 300_000.0
    # Safety caps that explicitly request equity never inherit paper margin.
    assert rp._account_equity_usd("alpaca_spot", prefer_equity=True) == 75_000.0
