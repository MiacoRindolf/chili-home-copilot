from __future__ import annotations

from app.metrics import conversation_stats


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
