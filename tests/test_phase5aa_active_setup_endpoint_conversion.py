from __future__ import annotations

import inspect

from app.routers.trading_sub import monitor


def test_active_setup_endpoint_uses_envelope_runtime_helper_not_trade_query() -> None:
    source = inspect.getsource(monitor.api_monitor_active)
    helper_source = inspect.getsource(
        monitor._monitored_active_setup_envelopes_with_suppressed
    )

    assert "_monitored_active_setup_envelopes_with_suppressed" in source
    assert "load_open_active_setup_envelope_objects" in helper_source
    assert "filter_broker_stale_open_trades" in helper_source
    assert "db.query(Trade)" not in helper_source


def test_monitor_run_stays_on_legacy_trade_helper_for_live_actions() -> None:
    source = inspect.getsource(monitor.api_monitor_run)
    live_helper_source = inspect.getsource(monitor._monitored_open_trades)

    assert "_monitored_open_trades" in source
    assert "_monitored_live_trades_with_suppressed" in live_helper_source
    assert "load_open_active_setup_envelope_objects" not in live_helper_source


def test_active_setup_public_payload_contract_stays_pinned() -> None:
    source = inspect.getsource(monitor.api_monitor_active)

    for field in (
        '"summary"',
        '"setups"',
        '"suppressed_stale_trades"',
        '"trade_id": trade.id',
        '"ticker": trade.ticker',
        '"direction": trade.direction',
        '"entry_price": json_safe(display_entry)',
        '"quantity": json_safe(display_quantity)',
        '"broker_truth_entry_price": json_safe(broker_metrics.get("entry_price"))',
        '"latest_decision": _serialize_decision(latest) if latest else None',
        '"execution_state": exec_meta.get("execution_state")',
    ):
        assert field in source
