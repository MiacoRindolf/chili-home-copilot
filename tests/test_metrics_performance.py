from __future__ import annotations

from app.metrics import conversation_stats, get_counts


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
