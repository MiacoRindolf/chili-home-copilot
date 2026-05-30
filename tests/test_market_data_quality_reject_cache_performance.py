from __future__ import annotations

from collections import OrderedDict

from app.services.trading import market_data


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("quality-reject log pruning should not snapshot keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("quality-reject log pruning should not snapshot items")


def _integrity(issue: str = "gap") -> dict:
    return {"issues": [issue]}


def test_quality_reject_log_cache_suppresses_duplicate_within_ttl(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(market_data._time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(market_data.logger, "warning", lambda *args, **kwargs: calls.append(args))
    market_data._ohlcv_quality_reject_log_cache.clear()
    try:
        for _ in range(2):
            market_data._log_ohlcv_integrity_failure(
                ticker="AAPL",
                interval="1d",
                provider="massive",
                integrity=_integrity(),
            )

        assert len(calls) == 1
        assert len(market_data._ohlcv_quality_reject_log_cache) == 1
    finally:
        market_data._ohlcv_quality_reject_log_cache.clear()


def test_quality_reject_log_cache_caps_oldest_entries(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(market_data, "_OHLCV_QUALITY_REJECT_LOG_MAX", 3)
    monkeypatch.setattr(market_data._time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(market_data.logger, "warning", lambda *args, **kwargs: calls.append(args))
    market_data._ohlcv_quality_reject_log_cache.clear()
    try:
        for symbol in ["A", "B", "C", "D"]:
            market_data._log_ohlcv_integrity_failure(
                ticker=symbol,
                interval="1d",
                provider="massive",
                integrity=_integrity(),
            )

        assert len(calls) == 4
        assert list(market_data._ohlcv_quality_reject_log_cache) == [
            "B|1d|massive|gap",
            "C|1d|massive|gap",
            "D|1d|massive|gap",
        ]
    finally:
        market_data._ohlcv_quality_reject_log_cache.clear()


def test_quality_reject_log_cache_refreshes_stale_existing_key(monkeypatch) -> None:
    calls: list[tuple] = []
    now = {"value": 1_000.0}
    monkeypatch.setattr(market_data, "_OHLCV_QUALITY_REJECT_LOG_MAX", 2)
    monkeypatch.setattr(market_data, "_OHLCV_QUALITY_REJECT_LOG_TTL", 10.0)
    monkeypatch.setattr(market_data._time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(market_data.logger, "warning", lambda *args, **kwargs: calls.append(args))
    market_data._ohlcv_quality_reject_log_cache.clear()
    try:
        market_data._log_ohlcv_integrity_failure(
            ticker="A",
            interval="1d",
            provider="massive",
            integrity=_integrity(),
        )
        market_data._log_ohlcv_integrity_failure(
            ticker="B",
            interval="1d",
            provider="massive",
            integrity=_integrity(),
        )
        now["value"] += 11.0
        market_data._log_ohlcv_integrity_failure(
            ticker="A",
            interval="1d",
            provider="massive",
            integrity=_integrity(),
        )
        market_data._log_ohlcv_integrity_failure(
            ticker="C",
            interval="1d",
            provider="massive",
            integrity=_integrity(),
        )

        assert len(calls) == 4
        assert list(market_data._ohlcv_quality_reject_log_cache) == [
            "A|1d|massive|gap",
            "C|1d|massive|gap",
        ]
    finally:
        market_data._ohlcv_quality_reject_log_cache.clear()


def test_quality_reject_log_cache_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_OHLCV_QUALITY_REJECT_LOG_TTL", 10.0)
    monkeypatch.setattr(market_data, "_OHLCV_QUALITY_REJECT_LOG_MAX", 5)
    cache = _NoSnapshotOrderedDict(
        [
            ("OLD1|1d|massive|gap", 900.0),
            ("OLD2|1d|massive|gap", 901.0),
            ("FRESH|1d|massive|gap", 995.0),
        ]
    )
    monkeypatch.setattr(market_data, "_ohlcv_quality_reject_log_cache", cache)

    market_data._prune_ohlcv_quality_reject_log_cache(1_000.0)

    assert list(market_data._ohlcv_quality_reject_log_cache) == ["FRESH|1d|massive|gap"]
