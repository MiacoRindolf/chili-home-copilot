from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.trading import MarketSnapshot, TripleBarrierLabelRow
from app.services.trading.triple_barrier import TripleBarrierConfig, TripleBarrierLabel
from app.services.trading.triple_barrier_labeler import (
    LabelWriteOutcome,
    label_snapshots,
)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filter_expr = None
        self.order_expr = None
        self.limit_value = None

    def filter(self, expr):
        self.filter_expr = expr
        return self

    def order_by(self, expr):
        self.order_expr = expr
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self.query_obj = _FakeQuery(rows)

    def query(self, *_args, **_kwargs):
        return self.query_obj


def _cfg() -> TripleBarrierConfig:
    return TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=3, side="long")


def _label() -> TripleBarrierLabel:
    return TripleBarrierLabel(
        label=1,
        exit_bar_idx=0,
        realized_return_pct=0.02,
        barrier_hit="tp",
        entry_close=100.0,
        tp_price=102.0,
        sl_price=99.0,
    )


def test_label_snapshots_uses_bar_start_at_as_canonical_anchor(monkeypatch):
    import app.services.trading.triple_barrier_labeler as labeler

    bar_start = datetime.utcnow() - timedelta(days=15)
    ingestion_time = bar_start + timedelta(days=1, hours=6)
    snap = SimpleNamespace(
        id=77,
        ticker="ANCHOR",
        snapshot_date=ingestion_time,
        bar_start_at=bar_start,
        close_price=100.0,
    )
    db = _FakeDb([snap])
    fetch_calls: list[tuple[str, object, int]] = []
    label_calls: list[dict] = []

    def _fake_fetch_forward_bars(ticker, from_date, max_bars):
        fetch_calls.append((ticker, from_date, max_bars))
        return [{"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.5}]

    def _fake_label_single(*_args, **kwargs):
        label_calls.append(kwargs)
        return LabelWriteOutcome(
            inserted=True,
            label=_label(),
            ticker=kwargs["ticker"],
            label_date=kwargs["label_date"],
            side=kwargs["side"],
            cfg=kwargs["cfg"],
            snapshot_id=kwargs["snapshot_id"],
        )

    monkeypatch.setattr(labeler, "_fetch_forward_bars", _fake_fetch_forward_bars)
    monkeypatch.setattr(labeler, "label_single", _fake_label_single)

    report = label_snapshots(
        db,
        cfg=_cfg(),
        mode_override="shadow",
        min_lookback_days=10,
    )

    assert report.requested == 1
    assert report.written == 1
    assert fetch_calls == [("ANCHOR", bar_start.date(), 3)]
    assert label_calls[0]["label_date"] == bar_start.date()
    assert label_calls[0]["snapshot_id"] == 77

    filter_sql = str(db.query_obj.filter_expr).lower()
    order_sql = str(db.query_obj.order_expr).lower()
    assert "coalesce" in filter_sql
    assert "bar_start_at" in filter_sql
    assert "snapshot_date" in filter_sql
    assert "coalesce" in order_sql
    assert db.query_obj.limit_value == 200


def test_label_snapshots_falls_back_to_snapshot_date_for_legacy_rows(monkeypatch):
    import app.services.trading.triple_barrier_labeler as labeler

    snapshot_date = datetime.utcnow() - timedelta(days=15)
    snap = SimpleNamespace(
        id=78,
        ticker="LEGACY",
        snapshot_date=snapshot_date,
        bar_start_at=None,
        close_price=100.0,
    )
    fetch_dates = []

    monkeypatch.setattr(
        labeler,
        "_fetch_forward_bars",
        lambda ticker, from_date, max_bars: fetch_dates.append(from_date)
        or [{"open": 100.0, "high": 103.0, "low": 99.5, "close": 102.5}],
    )
    monkeypatch.setattr(
        labeler,
        "label_single",
        lambda *_args, **kwargs: LabelWriteOutcome(
            inserted=True,
            label=_label(),
            ticker=kwargs["ticker"],
            label_date=kwargs["label_date"],
            side=kwargs["side"],
            cfg=kwargs["cfg"],
            snapshot_id=kwargs["snapshot_id"],
        ),
    )

    report = label_snapshots(
        _FakeDb([snap]),
        cfg=_cfg(),
        mode_override="shadow",
        min_lookback_days=10,
    )

    assert report.written == 1
    assert fetch_dates == [snapshot_date.date()]


def test_label_snapshots_db_fixture_uses_bar_start_at_for_cutoff_label_and_fetch(
    db,
    monkeypatch,
):
    import app.services.trading.triple_barrier_labeler as labeler

    now = datetime.utcnow()
    eligible_bar_start = now - timedelta(days=15)
    recent_ingestion = now - timedelta(days=1)
    decoy_old_ingestion = now - timedelta(days=15)
    decoy_recent_bar_start = now - timedelta(days=1)

    eligible = MarketSnapshot(
        ticker="ANCHDB",
        snapshot_date=recent_ingestion,
        bar_start_at=eligible_bar_start,
        bar_interval="1d",
        snapshot_legacy=False,
        close_price=100.0,
    )
    decoy = MarketSnapshot(
        ticker="DECOYDB",
        snapshot_date=decoy_old_ingestion,
        bar_start_at=decoy_recent_bar_start,
        bar_interval="1d",
        snapshot_legacy=False,
        close_price=100.0,
    )
    db.add_all([eligible, decoy])
    db.commit()

    fetch_calls: list[tuple[str, object, int]] = []

    def _fake_fetch_forward_bars(ticker, from_date, max_bars):
        fetch_calls.append((ticker, from_date, max_bars))
        return [{"open": 100.0, "high": 103.0, "low": 100.0, "close": 102.5}]

    monkeypatch.setattr(labeler, "_fetch_forward_bars", _fake_fetch_forward_bars)

    report = label_snapshots(
        db,
        cfg=_cfg(),
        mode_override="shadow",
        min_lookback_days=10,
    )

    assert report.requested == 1
    assert report.written == 1
    assert fetch_calls == [("ANCHDB", eligible_bar_start.date(), 3)]

    row = db.query(TripleBarrierLabelRow).filter_by(ticker="ANCHDB").one()
    assert row.snapshot_id == eligible.id
    assert row.label_date == eligible_bar_start.date()
    assert row.label == 1
    assert row.barrier_hit == "tp"
    assert row.mode == "shadow"
    assert db.query(TripleBarrierLabelRow).filter_by(ticker="DECOYDB").count() == 0
