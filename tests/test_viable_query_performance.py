from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import MomentumStrategyVariant
from app.services.trading.momentum_neural import viable_query
from app.services.trading.momentum_neural.viable_query import (
    _hot_variants_by_family_version,
    _momentum_tables_present,
    _session_counts,
)


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.order_by_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        self.order_by_calls += 1
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.rows


class _FakeSession:
    def __init__(self, query_rows: list[list[SimpleNamespace]]) -> None:
        self.query_rows = query_rows
        self.query_calls = 0
        self.queries: list[_FakeQuery] = []

    def query(self, model: object) -> _FakeQuery:
        assert model is MomentumStrategyVariant
        rows = self.query_rows[self.query_calls] if self.query_calls < len(self.query_rows) else []
        self.query_calls += 1
        query = _FakeQuery(rows)
        self.queries.append(query)
        return query

    def get_bind(self) -> object:
        return object()


def _variant(
    *,
    variant_id: int,
    family: str,
    version: int,
    variant_key: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=variant_id,
        family=family,
        variant_key=variant_key or family,
        version=version,
    )


def test_hot_variants_by_family_version_batches_exact_lookup() -> None:
    exact = _variant(variant_id=11, family="impulse_breakout", version=2)
    ignored = _variant(
        variant_id=12,
        family="impulse_breakout",
        variant_key="refined_impulse",
        version=2,
    )
    db = _FakeSession([[ignored, exact]])

    result = _hot_variants_by_family_version(
        db,  # type: ignore[arg-type]
        [
            {"family_id": "impulse_breakout", "family_version": 2},
            {"family_id": "impulse_breakout", "family_version": 2},
            {"family_id": "", "family_version": 2},
        ],
    )

    assert result == {("impulse_breakout", 2): exact}
    assert db.query_calls == 1
    assert db.queries[0].filter_calls == 1
    assert db.queries[0].order_by_calls == 0


def test_hot_variants_by_family_version_batches_active_fallbacks() -> None:
    active = _variant(variant_id=21, family="mean_reversion", version=4)
    db = _FakeSession([[], [active]])

    result = _hot_variants_by_family_version(
        db,  # type: ignore[arg-type]
        [{"family_id": "mean_reversion", "family_version": "bad"}],
    )

    assert result == {("mean_reversion", 1): active}
    assert db.query_calls == 2
    assert db.queries[0].filter_calls == 1
    assert db.queries[1].filter_calls == 1
    assert db.queries[1].order_by_calls == 1


def test_hot_variants_by_family_version_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _hot_variants_by_family_version(db, []) == {}  # type: ignore[arg-type]
    assert db.query_calls == 0


def test_session_counts_scans_rows_once() -> None:
    class OneShotRows(list):
        def __init__(self, values: list[SimpleNamespace]):
            super().__init__(values)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("session rows were scanned more than once")
            return super().__iter__()

    rows = OneShotRows(
        [
            SimpleNamespace(mode="paper", state="done"),
            SimpleNamespace(mode="paper", state="queued_live"),
            SimpleNamespace(mode="live", state="armed_pending_runner"),
            SimpleNamespace(mode="live", state="live_arm_pending"),
            SimpleNamespace(mode="shadow", state="live_arm_pending"),
        ]
    )

    assert _session_counts(rows) == (2, 2, 4)
    assert rows.iterations == 1


def test_momentum_tables_present_uses_targeted_has_table(monkeypatch) -> None:
    class _Inspector:
        def __init__(self) -> None:
            self.has_table_calls: list[str] = []

        def has_table(self, name: str) -> bool:
            self.has_table_calls.append(name)
            return name in {"momentum_symbol_viability", "momentum_strategy_variants"}

        def get_table_names(self) -> list[str]:
            raise AssertionError("full table-name scan should not be used")

    inspector = _Inspector()
    monkeypatch.setattr(viable_query, "sa_inspect", lambda _bind: inspector)

    assert _momentum_tables_present(_FakeSession([])) is True  # type: ignore[arg-type]
    assert inspector.has_table_calls == ["momentum_symbol_viability", "momentum_strategy_variants"]


def test_momentum_tables_present_keeps_table_list_fallback(monkeypatch) -> None:
    class _Inspector:
        def get_table_names(self) -> list[str]:
            return ["momentum_symbol_viability", "momentum_strategy_variants"]

    monkeypatch.setattr(viable_query, "sa_inspect", lambda _bind: _Inspector())

    assert _momentum_tables_present(_FakeSession([])) is True  # type: ignore[arg-type]
