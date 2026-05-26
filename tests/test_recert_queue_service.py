"""Phase J - DB integration tests for ``recert_queue_service``."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text

from app.models.trading import BacktestResult, ScanPattern
from app.services.trading.drift_monitor_model import (
    DriftMonitorInput,
    compute_drift,
)
from app.services.trading.recert_queue_service import (
    complete_open_recerts_from_backtest,
    queue_from_drift,
    queue_manual,
    recert_summary,
    mode_is_active,
    reconcile_dispatched_recerts_from_backtests,
)


def _cleanup(db) -> None:
    db.execute(text("DELETE FROM trading_pattern_recert_log"))
    db.commit()


def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        "app.services.trading.recert_queue_service.settings.brain_recert_queue_mode",
        mode,
        raising=False,
    )


def _red_drift(pattern_id: int = 42):
    return compute_drift(
        DriftMonitorInput(
            scan_pattern_id=pattern_id,
            pattern_name=f"pat_{pattern_id}",
            baseline_win_prob=0.7,
            outcomes=[0] * 30,
            as_of_key="2026-04-17",
        )
    )


def _green_drift(pattern_id: int = 42):
    return compute_drift(
        DriftMonitorInput(
            scan_pattern_id=pattern_id,
            pattern_name=f"pat_{pattern_id}",
            baseline_win_prob=0.5,
            outcomes=[1, 0] * 15,
            as_of_key="2026-04-17",
        )
    )


class TestModeGate:
    def test_off_mode_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "off")
        assert mode_is_active() is False
        res = queue_from_drift(
            db, _red_drift(), as_of_date=date(2026, 4, 17),
        )
        assert res is None
        manual = queue_manual(
            db, scan_pattern_id=1, pattern_name="x",
            as_of_date=date(2026, 4, 17), reason="test",
        )
        assert manual is None

    def test_shadow_red_writes_row(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        res = queue_from_drift(
            db, _red_drift(pattern_id=701),
            as_of_date=date(2026, 4, 17),
            drift_log_id=5555,
        )
        assert res is not None
        assert res.mode == "shadow"
        assert res.status == "proposed"
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_pattern_recert_log "
            "WHERE scan_pattern_id = 701 AND severity = 'red'"
        )).scalar_one()
        assert count == 1

    def test_shadow_green_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        res = queue_from_drift(
            db, _green_drift(pattern_id=702),
            as_of_date=date(2026, 4, 17),
        )
        assert res is None
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_pattern_recert_log "
            "WHERE scan_pattern_id = 702"
        )).scalar_one()
        assert count == 0

    def test_authoritative_refuses(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "authoritative")
        with pytest.raises(RuntimeError, match="authoritative"):
            queue_from_drift(
                db, _red_drift(pattern_id=703),
                as_of_date=date(2026, 4, 17),
            )
        with pytest.raises(RuntimeError, match="authoritative"):
            queue_manual(
                db, scan_pattern_id=704, pattern_name="x",
                as_of_date=date(2026, 4, 17), reason="test",
            )


class TestIdempotencyAndManual:
    def test_duplicate_recert_id_is_skipped(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        r1 = queue_from_drift(
            db, _red_drift(pattern_id=801),
            as_of_date=date(2026, 4, 17),
        )
        r2 = queue_from_drift(
            db, _red_drift(pattern_id=801),
            as_of_date=date(2026, 4, 17),
        )
        assert r1 is not None and r2 is not None
        assert r1.recert_id == r2.recert_id
        assert r1.log_id == r2.log_id
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_pattern_recert_log "
            "WHERE scan_pattern_id = 801"
        )).scalar_one()
        assert count == 1

    def test_open_manual_recert_for_pattern_source_is_reused(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        r1 = queue_manual(
            db,
            scan_pattern_id=902,
            pattern_name="op_pattern",
            as_of_date=date(2026, 4, 17),
            reason="operator initiated",
        )
        r2 = queue_manual(
            db,
            scan_pattern_id=902,
            pattern_name="op_pattern",
            as_of_date=date(2026, 4, 18),
            reason="still required",
        )
        assert r1 is not None and r2 is not None
        assert r1.log_id == r2.log_id
        assert r1.recert_id == r2.recert_id
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_pattern_recert_log "
            "WHERE scan_pattern_id = 902"
        )).scalar_one()
        assert count == 1

    def test_manual_writes_row(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        res = queue_manual(
            db,
            scan_pattern_id=901,
            pattern_name="op_pattern",
            as_of_date=date(2026, 4, 17),
            reason="operator initiated",
        )
        assert res is not None
        row = db.execute(text(
            "SELECT source, status, reason, severity "
            "FROM trading_pattern_recert_log WHERE id = :id"
        ), {"id": res.log_id}).fetchone()
        assert row[0] == "manual"
        assert row[1] == "proposed"
        assert row[2] == "operator initiated"
        assert row[3] is None


class TestBacktestCompletion:
    def test_completion_prefers_oos_backtest_evidence_over_full_run_totals(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        observed_at = datetime.utcnow() - timedelta(minutes=5)
        pat = ScanPattern(
            name="oos repair pattern",
            rules_json={},
            active=True,
            lifecycle_stage="promoted",
            promotion_status="promoted",
            promotion_gate_passed=True,
            cpcv_n_paths=10,
            quality_composite_score=0.9,
            raw_realized_trade_count=12,
            raw_realized_avg_return_pct=1.2,
            payoff_ratio=2.0,
            payoff_ratio_n=12,
            recert_required=True,
            recert_reason="missing_oos_recert",
        )
        db.add(pat)
        db.flush()
        db.execute(text("""
            INSERT INTO trading_pattern_recert_log (
                recert_id, scan_pattern_id, pattern_name, as_of_date,
                source, severity, status, reason, payload_json, mode, observed_at
            ) VALUES (
                :rid, :pid, :name, :as_of, 'scheduler', 'red', 'dispatched',
                'alpha_portfolio_gate:missing_oos_recert', '{}'::jsonb, 'shadow', :observed_at
            )
        """), {
            "rid": f"test-oos-{pat.id}",
            "pid": pat.id,
            "name": pat.name,
            "as_of": date.today(),
            "observed_at": observed_at,
        })
        db.add_all([
            BacktestResult(
                ticker="AAA",
                strategy_name="test",
                return_pct=10.0,
                win_rate=1.0,
                max_drawdown=0.0,
                trade_count=10,
                scan_pattern_id=pat.id,
                ran_at=datetime.utcnow(),
                oos_win_rate=0.5,
                oos_return_pct=2.0,
                oos_trade_count=2,
            ),
            BacktestResult(
                ticker="BBB",
                strategy_name="test",
                return_pct=99.0,
                win_rate=1.0,
                max_drawdown=0.0,
                trade_count=99,
                scan_pattern_id=pat.id,
                ran_at=datetime.utcnow(),
                oos_win_rate=2.0 / 3.0,
                oos_return_pct=4.0,
                oos_trade_count=3,
            ),
        ])
        db.commit()

        out = complete_open_recerts_from_backtest(
            db,
            scan_pattern_id=pat.id,
            total=99,
            wins=99,
            win_rate=1.0,
            avg_return=99.0,
            backtests_run=99,
        )

        db.refresh(pat)
        assert out["completed"] == 1
        assert pat.oos_trade_count == 5
        assert pat.oos_win_rate == pytest.approx(0.6)
        assert pat.oos_avg_return_pct == pytest.approx(3.2)
        assert pat.recert_required is False
        status = db.execute(text(
            "SELECT status FROM trading_pattern_recert_log WHERE scan_pattern_id = :pid"
        ), {"pid": pat.id}).scalar_one()
        assert status == "completed"

    def test_reconcile_repairs_dispatched_row_after_backtest_already_ran(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        observed_at = datetime.utcnow() - timedelta(hours=1)
        pat = ScanPattern(
            name="stale dispatched recert",
            rules_json={},
            active=True,
            lifecycle_stage="promoted",
            promotion_status="promoted",
            promotion_gate_passed=True,
            cpcv_n_paths=10,
            quality_composite_score=0.8,
            raw_realized_trade_count=8,
            raw_realized_avg_return_pct=0.7,
            recert_required=True,
            recert_reason="missing_oos_recert",
        )
        db.add(pat)
        db.flush()
        db.execute(text("""
            INSERT INTO trading_pattern_recert_log (
                recert_id, scan_pattern_id, pattern_name, as_of_date,
                source, severity, status, reason, payload_json, mode, observed_at
            ) VALUES (
                :rid, :pid, :name, :as_of, 'scheduler', 'red', 'dispatched',
                'alpha_portfolio_gate:missing_oos_recert', '{}'::jsonb, 'shadow', :observed_at
            )
        """), {
            "rid": f"test-reconcile-{pat.id}",
            "pid": pat.id,
            "name": pat.name,
            "as_of": date.today(),
            "observed_at": observed_at,
        })
        db.add(BacktestResult(
            ticker="CCC",
            strategy_name="test",
            return_pct=3.0,
            win_rate=0.5,
            max_drawdown=0.0,
            trade_count=4,
            scan_pattern_id=pat.id,
            ran_at=datetime.utcnow(),
            oos_win_rate=0.75,
            oos_return_pct=1.5,
            oos_trade_count=5,
        ))
        db.commit()

        out = reconcile_dispatched_recerts_from_backtests(db, limit=10)

        db.refresh(pat)
        assert out["completed"] >= 1
        assert any(r["scan_pattern_id"] == pat.id for r in out["repaired"])
        assert pat.oos_trade_count == 5
        assert pat.oos_win_rate == pytest.approx(0.75)
        assert pat.recert_required is False


class TestSummary:
    def test_summary_frozen_shape(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        queue_from_drift(
            db, _red_drift(pattern_id=1001),
            as_of_date=date(2026, 4, 17),
        )
        queue_manual(
            db, scan_pattern_id=1002, pattern_name="m",
            as_of_date=date(2026, 4, 17), reason="check",
        )
        summary = recert_summary(db, lookback_days=14)
        assert set(summary.keys()) == {
            "mode", "lookback_days", "recert_events_total",
            "by_source", "by_severity", "by_status",
            "patterns_queued_distinct", "latest_recert",
        }
        assert summary["mode"] == "shadow"
        assert summary["recert_events_total"] >= 2
        assert set(summary["by_source"].keys()) == {
            "drift_monitor", "manual", "scheduler", "other",
        }
        assert set(summary["by_severity"].keys()) == {
            "red", "yellow", "green", "null",
        }
        assert set(summary["by_status"].keys()) == {
            "proposed", "dispatched", "completed", "cancelled", "other",
        }
        assert summary["latest_recert"] is not None
        assert set(summary["latest_recert"].keys()) == {
            "recert_id", "scan_pattern_id", "pattern_name", "severity",
            "source", "status", "observed_at",
        }
