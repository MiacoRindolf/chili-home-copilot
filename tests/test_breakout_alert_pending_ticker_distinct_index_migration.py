import inspect

from app import migrations


def test_pending_breakout_alert_ticker_distinct_index_migration_registered() -> None:
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]

    assert "299_breakout_alert_pending_ticker_distinct_index" in ids
    assert ids.index("299_breakout_alert_pending_ticker_distinct_index") > ids.index(
        "296_divergence_discovery_probe_indexes"
    )


def test_pending_breakout_alert_ticker_distinct_index_shape() -> None:
    src = inspect.getsource(
        migrations._migration_299_breakout_alert_pending_ticker_distinct_index
    )

    assert "ix_breakout_alerts_pending_ticker_distinct_299" in src
    assert "ON trading_breakout_alerts (ticker)" in src
    assert "WHERE outcome = 'pending'" in src
    assert "ANALYZE trading_breakout_alerts" in src
    assert "NOT indisvalid" in src
