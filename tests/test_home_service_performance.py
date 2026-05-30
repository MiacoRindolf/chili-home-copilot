from __future__ import annotations

from app.services.home_service import get_insights


class _InsightFakeQuery:
    def __init__(
        self,
        kind: str,
        *,
        aggregate: tuple[int | None, int | None, int | None] = (0, 0, 0),
        rows: list[object] | None = None,
    ) -> None:
        self.kind = kind
        self.aggregate = aggregate
        self.rows = rows or []
        self.one_calls = 0
        self.all_calls = 0

    def filter(self, *args: object) -> "_InsightFakeQuery":
        raise AssertionError("get_insights should not issue separate filtered chore counts")

    def count(self) -> int:
        raise AssertionError("get_insights should aggregate chore counts in one query")

    def one(self) -> tuple[int | None, int | None, int | None]:
        if self.kind != "chore_counts":
            raise AssertionError(f"unexpected one() on {self.kind}")
        self.one_calls += 1
        return self.aggregate

    def all(self) -> list[object]:
        if self.kind != "birthdays":
            raise AssertionError(f"unexpected all() on {self.kind}")
        self.all_calls += 1
        return self.rows


class _InsightFakeSession:
    def __init__(self, aggregate: tuple[int | None, int | None, int | None]) -> None:
        self.aggregate = aggregate
        self.queries: list[_InsightFakeQuery] = []

    def query(self, *args: object) -> _InsightFakeQuery:
        if not self.queries:
            query = _InsightFakeQuery("chore_counts", aggregate=self.aggregate)
        else:
            query = _InsightFakeQuery("birthdays")
        self.queries.append(query)
        return query


def test_get_insights_batches_chore_status_counts() -> None:
    db = _InsightFakeSession((5, 2, 1))

    insights = get_insights(db)  # type: ignore[arg-type]

    texts = [row["text"] for row in insights]
    assert "2 chores overdue" in texts
    assert "5 chores pending - time to get busy!" in texts
    assert "1 chore due today" in texts
    assert len(db.queries) == 2
    assert db.queries[0].one_calls == 1
    assert db.queries[1].all_calls == 1


def test_get_insights_coerces_empty_chore_sums_to_zero() -> None:
    db = _InsightFakeSession((None, None, None))

    insights = get_insights(db)  # type: ignore[arg-type]

    assert [row["text"] for row in insights] == ["All chores are done! Great job!"]
    assert len(db.queries) == 2
