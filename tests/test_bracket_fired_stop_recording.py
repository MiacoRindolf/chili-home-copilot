"""f-bracket-fired-stop-recording (2026-05-19) — pin the writer hooks
that record sell-side execution_events when broker-fired stops fill OR
the stop_engine auto-exec path submits a sell.

The Coinbase / Robinhood-crypto exit-monitor writers (shipped
2026-05-19 in f-coinbase-exit-side-recording) covered the
``crypto/exit_monitor.py`` path. The Robinhood polling-driven exit
writer (shipped 2026-05-18 in f-execution-events-sell-side-recording)
covered ``sync_pending_exit_order``. The remaining sell-event blind
spots were:

1. ``broker_service.py`` Robinhood stale-close branch — when a
   bracket-fired stop autonomously fires at the broker, the position
   vanishes, ``sync_positions_to_db`` sees the stale-open Trade row
   and auto-closes with ``exit_reason='broker_reconcile_position_gone'``.
   Mirrors the Coinbase version. Now writes a sell event.

2. ``stop_engine.py`` auto-exec path — when an alert hits a stop
   level, ``maybe_execute_alert_sell`` calls ``bm.sell(...)`` directly
   (NOT via ``pending_exit_order_id``), so the
   ``sync_pending_exit_order`` writer at
   ``robinhood_exit_execution.py:1267`` does NOT fire for it. Now
   writes a sell event directly.

Both pinned by static-grep + try/except wrapper checks, same pattern
as ``test_coinbase_exit_side_recording.py``.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def _writer_wrapped_in_try_except(text: str, import_pat: str) -> bool:
    """Mirror of the helper from test_coinbase_exit_side_recording.py:
    walks line-by-line so dict literals don't false-negative."""
    lines = text.splitlines()
    n = len(lines)
    for i, ln in enumerate(lines):
        if import_pat not in ln:
            continue
        try_found = any(
            lines[j].rstrip().endswith("try:")
            for j in range(max(0, i - 8), i)
        )
        if not try_found:
            continue
        writer_found = False
        except_found = False
        for k in range(i, min(n, i + 60)):
            if "record_execution_event(" in lines[k]:
                writer_found = True
            if writer_found and re.match(r"\s*except\b", lines[k]):
                except_found = True
                break
        if writer_found and except_found:
            return True
    return False


# ── #1 — broker_service.py Robinhood stale-close writer ───────────────


def test_broker_service_stale_close_writes_sell_side_event():
    """``broker_service.sync_positions_to_db`` Robinhood stale-close
    branch must call ``record_execution_event`` after the
    ``broker_reconcile_position_gone`` auto-close, with payload
    ``{'side': 'sell', 'synthetic': True, ...}``."""
    text = _read("app/services/broker_service.py")

    # The Coinbase writer is in coinbase_service.py; this test is for
    # the Robinhood twin in broker_service.py. The signature is
    # event_type='broker_reconcile_position_gone_close'.
    assert "broker_reconcile_position_gone_close" in text, (
        "broker_service.py is missing the "
        "`broker_reconcile_position_gone_close` event_type label. "
        "f-bracket-fired-stop-recording mandates this writer so "
        "broker-fired bracket stops on RH equity get recorded as "
        "sell events for Phase 4."
    )

    assert re.search(r'"side"\s*:\s*"sell"', text), (
        "broker_service.py is missing the payload `\"side\": \"sell\"` "
        "entry. The Phase 4 helper queries `payload_json->>'side' = 'sell'`."
    )

    assert re.search(r'"synthetic"\s*:\s*True', text), (
        "broker_service.py is missing the `\"synthetic\": True` payload "
        "flag for the stale-close path. This distinguishes broker-truth-"
        "driven auto-closes from explicit submitted sells."
    )


def test_broker_service_stale_close_writer_wrapped_in_try():
    """The writer must NEVER block the auto-close flow."""
    text = _read("app/services/broker_service.py")
    assert _writer_wrapped_in_try_except(
        text, "from .trading.execution_audit import record_execution_event"
    ), (
        "broker_service.py record_execution_event call (in the stale-"
        "close path) is not wrapped in try/except. Required so a writer "
        "DB error never blocks the close."
    )


# ── #2 — stop_engine.py auto-exec writer ──────────────────────────────


def test_stop_engine_auto_exec_writes_sell_side_event():
    """``stop_engine.maybe_execute_alert_sell`` calls ``bm.sell(...)``
    directly. The Robinhood ``sync_pending_exit_order`` writer DOES NOT
    fire for this path (no ``pending_exit_order_id``). So the writer
    must be inline here, with event_type='stop_engine_auto_sell'."""
    text = _read("app/services/trading/stop_engine.py")

    assert "stop_engine_auto_sell" in text, (
        "stop_engine.py is missing the `stop_engine_auto_sell` event_type "
        "label. f-bracket-fired-stop-recording mandates this writer so "
        "stop-engine-initiated sells get recorded as sell events for "
        "Phase 4."
    )

    assert re.search(r'"side"\s*:\s*"sell"', text), (
        "stop_engine.py is missing the payload `\"side\": \"sell\"` "
        "entry. The Phase 4 helper queries `payload_json->>'side' = 'sell'`."
    )


def test_stop_engine_writer_wrapped_in_try():
    """The writer must NEVER block the sell flow."""
    text = _read("app/services/trading/stop_engine.py")
    assert _writer_wrapped_in_try_except(
        text, "from .execution_audit import record_execution_event"
    ), (
        "stop_engine.py record_execution_event call is not wrapped in "
        "try/except. Required so a writer DB error never blocks an "
        "already-submitted sell."
    )
