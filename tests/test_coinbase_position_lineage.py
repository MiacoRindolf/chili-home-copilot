"""Coinbase broker-sync position-lineage guards.

Coinbase ``sync_positions_to_db`` can discover a real exchange holding
without seeing the original order/fill event. That path must still seed the
position-identity sidecar and a synthetic BUY event so Phase 4/5 readers have
lineage for the broker inventory.
"""
from __future__ import annotations

import re
from pathlib import Path


SVC_PATH = Path(__file__).parent.parent / "app" / "services" / "coinbase_service.py"


def _sync_source() -> str:
    src = SVC_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"def sync_positions_to_db\(.*?\n(?=\n\ndef\s|\Z)",
        src,
        flags=re.DOTALL,
    )
    assert m is not None
    return m.group(0)


def test_coinbase_sync_writes_position_identity_sidecar() -> None:
    src = SVC_PATH.read_text(encoding="utf-8")
    assert "def _ensure_coinbase_position_identity" in src
    assert "_phase1_record_position_observation" in src
    assert "current_envelope_id" in src
    assert "UPDATE trading_bracket_intents" in src


def test_sync_positions_calls_lineage_helpers_on_update_and_create() -> None:
    fn_src = _sync_source()
    assert fn_src.count("_ensure_coinbase_position_identity(") >= 2
    assert fn_src.count("_ensure_coinbase_sync_entry_event(") >= 2
    assert "canonical_user_id" in fn_src
    assert "existing.user_id = canonical_user_id" in fn_src


def test_synthetic_entry_event_is_idempotent_and_marked_synthetic() -> None:
    src = SVC_PATH.read_text(encoding="utf-8")
    assert "coinbase_position_sync_entry" in src
    assert "SELECT id FROM trading_execution_events" in src
    assert "AND position_id IS NULL" in src
    assert '"side": "buy"' in src
    assert '"synthetic": True' in src


def test_coinbase_empty_snapshot_does_not_mass_close_open_trades() -> None:
    fn_src = _sync_source()
    assert "returned zero live tickers" in fn_src
    assert "skipping stale-close" in fn_src
    assert "stale = []" in fn_src


def test_coinbase_ticker_level_snapshot_gap_checks_open_orders() -> None:
    src = SVC_PATH.read_text(encoding="utf-8")
    fn_src = _sync_source()
    assert "def _coinbase_has_working_sell_orders" in src
    assert "_coinbase_has_working_sell_orders(trade.ticker)" in fn_src
    assert "positions snapshot" in fn_src
    assert "working sell order" in fn_src
