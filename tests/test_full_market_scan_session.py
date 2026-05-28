from __future__ import annotations

from types import SimpleNamespace


def test_full_market_scan_releases_db_before_network_scoring(monkeypatch):
    from app.services.trading import prescreen_job, scanner

    events: list[object] = []

    class _FakeQuery:
        def filter(self, *args, **kwargs):
            return self

        def delete(self, *args, **kwargs):
            events.append("delete_old_results")
            return 0

    class _FakeDb:
        def __init__(self) -> None:
            self.rollbacks = 0

        def query(self, *args, **kwargs):
            events.append("query_results")
            return _FakeQuery()

        def rollback(self) -> None:
            self.rollbacks += 1
            events.append("rollback")

        def commit(self) -> None:
            events.append("commit")

        def add(self, obj) -> None:
            events.append(("add", obj))

    class _NoopThread:
        def __init__(self, *args, **kwargs) -> None:
            events.append("thread_created")

        def start(self) -> None:
            events.append("thread_started")

    monkeypatch.setattr(
        prescreen_job,
        "load_active_global_candidate_tickers",
        lambda db: ["AAA"],
    )
    monkeypatch.setattr(
        scanner,
        "get_watchlist",
        lambda db, user_id: [SimpleNamespace(ticker="BBB")],
    )
    monkeypatch.setattr(
        scanner,
        "_prewarm_cache",
        lambda tickers: events.append(("prewarm", tuple(tickers))),
    )

    def _fake_batch_score_tickers(tickers, **kwargs):
        events.append(("score", tuple(tickers)))
        return []

    monkeypatch.setattr(scanner, "batch_score_tickers", _fake_batch_score_tickers)
    monkeypatch.setattr(scanner.threading, "Thread", _NoopThread)

    db = _FakeDb()
    results = scanner.run_full_market_scan(db, user_id=1, use_full_universe=True)

    rollback_idx = events.index("rollback")
    prewarm_idx = next(
        i for i, e in enumerate(events) if isinstance(e, tuple) and e[0] == "prewarm"
    )
    score_idx = next(
        i for i, e in enumerate(events) if isinstance(e, tuple) and e[0] == "score"
    )

    assert results == []
    assert db.rollbacks == 1
    assert rollback_idx < prewarm_idx < score_idx
    assert ("prewarm", ("AAA", "BBB")) in events
    assert ("score", ("AAA", "BBB")) in events
