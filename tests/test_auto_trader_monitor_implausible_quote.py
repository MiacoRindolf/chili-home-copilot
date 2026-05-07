"""Tests for the equity-lane implausible-quote guard added in
f-exit-monitor-quote-guard-unification (2026-05-06).

Equity had no guard until this brief; a $0.50 quote on a $50 entry
would force ``hit_stop=True`` and force-sell at the bad price. Pin
that the new guard:

  1. Skips the trade (no broker call) when the quote is implausible.
  2. Increments a NEW summary counter ``skipped_implausible_quote``
     (additive; doesn't double-count with ``skipped_no_quote`` or
     ``errors``).
  3. Lets normal-range quotes through to the regular trigger logic.

Each test seeds a real Trade row and mocks only the broker adapter
edge -- same pattern as the existing equity-lane tests at
``tests/test_auto_trader_monitor.py``.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from app import models
from app.models.trading import Trade


def _patch_settings(uid: int):
    s = patch("app.services.trading.auto_trader_monitor.settings").start()
    s.chili_autotrader_enabled = True
    s.chili_autotrader_rth_only = False
    s.chili_autotrader_live_enabled = True
    s.chili_autotrader_daily_loss_cap_usd = 500.0
    s.chili_autotrader_user_id = uid
    s.brain_default_user_id = uid
    return s


def _seed_open_equity(db, *, name_suffix: str, entry_price: float = 50.0) -> Trade:
    u = models.User(name=f"equity_implausible_{name_suffix}_u")
    db.add(u)
    db.flush()
    t = Trade(
        user_id=u.id,
        ticker="ZZZ",
        direction="long",
        entry_price=entry_price,
        quantity=10.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=entry_price * 0.9,
        take_profit=entry_price * 1.5,
        auto_trader_version="v1",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _run_tick_with_quote(db, *, ticker: str, quote_px: float, uid: int):
    """Patch the Robinhood adapter + market_data fallback to return a
    specific quote, then run a single tick. Returns (summary, sell_mock)."""
    from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor

    sell_mock = MagicMock(return_value={
        "ok": True,
        "order_id": "oid-should-not-fire",
        "raw": {"average_price": quote_px, "cumulative_quantity": 10},
    })
    ad = MagicMock()
    ad.is_enabled.return_value = True
    ad.get_quote_price.return_value = quote_px
    ad.place_market_order = sell_mock

    _patch_settings(uid)
    try:
        with patch(
            "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
            return_value=ad,
        ), patch(
            "app.services.trading.auto_trader_monitor._maybe_trip_daily_loss_kill_switch",
        ), patch(
            "app.services.trading.robinhood_exit_execution.submit_robinhood_trade_exit",
            return_value={"ok": True, "state": "filled", "order_id": "should-not-fire"},
        ) as submit_mock:
            out = tick_auto_trader_monitor(db)
    finally:
        patch.stopall()
    return out, sell_mock, submit_mock


# ---------------------------------------------------------------------------
# Case 1 -- implausible quote skips the trade
# ---------------------------------------------------------------------------

def test_equity_implausible_quote_skips_trade(db, caplog):
    """Entry $50, quote $0.50 (ratio 0.01 -- below 0.1x threshold).
    The guard fires: trade is skipped, no broker call, counter
    increments, WARNING log emitted."""
    import logging
    caplog.set_level(logging.WARNING, logger="app.services.trading.auto_trader_monitor")

    t = _seed_open_equity(db, name_suffix="case1", entry_price=50.0)
    out, sell_mock, submit_mock = _run_tick_with_quote(
        db, ticker="ZZZ", quote_px=0.50, uid=t.user_id,
    )

    assert out.get("closed", 0) == 0
    assert out.get("skipped_implausible_quote", 0) >= 1
    sell_mock.assert_not_called()
    submit_mock.assert_not_called()
    db.refresh(t)
    # Trade row state is unchanged: still open, no pending exit.
    assert t.status == "open"
    assert t.pending_exit_order_id is None
    # WARNING log includes the canonical phrasing the operator greps for.
    assert any(
        "implausible quote refused" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Case 2 -- normal-range quote proceeds to trigger logic
# ---------------------------------------------------------------------------

def test_equity_normal_quote_proceeds(db):
    """Entry $50, quote $48 (ratio 0.96 -- well within (0.1, 10)).
    The implausibility guard does NOT fire; trigger logic runs as
    normal. Quote $48 is above stop $45 and below target $75, so no
    exit fires and the trade stays open with no pending exit."""
    t = _seed_open_equity(db, name_suffix="case2", entry_price=50.0)
    out, sell_mock, submit_mock = _run_tick_with_quote(
        db, ticker="ZZZ", quote_px=48.0, uid=t.user_id,
    )

    assert out.get("skipped_implausible_quote", 0) == 0
    assert out.get("closed", 0) == 0
    sell_mock.assert_not_called()
    submit_mock.assert_not_called()
    db.refresh(t)
    assert t.status == "open"


# ---------------------------------------------------------------------------
# Case 3 -- implausible-quote skip is a distinct counter
# ---------------------------------------------------------------------------

def test_equity_implausible_quote_does_not_double_count_skip(db):
    """Pin that ``skipped_implausible_quote`` is its own counter -- it
    must NOT also increment ``skipped_no_quote`` or ``errors`` for the
    same trade. Catches future refactors that route the new branch
    through an existing counter and silently obscure the audit trail."""
    t = _seed_open_equity(db, name_suffix="case3", entry_price=50.0)
    out, _sell_mock, _submit_mock = _run_tick_with_quote(
        db, ticker="ZZZ", quote_px=0.50, uid=t.user_id,
    )

    assert out.get("skipped_implausible_quote", 0) >= 1
    # ``skipped_no_quote`` may not exist as a key for the equity lane
    # at all; the assertion is that if it exists, this trade did NOT
    # contribute to it.
    no_quote_skips = out.get("skipped_no_quote", 0)
    if isinstance(no_quote_skips, list):
        assert int(t.id) not in no_quote_skips
    else:
        # Counter form: at most some other lane logic could have
        # incremented it for ANOTHER trade, but for this single-trade
        # test we expect 0.
        assert no_quote_skips == 0
    # Errors list must not contain a no_quote entry for this ticker
    # (the implausible-quote path uses a dedicated counter, not the
    # errors list).
    errors = out.get("errors", []) or []
    assert not any(
        isinstance(e, str) and e.startswith(f"no_quote:{t.ticker}")
        for e in errors
    )
