from __future__ import annotations

import math
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.trading import setup_vitals


def _polyfit_equivalent_normalized_slope(values: list[float | None]) -> float:
    ys = [float(v) for v in values if v is not None and not math.isnan(v)]
    n = len(ys)
    if n < 2:
        return 0.0
    mean_y = sum(ys) / n
    variance = sum((y - mean_y) ** 2 for y in ys) / n
    if variance < 1e-24:
        return 0.0
    mean_x = (n - 1) / 2.0
    denom = sum((i - mean_x) ** 2 for i in range(n))
    slope = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys)) / denom
    return float(max(-1.0, min(1.0, slope / max(math.sqrt(variance), 1e-6))))


def test_normalized_slope_matches_closed_form_reference() -> None:
    samples = [
        [40.0, 42.0, 44.0, 46.0, 48.0],
        [70.0, 68.0, 65.0, 64.0, 61.0],
        [None, 1.0, 2.0, None, 3.0],
        [1.0, 1.0, 1.0, 1.0],
    ]

    for values in samples:
        assert setup_vitals._normalized_slope(values) == _polyfit_equivalent_normalized_slope(values)


def test_normalized_slope_avoids_numpy_polyfit(monkeypatch) -> None:
    def fail_polyfit(*_args, **_kwargs):
        raise AssertionError("_normalized_slope should use closed-form least squares")

    monkeypatch.setattr(setup_vitals.np, "polyfit", fail_polyfit)

    assert setup_vitals._normalized_slope([10.0, 11.0, 12.0, 13.0]) > 0


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


class _SnapshotDb:
    def __init__(self, rows):
        self._rows = rows
        self.queried = None

    def query(self, *_args, **_kwargs):
        self.queried = _args
        return _SnapshotRows(self._rows)


def test_load_snapshot_flats_reads_indicator_and_close_columns_only(monkeypatch) -> None:
    db = _SnapshotDb(
        [
            ({"marker": "newest"}, 103.0),
            ({"marker": "middle"}, 102.0),
            ({"marker": "oldest"}, 101.0),
        ]
    )

    monkeypatch.setattr(
        setup_vitals,
        "_flat_from_snap",
        lambda indicator_data, close_price: {
            "marker": indicator_data["marker"],
            "price": close_price,
        },
    )

    flats = setup_vitals.load_snapshot_flats_chronological(db, "ABC", "1d", limit=3)

    assert [getattr(col, "key", None) for col in db.queried] == [
        "indicator_data",
        "close_price",
    ]
    assert flats == [
        {"marker": "oldest", "price": 101.0},
        {"marker": "middle", "price": 102.0},
        {"marker": "newest", "price": 103.0},
    ]


def test_snapshot_flat_values_supports_object_tuple_and_mapping_rows() -> None:
    obj = SimpleNamespace(indicator_data={"rsi": 55}, close_price=10.0)
    mapping = {"indicator_data": {"rsi": 56}, "close_price": 11.0}

    assert setup_vitals._snapshot_flat_values(obj) == ({"rsi": 55}, 10.0)
    assert setup_vitals._snapshot_flat_values(({"rsi": 57}, 12.0)) == ({"rsi": 57}, 12.0)
    assert setup_vitals._snapshot_flat_values(mapping) == ({"rsi": 56}, 11.0)


class _TickerVitalsQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _TickerVitalsDb:
    def __init__(self, row):
        self._row = row
        self.queried = None
        self.flushed = False

    def query(self, *_args, **_kwargs):
        self.queried = _args
        return _TickerVitalsQuery(self._row)

    def flush(self):
        self.flushed = True


