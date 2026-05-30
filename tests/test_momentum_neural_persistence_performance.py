from app.models.trading import MomentumStrategyVariant
from app.services.trading.momentum_neural.persistence import _variant_ids_for_tick_rows


class _FakeQuery:
    def __init__(self, rows: list[MomentumStrategyVariant]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def all(self) -> list[MomentumStrategyVariant]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[MomentumStrategyVariant]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, model: object) -> _FakeQuery:
        assert model is MomentumStrategyVariant
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def _variant(
    family: str,
    version: int,
    row_id: int,
    *,
    active: bool,
) -> MomentumStrategyVariant:
    return MomentumStrategyVariant(
        id=row_id,
        family=family,
        variant_key=family,
        version=version,
        is_active=active,
    )


def test_variant_ids_for_tick_rows_batches_family_lookup_and_prefers_latest_active() -> None:
    rows = [
        _variant("impulse_breakout", 1, 10, active=True),
        _variant("impulse_breakout", 2, 11, active=True),
        _variant("pullback_reversal", 1, 20, active=False),
    ]
    db = _FakeSession(rows)

    active_by_family, by_key = _variant_ids_for_tick_rows(
        db,  # type: ignore[arg-type]
        [
            {"family_id": "impulse_breakout", "family_version": 1},
            {"family_id": "impulse_breakout", "family_version": 2},
            {"family_id": "pullback_reversal", "family_version": 1},
        ],
    )

    assert active_by_family == {"impulse_breakout": 11}
    assert by_key[("impulse_breakout", "impulse_breakout", 1)] == 10
    assert by_key[("pullback_reversal", "pullback_reversal", 1)] == 20
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_variant_ids_for_tick_rows_skips_empty_family_lookup() -> None:
    db = _FakeSession([])

    assert _variant_ids_for_tick_rows(db, []) == ({}, {})  # type: ignore[arg-type]
    assert db.query_calls == 0
