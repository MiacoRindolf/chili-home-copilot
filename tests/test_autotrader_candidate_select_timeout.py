from types import SimpleNamespace

from sqlalchemy.exc import DBAPIError

from app.services.trading import auto_trader as at_mod


class _FakePostgresSession:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rollback_count = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement):
        self.calls.append(str(statement))

    def rollback(self):
        self.rollback_count += 1


class _FakeSqliteSession(_FakePostgresSession):
    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


class _FakeAlertCountQuery:
    def __init__(
        self,
        rows: int,
        *,
        session: object | None = None,
        error: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.session = session
        self.error = error
        self.limit_value: int | None = None
        self.with_entities_called = False

    def with_entities(self, *_entities):
        self.with_entities_called = True
        return self

    def limit(self, value: int):
        self.limit_value = int(value)
        return self

    def all(self):
        if self.error is not None:
            raise self.error
        return [(i,) for i in range(self.rows)]


def test_candidate_select_statement_timeout_uses_configured_value(monkeypatch) -> None:
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_candidate_select_statement_timeout_ms",
        1234,
        raising=False,
    )

    assert at_mod._candidate_select_statement_timeout_ms(tick_budget_s=15) == 1234


def test_bounded_breakout_alert_count_caps_without_exact_count() -> None:
    query = _FakeAlertCountQuery(rows=5)

    count = at_mod._bounded_breakout_alert_count(query, cap=3)

    assert count == 3
    assert query.with_entities_called is True
    assert query.limit_value == 4


def test_bounded_breakout_alert_count_times_out_conservatively(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_candidate_select_statement_timeout_ms",
        1500,
        raising=False,
    )
    session = _FakePostgresSession()
    query = _FakeAlertCountQuery(
        rows=0,
        session=session,
        error=DBAPIError("select", {}, Exception("statement timeout")),
    )

    count = at_mod._bounded_breakout_alert_count(
        query,
        cap=3,
        tick_budget_s=15,
        context="stock_stale_unprocessed",
    )

    assert count == 3
    assert query.with_entities_called is True
    assert query.limit_value == 4
    assert session.calls == ["SET LOCAL statement_timeout = '1500ms'"]
    assert session.rollback_count == 1


def test_queue_pressure_floor_one_disables_shadow_suppression(monkeypatch) -> None:
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_paper_shadow_queue_pressure_suppression_floor",
        1.0,
        raising=False,
    )
    out = {"candidate_queue_pressure": 1.0}

    reason = at_mod._paper_shadow_queue_pressure_suppression_reason(
        out,
        reject_reason="non_positive_expected_edge",
        snap={"expected_net_pct": -1.0},
    )

    assert reason is None
    assert "paper_shadow_queue_pressure_suppressed" not in out


def test_candidate_batch_size_still_uses_keyword_clamps(monkeypatch) -> None:
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_candidate_batch_size",
        7,
        raising=False,
    )

    assert at_mod._autotrader_candidate_batch_size() == 7


def test_candidate_select_statement_timeout_derives_from_tick_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_candidate_select_statement_timeout_ms",
        0,
        raising=False,
    )
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_candidate_select_timeout_fraction",
        0.1,
        raising=False,
    )

    assert at_mod._candidate_select_statement_timeout_ms(tick_budget_s=20) == 2000


def test_candidate_select_applies_and_resets_postgres_statement_timeout(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_candidate_select_statement_timeout_ms",
        1500,
        raising=False,
    )
    session = _FakePostgresSession()

    timeout_ms = at_mod._apply_candidate_select_statement_timeout(
        session,
        tick_budget_s=15,
    )
    at_mod._reset_candidate_select_statement_timeout(session, timeout_ms)

    assert timeout_ms == 1500
    assert session.calls == [
        "SET LOCAL statement_timeout = '1500ms'",
        "SET LOCAL statement_timeout = DEFAULT",
    ]


def test_candidate_select_skips_statement_timeout_for_non_postgres() -> None:
    session = _FakeSqliteSession()

    timeout_ms = at_mod._apply_candidate_select_statement_timeout(
        session,
        tick_budget_s=15,
    )

    assert timeout_ms is None
    assert session.calls == []
