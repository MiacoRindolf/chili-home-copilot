from __future__ import annotations

from collections import OrderedDict

from app.services import broker_manager


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("cache pruning should not snapshot all cache keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("cache pruning should walk oldest entries directly")


def test_combined_positions_can_skip_equity_sort(monkeypatch) -> None:
    monkeypatch.setattr(broker_manager.broker_service, "is_connected", lambda: True)
    monkeypatch.setattr(broker_manager.coinbase_service, "is_connected", lambda: False)
    monkeypatch.setattr(
        broker_manager.broker_service,
        "get_positions",
        lambda: [
            {"ticker": "LOW", "equity": 10.0},
            {"ticker": "HIGH", "equity": 20.0},
        ],
    )
    monkeypatch.setattr(
        broker_manager.broker_service,
        "get_crypto_positions",
        lambda: [{"ticker": "MID", "equity": 15.0}],
    )

    sorted_positions = broker_manager.get_combined_positions()
    unsorted_positions = broker_manager.get_combined_positions(sort_by_equity=False)

    assert [p["ticker"] for p in sorted_positions] == ["HIGH", "MID", "LOW"]
    assert [p["ticker"] for p in unsorted_positions] == ["LOW", "HIGH", "MID"]
    assert all(p["broker_source"] == broker_manager.BROKER_ROBINHOOD for p in unsorted_positions)


def test_duplicate_position_check_skips_unneeded_sort(monkeypatch) -> None:
    calls: list[bool] = []

    def fake_positions(*, fresh: bool = False, sort_by_equity: bool = True) -> list[dict]:
        calls.append(sort_by_equity)
        return [
            {"ticker": "AAPL", "quantity": 1, "broker_source": broker_manager.BROKER_ROBINHOOD},
            {"ticker": "AAPL", "quantity": 2, "broker_source": broker_manager.BROKER_COINBASE},
        ]

    monkeypatch.setattr(broker_manager, "get_combined_positions", fake_positions)

    assert broker_manager.check_duplicate_position("aapl") == [
        broker_manager.BROKER_ROBINHOOD,
        broker_manager.BROKER_COINBASE,
    ]
    assert calls == [False]


def test_user_session_cache_get_removes_expired_entry(monkeypatch) -> None:
    session = broker_manager.UserBrokerSession(user_id=1)
    session._cache_ttl = 10
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)
    session._cache["stale"] = (900.0, "old")

    assert session.cache_get("stale") is None
    assert "stale" not in session._cache


def test_user_session_cache_get_refreshes_hit_recency(monkeypatch) -> None:
    session = broker_manager.UserBrokerSession(user_id=1)
    session._cache_ttl = 100
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)
    session._cache["hot"] = (990.0, "a")
    session._cache["cold"] = (991.0, "b")

    assert session.cache_get("hot") == "a"

    assert list(session._cache) == ["cold", "hot"]


def test_user_session_cache_set_prunes_expired_and_caps_oldest(monkeypatch) -> None:
    session = broker_manager.UserBrokerSession(user_id=1)
    session._cache_ttl = 10
    session._cache_max = 3
    session._cache = {
        "expired": (900.0, "old"),
        "oldest": (991.0, "a"),
        "middle": (992.0, "b"),
        "newest": (993.0, "c"),
    }
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)

    session.cache_set("fresh", "d")

    assert list(session._cache) == ["middle", "newest", "fresh"]
    assert session.cache_get("fresh") == "d"


def test_user_session_cache_prunes_expired_without_snapshot(monkeypatch) -> None:
    session = broker_manager.UserBrokerSession(user_id=1)
    session._cache_ttl = 10
    session._cache_max = 5
    session._cache = _NoSnapshotOrderedDict(
        [
            ("expired-1", (900.0, "a")),
            ("expired-2", (901.0, "b")),
            ("fresh", (995.0, "c")),
        ]
    )
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)

    session.cache_set("new", "d")

    assert list(session._cache) == ["fresh", "new"]


def test_user_session_cache_set_refreshes_existing_key_order(monkeypatch) -> None:
    session = broker_manager.UserBrokerSession(user_id=1)
    session._cache_ttl = 100
    session._cache_max = 2
    session._cache = {
        "keep": (990.0, "a"),
        "refresh": (991.0, "old"),
    }
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)

    session.cache_set("refresh", "new")
    session.cache_set("third", "c")

    assert list(session._cache) == ["refresh", "third"]
    assert session.cache_get("refresh") == "new"


def test_get_user_session_refreshes_recency_and_caps_oldest(monkeypatch) -> None:
    monkeypatch.setattr(broker_manager, "_USER_SESSIONS_MAX", 2)
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)
    broker_manager._user_sessions.clear()
    try:
        broker_manager._user_sessions[1] = broker_manager.UserBrokerSession(1, now=990.0)
        broker_manager._user_sessions[2] = broker_manager.UserBrokerSession(2, now=991.0)

        assert broker_manager.get_user_session(1).user_id == 1
        assert list(broker_manager._user_sessions) == [2, 1]

        assert broker_manager.get_user_session(3).user_id == 3

        assert list(broker_manager._user_sessions) == [1, 3]
    finally:
        broker_manager._user_sessions.clear()


def test_get_user_session_prunes_expired_oldest_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(broker_manager, "_SESSION_TTL", 10)
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)
    sessions = _NoSnapshotOrderedDict(
        [
            (1, broker_manager.UserBrokerSession(1, now=900.0)),
            (2, broker_manager.UserBrokerSession(2, now=995.0)),
        ]
    )
    monkeypatch.setattr(broker_manager, "_user_sessions", sessions)

    assert broker_manager.get_user_session(3).user_id == 3

    assert list(sessions) == [2, 3]


def test_cleanup_expired_sessions_removes_all_expired_entries(monkeypatch) -> None:
    monkeypatch.setattr(broker_manager, "_SESSION_TTL", 10)
    monkeypatch.setattr(broker_manager.time, "time", lambda: 1_000.0)
    broker_manager._user_sessions.clear()
    try:
        broker_manager._user_sessions[1] = broker_manager.UserBrokerSession(1, now=995.0)
        broker_manager._user_sessions[2] = broker_manager.UserBrokerSession(2, now=900.0)
        broker_manager._user_sessions[3] = broker_manager.UserBrokerSession(3, now=996.0)
        broker_manager._user_sessions[4] = broker_manager.UserBrokerSession(4, now=901.0)

        assert broker_manager.cleanup_expired_sessions() == 2

        assert list(broker_manager._user_sessions) == [1, 3]
    finally:
        broker_manager._user_sessions.clear()
