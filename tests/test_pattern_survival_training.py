from __future__ import annotations

import math
from datetime import date

from app.services.trading.pattern_survival import training as survival_training
from app.services.trading.pattern_survival.training import backfill_survival_labels


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.select_sql = ""
        self.select_params = {}
        self.updates = []
        self.commits = 0

    def execute(self, stmt, params=None):
        sql = str(stmt)
        bound = dict(params or {})
        if "SELECT pp.id" in sql:
            self.select_sql = sql
            self.select_params = bound
            return _Result(self.rows)
        if "UPDATE pattern_survival_predictions" in sql:
            self.updates.append(bound)
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        self.commits += 1


def test_backfill_survival_labels_uses_horizon_trade_activity() -> None:
    db = _Session(
        [
            (1, 101, date(2026, 4, 1), "live", False, 1),
            (2, 102, date(2026, 4, 1), "live", False, 0),
            (3, 103, date(2026, 4, 1), "live", True, 0),
            (4, 104, date(2026, 4, 1), "demoted", True, 4),
        ]
    )

    out = backfill_survival_labels(db, horizon_days=30)

    assert out == {
        "rows_resolved": 4,
        "rows_survived": 2,
        "rows_dnf": 2,
        "horizon_days": 30,
    }
    assert db.select_params == {"h": 30}
    assert "FROM trading_management_envelopes t" in db.select_sql
    assert "FROM trading_trades t" not in db.select_sql
    assert "horizon_trade_count" in db.select_sql
    assert "sp.trade_count" not in db.select_sql
    assert "t.entry_date >= pp.snapshot_date::timestamp" in db.select_sql
    assert "make_interval(days => :h)" in db.select_sql
    assert "cancelled" in db.select_sql
    assert "rejected" in db.select_sql
    assert db.updates == [
        {"s": True, "r": None, "i": 1},
        {
            "s": False,
            "r": "lifecycle=live active=False horizon_trades=0",
            "i": 2,
        },
        {"s": True, "r": None, "i": 3},
        {
            "s": False,
            "r": "lifecycle=demoted active=True horizon_trades=4",
            "i": 4,
        },
    ]
    assert db.commits == 1


class _ScoreSession:
    def __init__(self, rows):
        self.rows = rows
        self.select_sql = ""
        self.inserts = []
        self.commits = 0

    def execute(self, stmt, params=None):
        sql = str(stmt)
        bound = dict(params or {})
        if "SELECT f.id" in sql:
            self.select_sql = sql
            return _Result(self.rows)
        if "INSERT INTO pattern_survival_predictions" in sql:
            self.inserts.append(bound)
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self):
        self.commits += 1


class _Model:
    def __init__(self):
        self.seen = None

    def predict_proba(self, x):
        self.seen = x.copy()
        import numpy as np

        return np.array([[0.25, 0.75]])


def test_score_pending_features_sanitizes_nonfinite_feature_cells(monkeypatch) -> None:
    features = [1.0] * len(survival_training._FEATURE_COLUMNS)
    features[1] = float("inf")
    features[2] = "-inf"
    features[3] = "not-a-number"
    features[4] = None
    db = _ScoreSession([(10, 101, date(2026, 5, 1), *features)])
    model = _Model()
    monkeypatch.setattr(
        survival_training,
        "_load_latest_model",
        lambda: (model, "survival-v1"),
    )

    out = survival_training.score_pending_features(db, decision_threshold=0.8)

    assert out == {"ok": True, "scored": 1, "model_version": "survival-v1"}
    assert model.seen[0, 0] == 1.0
    assert math.isnan(model.seen[0, 1])
    assert math.isnan(model.seen[0, 2])
    assert math.isnan(model.seen[0, 3])
    assert math.isnan(model.seen[0, 4])
    assert db.inserts == [
        {
            "fi": 10,
            "pi": 101,
            "sd": date(2026, 5, 1),
            "mn": "survival_clf",
            "mv": "survival-v1",
            "sp": 0.75,
            "dt": 0.8,
            "pl": False,
            "hd": 30,
        }
    ]
    assert db.commits == 1
