"""Tests for f-crypto-exit-monitor-pattern-exit-now-test.

Pins the pattern-monitor ``exit_now`` branch wired into
``crypto/exit_monitor.run_crypto_exit_pass`` on 2026-05-06 (live-debug
fix that shipped without unit tests). Mirrors the equity-lane suite at
``tests/test_auto_trader_monitor.py:338-454`` plus two crypto-specific
cases (price-trigger-on-tie, implausible-quote-vs-exit_now).

Five behavioural cases + one source guard. Each behavioural case
commits a real Trade + PatternMonitorDecision pair to the test DB and
mocks only the broker / quote / governance edges -- same pattern as
the equity lane.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import models
from app.models.trading import PatternMonitorDecision, Trade

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Source guard (Case 6) -- alias resolution
# ---------------------------------------------------------------------------

def test_crypto_local_alias_resolves_to_shared_callable():
    """Refactor regression: the private re-exports in
    ``crypto/exit_monitor.py`` MUST point at the shared
    ``_exit_monitor_common`` symbols. Catches the next time someone
    re-introduces a local copy."""
    from app.services.trading.crypto import exit_monitor as crypto_exit
    from app.services.trading import _exit_monitor_common as common

    assert crypto_exit._latest_monitor_decisions_by_trade is common.latest_monitor_decisions_by_trade
    assert crypto_exit._fresh_monitor_exit_meta is common.fresh_monitor_exit_meta


# ---------------------------------------------------------------------------
# Shared helpers for behavioural cases
# ---------------------------------------------------------------------------

def _seed_open_crypto_trade(
    db, *, ticker: str = "TRUMP-USD", name_suffix: str
) -> Trade:
    """Seed an open crypto Trade. ``name_suffix`` makes the User name
    per-test-unique so collisions don't cascade across cases when a
    prior run leaked rows."""
    u = models.User(name=f"crypto_exit_{name_suffix}_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.0,
        take_profit=14.0,
        broker_source="robinhood",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_decision(
    db,
    trade_id: int,
    *,
    action: str,
    age: timedelta = timedelta(minutes=5),
    price: float = 10.40,
) -> PatternMonitorDecision:
    d = PatternMonitorDecision(
        trade_id=trade_id,
        health_score=0.15 if action == "exit_now" else 0.70,
        action=action,
        decision_source="plan_levels",
        price_at_decision=price,
        created_at=datetime.utcnow() - age,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _run_with_mocks(
    db,
    *,
    quote_price: float | None,
    broker_qty: float = 5.0,
    sell_order_id: str = "test-oid-1",
):
    """Patch only the edges (quote, broker) and run a single pass."""
    sell_mock = MagicMock(
        return_value={"ok": True, "raw": {"id": sell_order_id}}
    )
    positions_mock = MagicMock(
        return_value=[{"ticker": "TRUMP-USD", "quantity": broker_qty}]
    )
    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=quote_price,
    ), patch(
        "app.services.broker_service.place_crypto_sell_order",
        sell_mock,
    ), patch(
        "app.services.broker_service.get_crypto_positions",
        positions_mock,
    ), patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        from app.services.trading.crypto.exit_monitor import run_crypto_exit_pass
        out = run_crypto_exit_pass(db)
    return out, sell_mock, positions_mock


# ---------------------------------------------------------------------------
# Case 1 -- closes on fresh exit_now (price between stop and target)
# ---------------------------------------------------------------------------

def test_case1_closes_on_fresh_exit_now(db):
    t = _seed_open_crypto_trade(db, name_suffix="case1")
    _seed_decision(db, t.id, action="exit_now")

    out, sell_mock, _ = _run_with_mocks(db, quote_price=10.40)

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.pending_exit_order_id == "test-oid-1"
    # Canonical literal -- protect against truncation regressions.
    assert t.pending_exit_reason == "pattern_exit_now"
    assert t.pending_exit_status == "submitted"
    assert t.pending_exit_requested_at is not None
    sell_mock.assert_called_once_with(
        ticker=t.ticker, quantity=5.0, order_type="market"
    )


# ---------------------------------------------------------------------------
# Case 2 -- newer hold supersedes older exit_now
# ---------------------------------------------------------------------------

def test_case2_latest_hold_supersedes_older_exit_now(db):
    t = _seed_open_crypto_trade(db, name_suffix="case2")
    _seed_decision(db, t.id, action="exit_now", age=timedelta(hours=2))
    _seed_decision(db, t.id, action="hold", age=timedelta(minutes=5))

    out, sell_mock, _ = _run_with_mocks(db, quote_price=10.40)

    assert out.get("closed") == 0
    db.refresh(t)
    assert t.pending_exit_order_id is None
    sell_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Case 3 -- exit_now older than freshness window does not trigger
# ---------------------------------------------------------------------------

def test_case3_stale_exit_now_does_not_trigger(db):
    """Beyond the shared 96h ``MONITOR_EXIT_NOW_MAX_AGE_HOURS``."""
    t = _seed_open_crypto_trade(db, name_suffix="case3")
    _seed_decision(db, t.id, action="exit_now", age=timedelta(hours=100))

    out, sell_mock, _ = _run_with_mocks(db, quote_price=10.40)

    assert out.get("closed") == 0
    db.refresh(t)
    assert t.pending_exit_order_id is None
    sell_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Case 4 -- price triggers fire even when exit_now is also fresh
# ---------------------------------------------------------------------------

def test_case4_native_stop_trigger_wins_on_tie(db):
    """Stop-on-tie ordering: when ``_evaluate_exit_triggers`` returns
    a reason (price below stop), the native trigger wins. The
    ``exit_now`` consultation only runs when ``should_exit=False``."""
    t = _seed_open_crypto_trade(db, name_suffix="case4")
    _seed_decision(db, t.id, action="exit_now")

    out, sell_mock, _ = _run_with_mocks(db, quote_price=8.50)

    assert out.get("closed") == 1
    db.refresh(t)
    # Native price-trigger reason wins; NOT pattern_exit_now.
    assert t.pending_exit_reason is not None
    assert t.pending_exit_reason.startswith("stop_loss_hit")
    assert "pattern_exit_now" not in (t.pending_exit_reason or "")
    sell_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Case 5 -- implausible-quote guard MUST win over fresh exit_now
# ---------------------------------------------------------------------------

def test_case5_implausible_quote_guard_wins_over_exit_now(db):
    """When the price feed is poisoned (px ~ 0.00003x entry), the
    implausible-quote guard inside ``_evaluate_exit_triggers`` short-
    circuits to ``should_exit=False, reason='no_trigger:implausible_quote'``.
    A fresh ``exit_now`` must NOT override that refusal -- the LLM
    may be reading a different (clean) feed than the exit-engine, and
    acting on its recommendation while the engine itself doesn't trust
    its own price is a different kind of foot-gun than acting on the
    bad price.

    f-fix-implausible-quote-vs-exit_now-ordering (2026-05-06): the
    fix gates ``fresh_monitor_exit_meta`` consultation on the refusal
    prefix. xfail removed; assertion now passes."""
    t = _seed_open_crypto_trade(db, name_suffix="case5")
    _seed_decision(db, t.id, action="exit_now")

    # entry $10, ratio 0.00003 -- below the 0.1x threshold inside
    # _evaluate_exit_triggers, so it returns no_trigger:implausible_quote.
    out, sell_mock, _ = _run_with_mocks(db, quote_price=0.0003)

    assert out.get("closed") == 0
    db.refresh(t)
    assert t.pending_exit_order_id is None
    sell_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Upstream contract pin: _evaluate_exit_triggers refusal prefix
# ---------------------------------------------------------------------------

def test_evaluate_exit_triggers_implausible_quote_prefix():
    """Pins the contract that Phase 1's prefix-match gate relies on:
    when ``entry > 0`` and ``px / entry < 0.1``, the function returns
    ``(False, "no_trigger:implausible_quote ...")``. If this contract
    ever changes (different reason wording, sentinel restructure),
    the gate in ``run_crypto_exit_pass`` would silently regress."""
    from app.services.trading.crypto.exit_monitor import _evaluate_exit_triggers

    should_exit, reason = _evaluate_exit_triggers(
        px=0.0003, entry=10.0, stop=9.0, target=14.0, direction="long",
    )
    assert should_exit is False
    assert reason.startswith("no_trigger:implausible_quote")


# ---------------------------------------------------------------------------
# Case 5b -- ordinary "no_trigger" + fresh exit_now -> closes
# ---------------------------------------------------------------------------

def test_case5b_no_trigger_plus_fresh_exit_now_still_closes(db):
    """Regression: the Phase 1 gate must NOT extend its refusal to the
    ordinary "no_trigger" reason (price between stop and target with no
    plausibility issue). That was Case 1's success path; this case
    re-exercises it after the gate to confirm the gate's scope is
    surgical (only the implausible-quote refusal blocks consultation)."""
    t = _seed_open_crypto_trade(db, name_suffix="case5b")
    _seed_decision(db, t.id, action="exit_now")

    # entry $10, px $11 -- ratio 1.1, well within (0.1, 10), so
    # _evaluate_exit_triggers returns (False, "no_trigger") and the
    # monitor consultation IS allowed to fire.
    out, sell_mock, _ = _run_with_mocks(
        db, quote_price=11.00, sell_order_id="test-oid-5b"
    )

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.pending_exit_reason == "pattern_exit_now"
    assert t.pending_exit_order_id == "test-oid-5b"
    sell_mock.assert_called_once()
