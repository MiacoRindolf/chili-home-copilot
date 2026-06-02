from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from app.services.trading import triple_barrier_labeler as tbl
from app.services.trading.triple_barrier import TripleBarrierConfig, TripleBarrierLabel


class _SnapshotRows:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    def __init__(self, rows):
        self._rows = rows
        self.queried = None

    def query(self, *_args, **_kwargs):
        self.queried = _args
        return _SnapshotRows(self._rows)


def _label_outcome(**kwargs):
    cfg = kwargs["cfg"]
    return tbl.LabelWriteOutcome(
        inserted=True,
        label=TripleBarrierLabel(
            label=1,
            exit_bar_idx=1,
            realized_return_pct=0.02,
            barrier_hit="tp",
            entry_close=float(kwargs["entry_close"]),
            tp_price=float(kwargs["entry_close"]) * (1 + cfg.tp_pct),
            sl_price=float(kwargs["entry_close"]) * (1 - cfg.sl_pct),
        ),
        ticker=kwargs["ticker"],
        label_date=kwargs["label_date"],
        side=kwargs["side"],
        cfg=cfg,
        snapshot_id=kwargs["snapshot_id"],
    )


def test_label_snapshots_reads_snapshot_label_columns_only(monkeypatch) -> None:
    db = _Db([(101, "ABC", datetime(2026, 5, 1, 14, 30), 123.45)])
    captured = {}

    monkeypatch.setattr(tbl, "_ops_log_enabled", lambda: False)
    monkeypatch.setattr(tbl, "_fetch_forward_bars", lambda **kwargs: [{"close": 125.0}])

    def label_single(_db, **kwargs):
        captured.update(kwargs)
        return _label_outcome(**kwargs)

    monkeypatch.setattr(tbl, "label_single", label_single)

    report = tbl.label_snapshots(
        db,
        limit=1,
        cfg=TripleBarrierConfig(tp_pct=0.01, sl_pct=0.01, max_bars=3),
        mode_override="shadow",
    )

    assert [getattr(col, "key", None) for col in db.queried] == [
        "id",
        "ticker",
        "snapshot_date",
        "close_price",
    ]
    assert report.requested == 1
    assert report.written == 1
    assert report.labels_tp == 1
    assert captured["ticker"] == "ABC"
    assert captured["label_date"] == date(2026, 5, 1)
    assert captured["entry_close"] == 123.45
    assert captured["snapshot_id"] == 101


def test_snapshot_label_values_supports_object_tuple_and_mapping_rows() -> None:
    obj = SimpleNamespace(
        id=1,
        ticker="OBJ",
        snapshot_date=datetime(2026, 1, 1),
        close_price=10.0,
    )
    mapping = {
        "id": 2,
        "ticker": "MAP",
        "snapshot_date": datetime(2026, 1, 2),
        "close_price": 20.0,
    }

    assert tbl._snapshot_label_values(obj) == (1, "OBJ", datetime(2026, 1, 1), 10.0)
    assert tbl._snapshot_label_values((3, "TUP", datetime(2026, 1, 3), 30.0)) == (
        3,
        "TUP",
        datetime(2026, 1, 3),
        30.0,
    )
    assert tbl._snapshot_label_values(mapping) == (2, "MAP", datetime(2026, 1, 2), 20.0)
