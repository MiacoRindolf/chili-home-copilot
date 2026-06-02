import inspect

import app.migrations as migrations


def test_kill_switch_runtime_index_migration_registered_after_slippage_hygiene():
    migrations._assert_migration_ids_unique()
    ids = [version_id for version_id, _ in migrations.MIGRATIONS]

    assert "294_kill_switch_runtime_lookup_index" in ids
    assert ids.index("294_kill_switch_runtime_lookup_index") > ids.index(
        "293_execution_slippage_unfilled_hygiene"
    )


def test_kill_switch_runtime_index_matches_lookup_shape():
    src = inspect.getsource(migrations._migration_294_kill_switch_runtime_lookup_index)

    assert "ix_risk_state_kill_switch_latest" in src
    assert "trading_risk_state" in src
    assert "regime, created_at DESC, id DESC" in src
