from __future__ import annotations

import inspect

from app import migrations


def test_active_link_guard_migration_registered_after_agent_os() -> None:
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]

    assert "285_project_autonomy_agent_os_v1" in ids
    assert "286_position_identity_active_link_guard" in ids
    assert ids.index("286_position_identity_active_link_guard") == (
        ids.index("285_project_autonomy_agent_os_v1") + 1
    )


def test_active_link_guard_trigger_uses_only_broker_live_positions() -> None:
    src = inspect.getsource(migrations._migration_286_position_identity_active_link_guard)

    assert "CREATE OR REPLACE FUNCTION trading_trades_phase5a_after_insert" in src
    assert "p.state = 'open'" in src
    assert "ABS(COALESCE(p.current_quantity, 0)) > 0" in src
    assert "LOWER(COALESCE(t.status, '')) IN" in src
    assert "COALESCE(t.filled_quantity, 0) <= 0" in src
    assert "last_diff_reason = 'position_identity_active_link_guard'" in src
