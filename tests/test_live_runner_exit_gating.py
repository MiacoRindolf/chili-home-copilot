"""A held momentum position must keep its stop/target exit management even when
the entry-oriented boundary risk eval refuses (the stop is a safety mechanism)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.config import settings
from app.services.trading.momentum_neural.live_runner import (
    _held_position_keeps_exit_on_boundary_fail,
    _live_tick_bbo,
)
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_BAILOUT,
    STATE_LIVE_ENTERED,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    STATE_LIVE_PENDING_ENTRY,
)
from app.services.trading.venue.protocol import FreshnessMeta, NormalizedTicker

_POS = {"quantity": 100.0, "stop_price": 1.0}


def test_held_states_with_position_keep_exit():
    for st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT):
        assert _held_position_keeps_exit_on_boundary_fail(st, _POS) is True, st


def test_held_state_without_position_does_not_fall_through():
    # no position -> nothing to manage; the normal block applies
    assert _held_position_keeps_exit_on_boundary_fail(STATE_LIVE_ENTERED, None) is False
    assert _held_position_keeps_exit_on_boundary_fail(STATE_LIVE_ENTERED, {}) is False


def test_entry_states_still_block_on_boundary_fail():
    for st in (STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_PENDING_ENTRY):
        assert _held_position_keeps_exit_on_boundary_fail(st, _POS) is False, st


def test_held_alpaca_tick_rejects_60_second_quote_and_never_uses_default_path(
    monkeypatch,
):
    """The ordinary Alpaca quote path permits a much older IQFeed row. A held
    position must instead ask for the strict execution BBO and reject that row."""
    monkeypatch.setattr(settings, "chili_momentum_entry_bbo_max_age_seconds", 10.0)
    now = datetime.now(timezone.utc)
    stale = FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now - timedelta(seconds=60),
        max_age_seconds=60.0,
    )
    adapter = MagicMock()
    adapter.get_execution_bbo.return_value = (
        NormalizedTicker(
            product_id="ACTU",
            bid=1.47,
            ask=1.48,
            mid=1.475,
            freshness=stale,
            raw={"feed": "iqfeed_l1", "tape_row_id": 77},
        ),
        stale,
    )

    tick, freshness, snapshot = _live_tick_bbo(
        adapter,
        "ACTU",
        execution_family="alpaca_spot",
        state=STATE_LIVE_ENTERED,
    )

    assert tick is None
    assert freshness is None
    assert snapshot is not None
    assert snapshot["reason"] == "execution_bbo_stale"
    assert snapshot["max_age_seconds"] == 2.0  # hard cap despite looser config
    adapter.get_execution_bbo.assert_called_once_with("ACTU", max_age_seconds=2.0)
    adapter.get_best_bid_ask.assert_not_called()
