from __future__ import annotations

from collections import OrderedDict, deque

from app.services.trading import market_data


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("rejection-window pruning should not snapshot keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("rejection-window pruning should not snapshot items")


def test_rejection_windows_cap_oldest_sources(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_REJECTIONS_MAX", 3)
    monkeypatch.setattr(market_data._time, "time", lambda: 1_000.0)
    market_data._REJECTIONS.clear()
    try:
        for source in ["A", "B", "C", "D"]:
            market_data._record_implausible_rejection("FOO-USD", source, 0.01, 10.0)

        assert list(market_data._REJECTIONS) == [
            ("FOO-USD", "B"),
            ("FOO-USD", "C"),
            ("FOO-USD", "D"),
        ]
    finally:
        market_data._REJECTIONS.clear()


def test_rejection_window_existing_key_moves_to_newest(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_REJECTIONS_MAX", 2)
    now = {"value": 1_000.0}
    monkeypatch.setattr(market_data._time, "time", lambda: now["value"])
    market_data._REJECTIONS.clear()
    try:
        market_data._record_implausible_rejection("FOO-USD", "A", 0.01, 10.0)
        market_data._record_implausible_rejection("FOO-USD", "B", 0.01, 10.0)
        now["value"] += 1.0
        market_data._record_implausible_rejection("FOO-USD", "A", 0.01, 10.0)
        market_data._record_implausible_rejection("FOO-USD", "C", 0.01, 10.0)

        assert list(market_data._REJECTIONS) == [("FOO-USD", "A"), ("FOO-USD", "C")]
        assert len(market_data._REJECTIONS[("FOO-USD", "A")]) == 2
    finally:
        market_data._REJECTIONS.clear()


def test_rejection_window_prunes_empty_oldest_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_REJECTIONS_MAX", 5)
    cache = _NoSnapshotOrderedDict(
        [
            (("OLD-USD", "A"), deque([100.0], maxlen=64)),
            (("FRESH-USD", "B"), deque([995.0], maxlen=64)),
        ]
    )
    monkeypatch.setattr(market_data, "_REJECTIONS", cache)

    market_data._prune_rejection_windows(1_000.0)

    assert list(market_data._REJECTIONS) == [("FRESH-USD", "B")]


def test_rejection_window_threshold_behavior_survives_bounding(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_REJECTIONS_MAX", 10)
    monkeypatch.setattr(market_data._time, "time", lambda: 1_000.0)
    market_data._REJECTIONS.clear()
    try:
        for _ in range(market_data._REJECTION_THRESHOLD):
            market_data._record_implausible_rejection("FOO-USD", "A", 0.01, 10.0)

        assert len(market_data._REJECTIONS[("FOO-USD", "A")]) == market_data._REJECTION_THRESHOLD
    finally:
        market_data._REJECTIONS.clear()
