"""FIX-DD — DRAWDOWN-CALC DENOMINATOR (the false-breaker class).

The 5/30-day + MTM drawdown-% trips divided the rolling realized PnL by a `capital` basis
the caller passed. A contaminated caller basis (~$19.5 instead of the real ~$13k account
equity) inflated a -$76 realized loss to "-389.7%" and RE-TRIPPED the breaker twice
(trading_risk_state ids 151-154 class).

Fix (`chili_drawdown_breaker_real_equity_denominator_enabled`, default True): the drawdown-%
denominator is resolved from the SAME real account-equity source the sizing uses; fail-closed
(SKIP the % trip) when equity is unavailable and the passed basis is sub-floor garbage.

The canonical case: realized=-$76 on ~$13k equity => -0.58% => NO trip; the same loss on a
$1k account => -7.6% => trips at the -6% limit.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import Settings
import app.services.trading.portfolio_risk as pr
from app.services.trading.portfolio_risk import DrawdownLimits, check_drawdown_breaker


@pytest.fixture(autouse=True)
def _reset_breaker_and_isolate(monkeypatch):
    # Isolate: no DB persistence, deterministic realized/unrealized, module globals reset.
    monkeypatch.setattr(pr, "_persist_breaker_state", lambda *a, **k: None)
    monkeypatch.setattr(pr, "_compute_unrealized_pnl", lambda db, uid: 0.0)
    pr._breaker_tripped = False
    pr._breaker_reason = None
    yield
    pr._breaker_tripped = False
    pr._breaker_reason = None


def _mock_db() -> MagicMock:
    db = MagicMock()
    # The closed-trade queries end in _breaker_trade_filter(q).all(); return [] so the
    # realized sum comes entirely from the monkeypatched _sum_trade_realized_pnl below.
    db.query.return_value.filter.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.all.return_value = []
    return db


# -$6.00 / -6.00% symmetric limits so the plan's arithmetic maps to one threshold.
_LIMITS = DrawdownLimits(max_5day_dd_pct=6.0, max_30day_dd_pct=6.0)


def test_flag_and_floor_defaults():
    assert (
        Settings.model_fields[
            "chili_drawdown_breaker_real_equity_denominator_enabled"
        ].default
        is True
    )
    assert (
        Settings.model_fields["chili_drawdown_breaker_min_equity_basis_usd"].default
        == 1_000.0
    )


def test_minus76_on_13k_equity_does_not_trip(monkeypatch):
    """realized=-$76 on the REAL ~$13k equity => -0.58% => NO trip, even though the caller
    passed a contaminated ~$19.5 capital basis."""
    monkeypatch.setattr(pr, "_sum_trade_realized_pnl", lambda trades: -76.0)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *a, **k: 13_000.0,
    )
    tripped, reason = check_drawdown_breaker(
        _mock_db(), user_id=1, capital=19.5, limits=_LIMITS
    )
    assert tripped is False, reason
    assert reason is None


def test_minus76_on_1k_account_trips_at_6pct(monkeypatch):
    """The SAME -$76 loss on a genuine $1k account => -7.6% => trips at the -6% limit."""
    monkeypatch.setattr(pr, "_sum_trade_realized_pnl", lambda trades: -76.0)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *a, **k: 1_000.0,
    )
    tripped, reason = check_drawdown_breaker(
        _mock_db(), user_id=1, capital=1_000.0, limits=_LIMITS
    )
    assert tripped is True
    assert reason is not None
    assert "drawdown" in reason.lower()


def test_no_equity_and_garbage_capital_skips_pct_trip(monkeypatch):
    """Fail-closed: no real equity read AND a sub-floor garbage capital ($19.5) => the %
    trips are SKIPPED (never divide by a garbage base). This is exactly the false-trip class
    (a -$76 loss / $19.5 = -389.7% would otherwise trip)."""
    monkeypatch.setattr(pr, "_sum_trade_realized_pnl", lambda trades: -76.0)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *a, **k: None,
    )
    tripped, reason = check_drawdown_breaker(
        _mock_db(), user_id=1, capital=19.5, limits=_LIMITS
    )
    assert tripped is False, reason
    assert reason is None


def test_no_equity_but_sane_capital_still_uses_it(monkeypatch):
    """When no real equity read is available but the passed capital clears the floor, it is
    still used as the denominator — a genuine -30% drawdown on a sane $10k basis still trips."""
    monkeypatch.setattr(pr, "_sum_trade_realized_pnl", lambda trades: -3_000.0)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *a, **k: None,
    )
    tripped, reason = check_drawdown_breaker(
        _mock_db(), user_id=1, capital=10_000.0, limits=_LIMITS
    )
    assert tripped is True  # -3000/10000 = -30% < -6%
    assert reason is not None


def test_flag_off_is_legacy_trusts_passed_capital(monkeypatch):
    """OFF => byte-identical legacy: the passed (garbage) capital IS the denominator, so the
    -$76/$19.5 = -389.7% false trip fires. Proves the flag is the only behavior gate."""
    monkeypatch.setattr(pr, "_sum_trade_realized_pnl", lambda trades: -76.0)
    monkeypatch.setattr(
        "app.config.settings.chili_drawdown_breaker_real_equity_denominator_enabled",
        False,
        raising=False,
    )
    tripped, reason = check_drawdown_breaker(
        _mock_db(), user_id=1, capital=19.5, limits=_LIMITS
    )
    assert tripped is True  # legacy divides by the garbage $19.5 -> huge % -> trips
    assert reason is not None
