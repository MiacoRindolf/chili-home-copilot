"""Phase J - DB integration tests for ``drift_monitor_service``.

Exercises mode gating, authoritative refusal, row writes, JSON
payload shape, and summary frozen shape.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from app.services.trading.drift_monitor_service import (
    DriftInputBundle,
    drift_summary,
    evaluate_one,
    mode_is_active,
    mode_is_authoritative,
    run_sweep,
)


def _cleanup(db) -> None:
    db.execute(text("DELETE FROM trading_pattern_drift_log"))
    db.commit()


def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        "app.services.trading.drift_monitor_service.settings.brain_drift_monitor_mode",
        mode,
        raising=False,
    )


def _bundle(
    pattern_id: int = 101,
    baseline: float | None = 0.55,
    outcomes: list[int] | None = None,
) -> DriftInputBundle:
    return DriftInputBundle(
        scan_pattern_id=pattern_id,
        pattern_name=f"pat_{pattern_id}",
        baseline_win_prob=baseline,
        outcomes=outcomes if outcomes is not None else [1, 0, 1, 0],
    )


class TestModeGate:
    def test_off_mode_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "off")
        assert mode_is_active() is False
        res = evaluate_one(
            db, bundle=_bundle(), as_of_key="2026-04-17",
        )
        assert res is None
        rows_sweep = run_sweep(
            db, bundles=[_bundle()], as_of_date=date(2026, 4, 17),
        )
        assert rows_sweep == []

    def test_shadow_mode_writes_one_row(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        assert mode_is_active() is True
        assert mode_is_authoritative() is False
        res = evaluate_one(
            db, bundle=_bundle(), as_of_key="2026-04-17",
        )
        assert res is not None
        assert res.mode == "shadow"
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_pattern_drift_log "
            "WHERE scan_pattern_id = 101"
        )).scalar_one()
        assert count == 1

    def test_authoritative_mode_refuses_and_raises(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "authoritative")
        with pytest.raises(RuntimeError, match="authoritative"):
            evaluate_one(
                db, bundle=_bundle(), as_of_key="2026-04-17",
            )
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_pattern_drift_log "
            "WHERE mode = 'authoritative'"
        )).scalar_one()
        assert count == 0


class TestRowContents:
    def test_row_captures_stats_and_severity(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        outcomes = [0] * 30
        res = evaluate_one(
            db,
            bundle=_bundle(pattern_id=202, baseline=0.7, outcomes=outcomes),
            as_of_key="2026-04-17",
        )
        assert res is not None
        assert res.severity == "red"
        row = db.execute(text("""
            SELECT drift_id, scan_pattern_id, pattern_name,
                   baseline_win_prob, observed_win_prob, brier_delta,
                   cusum_statistic, cusum_threshold, sample_size,
                   severity, mode
            FROM trading_pattern_drift_log WHERE id = :id
        """), {"id": res.log_id}).fetchone()
        assert row is not None
        assert row[0] == res.drift_id
        assert row[1] == 202
        assert float(row[3]) == pytest.approx(0.7)
        assert float(row[4]) == pytest.approx(0.0)
        assert float(row[5]) == pytest.approx(-0.7)
        assert float(row[6]) > 0
        assert int(row[8]) == 30
        assert row[9] == "red"
        assert row[10] == "shadow"

    def test_payload_json_populated(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        res = evaluate_one(
            db,
            bundle=_bundle(pattern_id=303, baseline=0.5, outcomes=[1, 0] * 15),
            as_of_key="2026-04-17",
        )
        assert res is not None
        payload = db.execute(text(
            "SELECT payload_json FROM trading_pattern_drift_log WHERE id = :id"
        ), {"id": res.log_id}).scalar_one()
        assert isinstance(payload, dict)
        assert "baseline" in payload
        assert "cusum_k" in payload


class TestDeterministicIdAndDedupe:
    def test_same_pattern_day_produces_same_drift_id(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        r1 = evaluate_one(
            db, bundle=_bundle(pattern_id=404), as_of_key="2026-04-17",
        )
        r2 = evaluate_one(
            db, bundle=_bundle(pattern_id=404), as_of_key="2026-04-17",
        )
        assert r1 is not None and r2 is not None
        assert r1.drift_id == r2.drift_id
        # Both rows persisted; service is append-only (scheduler handles dedupe).
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_pattern_drift_log "
            "WHERE scan_pattern_id = 404"
        )).scalar_one()
        assert count == 2


class TestSweepAcrossBundles:
    def test_run_sweep_writes_one_row_per_bundle(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        bundles = [
            _bundle(pattern_id=501, baseline=0.5, outcomes=[1, 0] * 10),
            _bundle(pattern_id=502, baseline=0.7, outcomes=[0] * 30),
            _bundle(pattern_id=503, baseline=None, outcomes=[1, 1, 1]),
        ]
        rows = run_sweep(
            db, bundles=bundles, as_of_date=date(2026, 4, 17),
        )
        assert len(rows) == 3
        sev_by_pattern = {r.scan_pattern_id: r.severity for r in rows}
        assert sev_by_pattern[502] == "red"
        assert sev_by_pattern[501] == "green"
        assert sev_by_pattern[503] == "green"


class TestSummary:
    def test_summary_frozen_shape(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        evaluate_one(
            db,
            bundle=_bundle(pattern_id=601, baseline=0.5, outcomes=[1, 0] * 15),
            as_of_key="2026-04-17",
        )
        evaluate_one(
            db,
            bundle=_bundle(pattern_id=602, baseline=0.7, outcomes=[0] * 30),
            as_of_key="2026-04-17",
        )
        summary = drift_summary(db, lookback_days=14)
        assert set(summary.keys()) == {
            "mode", "lookback_days", "drift_events_total",
            "by_severity", "patterns_red", "patterns_yellow",
            "mean_brier_delta", "mean_cusum_statistic", "latest_drift",
        }
        assert summary["mode"] == "shadow"
        assert summary["drift_events_total"] >= 2
        assert set(summary["by_severity"].keys()) == {"green", "yellow", "red"}
        assert summary["patterns_red"] >= 1
        assert summary["latest_drift"] is not None
        assert set(summary["latest_drift"].keys()) == {
            "drift_id", "scan_pattern_id", "pattern_name", "severity",
            "sample_size", "observed_at",
        }
