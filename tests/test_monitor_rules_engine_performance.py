from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.models.trading import MonitorDecisionRule, PatternMonitorDecision, ScanPattern
from app.services.trading.monitor_rules_engine import (
    _monitor_decision_rules_by_key,
    _recent_decisions,
    _scan_pattern_names_by_id,
    aggregate_decision_outcomes,
)


def _query_matches(args: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    return len(args) == len(expected) and all(
        actual is wanted for actual, wanted in zip(args, expected)
    )


class _FakeQuery:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def all(self) -> list[object]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[object], expected_query: tuple[object, ...]) -> None:
        self.rows = rows
        self.expected_query = expected_query
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, *args: object) -> _FakeQuery:
        assert _query_matches(args, self.expected_query)
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


class _AggregateSession:
    def __init__(self, decisions: list[SimpleNamespace]) -> None:
        self.decisions = decisions
        self.queries: list[tuple[object, ...]] = []
        self.added: list[object] = []
        self.flush_calls = 0

    def query(self, *args: object) -> _FakeQuery:
        self.queries.append(args)
        if _query_matches(args, (PatternMonitorDecision,)):
            return _FakeQuery(self.decisions)
        if _query_matches(args, (ScanPattern.id, ScanPattern.name)):
            return _FakeQuery([(7, "Gap Continuation")])
        if _query_matches(args, (MonitorDecisionRule,)):
            return _FakeQuery([])
        raise AssertionError(f"unexpected query: {args!r}")

    def add(self, row: object) -> None:
        self.added.append(row)

    def flush(self) -> None:
        self.flush_calls += 1


def test_scan_pattern_names_by_id_batches_lookup() -> None:
    db = _FakeSession(
        [(3, "Breakout Pullback"), (5, None), (None, "ignored")],
        (ScanPattern.id, ScanPattern.name),
    )

    result = _scan_pattern_names_by_id(db, {3, 5})  # type: ignore[arg-type]

    assert result == {3: "Breakout Pullback", 5: "pattern_5"}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_scan_pattern_names_by_id_skips_empty_lookup() -> None:
    db = _FakeSession([], (ScanPattern.id, ScanPattern.name))

    assert _scan_pattern_names_by_id(db, set()) == {}  # type: ignore[arg-type]
    assert db.query_calls == 0


def test_monitor_decision_rules_by_key_batches_lookup() -> None:
    wanted = SimpleNamespace(pattern_type="breakout", signal_signature="sig:a")
    unrelated = SimpleNamespace(pattern_type="breakout", signal_signature="sig:b")
    db = _FakeSession([wanted, unrelated], (MonitorDecisionRule,))

    result = _monitor_decision_rules_by_key(
        db,  # type: ignore[arg-type]
        {("breakout", "sig:a")},
    )

    assert result == {("breakout", "sig:a"): wanted}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_monitor_decision_rules_by_key_skips_empty_lookup() -> None:
    db = _FakeSession([], (MonitorDecisionRule,))

    assert _monitor_decision_rules_by_key(db, set()) == {}  # type: ignore[arg-type]
    assert db.query_calls == 0


def test_recent_decisions_uses_bounded_latest_window_with_stable_ties() -> None:
    first = SimpleNamespace(created_at=datetime(2026, 5, 28, 12, 0))
    second = SimpleNamespace(created_at=datetime(2026, 5, 28, 14, 0))
    third = SimpleNamespace(created_at=datetime(2026, 5, 28, 14, 0))
    fourth = SimpleNamespace(created_at=datetime(2026, 5, 28, 13, 0))

    assert _recent_decisions([first, second, third, fourth], limit=3) == [
        second,
        third,
        fourth,
    ]
    assert _recent_decisions([first], limit=0) == []


def test_aggregate_decision_outcomes_batches_rule_inputs() -> None:
    now = datetime(2026, 5, 28, 14, 15)
    decisions = [
        SimpleNamespace(
            scan_pattern_id=7,
            conditions_snapshot={"trade_plan": {}, "health_score": 0.9},
            was_beneficial=True,
            action="hold",
            mechanical_action="hold",
            new_stop=None,
            new_target=None,
            price_at_decision=100.0,
            created_at=now,
        ),
        SimpleNamespace(
            scan_pattern_id=7,
            conditions_snapshot={"trade_plan": {}, "health_score": 0.9},
            was_beneficial=False,
            action="hold",
            mechanical_action="hold",
            new_stop=None,
            new_target=None,
            price_at_decision=100.0,
            created_at=now,
        ),
    ]
    db = _AggregateSession(decisions)

    result = aggregate_decision_outcomes(db)  # type: ignore[arg-type]

    assert result == {"rules_updated": 1, "rows_processed": 2}
    expected_queries = [
        (PatternMonitorDecision,),
        (ScanPattern.id, ScanPattern.name),
        (MonitorDecisionRule,),
    ]
    assert len(db.queries) == len(expected_queries)
    for actual, expected in zip(db.queries, expected_queries):
        assert _query_matches(actual, expected)
    assert len(db.added) == 1
    assert db.flush_calls == 1
