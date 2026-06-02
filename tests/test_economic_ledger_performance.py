from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import economic_ledger as el


class _Query:
    def __init__(self, db, model_or_cols):
        self.db = db
        self.model_or_cols = model_or_cols

    def filter(self, *_args, **_kwargs):
        return self

    def count(self):
        return 0

    def with_entities(self, *args):
        self.db.with_entities_calls.append(args)
        return _AggregateQuery()

    def group_by(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        if len(self.model_or_cols) > 1:
            return [
                (7, "paper", "AAPL", None, 101, 10.0, 8.0, -2.0),
            ]
        return []


class _AggregateQuery:
    def group_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return []

    def scalar(self):
        return 0.0


class _Db:
    def __init__(self):
        self.query_calls = []
        self.with_entities_calls = []

    def query(self, *args):
        self.query_calls.append(args)
        return _Query(self, args)


def test_ledger_summary_reads_top_disagreement_columns_only(monkeypatch) -> None:
    db = _Db()
    monkeypatch.setattr(el, "_current_mode", lambda: "shadow")
    monkeypatch.setattr(el, "_parity_tolerance_usd", lambda: 0.01)

    summary = el.ledger_summary(db, lookback_hours=1)

    top_query_cols = [getattr(col, "key", None) for col in db.query_calls[-1]]
    assert top_query_cols == [
        "id",
        "source",
        "ticker",
        "trade_id",
        "paper_trade_id",
        "legacy_pnl",
        "ledger_pnl",
        "delta_pnl",
    ]
    assert summary["top_disagreements"] == [
        {
            "id": 7,
            "source": "paper",
            "ticker": "AAPL",
            "trade_id": None,
            "paper_trade_id": 101,
            "legacy_pnl": 10.0,
            "ledger_pnl": 8.0,
            "delta_pnl": -2.0,
        }
    ]


def test_parity_summary_field_supports_object_tuple_and_mapping_rows() -> None:
    obj = SimpleNamespace(id=1, ticker="OBJ")
    mapping = {"id": 2, "ticker": "MAP"}

    assert el._parity_summary_field(obj, "ticker", 2) == "OBJ"
    assert el._parity_summary_field((3, "paper", "TUP"), "ticker", 2) == "TUP"
    assert el._parity_summary_field(mapping, "id", 0) == 2
