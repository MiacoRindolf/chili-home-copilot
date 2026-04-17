"""DB integration tests for `venue_truth` (Phase F)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.trading import VenueTruthLog
from app.services.trading.venue_truth import (
    FillObservation,
    mode_is_active,
    record_fill_observation,
    venue_truth_summary,
)


def _obs(
    ticker: str = "VT_PHF",
    *,
    expected_cost_fraction: float = 0.0005,
    realized_cost_fraction: float = 0.0007,
    **kw,
) -> FillObservation:
    return FillObservation(
        ticker=ticker,
        side=kw.pop("side", "long"),
        notional_usd=kw.pop("notional_usd", 10_000.0),
        expected_spread_bps=kw.pop("expected_spread_bps", 3.0),
        realized_spread_bps=kw.pop("realized_spread_bps", 4.5),
        expected_slippage_bps=kw.pop("expected_slippage_bps", 2.0),
        realized_slippage_bps=kw.pop("realized_slippage_bps", 2.5),
        expected_cost_fraction=expected_cost_fraction,
        realized_cost_fraction=realized_cost_fraction,
        trade_id=kw.pop("trade_id", None),
        paper_bool=kw.pop("paper_bool", True),
    )


def _cleanup(db, tickers):
    db.query(VenueTruthLog).filter(
        VenueTruthLog.ticker.in_(tickers)
    ).delete(synchronize_session=False)
    db.commit()


class TestRecordFillObservation:
    def test_off_mode_noop(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "off",
            raising=False,
        )
        wrote = record_fill_observation(db, _obs(ticker="OFF_VT_PHF"))
        assert wrote is False
        assert db.query(VenueTruthLog).filter_by(ticker="OFF_VT_PHF").count() == 0

    def test_shadow_mode_writes(self, db, monkeypatch):
        _cleanup(db, ["SHADOW_VT_PHF"])
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "shadow",
            raising=False,
        )
        wrote = record_fill_observation(db, _obs(ticker="SHADOW_VT_PHF"))
        assert wrote is True
        rows = db.query(VenueTruthLog).filter_by(ticker="SHADOW_VT_PHF").all()
        assert len(rows) == 1
        r = rows[0]
        assert r.mode == "shadow"
        assert r.paper_bool is True
        assert r.expected_cost_fraction == pytest.approx(0.0005)
        assert r.realized_cost_fraction == pytest.approx(0.0007)
        _cleanup(db, ["SHADOW_VT_PHF"])

    def test_nullable_expected_fields(self, db, monkeypatch):
        _cleanup(db, ["NULL_VT_PHF"])
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "shadow",
            raising=False,
        )
        obs = FillObservation(
            ticker="NULL_VT_PHF", side="long", notional_usd=1_000.0,
            expected_spread_bps=None, realized_spread_bps=None,
            expected_slippage_bps=None, realized_slippage_bps=5.0,
            expected_cost_fraction=None, realized_cost_fraction=0.001,
        )
        assert record_fill_observation(db, obs) is True
        rows = db.query(VenueTruthLog).filter_by(ticker="NULL_VT_PHF").all()
        assert len(rows) == 1
        assert rows[0].expected_cost_fraction is None
        assert rows[0].realized_cost_fraction == pytest.approx(0.001)
        _cleanup(db, ["NULL_VT_PHF"])

    def test_override_mode_wins_over_setting(self, db, monkeypatch):
        _cleanup(db, ["OVR_VT_PHF"])
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "off",
            raising=False,
        )
        wrote = record_fill_observation(
            db, _obs(ticker="OVR_VT_PHF"), mode_override="shadow"
        )
        assert wrote is True
        rows = db.query(VenueTruthLog).filter_by(ticker="OVR_VT_PHF").all()
        assert len(rows) == 1
        assert rows[0].mode == "shadow"
        _cleanup(db, ["OVR_VT_PHF"])


class TestModeIsActive:
    def test_default_off(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "off",
            raising=False,
        )
        assert mode_is_active() is False

    def test_shadow_active(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "shadow",
            raising=False,
        )
        assert mode_is_active() is True

    def test_bogus_falls_off(self):
        assert mode_is_active("garbage") is False

    def test_override_path(self):
        assert mode_is_active("authoritative") is True


class TestVenueTruthSummary:
    def test_empty_summary_shape(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "shadow",
            raising=False,
        )
        summary = venue_truth_summary(db, lookback_hours=1)
        assert summary["mode"] == "shadow"
        assert summary["lookback_hours"] == 1
        # observations_total may be nonzero from other tests in the same DB, so
        # the real invariant is the schema, not the count.
        for key in (
            "observations_total",
            "mean_expected_cost_fraction",
            "mean_realized_cost_fraction",
            "mean_gap_bps",
            "p90_gap_bps",
            "worst_tickers",
        ):
            assert key in summary
        assert isinstance(summary["worst_tickers"], list)

    def test_summary_computes_gap_stats(self, db, monkeypatch):
        _cleanup(db, ["GAP_VT_PHF"])
        monkeypatch.setattr(
            "app.services.trading.venue_truth.settings.brain_venue_truth_mode",
            "shadow",
            raising=False,
        )
        # expected = 5 bps (0.0005), realized = 7 bps (0.0007) → gap = 2 bps
        for _ in range(3):
            record_fill_observation(db, _obs(
                ticker="GAP_VT_PHF",
                expected_cost_fraction=0.0005,
                realized_cost_fraction=0.0007,
            ))

        summary = venue_truth_summary(db, lookback_hours=24, top_n=5)
        assert summary["observations_total"] >= 3
        worst = [w for w in summary["worst_tickers"] if w["ticker"] == "GAP_VT_PHF"]
        assert len(worst) == 1
        assert worst[0]["mean_gap_bps"] == pytest.approx(2.0, abs=1e-6)
        assert worst[0]["observations"] == 3
        _cleanup(db, ["GAP_VT_PHF"])
