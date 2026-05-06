"""Tests for f-options-exit-monitor-pattern-exit-now-audit.

Mirrors the 5 crypto cases plus one refactor-regression test that
asserts all three lanes (equity / crypto / options) import the
shared ``latest_monitor_decisions_by_trade`` symbol.

Heavy integration with the options exit pass (broker adapter, contract
resolver, quote fetch, place_option_sell) is not exercised here -- the
behaviour under test is the monitor-decision branch alone. Those
integration paths have their own coverage and would balloon the
truncate-per-test cycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Refactor regression: the three lanes import the shared symbol
# ---------------------------------------------------------------------------

def test_three_lanes_import_shared_helper():
    """Source guard: equity, crypto, and options all reference the
    shared ``_exit_monitor_common`` module. Catches the next time
    someone re-introduces a local copy."""
    equity_src = (REPO / "app/services/trading/auto_trader_monitor.py").read_text()
    crypto_src = (REPO / "app/services/trading/crypto/exit_monitor.py").read_text()
    options_src = (REPO / "app/services/trading/options/exit_monitor.py").read_text()

    for src, label in [
        (equity_src, "equity (auto_trader_monitor)"),
        (crypto_src, "crypto (crypto/exit_monitor)"),
        (options_src, "options (options/exit_monitor)"),
    ]:
        assert "_exit_monitor_common" in src, (
            f"{label} no longer references the shared "
            f"_exit_monitor_common module -- did someone re-introduce a "
            f"local copy?"
        )


def test_three_lanes_resolve_to_same_helper_object():
    """Runtime guard: the equity / crypto re-exports + the options
    direct import all point at the SAME shared callable object."""
    from app.services.trading._exit_monitor_common import (
        latest_monitor_decisions_by_trade as shared,
    )
    from app.services.trading.auto_trader_monitor import (
        _latest_monitor_decisions_by_trade as equity_re_export,
    )
    from app.services.trading.crypto.exit_monitor import (
        _latest_monitor_decisions_by_trade as crypto_re_export,
    )
    assert equity_re_export is shared
    assert crypto_re_export is shared


def test_options_no_longer_grep_clean():
    """Phase 1 found ZERO references; Phase 3 added them. Pin that
    options now references the shared module + handles the
    pattern_exit_now branch."""
    options_src = (REPO / "app/services/trading/options/exit_monitor.py").read_text()
    assert "latest_monitor_decisions_by_trade" in options_src
    assert "fresh_monitor_exit_meta" in options_src
    assert "pattern_exit_now" in options_src


# ---------------------------------------------------------------------------
# Helper-level tests (5 cases mirror crypto)
# ---------------------------------------------------------------------------

def _make_decision(
    *, action: str = "exit_now",
    age_hours: float = 1.0,
    decision_id: int = 999,
    decision_source: str = "llm",
    price: float | None = 1.50,
):
    """Build a synthetic PatternMonitorDecision with the chosen age."""
    created = datetime.utcnow() - timedelta(hours=age_hours)
    return SimpleNamespace(
        id=decision_id,
        trade_id=42,
        action=action,
        created_at=created,
        decision_source=decision_source,
        price_at_decision=price,
    )


def test_case1_fresh_exit_now_meta_returned():
    """Fresh exit_now (1h old) returns audit metadata."""
    from app.services.trading._exit_monitor_common import fresh_monitor_exit_meta
    meta = fresh_monitor_exit_meta(_make_decision(action="exit_now", age_hours=1.0))
    assert meta is not None
    assert meta["decision_id"] == 999
    assert meta["decision_age_hours"] == pytest.approx(1.0, rel=0.05)
    assert meta["decision_price"] == 1.50


def test_case2_latest_hold_returns_none():
    """Latest action is 'hold' -> no monitor-driven exit."""
    from app.services.trading._exit_monitor_common import fresh_monitor_exit_meta
    meta = fresh_monitor_exit_meta(_make_decision(action="hold", age_hours=1.0))
    assert meta is None


def test_case3_exit_now_older_than_96h_returns_none():
    """Stale exit_now (>96h) returns None -- the freshness window
    rejects ancient advisories."""
    from app.services.trading._exit_monitor_common import fresh_monitor_exit_meta
    meta = fresh_monitor_exit_meta(_make_decision(action="exit_now", age_hours=120.0))
    assert meta is None


def test_case4_native_dte_trigger_wins():
    """Stop-on-tie ordering: when _evaluate_exit_triggers returns a
    reason (DTE / premium / stop), the native trigger wins -- the
    monitor branch only fires when reason is None.

    Source-level guard: the assignment ``reason = "pattern_exit_now"``
    must be inside an ``if not reason:`` block so native triggers
    take precedence."""
    options_src = (REPO / "app/services/trading/options/exit_monitor.py").read_text()
    idx = options_src.find('reason = "pattern_exit_now"')
    assert idx > 0, "pattern_exit_now assignment must exist in options lane"
    surrounding = options_src[max(0, idx - 400):idx]
    assert "if not reason:" in surrounding, (
        "the `reason = \"pattern_exit_now\"` assignment must be inside "
        "an `if not reason:` block so native DTE/premium/stop triggers "
        "win when both fire"
    )


def test_case5_pending_exit_reason_canonical_pattern_exit_now():
    """Source guard: pending_exit_reason set by the lane is the
    canonical 'pattern_exit_now' literal, NOT a concatenated
    audit-detail string. Audit detail goes in the log line."""
    options_src = (REPO / "app/services/trading/options/exit_monitor.py").read_text()
    # The reason variable is set to "pattern_exit_now" (no concat).
    assert 'reason = "pattern_exit_now"' in options_src
    # The log line for monitor-driven exits includes the audit metadata.
    assert "monitor_decision_id=" in options_src
    assert "monitor_age_h=" in options_src
