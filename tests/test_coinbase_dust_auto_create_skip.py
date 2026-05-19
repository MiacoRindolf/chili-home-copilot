"""f-coinbase-dust-auto-create-skip (2026-05-19) tests.

Verifies the dust-notional guard is wired in
``coinbase_service.sync_positions_to_db`` and that the constant has a
sensible value. Without this guard, dust wallet holdings (0.269 ACS at
$0.00019 = $0.00005 notional) get auto-created as Trade rows that
then loop through ``coinbase_position_sync_gone`` close/re-create
cycles, burning the autotrader's Coinbase notional cap and obscuring
real placement intent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


SVC_PATH = Path(__file__).parent.parent / "app" / "services" / "coinbase_service.py"


def _read_svc_source() -> str:
    return SVC_PATH.read_text(encoding="utf-8")


def test_dust_min_constant_exists() -> None:
    src = _read_svc_source()
    assert "_MIN_AUTO_CREATE_NOTIONAL_USD" in src, (
        "Expected the dust-notional guard constant in coinbase_service.py"
    )


def test_dust_min_constant_sensible_value() -> None:
    """Constant must be >= $1 (Coinbase's typical quote_min_size) and
    <= $50 (above which we'd over-block legitimate autosyncs).
    """
    from app.services import coinbase_service as svc
    assert hasattr(svc, "_MIN_AUTO_CREATE_NOTIONAL_USD")
    val = float(svc._MIN_AUTO_CREATE_NOTIONAL_USD)
    assert 1.0 <= val <= 50.0, (
        f"_MIN_AUTO_CREATE_NOTIONAL_USD = {val} outside sensible [1, 50] range"
    )


def test_sync_positions_has_dust_notional_check() -> None:
    """The function must compute notional = avg_price * qty and skip
    auto-create when below the threshold."""
    src = _read_svc_source()
    # Find the sync_positions_to_db function body
    m = re.search(
        r"def sync_positions_to_db\(.*?\n(?=\Z|\ndef\s)",
        src,
        flags=re.DOTALL,
    )
    assert m is not None, "sync_positions_to_db not found"
    fn_src = m.group(0)
    # Must reference the dust constant
    assert "_MIN_AUTO_CREATE_NOTIONAL_USD" in fn_src, (
        "sync_positions_to_db must consult the dust-notional threshold"
    )
    # Must compute notional and compare
    assert "notional_usd" in fn_src, (
        "sync_positions_to_db must compute notional_usd before auto-create"
    )


def test_dust_skip_appears_before_trade_construction() -> None:
    """The dust check must be wired BEFORE the Trade(...) construction
    so dust positions never become Trade rows."""
    src = _read_svc_source()
    m = re.search(
        r"def sync_positions_to_db\(.*?\n(?=\Z|\ndef\s)",
        src,
        flags=re.DOTALL,
    )
    fn_src = m.group(0)
    dust_idx = fn_src.find("_MIN_AUTO_CREATE_NOTIONAL_USD")
    trade_construct_idx = fn_src.find("trade = Trade(")
    assert dust_idx != -1 and trade_construct_idx != -1, (
        "Expected to find both the dust check and the Trade() constructor"
    )
    assert dust_idx < trade_construct_idx, (
        "Dust-notional check must precede Trade() construction so dust "
        "positions are filtered out, not created and then filtered."
    )


def test_dust_skip_warning_logged() -> None:
    """The skip path must log a warning so operator can see why the
    position wasn't materialized."""
    src = _read_svc_source()
    m = re.search(
        r"def sync_positions_to_db\(.*?\n(?=\Z|\ndef\s)",
        src,
        flags=re.DOTALL,
    )
    fn_src = m.group(0)
    # Crude check: a logger.warning that mentions 'dust' should be present
    # near the dust check
    assert "dust" in fn_src.lower(), (
        "Expected the dust-skip path to mention 'dust' in its warning"
    )
    assert "logger.warning" in fn_src, (
        "Expected a logger.warning call in sync_positions_to_db"
    )
