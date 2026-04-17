"""Unit tests for app.services.trading.pit_audit (Phase C)."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.models.trading import PitAuditLog, ScanPattern, UniverseSnapshot
from app.services.trading import pit_audit, universe_snapshot


def _mk_pattern(
    db,
    *,
    name: str,
    conditions: list[dict],
    origin: str = "mined",
    lifecycle_stage: str = "validated",
    active: bool = True,
) -> ScanPattern:
    p = ScanPattern(
        name=name,
        description=name,
        rules_json=json.dumps({"conditions": conditions}),
        origin=origin,
        active=active,
        lifecycle_stage=lifecycle_stage,
        confidence=0.6,
        evidence_count=25,
    )
    db.add(p)
    db.flush()
    return p


class TestAuditPattern:
    def test_clean_pattern(self, db):
        p = _mk_pattern(
            db,
            name="clean",
            conditions=[
                {"indicator": "rsi_14", "op": "<", "value": 30},
                {"indicator": "macd_histogram", "op": ">", "value": 0},
            ],
        )
        result = pit_audit.audit_pattern(p)
        assert result.pattern_id == p.id
        assert result.pit_fields == ["rsi_14", "macd_histogram"]
        assert result.non_pit_fields == []
        assert result.unknown_fields == []
        assert result.agree_bool is True
        assert result.violation_count == 0

    def test_pattern_with_forbidden_field(self, db):
        p = _mk_pattern(
            db,
            name="lookahead",
            conditions=[
                {"indicator": "rsi_14", "op": "<", "value": 30},
                {"indicator": "future_return_5d", "op": ">", "value": 0.02},
            ],
        )
        result = pit_audit.audit_pattern(p)
        assert result.non_pit_fields == ["future_return_5d"]
        assert result.agree_bool is False
        assert result.violation_count == 1

    def test_pattern_with_unknown_field(self, db):
        p = _mk_pattern(
            db,
            name="unknown",
            conditions=[
                {"indicator": "my_secret_feature", "op": ">", "value": 0.5},
            ],
        )
        result = pit_audit.audit_pattern(p)
        assert result.unknown_fields == ["my_secret_feature"]
        assert result.agree_bool is False
        assert result.violation_count == 1

    def test_pattern_with_empty_rules(self, db):
        p = _mk_pattern(db, name="empty", conditions=[])
        result = pit_audit.audit_pattern(p)
        assert result.pit_fields == []
        assert result.non_pit_fields == []
        assert result.unknown_fields == []
        assert result.agree_bool is True


class TestAuditActivePatterns:
    def test_only_active_respected(self, db):
        _mk_pattern(db, name="active_v", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}], active=True, lifecycle_stage="validated")
        _mk_pattern(db, name="inactive", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}], active=False, lifecycle_stage="validated")
        results = pit_audit.audit_active_patterns(db)
        names = {r.name for r in results}
        assert "active_v" in names
        assert "inactive" not in names

    def test_lifecycle_stage_filter_default_excludes_candidate(self, db):
        _mk_pattern(db, name="cand", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}], lifecycle_stage="candidate")
        _mk_pattern(db, name="val", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}], lifecycle_stage="validated")
        results = pit_audit.audit_active_patterns(db)
        names = {r.name for r in results}
        assert "val" in names
        assert "cand" not in names

    def test_custom_lifecycle_stages(self, db):
        _mk_pattern(db, name="cand", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}], lifecycle_stage="candidate")
        results = pit_audit.audit_active_patterns(db, lifecycle_stages=("candidate",))
        names = {r.name for r in results}
        assert "cand" in names

    def test_limit(self, db):
        for i in range(5):
            _mk_pattern(db, name=f"p_{i}", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}])
        results = pit_audit.audit_active_patterns(db, limit=2)
        assert len(results) == 2


class TestRecordAudit:
    def test_record_writes_row(self, db):
        p = _mk_pattern(db, name="clean", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}])
        result = pit_audit.audit_pattern(p)
        row_id = pit_audit.record_audit(db, result, mode="shadow")
        assert row_id is not None
        row = db.query(PitAuditLog).filter(PitAuditLog.id == row_id).one()
        assert row.pattern_id == p.id
        assert row.agree_bool is True
        assert row.pit_count == 1
        assert row.non_pit_count == 0
        assert row.unknown_count == 0
        assert row.mode == "shadow"
        assert row.pit_fields == ["rsi_14"]

    def test_record_violation(self, db):
        p = _mk_pattern(
            db,
            name="bad",
            conditions=[{"indicator": "future_return_5d", "op": ">", "value": 0.02}],
        )
        result = pit_audit.audit_pattern(p)
        row_id = pit_audit.record_audit(db, result, mode="shadow")
        row = db.query(PitAuditLog).filter(PitAuditLog.id == row_id).one()
        assert row.agree_bool is False
        assert row.non_pit_count == 1
        assert row.non_pit_fields == ["future_return_5d"]


class TestAuditAndRecord:
    def test_mode_off_returns_empty(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.pit_audit.settings.brain_pit_audit_mode", "off", raising=False
        )
        _mk_pattern(db, name="p", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}])
        results = pit_audit.audit_and_record_active(db)
        assert results == []
        assert db.query(PitAuditLog).count() == 0

    def test_mode_shadow_records(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.pit_audit.settings.brain_pit_audit_mode", "shadow", raising=False
        )
        _mk_pattern(db, name="p1", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}])
        _mk_pattern(
            db,
            name="p2",
            conditions=[{"indicator": "future_return_5d", "op": ">", "value": 0.02}],
        )
        results = pit_audit.audit_and_record_active(db)
        assert len(results) == 2
        rows = db.query(PitAuditLog).all()
        assert len(rows) == 2
        modes = {r.mode for r in rows}
        assert modes == {"shadow"}


class TestAuditSummary:
    def test_empty_db(self, db):
        summary = pit_audit.audit_summary(db, lookback_hours=24)
        assert summary["audits_total"] == 0
        assert summary["patterns_audited"] == 0
        assert summary["patterns_clean"] == 0
        assert summary["patterns_violating"] == 0
        assert summary["top_violators"] == []

    def test_mixed_patterns(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.pit_audit.settings.brain_pit_audit_mode", "shadow", raising=False
        )
        _mk_pattern(db, name="clean", conditions=[{"indicator": "rsi_14", "op": "<", "value": 30}])
        _mk_pattern(
            db,
            name="bad",
            conditions=[
                {"indicator": "future_return_5d", "op": ">", "value": 0.02},
                {"indicator": "some_unknown_field", "op": ">", "value": 0.5},
            ],
        )
        pit_audit.audit_and_record_active(db)
        summary = pit_audit.audit_summary(db, lookback_hours=24)
        assert summary["audits_total"] == 2
        assert summary["patterns_audited"] == 2
        assert summary["patterns_clean"] == 1
        assert summary["patterns_violating"] == 1
        assert summary["forbidden_hits_by_field"] == {"future_return_5d": 1}
        assert summary["unknown_hits_by_field"] == {"some_unknown_field": 1}
        assert len(summary["top_violators"]) == 1
        assert summary["top_violators"][0]["name"] == "bad"


class TestUniverseSnapshot:
    def test_record_and_lookup(self, db):
        uid = universe_snapshot.record_snapshot(
            db,
            as_of_date=date(2025, 1, 15),
            ticker="AAPL",
            asset_class="equity",
            status="active",
            primary_exchange="NASDAQ",
            source="manual",
        )
        assert uid is not None
        row = db.query(UniverseSnapshot).filter(UniverseSnapshot.id == uid).one()
        assert row.ticker == "AAPL"
        assert row.status == "active"
        assert row.as_of_date == date(2025, 1, 15)

    def test_record_idempotent(self, db):
        for _ in range(3):
            universe_snapshot.record_snapshot(
                db,
                as_of_date=date(2025, 1, 15),
                ticker="AAPL",
                asset_class="equity",
                status="active",
            )
        count = db.query(UniverseSnapshot).filter(UniverseSnapshot.ticker == "AAPL").count()
        assert count == 1

    def test_record_invalid_status(self, db):
        with pytest.raises(ValueError):
            universe_snapshot.record_snapshot(
                db,
                as_of_date=date(2025, 1, 15),
                ticker="AAPL",
                asset_class="equity",
                status="not_a_real_status",
            )

    def test_record_empty_ticker_raises(self, db):
        with pytest.raises(ValueError):
            universe_snapshot.record_snapshot(
                db,
                as_of_date=date(2025, 1, 15),
                ticker="",
                asset_class="equity",
                status="active",
            )

    def test_lookup_status_falls_back(self, db):
        universe_snapshot.record_snapshot(
            db,
            as_of_date=date(2025, 1, 10),
            ticker="AAPL",
            asset_class="equity",
            status="active",
        )
        universe_snapshot.record_snapshot(
            db,
            as_of_date=date(2025, 1, 12),
            ticker="AAPL",
            asset_class="equity",
            status="halted",
        )
        assert universe_snapshot.lookup_status(db, ticker="AAPL", as_of_date=date(2025, 1, 15)) == "halted"
        assert universe_snapshot.lookup_status(db, ticker="AAPL", as_of_date=date(2025, 1, 11)) == "active"
        assert universe_snapshot.lookup_status(db, ticker="AAPL", as_of_date=date(2025, 1, 1)) is None
