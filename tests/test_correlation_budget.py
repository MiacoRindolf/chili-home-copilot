"""Phase H - DB integration tests for ``correlation_budget``.

Verifies:
  * Bucket key matches :func:`portfolio_allocator._correlation_bucket`.
  * Open-trade aggregation respects the ``status`` filter and the
    bucket the caller is sizing for.
  * Bucket cap %s feed from ``settings.brain_position_sizer_*``.
  * ``compute_portfolio_budget`` correctly sums deployed + ticker-open
    notional from the live ``trading_trades`` table.

Notes
-----

* Tests use **fake ticker symbols** that are NOT present in the
  ``backtest_engine.TICKER_TO_SECTOR`` mapping, so the sizer's
  fallback asset family (``equity``) drives the bucket key. This
  makes the tests independent of future sector registrations.
* All trades created here use ``user_id=None`` to avoid the
  ``users`` FK constraint on ``trading_trades.user_id``.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.trading import Trade
from app.services.trading.correlation_budget import (
    bucket_for,
    compute_correlation_budget,
    compute_portfolio_budget,
    is_crypto_symbol,
)


def _make_trade(
    db,
    *,
    ticker: str,
    qty: float,
    entry: float,
    status: str = "open",
) -> Trade:
    t = Trade(
        user_id=None,
        ticker=ticker,
        direction="long",
        entry_price=entry,
        quantity=qty,
        status=status,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _cleanup_trades(db):
    # Scoped deletion keeps the same session's bucket sums deterministic.
    db.execute(text("DELETE FROM trading_trades WHERE ticker LIKE 'PHHCB%'"))
    db.commit()


# ---------------------------------------------------------------------------
# Bucket key tests (pure, no DB needed)
# ---------------------------------------------------------------------------


class TestBucketFor:
    def test_crypto_dash_usd_groups_by_base(self):
        assert bucket_for("BTC-USD") == "crypto:BTC"
        assert bucket_for("ZK-USD") == "crypto:ZK"

    def test_unknown_equity_uses_first_letter(self):
        # PHHCB_ACORP is not in TICKER_TO_SECTOR so the family is
        # "equity" and the bucket groups by first character.
        assert bucket_for("PHHCB_ACORP") == "equity:P"
        assert bucket_for("ZZFAKE") == "equity:Z"

    def test_registered_ticker_uses_its_sector(self):
        # AAPL is in `mega_tech` - the bucket key reflects that so
        # cluster risk is actually constrained by sector, not just
        # the first letter of the name.
        assert bucket_for("AAPL") == "mega_tech:A"

    def test_explicit_asset_class_overrides_default(self):
        assert bucket_for("PHHCB_XYZ", asset_class="equity") == "equity:P"
        assert bucket_for("BTC-USD", asset_class="crypto") == "crypto:BTC"

    def test_empty_symbol_falls_back_gracefully(self):
        assert bucket_for(None) == "equity:x"
        assert bucket_for("") == "equity:x"

    def test_is_crypto_symbol_recognizes_dash_usd(self):
        assert is_crypto_symbol("BTC-USD") is True
        assert is_crypto_symbol("PHHCB_AAA") is False


# ---------------------------------------------------------------------------
# Correlation budget
# ---------------------------------------------------------------------------


class TestComputeCorrelationBudget:
    def test_empty_db_returns_zero_open_notional(self, db):
        _cleanup_trades(db)
        budget = compute_correlation_budget(
            db, user_id=None, ticker="PHHCB_EMPTY", capital=100_000.0,
            asset_class="equity",
        )
        assert budget.bucket == bucket_for("PHHCB_EMPTY", asset_class="equity")
        assert budget.open_notional == 0.0
        assert budget.max_bucket_notional > 0.0

    def test_aggregates_same_bucket_only(self, db):
        _cleanup_trades(db)
        # All three share first-letter 'P' -> same bucket 'equity:P'.
        _make_trade(db, ticker="PHHCB_FOO", qty=10, entry=100.0)   # equity:P, 1000
        _make_trade(db, ticker="PHHCB_BAR", qty=20, entry=50.0)    # equity:P, 1000
        # 'Z' in different bucket -> excluded from PHHCB_FOO's bucket sum.
        _make_trade(db, ticker="ZHHCB_OUT", qty=10, entry=200.0)   # equity:Z, excluded

        budget = compute_correlation_budget(
            db, user_id=None, ticker="PHHCB_NEW", capital=100_000.0,
            asset_class="equity",
        )
        assert budget.bucket == "equity:P"
        assert budget.open_notional == pytest.approx(2000.0)

    def test_ignores_closed_trades(self, db):
        _cleanup_trades(db)
        _make_trade(db, ticker="PHHCB_CLOSED", qty=10, entry=100.0, status="closed")
        _make_trade(db, ticker="PHHCB_OPEN", qty=10, entry=100.0, status="open")

        budget = compute_correlation_budget(
            db, user_id=None, ticker="PHHCB_NEW", capital=100_000.0,
            asset_class="equity",
        )
        assert budget.open_notional == pytest.approx(1000.0)

    def test_crypto_cap_uses_crypto_bucket_pct(self, db, monkeypatch):
        _cleanup_trades(db)
        monkeypatch.setattr(
            "app.services.trading.correlation_budget.settings.brain_position_sizer_crypto_bucket_cap_pct",
            7.0,
            raising=False,
        )
        budget = compute_correlation_budget(
            db, user_id=None, ticker="BTC-USD", capital=100_000.0,
            asset_class="crypto",
        )
        assert budget.max_bucket_notional == pytest.approx(7_000.0)

    def test_equity_cap_uses_equity_bucket_pct(self, db, monkeypatch):
        _cleanup_trades(db)
        monkeypatch.setattr(
            "app.services.trading.correlation_budget.settings.brain_position_sizer_equity_bucket_cap_pct",
            12.0,
            raising=False,
        )
        budget = compute_correlation_budget(
            db, user_id=None, ticker="PHHCB_ANY", capital=100_000.0,
            asset_class="equity",
        )
        assert budget.max_bucket_notional == pytest.approx(12_000.0)


# ---------------------------------------------------------------------------
# Portfolio budget
# ---------------------------------------------------------------------------


class TestComputePortfolioBudget:
    def test_empty_db_returns_zero_deployed(self, db):
        _cleanup_trades(db)
        pb = compute_portfolio_budget(
            db, user_id=None, ticker="PHHCB_NONE", capital=100_000.0,
        )
        assert pb.deployed_notional == 0.0
        assert pb.ticker_open_notional == 0.0
        assert pb.max_total_notional == pytest.approx(100_000.0)

    def test_sums_only_open_trades(self, db):
        _cleanup_trades(db)
        _make_trade(db, ticker="PHHCB_O1", qty=10, entry=100.0)        # 1000 open
        _make_trade(db, ticker="PHHCB_O2", qty=20, entry=50.0)         # 1000 open
        _make_trade(db, ticker="PHHCB_CLS", qty=100, entry=100.0, status="closed")

        pb = compute_portfolio_budget(
            db, user_id=None, ticker="PHHCB_O1", capital=100_000.0,
        )
        assert pb.deployed_notional == pytest.approx(2000.0)
        assert pb.ticker_open_notional == pytest.approx(1000.0)

    def test_ticker_open_notional_is_scoped_to_ticker(self, db):
        _cleanup_trades(db)
        _make_trade(db, ticker="PHHCB_SAME", qty=10, entry=100.0)       # 1000
        _make_trade(db, ticker="PHHCB_SAME", qty=5, entry=200.0)        # 1000
        _make_trade(db, ticker="PHHCB_OTHER", qty=3, entry=100.0)       # 300

        pb = compute_portfolio_budget(
            db, user_id=None, ticker="PHHCB_SAME", capital=100_000.0,
        )
        assert pb.deployed_notional == pytest.approx(2300.0)
        assert pb.ticker_open_notional == pytest.approx(2000.0)

    def test_max_total_notional_pct_scales_cap(self, db):
        _cleanup_trades(db)
        pb = compute_portfolio_budget(
            db, user_id=None, ticker="PHHCB_X", capital=100_000.0,
            max_total_notional_pct=50.0,
        )
        assert pb.max_total_notional == pytest.approx(50_000.0)
