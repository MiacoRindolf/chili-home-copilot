import inspect

import app.migrations as migrations


def test_pattern_regime_rollup_index_registered_after_kill_switch_index():
    migrations._assert_migration_ids_unique()
    ids = [version_id for version_id, _ in migrations.MIGRATIONS]

    assert "295_pattern_regime_perf_pattern_asof_cover_index" in ids
    assert ids.index("295_pattern_regime_perf_pattern_asof_cover_index") > ids.index(
        "294_kill_switch_runtime_lookup_index"
    )


def test_pattern_regime_rollup_index_matches_history_query_shape():
    src = inspect.getsource(
        migrations._migration_295_pattern_regime_perf_pattern_asof_cover_index
    )

    assert "ix_pattern_regime_perf_pattern_asof_cover" in src
    assert "trading_pattern_regime_performance_daily" in src
    assert "(pattern_id, as_of_date)" in src
    assert "INCLUDE (has_confidence, expectancy)" in src
