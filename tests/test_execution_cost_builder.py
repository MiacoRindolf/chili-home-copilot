"""DB integration tests for `execution_cost_builder` (Phase F)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.trading import ExecutionCostEstimate, Trade
from app.services.trading.execution_cost_builder import (
    BuilderReport,
    EstimateRow,
    compute_rolling_estimate,
    estimates_summary,
    mode_is_active,
    rebuild_all,
    upsert_estimate,
)


def _mk_trade(
    ticker: str,
    *,
    entry_slip: float | None = 5.0,
    exit_slip: float | None = 3.0,
    qty: float = 100.0,
    price: float = 50.0,
    direction: str = "long",
    days_ago: int = 1,
) -> Trade:
    now = datetime.utcnow()
    return Trade(
        user_id=None,
        ticker=ticker,
        direction=direction,
        entry_price=price,
        exit_price=price * 1.01,
        quantity=qty,
        entry_date=now - timedelta(days=days_ago),
        exit_date=now - timedelta(days=days_ago - 1, hours=-1) if days_ago > 0 else now,
        status="closed",
        pnl=10.0,
        tca_entry_slippage_bps=entry_slip,
        tca_exit_slippage_bps=exit_slip,
    )


def _cleanup(db, tickers):
    db.query(ExecutionCostEstimate).filter(
        ExecutionCostEstimate.ticker.in_(tickers)
    ).delete(synchronize_session=False)
    db.query(Trade).filter(Trade.ticker.in_(tickers)).delete(synchronize_session=False)
    db.commit()


class TestComputeRollingEstimate:
    def test_no_trades_returns_none(self, db):
        out = compute_rolling_estimate(
            db, ticker="___NONEXISTENT___", side="long", window_days=30
        )
        assert out is None

    def test_returns_estimate_with_tca_slip(self, db):
        _cleanup(db, ["AAPL_PHF"])
        db.add(_mk_trade("AAPL_PHF", entry_slip=5.0, exit_slip=3.0))
        db.add(_mk_trade("AAPL_PHF", entry_slip=10.0, exit_slip=8.0, days_ago=2))
        db.add(_mk_trade("AAPL_PHF", entry_slip=2.0, exit_slip=1.0, days_ago=3))
        db.commit()

        est = compute_rolling_estimate(
            db, ticker="AAPL_PHF", side="long", window_days=30
        )
        assert est is not None
        assert est.ticker == "AAPL_PHF"
        assert est.side == "long"
        assert est.window_days == 30
        assert est.sample_trades == 3
        # p90 >= median for both spread and slippage
        assert est.p90_spread_bps >= est.median_spread_bps
        assert est.p90_slippage_bps >= est.median_slippage_bps
        # spread = abs(entry_slip); median of {5, 10, 2} = 5
        assert est.median_spread_bps == pytest.approx(5.0)

        _cleanup(db, ["AAPL_PHF"])

    def test_skips_trades_with_no_tca_data(self, db):
        _cleanup(db, ["MSFT_PHF"])
        # One usable, one not.
        db.add(_mk_trade("MSFT_PHF", entry_slip=7.0, exit_slip=None))
        db.add(_mk_trade("MSFT_PHF", entry_slip=None, exit_slip=None, days_ago=2))
        db.commit()

        est = compute_rolling_estimate(
            db, ticker="MSFT_PHF", side="long", window_days=30
        )
        assert est is not None
        assert est.sample_trades == 2   # both trades counted as "window samples"
        assert est.p90_slippage_bps == pytest.approx(7.0)  # only one slip value
        _cleanup(db, ["MSFT_PHF"])

    def test_filters_by_direction(self, db):
        _cleanup(db, ["NVDA_PHF"])
        db.add(_mk_trade("NVDA_PHF", direction="long", entry_slip=3.0))
        db.add(_mk_trade("NVDA_PHF", direction="short", entry_slip=9.0, days_ago=2))
        db.commit()

        long_est = compute_rolling_estimate(
            db, ticker="NVDA_PHF", side="long", window_days=30
        )
        short_est = compute_rolling_estimate(
            db, ticker="NVDA_PHF", side="short", window_days=30
        )
        assert long_est.sample_trades == 1
        assert short_est.sample_trades == 1
        assert long_est.median_spread_bps == pytest.approx(3.0)
        assert short_est.median_spread_bps == pytest.approx(9.0)
        _cleanup(db, ["NVDA_PHF"])

    def test_adv_lookup_preferred(self, db):
        _cleanup(db, ["TSLA_PHF"])
        db.add(_mk_trade("TSLA_PHF", entry_slip=4.0, qty=100, price=200))
        db.commit()
        est = compute_rolling_estimate(
            db, ticker="TSLA_PHF", side="long", window_days=30,
            adv_lookup_fn=lambda t, w: 5_000_000.0,
        )
        assert est.avg_daily_volume_usd == pytest.approx(5_000_000.0)
        _cleanup(db, ["TSLA_PHF"])

    def test_adv_fallback_when_lookup_returns_zero(self, db):
        _cleanup(db, ["AMD_PHF"])
        db.add(_mk_trade("AMD_PHF", entry_slip=4.0, qty=100, price=200))
        db.commit()
        est = compute_rolling_estimate(
            db, ticker="AMD_PHF", side="long", window_days=30,
            adv_lookup_fn=lambda t, w: 0.0,
        )
        # notional = 100 * 200 = 20_000; window_days = 30 → adv fallback ≈ 666.67
        assert est.avg_daily_volume_usd == pytest.approx(20_000 / 30)
        _cleanup(db, ["AMD_PHF"])


class TestUpsertEstimate:
    def _row(self, ticker: str = "ROW_PHF") -> EstimateRow:
        return EstimateRow(
            ticker=ticker, side="long", window_days=30,
            median_spread_bps=2.0, p90_spread_bps=5.0,
            median_slippage_bps=1.5, p90_slippage_bps=4.0,
            avg_daily_volume_usd=1_000_000.0, sample_trades=10,
        )

    def test_off_mode_does_not_insert(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "off",
            raising=False,
        )
        wrote = upsert_estimate(db, self._row("OFF_PHF"))
        assert wrote is False
        _cleanup(db, ["OFF_PHF"])

    def test_shadow_mode_inserts_then_upserts(self, db, monkeypatch):
        _cleanup(db, ["SHADOW_PHF"])
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "shadow",
            raising=False,
        )
        assert upsert_estimate(db, self._row("SHADOW_PHF")) is True
        existing = (
            db.query(ExecutionCostEstimate)
            .filter_by(ticker="SHADOW_PHF", side="long", window_days=30)
            .all()
        )
        assert len(existing) == 1
        first_id = existing[0].id
        first_ts = existing[0].last_updated_at

        # Re-upsert with same key, different values
        updated = EstimateRow(
            ticker="SHADOW_PHF", side="long", window_days=30,
            median_spread_bps=3.0, p90_spread_bps=6.0,
            median_slippage_bps=2.5, p90_slippage_bps=5.0,
            avg_daily_volume_usd=2_000_000.0, sample_trades=20,
        )
        assert upsert_estimate(db, updated) is True

        db.expire_all()
        rows = (
            db.query(ExecutionCostEstimate)
            .filter_by(ticker="SHADOW_PHF", side="long", window_days=30)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].id == first_id  # idempotent: same row
        assert rows[0].median_spread_bps == pytest.approx(3.0)
        assert rows[0].sample_trades == 20
        assert rows[0].last_updated_at >= first_ts
        _cleanup(db, ["SHADOW_PHF"])


class TestRebuildAll:
    def test_off_mode_returns_empty_report(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "off",
            raising=False,
        )
        rep = rebuild_all(db, tickers=["ANY_PHF"])
        assert isinstance(rep, BuilderReport)
        assert rep.mode == "off"
        assert rep.estimates_written == 0

    def test_shadow_writes_one_per_side(self, db, monkeypatch):
        _cleanup(db, ["REBUILD_PHF"])
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "shadow",
            raising=False,
        )
        db.add(_mk_trade("REBUILD_PHF", direction="long", entry_slip=3.0))
        db.add(_mk_trade("REBUILD_PHF", direction="short", entry_slip=9.0))
        db.commit()

        rep = rebuild_all(
            db,
            tickers=["REBUILD_PHF"],
            window_days=30,
            sides=("long", "short"),
            adv_lookup_fn=lambda t, w: 1_000_000.0,
        )
        assert rep.estimates_written == 2
        assert rep.estimates_skipped == 0
        assert rep.tickers_seen == 1

        rows = (
            db.query(ExecutionCostEstimate)
            .filter_by(ticker="REBUILD_PHF")
            .all()
        )
        assert len(rows) == 2
        _cleanup(db, ["REBUILD_PHF"])

    def test_auto_discover_tickers_from_closed_trades(self, db, monkeypatch):
        _cleanup(db, ["DISC_PHF_A", "DISC_PHF_B"])
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "shadow",
            raising=False,
        )
        db.add(_mk_trade("DISC_PHF_A", entry_slip=4.0))
        db.add(_mk_trade("DISC_PHF_B", entry_slip=6.0))
        db.commit()

        rep = rebuild_all(
            db,
            tickers=None,
            window_days=30,
            sides=("long",),
            adv_lookup_fn=lambda t, w: 1.0,
        )
        # Others may exist from other tests; ensure both of ours wrote
        written_tickers = {
            r.ticker
            for r in db.query(ExecutionCostEstimate)
            .filter(ExecutionCostEstimate.ticker.in_(["DISC_PHF_A", "DISC_PHF_B"]))
            .all()
        }
        assert written_tickers == {"DISC_PHF_A", "DISC_PHF_B"}
        _cleanup(db, ["DISC_PHF_A", "DISC_PHF_B"])


class TestModeIsActive:
    def test_default_off(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "off",
            raising=False,
        )
        assert mode_is_active() is False

    def test_shadow_active(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "shadow",
            raising=False,
        )
        assert mode_is_active() is True

    def test_override_wins(self):
        assert mode_is_active("shadow") is True
        assert mode_is_active("off") is False

    def test_bogus_mode_falls_to_off(self):
        assert mode_is_active("garbage") is False


class TestEstimatesSummary:
    def test_returns_frozen_shape(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.execution_cost_builder.settings.brain_execution_cost_mode",
            "shadow",
            raising=False,
        )
        summary = estimates_summary(db)
        for key in (
            "mode", "estimates_total", "tickers",
            "by_side", "stale_estimates",
            "stale_threshold_hours", "last_refresh_at",
        ):
            assert key in summary, f"missing key: {key}"
        assert summary["mode"] == "shadow"
        assert isinstance(summary["by_side"], dict)
