from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.services import home_service
from app.services.home_service import get_calendar_events, get_insights


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


class _CalendarFakeQuery:
    def __init__(self, kind: str, rows: list[object]) -> None:
        self.kind = kind
        self.rows = rows
        self.filter_calls = 0
        self.all_calls = 0

    def filter(self, *args: object) -> "_CalendarFakeQuery":
        self.filter_calls += 1
        return self

    def all(self) -> list[object]:
        self.all_calls += 1
        return self.rows


class _CalendarFakeSession:
    def __init__(self, *, chores: list[object], birthdays: list[object]) -> None:
        self.chores = chores
        self.birthdays = birthdays
        self.queries: list[_CalendarFakeQuery] = []

    def query(self, *args: object) -> _CalendarFakeQuery:
        rows = self.chores if not self.queries else self.birthdays
        kind = "chores" if not self.queries else "birthdays"
        query = _CalendarFakeQuery(kind, rows)
        self.queries.append(query)
        return query


class _NoQuerySession:
    def query(self, *args: object) -> object:
        raise AssertionError("precomputed insight inputs should avoid chore/birthday queries")


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


def test_get_calendar_events_filters_birthdays_by_month() -> None:
    db = _CalendarFakeSession(
        chores=[],
        birthdays=[SimpleNamespace(id=1, name="Ada", date=date(1990, 5, 12))],
    )

    result = get_calendar_events(db, 2026, 5)  # type: ignore[arg-type]

    assert result == [
        {
            "date": "2026-05-12",
            "type": "birthday",
            "title": "Ada's birthday",
        }
    ]
    assert len(db.queries) == 2
    assert db.queries[0].kind == "chores"
    assert db.queries[1].kind == "birthdays"
    assert db.queries[1].filter_calls == 1
    assert db.queries[1].all_calls == 1


def test_get_insights_reuses_precomputed_dashboard_counts_and_birthdays() -> None:
    insights = get_insights(
        _NoQuerySession(),  # type: ignore[arg-type]
        today=date(2026, 5, 30),
        chore_counts=(5, 2, 1),
        birthdays=[SimpleNamespace(id=1, name="Ada", date=date(1990, 6, 1))],
    )

    texts = [row["text"] for row in insights]
    assert "2 chores overdue" in texts
    assert "5 chores pending - time to get busy!" in texts
    assert "Ada's birthday is in 2 days" in texts
    assert "1 chore due today" in texts


def test_get_dashboard_data_passes_precomputed_inputs_to_insights(monkeypatch) -> None:
    captured: dict[str, object] = {}
    today = date.today()
    birthday = SimpleNamespace(id=1, name="Ada", date=date(1990, 6, 1))
    done_chore = SimpleNamespace(
        id=1,
        title="Done",
        done=True,
        due_date=date(2026, 5, 29),
        priority="medium",
        recurrence=None,
        assigned_to=None,
        created_at=None,
        completed_at=datetime.combine(today, datetime.min.time()),
    )
    old_done_chore = SimpleNamespace(
        id=4,
        title="Old Done",
        done=True,
        due_date=date(2026, 5, 20),
        priority="medium",
        recurrence=None,
        assigned_to=None,
        created_at=None,
        completed_at=datetime.combine(today - timedelta(days=8), datetime.min.time()),
    )
    overdue_chore = SimpleNamespace(
        id=2,
        title="Late",
        done=False,
        due_date=today - timedelta(days=1),
        priority="high",
        recurrence=None,
        assigned_to=None,
        created_at=None,
        completed_at=None,
    )
    today_chore = SimpleNamespace(
        id=3,
        title="Today",
        done=False,
        due_date=today,
        priority="low",
        recurrence=None,
        assigned_to=None,
        created_at=None,
        completed_at=None,
    )

    class _DashboardQuery:
        def __init__(self, rows: list[object], *, count_value: int = 0) -> None:
            self.rows = rows
            self.count_value = count_value

        def order_by(self, *args: object) -> "_DashboardQuery":
            return self

        def filter(self, *args: object) -> "_DashboardQuery":
            return self

        def all(self) -> list[object]:
            return self.rows

        def count(self) -> int:
            return self.count_value

    class _DashboardSession:
        def __init__(self) -> None:
            self.query_count = 0

        def query(self, *args: object) -> _DashboardQuery:
            self.query_count += 1
            if self.query_count == 1:
                return _DashboardQuery([done_chore, old_done_chore, overdue_chore, today_chore])
            if self.query_count == 2:
                return _DashboardQuery([birthday])
            raise AssertionError("dashboard should derive chores_done_week from loaded chores")

    def fake_get_insights(db, user_id=None, **kwargs):
        captured.update(kwargs)
        return [{"type": "ok", "icon": "check", "text": "cached"}]

    monkeypatch.setattr(home_service, "_users_by_id", lambda db, user_ids: {})
    monkeypatch.setattr(home_service, "get_insights", fake_get_insights)
    monkeypatch.setattr(home_service, "get_weather", lambda: None)

    result = home_service.get_dashboard_data(
        _DashboardSession(),  # type: ignore[arg-type]
        {"is_guest": True},
    )

    assert result["insights"] == [{"type": "ok", "icon": "check", "text": "cached"}]
    assert result["chores_done_week"] == 1
    assert result["birthdays"] == [
        {
            "id": 1,
            "name": "Ada",
            "date": "1990-06-01",
            "days_until": (date(today.year, 6, 1) - today).days
            if date(today.year, 6, 1) >= today
            else (date(today.year + 1, 6, 1) - today).days,
        }
    ]
    assert captured["chore_counts"] == (2, 1, 1)
    assert captured["birthdays"] == [birthday]
