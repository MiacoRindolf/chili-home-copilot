from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.trading import PatternEvidenceHypothesis, PatternTradeRow
from app.services.trading.pattern_evidence_service import walk_forward_validate


class _Query:
    def __init__(self, db, model):
        self._db = db
        self._model = model

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._db.hypothesis

    def all(self):
        if self._model is PatternTradeRow:
            self._db.trade_row_query_count += 1
            return self._db.rows
        return []


class _Db:
    def __init__(self, hypothesis, rows):
        self.hypothesis = hypothesis
        self.rows = rows
        self.trade_row_query_count = 0
        self.commit_count = 0

    def query(self, model):
        return _Query(self, model)

    def commit(self):
        self.commit_count += 1


def test_walk_forward_validate_queries_trade_rows_once() -> None:
    now = datetime.utcnow()
    hyp = SimpleNamespace(
        id=7,
        scan_pattern_id=11,
        predicate_json={
            "type": "median_split",
            "feature_key": "rsi",
            "median": 50.0,
            "favor": "above_median",
        },
        metrics_json={},
        status="proposed",
        updated_at=None,
    )
    rows = []
    for i in range(80):
        rows.append(
            SimpleNamespace(
                as_of_ts=now - timedelta(days=170 - i),
                features_json={"rsi": 60.0 if i % 2 else 40.0},
                outcome_return_pct=2.0 if i % 2 else -1.0,
            )
        )
    for i in range(80):
        rows.append(
            SimpleNamespace(
                as_of_ts=now - timedelta(days=80 - i),
                features_json={"rsi": 60.0 if i % 2 else 40.0},
                outcome_return_pct=3.0 if i % 2 else -2.0,
            )
        )
    db = _Db(hyp, rows)

    out = walk_forward_validate(db, 7, is_days=90, oos_days=90)

    assert db.trade_row_query_count == 1
    assert db.commit_count == 1
    assert out["ok"] is True
    assert out["is_n"] >= 15
    assert out["oos_n"] >= 15
    assert out["validated"] is True
    assert hyp.status == "validated"


def test_walk_forward_validate_nan_median_keeps_empty_buckets() -> None:
    now = datetime.utcnow()
    hyp = SimpleNamespace(
        id=8,
        scan_pattern_id=12,
        predicate_json={
            "type": "median_split",
            "feature_key": "rsi",
            "median": float("nan"),
            "favor": "below_median",
        },
        metrics_json={},
        status="proposed",
        updated_at=None,
    )
    rows = [
        SimpleNamespace(
            as_of_ts=now - timedelta(days=10),
            features_json={"rsi": 40.0},
            outcome_return_pct=2.0,
        )
    ]
    db = _Db(hyp, rows)

    out = walk_forward_validate(db, 8, is_days=90, oos_days=90)

    assert db.trade_row_query_count == 1
    assert out["is_n"] == 0
    assert out["oos_n"] == 0
    assert out["bench_is_mean"] == 0.0
    assert out["bench_oos_mean"] == 0.0
    assert out["validated"] is False
    assert hyp.status == "proposed"
