"""Phase I - DB integration tests for ``capital_reweight_service``.

Exercises mode gating, authoritative refusal, row writes, JSON
payload shape, and summary frozen shape.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from app.services.trading.capital_reweight_model import BucketContext
from app.services.trading.capital_reweight_service import (
    mode_is_active,
    mode_is_authoritative,
    run_sweep,
    sweep_summary,
)


def _cleanup(db) -> None:
    db.execute(text(
        "DELETE FROM trading_capital_reweight_log "
        "WHERE reweight_id LIKE '%' AND "
        "(regime LIKE 'phi_test_%' OR TRUE)"
    ))
    # simpler: wipe all for a clean slate since fixtures truncate anyway
    db.commit()


def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        "app.services.trading.capital_reweight_service.settings.brain_capital_reweight_mode",
        mode,
        raising=False,
    )


def _default_buckets() -> tuple[BucketContext, ...]:
    return (
        BucketContext(name="equity:tech", current_notional=0.0, volatility=1.0),
        BucketContext(name="equity:fin", current_notional=0.0, volatility=2.0),
        BucketContext(name="crypto:majors", current_notional=0.0, volatility=4.0),
    )


class TestModeGate:
    def test_off_mode_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "off")
        assert mode_is_active() is False
        res = run_sweep(
            db,
            user_id=None,
            as_of_date=date(2026, 4, 16),
            total_capital=100_000.0,
            regime="risk_on",
            dial_value=1.0,
            buckets=_default_buckets(),
        )
        assert res is None

    def test_shadow_mode_writes_one_row(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        assert mode_is_active() is True
        assert mode_is_authoritative() is False
        res = run_sweep(
            db,
            user_id=1,
            as_of_date=date(2026, 4, 16),
            total_capital=100_000.0,
            regime="risk_on",
            dial_value=1.0,
            buckets=_default_buckets(),
        )
        assert res is not None
        assert res.mode == "shadow"
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_capital_reweight_log "
            "WHERE user_id = 1 AND as_of_date = '2026-04-16'"
        )).scalar_one()
        assert count == 1

    def test_authoritative_mode_refuses_and_raises(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "authoritative")
        with pytest.raises(RuntimeError, match="authoritative"):
            run_sweep(
                db,
                user_id=None,
                as_of_date=date(2026, 4, 16),
                total_capital=1_000.0,
                regime="cautious",
                dial_value=0.7,
                buckets=_default_buckets(),
            )
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_capital_reweight_log "
            "WHERE mode = 'authoritative'"
        )).scalar_one()
        assert count == 0


class TestRowContents:
    def test_row_captures_allocations_and_drift(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        buckets = (
            BucketContext(name="equity:a", current_notional=20_000.0, volatility=1.0),
            BucketContext(name="equity:b", current_notional=10_000.0, volatility=2.0),
        )
        res = run_sweep(
            db,
            user_id=2,
            as_of_date=date(2026, 4, 16),
            total_capital=100_000.0,
            regime="risk_on",
            dial_value=1.0,
            buckets=buckets,
        )
        assert res is not None
        row = db.execute(text("""
            SELECT reweight_id, user_id, total_capital, mean_drift_bps,
                   p90_drift_bps,
                   jsonb_array_length(proposed_allocations_json),
                   jsonb_array_length(current_allocations_json),
                   drift_bucket_json,
                   cap_triggers_json,
                   mode
            FROM trading_capital_reweight_log WHERE id = :id
        """), {"id": res.log_id}).fetchone()
        assert row is not None
        assert row[0] == res.reweight_id
        assert row[1] == 2
        assert float(row[2]) == pytest.approx(100_000.0)
        assert float(row[3]) > 0  # drift > 0 since current differs from target
        assert int(row[5]) == 2
        assert int(row[6]) == 2
        drift = row[7]
        assert set(drift.keys()) == {
            "under_50_bps", "50_200_bps", "200_1000_bps", "over_1000_bps",
        }
        assert row[9] == "shadow"

    def test_same_user_day_produces_same_reweight_id(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        r1 = run_sweep(
            db, user_id=5, as_of_date=date(2026, 4, 16),
            total_capital=10_000.0, regime="risk_on", dial_value=1.0,
            buckets=_default_buckets(),
        )
        r2 = run_sweep(
            db, user_id=5, as_of_date=date(2026, 4, 16),
            total_capital=10_000.0, regime="risk_on", dial_value=1.0,
            buckets=_default_buckets(),
        )
        assert r1 is not None and r2 is not None
        assert r1.reweight_id == r2.reweight_id
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_capital_reweight_log "
            "WHERE reweight_id = :rid"
        ), {"rid": r1.reweight_id}).scalar_one()
        assert count == 2


class TestSummary:
    def test_summary_frozen_shape(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        run_sweep(
            db, user_id=11, as_of_date=date(2026, 4, 16),
            total_capital=50_000.0, regime="risk_on", dial_value=1.0,
            buckets=_default_buckets(),
        )
        run_sweep(
            db, user_id=12, as_of_date=date(2026, 4, 16),
            total_capital=80_000.0, regime="cautious", dial_value=0.7,
            buckets=_default_buckets(),
        )
        summary = sweep_summary(db, lookback_days=14)
        assert set(summary.keys()) == {
            "mode", "lookback_days", "sweeps_total",
            "mean_mean_drift_bps", "p90_p90_drift_bps",
            "single_bucket_cap_trigger_count",
            "concentration_cap_trigger_count", "latest_sweep",
        }
        assert summary["mode"] == "shadow"
        assert summary["lookback_days"] == 14
        assert summary["sweeps_total"] >= 2
        assert summary["latest_sweep"] is not None
        assert set(summary["latest_sweep"].keys()) == {
            "reweight_id", "user_id", "as_of_date", "regime",
            "mean_drift_bps", "p90_drift_bps", "observed_at",
        }
