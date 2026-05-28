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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import models
from app.models.trading import PatternMonitorDecision, Trade

REPO = Path(__file__).resolve().parent.parent
TEST_SECONDS_PER_MINUTE = 60
TEST_MISSING_QTY_BACKOFF_START_STREAK = 1
TEST_MISSING_QTY_BACKOFF_MINUTES = 10
TEST_MISSING_QTY_BACKOFF_SECONDS = (
    TEST_MISSING_QTY_BACKOFF_MINUTES * TEST_SECONDS_PER_MINUTE
)


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


def test_current_crypto_price_prefers_trade_broker_quote(monkeypatch):
    from app.services.trading.crypto import exit_monitor as crypto_exit
    from app.services.trading import market_data

    adapter = SimpleNamespace(
        is_enabled=lambda: True,
        get_ticker=lambda _ticker: (
            SimpleNamespace(bid=14.25, ask=14.40, mid=14.325, last_price=14.30),
            None,
        ),
    )
    monkeypatch.setattr(
        "app.services.trading.venue.factory.get_adapter",
        lambda broker_source: adapter if broker_source == "coinbase" else None,
    )
    monkeypatch.setattr(
        market_data,
        "fetch_quote",
        lambda _ticker: pytest.fail("market_data fallback should not be used"),
    )

    assert crypto_exit._current_crypto_price(
        "ADA-USD",
        broker_source="coinbase",
        direction="long",
    ) == 14.25


# ---------------------------------------------------------------------------
# Shared helpers for behavioural cases
# ---------------------------------------------------------------------------

def _seed_open_crypto_trade(
    db,
    *,
    ticker: str = "TRUMP-USD",
    name_suffix: str,
    broker_source: str = "robinhood",
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
        broker_source=broker_source,
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
# Coinbase venue routing -- stop hit exits on Coinbase, not Robinhood
# ---------------------------------------------------------------------------

def test_coinbase_stop_hit_uses_coinbase_position_and_sell(db):
    """Regression for 2026-05-12 live issue: a Coinbase-owned crypto
    trade that hits its stop must use Coinbase position truth and a
    Coinbase market sell. The prior monitor routed every crypto exit
    through Robinhood, so Coinbase stop hits were visible as alerts but
    never exited."""
    t = _seed_open_crypto_trade(
        db,
        ticker="ADA-USD",
        name_suffix="coinbase_stop",
        broker_source="coinbase",
    )

    cb_positions = MagicMock(
        return_value=[{"ticker": "ADA-USD", "quantity": 3.0}]
    )
    cb_adapter = MagicMock()
    cb_adapter.place_market_order.return_value = {
        "ok": True,
        "order_id": "cb-exit-1",
        "raw": {},
    }
    rh_positions = MagicMock(
        return_value=[{"ticker": "ADA-USD", "quantity": 5.0}]
    )
    rh_sell = MagicMock(return_value={"ok": True, "raw": {"id": "rh-wrong"}})

    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=8.50,
    ), patch(
        "app.services.coinbase_service.get_positions",
        cb_positions,
    ), patch(
        "app.services.trading.crypto.exit_monitor._coinbase_spot_adapter",
        return_value=cb_adapter,
    ), patch(
        "app.services.broker_service.get_crypto_positions",
        rh_positions,
    ), patch(
        "app.services.broker_service.place_crypto_sell_order",
        rh_sell,
    ), patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        from app.services.trading.crypto.exit_monitor import run_crypto_exit_pass
        out = run_crypto_exit_pass(db)

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.pending_exit_order_id == "cb-exit-1"
    assert t.pending_exit_reason is not None
    assert t.pending_exit_reason.startswith("stop_loss_hit")
    cb_positions.assert_called_once()
    cb_adapter.place_market_order.assert_called_once_with(
        product_id=t.ticker,
        side="sell",
        base_size="3.0",
    )
    cb_adapter.place_limit_order_gtc.assert_not_called()
    rh_positions.assert_not_called()
    rh_sell.assert_not_called()


