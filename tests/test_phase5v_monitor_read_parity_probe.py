from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5v-monitor-read-parity-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5v_monitor_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_relation_guard_allows_only_phase5_relations() -> None:
    module = _load_module()

    assert module._relation_sql("trading_trades") == "trading_trades"
    assert module._relation_sql("trading_management_envelopes") == (
        "trading_management_envelopes"
    )

    try:
        module._relation_sql("trading_trades; DROP TABLE users")
    except ValueError as exc:
        assert "unexpected relation" in str(exc)
    else:
        raise AssertionError("unsafe relation accepted")


def test_monitor_decisions_sql_swaps_only_relation_name() -> None:
    module = _load_module()

    old_sql, old_params = module.monitor_decisions_sql(
        module.OLD_RELATION,
        user_id=7,
        action="hold",
        limit=25,
        offset=5,
    )
    new_sql, new_params = module.monitor_decisions_sql(
        module.NEW_RELATION,
        user_id=7,
        action="hold",
        limit=25,
        offset=5,
    )

    assert "JOIN trading_trades t ON t.id = d.trade_id" in old_sql
    assert "JOIN trading_management_envelopes t ON t.id = d.trade_id" in new_sql
    assert "trading_management_envelopes" not in old_sql
    assert "trading_trades" not in new_sql
    assert old_params == new_params == (7, "hold", "hold", 25, 5)


def test_imminent_alerts_sql_preserves_actioned_alert_contract() -> None:
    module = _load_module()

    sql, params = module.imminent_alerts_sql(
        module.NEW_RELATION,
        user_id=7,
        hours=72,
        limit=200,
    )

    assert "FROM trading_management_envelopes" in sql
    assert "related_alert_id IS NOT NULL" in sql
    assert "status IN ('open', 'closed')" in sql
    assert "ba.alert_tier = 'pattern_imminent'" in sql
    assert "ba.outcome = 'pending'" in sql
    assert params == (7, 72, 7, 7, 200)


def test_stop_decisions_sql_uses_envelope_join_for_user_scope() -> None:
    module = _load_module()

    sql, params = module.stop_decisions_sql(
        module.NEW_RELATION,
        user_id=None,
        trade_id=123,
        limit=50,
    )

    assert "FROM trading_stop_decisions" in sql
    assert "WITH recent AS" in sql
    assert "JOIN trading_management_envelopes t ON t.id = d.trade_id" in sql
    assert "t.user_id IS NOT DISTINCT FROM" in sql
    assert params == (123, 123, None, 50)


def test_parity_check_normalizes_datetime_rows() -> None:
    module = _load_module()
    ts = datetime(2026, 5, 30, 17, 55)

    check = module.ParityCheck(
        name="demo",
        old_rows=[{"id": 1, "created_at": ts}],
        new_rows=[{"created_at": "2026-05-30T17:55:00", "id": 1}],
    )

    assert check.matched is True
