from __future__ import annotations

from types import SimpleNamespace

from app import migrations


def test_migration_285_registered_after_phase5b_tca_filter() -> None:
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]

    assert "284_phase5b_tca_quality_filter" in ids
    assert "285_execution_slippage_unfilled_hygiene" in ids
    assert ids.index("285_execution_slippage_unfilled_hygiene") == (
        ids.index("284_phase5b_tca_quality_filter") + 1
    )


def test_migration_285_nulls_only_unfilled_realized_slippage(monkeypatch) -> None:
    class FakeConn:
        def __init__(self) -> None:
            self.sql: list[str] = []
            self.commits = 0

        def execute(self, stmt):
            self.sql.append(str(stmt))
            return SimpleNamespace(rowcount=136)

        def commit(self) -> None:
            self.commits += 1

    fake = FakeConn()
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {"trading_execution_events"},
    )

    migrations._migration_285_execution_slippage_unfilled_hygiene(fake)

    sql = "\n".join(fake.sql)
    assert "UPDATE trading_execution_events" in sql
    assert "SET realized_slippage_bps = NULL" in sql
    assert "COALESCE(cumulative_filled_quantity, 0) <= 0" in sql
    assert "COALESCE(last_fill_quantity, 0) <= 0" in sql
    assert "COALESCE(average_fill_price, 0) > 0" in sql
    assert "LOWER(COALESCE(status, '')) IN ('filled', 'partially_filled')" in sql
    assert "LOWER(COALESCE(event_type, '')) IN ('fill', 'partial_fill')" in sql
    assert fake.commits == 1
