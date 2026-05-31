from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.routers.trading_sub import monitor


def test_monitor_decisions_uses_envelope_helper_not_trade_join() -> None:
    source = inspect.getsource(monitor.api_monitor_decisions)

    assert "load_monitor_decision_envelope_rows" in source
    assert "join(" not in source
    assert "db.query(Trade)" not in source
    assert '"decisions": json_safe(out)' in source
    assert '"ticker": row.get("ticker")' in source
    assert '"direction": row.get("direction")' in source


def test_imminent_alerts_uses_actioned_envelope_helper_not_trade_exists() -> None:
    source = inspect.getsource(monitor.api_monitor_imminent_alerts)

    assert "load_imminent_alert_actioned_envelope_ids" in source
    assert "exists()" not in source
    assert "Trade.related_alert_id" not in source
    assert "~BreakoutAlert.id.in_(actioned_alert_ids)" in source
    assert '"alerts": json_safe(items)' in source


def test_decision_serializer_accepts_envelope_row_namespace() -> None:
    decision = SimpleNamespace(
        id=1,
        trade_id=2,
        breakout_alert_id=3,
        scan_pattern_id=4,
        health_score=0.75,
        health_delta=0.05,
        conditions_snapshot={"monitor_health_source": "setup_vitals"},
        action="hold",
        old_stop=None,
        new_stop=9.5,
        old_target=None,
        new_target=12.0,
        llm_confidence=0.0,
        llm_reasoning="llm unavailable",
        mechanical_action="hold",
        mechanical_stop=None,
        mechanical_target=None,
        decision_source="llm",
        price_at_decision=10.0,
        price_after_1h=None,
        price_after_4h=None,
        was_beneficial=None,
        created_at=None,
    )

    row = monitor._serialize_decision(decision)

    assert row["trade_id"] == 2
    assert row["health_score_pct"] == 75.0
    assert row["health_source"] == "setup_vitals"
    assert row["decision_source"] == "llm_unavailable"
    assert row["llm_reasoning"] is None
