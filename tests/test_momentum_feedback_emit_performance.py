from __future__ import annotations

from app.services.trading.momentum_neural import feedback_emit
from app.services.trading.momentum_neural.feedback_emit import _outcomes_table_present


class _FakeDb:
    bind = object()


def test_outcomes_table_present_uses_targeted_has_table(monkeypatch) -> None:
    class _Inspector:
        def __init__(self) -> None:
            self.has_table_calls: list[str] = []

        def has_table(self, name: str) -> bool:
            self.has_table_calls.append(name)
            return name == "momentum_automation_outcomes"

        def get_table_names(self) -> list[str]:
            raise AssertionError("full table-name scan should not be used")

    inspector = _Inspector()
    monkeypatch.setattr(feedback_emit, "sa_inspect", lambda _bind: inspector)

    assert _outcomes_table_present(_FakeDb()) is True  # type: ignore[arg-type]
    assert inspector.has_table_calls == ["momentum_automation_outcomes"]


def test_outcomes_table_present_keeps_table_list_fallback(monkeypatch) -> None:
    class _Inspector:
        def get_table_names(self) -> list[str]:
            return ["users", "momentum_automation_outcomes"]

    monkeypatch.setattr(feedback_emit, "sa_inspect", lambda _bind: _Inspector())

    assert _outcomes_table_present(_FakeDb()) is True  # type: ignore[arg-type]