def test_coinbase_missing_qty_uses_configured_backoff(db, monkeypatch):
    """A Coinbase trade whose broker position cannot be resolved should not
    re-query Coinbase every monitor pass. The first miss records a deferred
    state; subsequent passes inside the configured window skip broker position
    fetches while leaving the live sell gate untouched.
    """
    t = _seed_open_crypto_trade(
        db,
        ticker="DIEM-USD",
        name_suffix="coinbase_missing_qty_backoff",
        broker_source="coinbase",
    )
    monkeypatch.setattr(
        "app.config.settings.chili_autotrader_crypto_exit_missing_qty_backoff_start_streak",
        TEST_MISSING_QTY_BACKOFF_START_STREAK,
        raising=False,
    )
    monkeypatch.setattr(
        "app.config.settings.chili_autotrader_crypto_exit_missing_qty_backoff_seconds",
        TEST_MISSING_QTY_BACKOFF_SECONDS,
        raising=False,
    )

    positions = MagicMock(return_value=[])
    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=8.50,
    ), patch(
        "app.services.coinbase_service.get_positions",
        positions,
    ), patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        from app.services.trading.crypto import exit_monitor as crypto_exit

        out = crypto_exit.run_crypto_exit_pass(db)

    assert out.get("deferred") == 1
    assert out.get("missing_qty_deferred") == 1
    positions.assert_called_once()
    db.refresh(t)
    assert t.crypto_broker_zero_qty_streak == 1
    assert t.pending_exit_status == "deferred"
    assert t.pending_exit_reason == crypto_exit.CRYPTO_EXIT_MISSING_QTY_PENDING_REASON
    meta = t.indicator_snapshot[crypto_exit.CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY]
    assert meta["streak"] == 1
    assert meta["backoff_until"]

    positions_during_backoff = MagicMock(
        side_effect=AssertionError("backoff should skip Coinbase position fetch")
    )
    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=8.50,
    ), patch(
        "app.services.coinbase_service.get_positions",
        positions_during_backoff,
    ), patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        out2 = crypto_exit.run_crypto_exit_pass(db)

    assert out2.get("deferred") == 1
    assert out2.get("missing_qty_backoff_skipped") == 1
    positions_during_backoff.assert_not_called()


def test_coinbase_missing_qty_long_streak_attempts_local_qty_exit(db):
    """Persistent Coinbase account-snapshot misses should not defer forever."""
    from app.services.trading.crypto import exit_monitor as crypto_exit

    t = _seed_open_crypto_trade(
        db,
        ticker="DIEM-USD",
        name_suffix="coinbase_missing_qty_local_fallback",
        broker_source="coinbase",
    )
    t.crypto_broker_zero_qty_streak = (
        crypto_exit.CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_START_STREAK
    )
    t.pending_exit_status = "deferred"
    t.pending_exit_reason = crypto_exit.CRYPTO_EXIT_MISSING_QTY_PENDING_REASON
    t.indicator_snapshot = {
        crypto_exit.CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY: {
            "streak": t.crypto_broker_zero_qty_streak,
            "backoff_until": "2026-01-01T00:00:00",
        }
    }
    db.add(t)
    db.commit()

    cb_positions = MagicMock(return_value=[])
    cb_adapter = MagicMock()
    cb_adapter.place_market_order.return_value = {
        "ok": True,
        "order_id": "cb-local-fallback",
        "raw": {},
    }

    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=8.50,
    ), patch(
        "app.services.coinbase_service.get_positions",
        cb_positions,
    ), patch(
        "app.services.trading.crypto.exit_monitor._coinbase_spot_adapter",
        return_value=cb_adapter,
    ), patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        out = crypto_exit.run_crypto_exit_pass(db)

    assert out.get("closed") == 1
    assert out.get("missing_qty_local_qty_fallback") == 1
    cb_positions.assert_called_once()
    cb_adapter.place_market_order.assert_called_once_with(
        product_id="DIEM-USD",
        side="sell",
        base_size="5.0",
    )
    db.refresh(t)
    assert t.pending_exit_order_id == "cb-local-fallback"
    assert t.pending_exit_status == "submitted"
    assert t.pending_exit_reason is not None
    assert t.pending_exit_reason.startswith("stop_loss_hit")
    assert t.crypto_broker_zero_qty_streak == 0
    assert crypto_exit.CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY not in t.indicator_snapshot


def test_coinbase_stop_hit_cancels_stale_sell_hold_and_retries(db):
    """Coinbase open stop-limit orders hold base currency. When the
    monitor promotes an exit to market, it must cancel stale open sells
    and retry instead of looping on insufficient available balance."""
    t = _seed_open_crypto_trade(
        db,
        ticker="ADA-USD",
        name_suffix="coinbase_stop_retry",
        broker_source="coinbase",
    )

    cb_adapter = MagicMock()
    cb_adapter.place_market_order.side_effect = [
        {"ok": False, "error": "Insufficient balance in source account"},
        {"ok": True, "order_id": "cb-exit-retry", "raw": {}},
    ]
    get_open_orders = MagicMock(
        return_value=[
            {
                "order_id": "cb-old-stop",
                "product_id": "ADA-USD",
                "side": "SELL",
                "status": "OPEN",
            }
        ]
    )
    cancel_order = MagicMock(return_value={"ok": True, "order_id": "cb-old-stop"})

    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=8.50,
    ), patch(
        "app.services.coinbase_service.get_positions",
        return_value=[{"ticker": "ADA-USD", "quantity": 3.0}],
    ), patch(
        "app.services.trading.crypto.exit_monitor._coinbase_spot_adapter",
        return_value=cb_adapter,
    ), patch(
        "app.services.coinbase_service.get_open_orders",
        get_open_orders,
    ), patch(
        "app.services.coinbase_service.cancel_order_by_id",
        cancel_order,
    ), patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        from app.services.trading.crypto.exit_monitor import run_crypto_exit_pass
        out = run_crypto_exit_pass(db)

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.pending_exit_order_id == "cb-exit-retry"
    assert cb_adapter.place_market_order.call_count == 2
    get_open_orders.assert_called_once_with(product_ids=["ADA-USD"])
    cancel_order.assert_called_once_with("cb-old-stop")


