from __future__ import annotations

import math
from datetime import date

import pytest

from app.services.trading.pattern_survival import training as training_mod
from app.services.trading.pattern_survival.training import (
    _feature_float_or_nan,
    backfill_survival_labels,
    score_pending_features,
)


class _Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchall(self):
        return list(self.rows)


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sqls = []
        self.params = []
        self.commits = 0

    def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        self.params.append(dict(params or {}))
        if len(self.sqls) == 1:
            return _Result(self.rows)
        return _Result()

    def commit(self):
        self.commits += 1


def test_backfill_survival_labels_treats_promoted_lifecycles_as_survived() -> None:
    db = _Session([
        (1, 101, date(2026, 4, 1), "promoted", True, 0),
        (2, 102, date(2026, 4, 1), "pilot_promoted", False, 3),
        (3, 103, date(2026, 4, 1), "shadow_promoted", True, 9),
        (4, 104, date(2026, 4, 1), "live", False, 0),
    ])

    out = backfill_survival_labels(db, horizon_days=30)

    assert out["rows_resolved"] == 4
    assert out["rows_survived"] == 2
    assert out["rows_dnf"] == 2
    updates = db.params[1:]
    assert updates[0]["s"] is True
    assert updates[0]["r"] is None
    assert updates[1]["s"] is True
    assert updates[1]["r"] is None
    assert updates[2]["s"] is False
    assert "lifecycle=shadow_promoted" in updates[2]["r"]
    assert updates[3]["s"] is False
    assert "lifecycle=live" in updates[3]["r"]
    assert "horizon_trades=0" in updates[3]["r"]
    query_sql = db.sqls[0]
    assert "horizon_trade_count" in query_sql
    assert "FROM trading_trades t" in query_sql
    assert "t.status = 'closed'" in query_sql
    assert "t.exit_date >= pp.snapshot_date::timestamp" in query_sql
    assert "pp.snapshot_date::timestamp + make_interval(days => :h)" in query_sql
    assert "sp.trade_count" not in query_sql
    assert db.commits == 1


def test_score_pending_features_enforces_feature_age_floor(monkeypatch) -> None:
    db = _Session([])
    monkeypatch.setattr(
        training_mod,
        "_load_latest_model",
        lambda: (object(), "model-v1"),
    )

    out = score_pending_features(db)

    assert out == {"ok": True, "scored": 0, "model_version": "model-v1"}
    query_sql = db.sqls[0]
    assert "min_feature_age_days" in query_sql
    assert "f.snapshot_date <= (" in query_sql
    assert "NOW() - make_interval(days => :min_feature_age_days)" in query_sql
    assert db.params[0] == {
        "v": "model-v1",
        "min_feature_age_days": 1,
    }


def test_feature_float_or_nan_rejects_boolean_and_nonfinite_inputs() -> None:
    assert math.isnan(_feature_float_or_nan(True))
    assert math.isnan(_feature_float_or_nan(False))
    assert math.isnan(_feature_float_or_nan(float("inf")))
    assert math.isnan(_feature_float_or_nan(float("-inf")))
    assert math.isnan(_feature_float_or_nan(float("nan")))
    assert math.isnan(_feature_float_or_nan("bad"))
    assert _feature_float_or_nan("1.25") == pytest.approx(1.25)
