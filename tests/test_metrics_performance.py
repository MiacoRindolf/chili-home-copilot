from __future__ import annotations

from app import health, metrics, openai_client
from app.metrics import (
    _LATENCIES_MS,
    conversation_stats,
    feature_usage,
    get_counts,
    latency_history,
    record_latency,
)


class _SubqueryColumns:
    message_count = object()


class _MessageCountsSubquery:
    c = _SubqueryColumns()


class _FakeQuery:
    def __init__(self, kind: str, *, total: int = 0, aggregate: tuple[float, int] = (0, 0)) -> None:
        self.kind = kind
        self.total = total
        self.aggregate = aggregate
        self.filter_calls = 0
        self.group_by_calls = 0
        self.subquery_calls = 0
        self.one_calls = 0

    def count(self) -> int:
        if self.kind != "total":
            raise AssertionError(f"unexpected count() on {self.kind}")
        return self.total

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def group_by(self, *args: object) -> "_FakeQuery":
        self.group_by_calls += 1
        return self

    def subquery(self) -> _MessageCountsSubquery:
        if self.kind != "message_counts":
            raise AssertionError(f"unexpected subquery() on {self.kind}")
        self.subquery_calls += 1
        return _MessageCountsSubquery()

    def one(self) -> tuple[float, int]:
        if self.kind != "aggregate":
            raise AssertionError(f"unexpected one() on {self.kind}")
        self.one_calls += 1
        return self.aggregate

    def all(self) -> list[object]:
        raise AssertionError("conversation_stats should not materialize grouped counts")


class _FakeSession:
    def __init__(self, *, total: int = 3, aggregate: tuple[float, int] = (1.5, 2)) -> None:
        self.total = total
        self.aggregate = aggregate
        self.queries: list[_FakeQuery] = []

    def query(self, *args: object) -> _FakeQuery:
        if not self.queries:
            query = _FakeQuery("total", total=self.total)
        elif len(self.queries) == 1:
            query = _FakeQuery("message_counts")
        else:
            query = _FakeQuery("aggregate", aggregate=self.aggregate)
        self.queries.append(query)
        return query


class _CountsFakeQuery:
    def __init__(
        self,
        kind: str,
        *,
        aggregate: tuple[int, int | None, int | None] = (0, None, None),
        count: int = 0,
    ) -> None:
        self.kind = kind
        self.aggregate = aggregate
        self.count_value = count
        self.one_calls = 0
        self.count_calls = 0

    def filter(self, *args: object) -> "_CountsFakeQuery":
        raise AssertionError("get_counts should not issue separate filtered chore counts")

    def one(self) -> tuple[int, int | None, int | None]:
        if self.kind != "chore_aggregate":
            raise AssertionError(f"unexpected one() on {self.kind}")
        self.one_calls += 1
        return self.aggregate

    def count(self) -> int:
        if self.kind != "birthday_count":
            raise AssertionError(f"unexpected count() on {self.kind}")
        self.count_calls += 1
        return self.count_value


class _CountsFakeSession:
    def __init__(
        self,
        *,
        chore_aggregate: tuple[int, int | None, int | None] = (4, 3, 1),
        birthday_count: int = 2,
    ) -> None:
        self.chore_aggregate = chore_aggregate
        self.birthday_count = birthday_count
        self.queries: list[_CountsFakeQuery] = []

    def query(self, *args: object) -> _CountsFakeQuery:
        if not self.queries:
            query = _CountsFakeQuery("chore_aggregate", aggregate=self.chore_aggregate)
        else:
            query = _CountsFakeQuery("birthday_count", count=self.birthday_count)
        self.queries.append(query)
        return query


class _ZeroCountQuery:
    def filter(self, *args: object) -> "_ZeroCountQuery":
        return self

    def count(self) -> int:
        return 0


class _ZeroCountSession:
    def query(self, *args: object) -> _ZeroCountQuery:
        return _ZeroCountQuery()


def test_conversation_stats_aggregates_message_lengths_in_database() -> None:
    db = _FakeSession(total=3, aggregate=(1.5, 2))

    result = conversation_stats(db)  # type: ignore[arg-type]

    assert result == {"total": 3, "avg_messages": 1.5, "longest": 2}
    assert len(db.queries) == 3
    assert db.queries[1].filter_calls == 1
    assert db.queries[1].group_by_calls == 1
    assert db.queries[1].subquery_calls == 1
    assert db.queries[2].one_calls == 1


def test_conversation_stats_skips_message_aggregate_when_empty() -> None:
    db = _FakeSession(total=0)

    assert conversation_stats(db) == {"total": 0, "avg_messages": 0, "longest": 0}  # type: ignore[arg-type]
    assert len(db.queries) == 1


