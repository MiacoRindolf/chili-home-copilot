from __future__ import annotations

from app.services.trading.momentum_neural import automation_query
from app.services.trading.momentum_neural.automation_query import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_ARCHIVED,
    STATE_CANCELLED,
    STATE_DRAFT,
    STATE_ENTERED,
    STATE_EXPIRED,
    STATE_LIVE_ARM_PENDING,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ENTERED,
    STATE_QUEUED,
    STATE_QUEUED_LIVE,
    _session_summary_counts_from_grouped_rows,
    _session_bulk_read_keys,
    _table_exists,
    _tables_present,
)


class _FakeDb:
    def get_bind(self) -> object:
        return object()


def test_tables_present_uses_targeted_has_table(monkeypatch) -> None:
    class _Inspector:
        def __init__(self) -> None:
            self.has_table_calls: list[str] = []

        def has_table(self, name: str) -> bool:
            self.has_table_calls.append(name)
            return name == "trading_automation_sessions"

        def get_table_names(self) -> list[str]:
            raise AssertionError("full table-name scan should not be used")

    inspector = _Inspector()
    monkeypatch.setattr(automation_query, "sa_inspect", lambda _bind: inspector)

    assert _tables_present(_FakeDb()) is True  # type: ignore[arg-type]
    assert inspector.has_table_calls == ["trading_automation_sessions"]


def test_table_exists_uses_targeted_has_table(monkeypatch) -> None:
    class _Inspector:
        def __init__(self) -> None:
            self.has_table_calls: list[str] = []

        def has_table(self, name: str) -> bool:
            self.has_table_calls.append(name)
            return name == "trading_automation_runtime_snapshots"

        def get_table_names(self) -> list[str]:
            raise AssertionError("full table-name scan should not be used")

    inspector = _Inspector()
    monkeypatch.setattr(automation_query, "sa_inspect", lambda _bind: inspector)

    assert _table_exists(_FakeDb(), "trading_automation_runtime_snapshots") is True  # type: ignore[arg-type]
    assert inspector.has_table_calls == ["trading_automation_runtime_snapshots"]


def test_table_exists_keeps_table_list_fallback(monkeypatch) -> None:
    class _Inspector:
        def get_table_names(self) -> list[str]:
            return ["users", "trading_automation_sessions"]

    monkeypatch.setattr(automation_query, "sa_inspect", lambda _bind: _Inspector())

    assert _table_exists(_FakeDb(), "trading_automation_sessions") is True  # type: ignore[arg-type]


def test_session_summary_counts_from_grouped_rows_preserves_bucket_semantics() -> None:
    rows = [
        ("paper", STATE_DRAFT, 2),
        ("paper", STATE_QUEUED, 3),
        ("paper", STATE_ENTERED, 5),
        ("live", STATE_LIVE_ARM_PENDING, 7),
        ("live", STATE_ARMED_PENDING_RUNNER, 11),
        ("live", STATE_QUEUED_LIVE, 13),
        ("live", STATE_LIVE_ENTERED, 17),
        ("paper", STATE_CANCELLED, 19),
        ("live", STATE_LIVE_CANCELLED, 23),
        ("paper", STATE_ARCHIVED, 29),
        ("live", STATE_EXPIRED, 31),
    ]

    assert _session_summary_counts_from_grouped_rows(rows) == {
        "total_sessions": 160,
        "pending_paper_drafts": 2,
        "paper_runner_queued": 3,
        "paper_runner_active": 5,
        "live_runner_queued": 13,
        "live_runner_active": 17,
        "pending_live_arms": 7,
        "armed_awaiting_runner": 11,
        "cancelled": 42,
        "archived": 29,
        "expired": 31,
    }


def test_session_bulk_read_keys_deduplicates_followup_query_inputs() -> None:
    class _Session:
        def __init__(self, sid: int, symbol: str, variant_id: int) -> None:
            self.id = sid
            self.symbol = symbol
            self.variant_id = variant_id

    rows = [
        (_Session(1, "BTC-USD", 7), object()),
        (_Session(2, "BTC-USD", 7), object()),
        (_Session(1, "ETH-USD", 8), object()),
        (_Session(3, "ETH-USD", 7), object()),
    ]

    ids, symbols, variant_ids = _session_bulk_read_keys(rows)

    assert ids == [1, 2, 3]
    assert symbols == ["BTC-USD", "ETH-USD"]
    assert variant_ids == [7, 8]
