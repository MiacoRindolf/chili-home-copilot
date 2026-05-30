from __future__ import annotations

from app.services import massive_client


def test_cache_set_prunes_fresh_overflow_in_batches(monkeypatch) -> None:
    monkeypatch.setattr(massive_client, "_MAX_CACHE", 10)
    monkeypatch.setattr(massive_client.time, "time", lambda: 1_000.0)
    massive_client._cache.clear()
    try:
        for idx in range(12):
            massive_client._cache[f"old-{idx}"] = (900.0 + idx, idx)

        massive_client._cache_set("new-key", "new-value")

        assert len(massive_client._cache) <= 10
        assert "new-key" in massive_client._cache
        assert "old-0" not in massive_client._cache
        assert "old-1" not in massive_client._cache
    finally:
        massive_client._cache.clear()


def test_top_ranked_preserves_stable_ties() -> None:
    rows = [
        {"ticker": "AAA", "score": 1},
        {"ticker": "BBB", "score": 3},
        {"ticker": "CCC", "score": 3},
        {"ticker": "DDD", "score": 2},
    ]

    top = massive_client._top_ranked(rows, 3, lambda row: row["score"])

    assert [row["ticker"] for row in top] == ["BBB", "CCC", "DDD"]


def test_screen_most_volatile_uses_bounded_ranked_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        lambda: [
            {"ticker": "LOW", "day": {"h": 10, "l": 9, "c": 10}},
            {"ticker": "BAD", "day": {"h": 10, "l": 1, "c": 0.5}},
            {"ticker": "AAA", "day": {"h": 12, "l": 6, "c": 6}},
            {"ticker": "BBB", "day": {"h": 15, "l": 5, "c": 5}},
            {"ticker": "CCC", "day": {"h": 20, "l": 10, "c": 10}},
        ],
    )

    assert massive_client.screen_most_volatile(limit=2) == ["BBB", "AAA"]


def test_screen_top_gainers_fallback_uses_bounded_ranked_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(massive_client, "get_top_movers", lambda _direction: [])
    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        lambda: [
            {"ticker": "LOWVOL", "todaysChangePerc": 99, "day": {"v": 9_999}},
            {"ticker": "AAA", "todaysChangePerc": 3.5, "day": {"v": 20_000}},
            {"ticker": "BBB", "todaysChangePerc": 9.0, "day": {"v": 20_000}},
            {"ticker": "CCC", "todaysChangePerc": 9.0, "day": {"v": 20_000}},
            {"ticker": "DDD", "todaysChangePerc": -1.0, "day": {"v": 20_000}},
        ],
    )

    assert massive_client.screen_top_gainers(limit=3) == ["BBB", "CCC", "AAA"]


def test_screen_unusual_volume_uses_bounded_ranked_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        lambda: [
            {"ticker": "LOWPREV", "day": {"v": 1_000_000}, "prevDay": {"v": 49_999}},
            {"ticker": "AAA", "day": {"v": 400_000}, "prevDay": {"v": 100_000}},
            {"ticker": "BBB", "day": {"v": 900_000}, "prevDay": {"v": 100_000}},
            {"ticker": "CCC", "day": {"v": 900_000}, "prevDay": {"v": 100_000}},
            {"ticker": "DDD", "day": {"v": 120_000}, "prevDay": {"v": 100_000}},
        ],
    )

    assert massive_client.screen_unusual_volume(limit=3) == ["BBB", "CCC", "AAA"]


def test_screen_high_volume_uses_bounded_ranked_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        massive_client,
        "get_full_market_snapshot",
        lambda: [
            {"ticker": "LOWPRICE", "day": {"v": 9_000_000, "c": 4.99}},
            {"ticker": "AAA", "day": {"v": 4_000_000, "c": 10}},
            {"ticker": "BBB", "day": {"v": 9_000_000, "c": 10}},
            {"ticker": "CCC", "day": {"v": 9_000_000, "c": 10}},
            {"ticker": "DDD", "day": {"v": 500_000, "c": 10}},
        ],
    )

    assert massive_client.screen_high_volume(limit=3) == ["BBB", "CCC", "AAA"]