def test_get_counts_batches_chore_status_counts() -> None:
    db = _CountsFakeSession(chore_aggregate=(4, 3, 1), birthday_count=2)

    result = get_counts(db)  # type: ignore[arg-type]

    assert result == {
        "chores": {"total": 4, "pending": 3, "done": 1},
        "birthdays": {"total": 2},
    }
    assert len(db.queries) == 2
    assert db.queries[0].one_calls == 1
    assert db.queries[1].count_calls == 1


def test_get_counts_coerces_empty_chore_sums_to_zero() -> None:
    db = _CountsFakeSession(chore_aggregate=(0, None, None), birthday_count=0)

    result = get_counts(db)  # type: ignore[arg-type]

    assert result == {
        "chores": {"total": 0, "pending": 0, "done": 0},
        "birthdays": {"total": 0},
    }


def test_record_latency_keeps_fixed_size_recent_window() -> None:
    _LATENCIES_MS.clear()
    try:
        for ms in range(505):
            record_latency(ms)

        assert _LATENCIES_MS.maxlen == 500
        assert len(_LATENCIES_MS) == 500
        assert _LATENCIES_MS[0][1] == 5
        history = latency_history()
        assert len(history) == 100
        assert history[0]["ms"] == 405
        assert history[-1]["ms"] == 504
    finally:
        _LATENCIES_MS.clear()


def test_feature_usage_reuses_supplied_action_stats(monkeypatch) -> None:
    monkeypatch.setattr(
        metrics,
        "action_type_stats",
        lambda db: (_ for _ in ()).throw(AssertionError("unexpected query")),
    )

    result = feature_usage(
        object(),  # type: ignore[arg-type]
        {"web_search": 2, "general_chat": 3, "add_chore": 4, "pair_device": 1},
    )

    assert result["web_search"] == 2
    assert result["general_chat"] == 3
    assert result["tool_actions"] == 5


def test_admin_dashboard_json_reuses_action_type_stats(monkeypatch) -> None:
    calls = 0
    ollama_calls = 0
    rag_calls = 0

    def fake_action_type_stats(db) -> dict:
        nonlocal calls
        calls += 1
        return {"web_search": 7, "add_chore": 2}

    def fake_check_ollama() -> dict:
        nonlocal ollama_calls
        ollama_calls += 1
        return {"ok": True}

    def fake_rag_stats() -> dict:
        nonlocal rag_calls
        rag_calls += 1
        return {"available": True}

    def fake_system_alerts(db, *, ollama_status=None, rag_status=None) -> list[dict]:
        assert ollama_status == {"ok": True}
        assert rag_status == {"available": True}
        return []

    monkeypatch.setattr(health, "check_db", lambda db: {"ok": True})
    monkeypatch.setattr(health, "check_ollama", fake_check_ollama)
    monkeypatch.setattr(openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(openai_client, "OPENAI_MODEL", "test-model")
    monkeypatch.setattr(metrics, "action_type_stats", fake_action_type_stats)
    monkeypatch.setattr(metrics, "total_stats", lambda db: {})
    monkeypatch.setattr(metrics, "get_counts", lambda db: {})
    monkeypatch.setattr(metrics, "latency_stats", lambda: {})
    monkeypatch.setattr(metrics, "latency_history", lambda: [])
    monkeypatch.setattr(metrics, "model_stats", lambda db: {})
    monkeypatch.setattr(metrics, "messages_per_day", lambda db: [])
    monkeypatch.setattr(metrics, "hourly_activity", lambda db: [])
    monkeypatch.setattr(metrics, "response_time_trend", lambda db: [])
    monkeypatch.setattr(metrics, "conversation_stats", lambda db: {})
    monkeypatch.setattr(metrics, "top_users", lambda db: [])
    monkeypatch.setattr(metrics, "per_user_chore_stats", lambda db: [])
    monkeypatch.setattr(metrics, "rag_stats", fake_rag_stats)
    monkeypatch.setattr(metrics, "system_alerts", fake_system_alerts)

    result = metrics.admin_dashboard_json(object())  # type: ignore[arg-type]

    assert calls == 1
    assert ollama_calls == 1
    assert rag_calls == 1
    assert result["action_types"] == {"web_search": 7, "add_chore": 2}
    assert result["features"]["web_search"] == 7
    assert result["features"]["tool_actions"] == 2


def test_system_alerts_reuses_supplied_statuses(monkeypatch) -> None:
    monkeypatch.setattr(
        health,
        "check_ollama",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected ollama check")),
    )
    monkeypatch.setattr(
        metrics,
        "rag_stats",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected rag stats")),
    )
    monkeypatch.setattr(openai_client, "is_configured", lambda: True)
    monkeypatch.setattr(metrics, "latency_stats", lambda: {"p95_ms": None})

    alerts = metrics.system_alerts(
        _ZeroCountSession(),  # type: ignore[arg-type]
        ollama_status={"ok": False},
        rag_status={"available": False},
    )

    texts = [alert["text"] for alert in alerts]
    assert "Ollama is offline" in texts
    assert "RAG knowledge base not ingested" in texts