def test_coinbase_limit_only_market_rejection_falls_back_to_marketable_limit(
    db,
    monkeypatch,
):
    """Some Coinbase products reject market orders in limit-only mode. A
    stop-hit exit should still flatten risk by submitting a takerable SELL
    limit, while keeping the same product-specific quantity normalization
    path through the Coinbase spot adapter."""
    t = _seed_open_crypto_trade(
        db,
        ticker="DIEM-USD",
        name_suffix="coinbase_limit_only",
        broker_source="coinbase",
    )
    monkeypatch.setattr(
        "app.config.settings.chili_coinbase_exit_limit_fallback_buffer_pct",
        0.02,
        raising=False,
    )

    cb_adapter = MagicMock()
    cb_adapter.place_market_order.return_value = {
        "ok": False,
        "error": "Orderbook is in limit only mode - please use limit order type",
    }
    cb_adapter.place_limit_order_gtc.return_value = {
        "ok": True,
        "order_id": "cb-limit-exit",
        "raw": {},
    }

    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=8.50,
    ), patch(
        "app.services.coinbase_service.get_positions",
        return_value=[{"ticker": "DIEM-USD", "quantity": 3.0}],
    ), patch(
        "app.services.trading.crypto.exit_monitor._coinbase_spot_adapter",
        return_value=cb_adapter,
    ), patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        from app.services.trading.crypto.exit_monitor import run_crypto_exit_pass

        out = run_crypto_exit_pass(db)

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.pending_exit_order_id == "cb-limit-exit"
    cb_adapter.place_market_order.assert_called_once_with(
        product_id="DIEM-USD",
        side="sell",
        base_size="3.0",
    )
    cb_adapter.place_limit_order_gtc.assert_called_once()
    limit_kwargs = cb_adapter.place_limit_order_gtc.call_args.kwargs
    assert limit_kwargs["product_id"] == "DIEM-USD"
    assert limit_kwargs["side"] == "sell"
    assert limit_kwargs["base_size"] == "3.0"
    assert float(limit_kwargs["limit_price"]) == pytest.approx(8.33)
    assert limit_kwargs["post_only"] is False


def test_coinbase_limit_only_fallback_balance_error_cancels_stale_sell_and_retries(
    db,
    monkeypatch,
):
    t = _seed_open_crypto_trade(
        db,
        ticker="DIEM-USD",
        name_suffix="coinbase_limit_only_reserved_balance",
        broker_source="coinbase",
    )
    monkeypatch.setattr(
        "app.config.settings.chili_coinbase_exit_limit_fallback_buffer_pct",
        0.02,
        raising=False,
    )

    cb_adapter = MagicMock()
    cb_adapter.place_market_order.return_value = {
        "ok": False,
        "error": "Orderbook is in limit only mode - please use limit order type",
    }
    cb_adapter.place_limit_order_gtc.side_effect = [
        {"ok": False, "error": "Insufficient balance"},
        {"ok": True, "order_id": "cb-limit-exit-retry", "raw": {}},
    ]

    with patch(
        "app.services.trading.crypto.exit_monitor._current_crypto_price",
        return_value=8.50,
    ), patch(
        "app.services.coinbase_service.get_positions",
        return_value=[{"ticker": "DIEM-USD", "quantity": 3.0}],
    ), patch(
        "app.services.trading.crypto.exit_monitor._coinbase_spot_adapter",
        return_value=cb_adapter,
    ), patch(
        "app.services.coinbase_service.get_open_orders",
        return_value=[{"order_id": "cb-old-stop", "side": "SELL"}],
    ) as get_open_orders, patch(
        "app.services.coinbase_service.cancel_order_by_id",
        return_value={"ok": True},
    ) as cancel_order, patch(
        "app.services.trading.governance.is_kill_switch_active",
        return_value=False,
    ):
        from app.services.trading.crypto.exit_monitor import run_crypto_exit_pass

        out = run_crypto_exit_pass(db)

    assert out.get("closed") == 1
    db.refresh(t)
    assert t.pending_exit_order_id == "cb-limit-exit-retry"
    assert cb_adapter.place_market_order.call_count == 2
    assert cb_adapter.place_limit_order_gtc.call_count == 2
    get_open_orders.assert_called_once_with(product_ids=["DIEM-USD"])
    cancel_order.assert_called_once_with("cb-old-stop")


def test_coinbase_dust_detection_uses_notional_threshold():
    from app.services.trading.crypto.exit_monitor import _is_coinbase_unmarketable_dust

    assert _is_coinbase_unmarketable_dust(0.01965443, 0.2005) is True
    assert _is_coinbase_unmarketable_dust(100.0, 0.2005) is False
    assert _is_coinbase_unmarketable_dust(0.01965443, None) is False


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
