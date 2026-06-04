from types import SimpleNamespace

from app.services.trading import auto_trader as at_mod


class _FakePostgresSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement):
        self.calls.append(str(statement))


class _FakeSqliteSession(_FakePostgresSession):
    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


def test_candidate_select_statement_timeout_uses_configured_value(monkeypatch) -> None:
    monkeypatch.setattr(
        at_mod.settings,
        "chili_autotrader_candidate_select_statement_timeout_ms",
        1234,
        raising=False,
    )

    assert at_mod._candidate_select_statement_timeout_ms(tick_budget_s=15) == 1234


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
