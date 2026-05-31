from __future__ import annotations

from collections import OrderedDict

from app.services.trading.venue import rate_limiter


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("bucket pruning should not snapshot all bucket keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("bucket pruning should not snapshot all bucket items")


def test_bucket_registry_refreshes_hit_recency(monkeypatch) -> None:
    monkeypatch.setattr(rate_limiter, "_BUCKETS_MAX", 2)
    monkeypatch.setattr(rate_limiter, "_buckets", OrderedDict())
    monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: 1_000.0)

    rate_limiter.peek("custom-a")
    rate_limiter.peek("custom-b")
    rate_limiter.peek("custom-a")
    rate_limiter.peek("custom-c")

    assert list(rate_limiter._buckets) == ["custom-a", "custom-c"]


def test_bucket_registry_caps_unknown_venue_churn_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(rate_limiter, "_BUCKETS_MAX", 3)
    monkeypatch.setattr(rate_limiter, "_buckets", _NoSnapshotOrderedDict())
    monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: 1_000.0)

    for idx in range(8):
        allowed, retry_after = rate_limiter.try_acquire(f"custom-{idx}")
        assert allowed is True
        assert retry_after == 0.0

    assert list(rate_limiter._buckets) == ["custom-5", "custom-6", "custom-7"]
