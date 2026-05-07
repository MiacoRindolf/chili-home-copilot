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
    surrounding = options_src[max(0, idx - 800):idx]
    # The gate must require ``not reason`` (so native DTE/premium/stop
    # triggers always win on tie). After
    # f-exit-monitor-quote-guard-unification the post-refusal piece
    # routes through the shared ``should_consult_monitor_after_refusal``
    # helper -- the `not reason` short-circuit still has to be present
    # so the helper isn't even consulted when a native trigger fired.
    assert "if not reason " in surrounding or "if not reason:" in surrounding, (
        "the `reason = \"pattern_exit_now\"` assignment must be inside "
        "a block that requires `not reason` so native DTE/premium/stop "
        "triggers win when both fire"
    )
    # And the post-refusal gate must call the shared helper (refusal-
    # aware) -- catches a future refactor that drops the gate or
    # re-implements it inline.
    assert "should_consult_monitor_after_refusal" in surrounding, (
        "the options call site must route the post-refusal gate "
        "through `should_consult_monitor_after_refusal` from "
        "_exit_monitor_common; inline gate is no longer the contract"
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


# ---------------------------------------------------------------------------
# f-fix-implausible-quote-vs-exit_now-ordering (2026-05-06):
# the options _evaluate_exit_triggers now returns (reason, abstained_implausible)
# so the call site can refuse to consult the LLM advisory when the
# implausible-quote guard fires. Pin both the new tuple return AND the
# call-site gate.
# ---------------------------------------------------------------------------

def test_evaluate_exit_triggers_implausible_quote_returns_abstained_true():
    """Pins the contract: when current/entry ratio is < 0.1 (or > 10),
    the function returns ``(None, True)``. The True flag tells the
    caller NOT to fall through to the LLM advisory."""
    from app.services.trading.options.exit_monitor import _evaluate_exit_triggers

    # entry premium $0.50, current premium $0.001 -> ratio 0.002 (< 0.1)
    reason, abstained = _evaluate_exit_triggers(
        dte=30, entry_premium=0.50, current_premium=0.001,
        dte_threshold=7, stop_pct=50.0, tp_pct=100.0,
    )
    assert reason is None
    assert abstained is True


def test_evaluate_exit_triggers_normal_path_returns_abstained_false():
    """Regression: ordinary "no trigger fired" path must return
    ``(None, False)`` so the LLM advisory IS consulted when nothing
    native fires. Otherwise the gate would over-fire and silently
    suppress legitimate pattern_exit_now closes."""
    from app.services.trading.options.exit_monitor import _evaluate_exit_triggers

    # entry $0.50, current $0.45 -> ratio 0.9, change -10%, no trigger.
    reason, abstained = _evaluate_exit_triggers(
        dte=30, entry_premium=0.50, current_premium=0.45,
        dte_threshold=7, stop_pct=50.0, tp_pct=100.0,
    )
    assert reason is None
    assert abstained is False


def test_options_call_site_gates_monitor_on_abstained_implausible():
    """Source guard: the call site in run_options_exit_pass MUST
    consult fresh_monitor_exit_meta only when the shared
    ``should_consult_monitor_after_refusal`` helper allows it. Catches
    future refactors that drop the gate or re-introduce an inline copy.
    """
    options_src = (REPO / "app/services/trading/options/exit_monitor.py").read_text()
    assert "abstained_implausible" in options_src
    # After f-exit-monitor-quote-guard-unification (2026-05-06), the
    # gate routes through the shared helper. The helper is imported
    # AND called with both pieces of state.
    assert "should_consult_monitor_after_refusal" in options_src
    assert "abstained_implausible=abstained_implausible" in options_src
