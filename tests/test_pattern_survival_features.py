from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.pattern_survival import features as features_mod
from app.services.trading.pattern_survival.features import (
    _collect_pnl_slope_14d,
    _collect_regime_and_diversity,
    _collect_realized_30d,
)


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.row or [])


class _Session:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = {}

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return _Result(self.row)


class _SequenceSession:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sqls = []
        self.params = []

    def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        self.params.append(dict(params or {}))
        return _Result(self.rows.pop(0))


def test_collect_realized_30d_filters_to_closed_valid_notional_trades() -> None:
    db = _Session((2, 0.5, 16.0, 8.0, -4.0))

    out = _collect_realized_30d(db, pattern_id=42)

    assert out["trades_30d"] == 2
    assert out["hit_rate_30d"] == pytest.approx(0.5)
    assert out["expectancy_30d_pct"] == pytest.approx(16.0)
    assert out["sharpe_30d"] == pytest.approx(2.0)
    assert out["max_drawdown_30d_pct"] == pytest.approx(-4.0)
    assert "status = 'closed'" in db.sql
    assert "realized_return_frac" in db.sql
    assert "COUNT(realized_return_frac)::int AS trades" in db.sql
    assert "realized_return_frac > 0" in db.sql
    assert "AVG(realized_return_frac * 100.0) AS expectancy_pct" in db.sql
    assert "WHERE realized_return_frac IS NOT NULL" in db.sql
    assert "pnl > 0" not in db.sql
    assert "COUNT(*)" not in db.sql
    assert "pnl IS NOT NULL" in db.sql
    assert "entry_price > 0" in db.sql
    assert "quantity > 0" in db.sql
    assert "asset_kind" in db.sql
    assert "partial_taken_qty" in db.sql
    assert "filled_quantity" in db.sql
    assert db.params == {"p": 42}


def test_collect_pnl_slope_14d_uses_contract_aware_realized_returns() -> None:
    db = _Session([
        ("2026-05-01", 1.0),
        ("2026-05-02", 2.0),
        ("2026-05-03", 3.0),
    ])

    out = _collect_pnl_slope_14d(db, pattern_id=42)

    assert out == pytest.approx(2.5)
    assert "realized_return_pct" in db.sql
    assert "AVG(realized_return_pct) AS realized_return_pct" in db.sql
    assert "WHERE realized_return_pct IS NOT NULL" in db.sql
    assert "status = 'closed'" in db.sql
    assert "entry_price > 0" in db.sql
    assert "quantity > 0" in db.sql
    assert "partial_taken_qty" in db.sql
    assert "filled_quantity" in db.sql
    assert "SUM(pnl)" not in db.sql
    assert db.params == {"p": 42}


def test_collect_regime_and_diversity_uses_contract_aware_return_magnitude() -> None:
    db = _SequenceSession([
        ("risk_on",),
        [("breakout", 3.0), ("mean_reversion", 1.0)],
    ])

    regime, fam_h, fam_n = _collect_regime_and_diversity(db)

    assert regime == "risk_on"
    assert fam_h == pytest.approx((3.0 / 4.0) ** 2 + (1.0 / 4.0) ** 2)
    assert fam_n == 2
    diversity_sql = db.sqls[1]
    assert "realized_return_pct" in diversity_sql
    assert "SUM(ABS(realized_return_pct)) AS realized_return_magnitude_pct" in diversity_sql
    assert "WHERE realized_return_pct IS NOT NULL" in diversity_sql
    assert "t.status = 'closed'" in diversity_sql
    assert "t.entry_price > 0" in diversity_sql
    assert "t.quantity > 0" in diversity_sql
    assert "partial_taken_qty" in diversity_sql
    assert "filled_quantity" in diversity_sql
    assert "SUM(t.pnl)" not in diversity_sql


def test_snapshot_job_covers_promoted_and_pilot_lifecycles(monkeypatch) -> None:
    db = _SequenceSession([
        [(101,), (102,), (103,), (104,)],
    ])
    snapshotted: list[int] = []
    monkeypatch.setattr(settings, "chili_pattern_survival_classifier_enabled", True)
    monkeypatch.setattr(
        features_mod,
        "snapshot_pattern_features",
        lambda _db, *, scan_pattern_id, snapshot_date: snapshotted.append(scan_pattern_id) or scan_pattern_id,
    )

    out = features_mod.run_pattern_survival_snapshot_job(db)

    assert out["patterns_snapshotted"] == 4
    assert out["patterns_failed"] == 0
    assert snapshotted == [101, 102, 103, 104]
    sql = db.sqls[0]
    assert "'live'" in sql
    assert "'challenged'" in sql
    assert "'promoted'" in sql
    assert "'pilot_promoted'" in sql
