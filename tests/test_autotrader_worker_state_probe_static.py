from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "d-autotrader-worker-state.ps1"


def test_worker_state_probe_is_schema_aware_for_alert_processed_at() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "information_schema.columns" in source
    assert "NULL::timestamp AS processed_at" in source
    assert "SELECT id, ticker, alert_type, created_at, {processed_at_expr}, {status_expr}" in source
    assert "SELECT id, ticker, alert_type, created_at, processed_at, status" not in source


def test_worker_state_probe_pg_activity_sql_has_valid_python_quoting() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'cur.execute(""""""' not in source
    assert 'cur.execute("""' in source
    assert 'LIMIT 20\n""")' in source
