from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import MomentumStrategyVariant
from app.services.trading.momentum_neural.viable_query import _hot_variants_by_family_version


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
