"""`_order_open` must recognize a placed-but-unfilled order as still live.

The lane's FIRST real Robinhood equity order (HIHO, 2026-06-09) was placed
successfully (RH state "confirmed" -> adapter status "working") but the old
allow-list (`open/pending/active/unknown/""`) didn't include "working", so it fell
through to the `entry_order_state` live_error branch and was ORPHANED on the broker
(the session errored without cancelling it). These pin the done-list approach: any
non-terminal status is "open" so the ack-timeout (cancel + re-watch) path handles it.
Pure — no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.momentum_neural.live_runner import _order_open


def _o(status):
    return SimpleNamespace(status=status)


def test_working_is_open():
    # THE regression: Robinhood resting-order status the old code missed.
    assert _order_open(_o("working")) is True


def test_other_live_statuses_are_open():
    for s in ("confirmed", "queued", "unconfirmed", "partially_filled", "open", "pending", "active", "submitted", "accepted"):
        assert _order_open(_o(s)) is True, s


def test_empty_or_unknown_is_open():
    # Indeterminate -> never abandon a possibly-live order.
    assert _order_open(_o("")) is True
    assert _order_open(_o(None)) is True
    assert _order_open(_o("unknown")) is True


def test_terminal_statuses_are_not_open():
    for s in ("filled", "done", "closed", "cancelled", "canceled", "expired", "failed", "rejected", "voided"):
        assert _order_open(_o(s)) is False, s


def test_case_insensitive():
    assert _order_open(_o("WORKING")) is True
    assert _order_open(_o("Filled")) is False