def test_get_or_compute_ticker_vitals_reads_cache_columns_only(monkeypatch) -> None:
    row = (
        0.11,
        0.22,
        0.33,
        0.44,
        0.55,
        {"rsi_14": {"direction": "rising"}},
        [{"type": "bullish"}],
        datetime.utcnow() - timedelta(seconds=5),
    )
    db = _TickerVitalsDb(row)

    monkeypatch.setattr(
        setup_vitals,
        "compute_setup_vitals",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fresh cache should not recompute")),
    )
    monkeypatch.setattr(
        setup_vitals,
        "upsert_ticker_vitals_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fresh cache should not upsert")),
    )

    vitals = setup_vitals.get_or_compute_ticker_vitals(db, "ABC", "1d", max_age_seconds=120)

    assert [getattr(col, "key", None) for col in db.queried] == [
        "momentum_score",
        "volume_score",
        "trend_score",
        "overextension_risk",
        "composite_health",
        "trajectory_json",
        "divergences_json",
        "computed_at",
    ]
    assert vitals.source == "cache"
    assert vitals.momentum_score == 0.11
    assert vitals.trajectory_details["rsi_14"]["direction"] == "rising"
    assert vitals.divergences == [{"type": "bullish"}]
    assert db.flushed is False


def test_ticker_vitals_row_to_setup_supports_object_tuple_and_mapping_rows() -> None:
    obj = SimpleNamespace(
        momentum_score=0.1,
        volume_score=0.2,
        trend_score=0.3,
        overextension_risk=0.4,
        composite_health=0.5,
        trajectory_json={"macd_hist": {"direction": "flat"}},
        divergences_json=[{"type": "bearish"}],
    )
    mapping = {
        "momentum_score": 0.6,
        "volume_score": 0.7,
        "trend_score": 0.8,
        "overextension_risk": 0.9,
        "composite_health": 1.0,
        "trajectory_json": {"obv": {"direction": "falling"}},
        "divergences_json": [],
    }

    obj_vitals = setup_vitals._ticker_vitals_row_to_setup(obj)
    tuple_vitals = setup_vitals._ticker_vitals_row_to_setup(
        (0.15, 0.25, 0.35, 0.45, 0.55, {"rsi_14": {"direction": "rising"}}, [])
    )
    mapping_vitals = setup_vitals._ticker_vitals_row_to_setup(mapping)

    assert obj_vitals.divergences == [{"type": "bearish"}]
    assert tuple_vitals.trend_score == 0.35
    assert tuple_vitals.trajectory_details["rsi_14"]["direction"] == "rising"
    assert mapping_vitals.composite_health == 1.0


class _HistoryQuery:
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


class _HistoryDb:
    def __init__(self, rows):
        self._rows = rows
        self.queried = None

    def query(self, *_args, **_kwargs):
        self.queried = _args
        return _HistoryQuery(self._rows)


def test_load_recent_vitals_history_reads_score_columns_only() -> None:
    db = _HistoryDb([(0.4, 0.2), (0.5, 0.3)])

    rows = setup_vitals.load_recent_vitals_history_for_trade(db, trade_id=123, limit=2)

    assert [getattr(col, "key", None) for col in db.queried] == [
        "momentum_score",
        "volume_score",
    ]
    assert rows == [(0.4, 0.2), (0.5, 0.3)]


def test_degradation_helpers_consume_compact_history_rows() -> None:
    rows_newest_first = [
        (0.40, 0.25),
        (0.50, 0.20),
        (0.60, 0.15),
        (0.70, 0.10),
    ]

    assert setup_vitals.momentum_drop_urgent(_HistoryDb(rows_newest_first[:1]), 123, 0.0) is False
    assert setup_vitals.momentum_drop_urgent(_HistoryDb(rows_newest_first[:1]), 123, -0.1) is True

    flags = setup_vitals.detect_multi_check_degradation(
        _HistoryDb(rows_newest_first),
        trade_id=123,
        current_momentum=0.35,
        current_volume=-0.1,
    )

    assert flags["consecutive_momentum_down"] == 3
    assert flags["degraded_3plus"] is True
    assert flags["volume_flip_negative"] is True


def test_vitals_history_field_supports_object_tuple_and_mapping_rows() -> None:
    obj = SimpleNamespace(momentum_score=0.3, volume_score=0.4)
    mapping = {"momentum_score": 0.5, "volume_score": 0.6}

    assert setup_vitals._vitals_history_field(obj, "momentum_score", 0) == 0.3
    assert setup_vitals._vitals_history_field((0.7, 0.8), "volume_score", 1) == 0.8
    assert setup_vitals._vitals_history_field(mapping, "momentum_score", 0) == 0.5
