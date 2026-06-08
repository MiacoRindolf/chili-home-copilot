"""Durable entry-occurred classification (EIGEN sessions 57/64 regression).

A live momentum session that REALLY entered and then exited at a loss via the
broker-zero-reconcile path was being mislabeled ``cancelled_pre_entry`` — a
self-contradictory outcome (it carried a real fill, a realized P&L, and a
post-exit excursion) that hides real losses from any ``outcome_class``-based
tally and could drop non-shakeout reconcile exits out of the strategy learner.

Root cause: ``entry_occurred`` was derived only from (a) the recent-events
window — which ages out for a long-held session — and (b) ``exec["position"]``
quantity — which the reconcile path zeroes. Both transient signals read False
post-reconcile, so a real round-trip looked like it never entered.

Fix: ``_entry_occurred_durable`` also accepts durable fill evidence
(``realized_pnl_usd`` / ``last_exit_entry_price``) that survives event-aging and
position-zeroing — while intentionally NOT trusting submission-only markers, so a
genuine zero-fill stays non-entered.
"""

from __future__ import annotations

from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_CANCELLED
from app.services.trading.momentum_neural.outcome_extract import (
    _entry_occurred_durable,
    derive_outcome_class,
)
from app.services.trading.momentum_neural.outcome_labels import (
    OUTCOME_CANCELLED_IN_TRADE,
    OUTCOME_CANCELLED_PRE_ENTRY,
)


def test_durable_marker_realized_pnl_proves_entry():
    # EIGEN session 57: position zeroed by reconcile, entry event aged out, but a
    # realized P&L is durable proof the entry filled.
    assert _entry_occurred_durable({"realized_pnl_usd": -1.13}) is True


def test_durable_marker_last_exit_entry_price_proves_entry():
    assert _entry_occurred_durable({"last_exit_entry_price": 0.1879953}) is True


def test_realized_pnl_zero_is_still_entry():
    # A realized P&L of exactly 0.0 still means a round-trip happened (breakeven),
    # NOT "no entry" — the marker is presence (is not None), not truthiness.
    assert _entry_occurred_durable({"realized_pnl_usd": 0.0}) is True


def test_submission_only_markers_stay_non_entered():
    # entry_submitted + entry_order_id without any fill evidence == a zero-fill
    # submission. It must NOT count as an entry (else a never-filled order would be
    # mislabeled cancelled_in_trade instead of staying pre-entry / no_fill).
    assert (
        _entry_occurred_durable(
            {"entry_submitted": True, "entry_order_id": "abc-123"}
        )
        is False
    )


def test_empty_and_non_dict_are_non_entered():
    assert _entry_occurred_durable({}) is False
    assert _entry_occurred_durable(None) is False
    assert _entry_occurred_durable("not-a-dict") is False


def test_live_cancelled_with_entry_is_in_trade_not_pre_entry():
    # The consumer side: once entry_occurred is correctly True, a live_cancelled
    # terminal classifies as cancelled_in_trade — never the self-contradictory
    # cancelled_pre_entry. This is the position-neutral cancel of a still-open
    # position (no recorded FULL exit reason). A reconcile exit that ALSO carries
    # a full exit reason (e.g. "trail_stop_broker_zero_reconcile") is the deeper
    # case routed to its true exit class — see test_outcome_reconcile_exit_class.
    assert (
        derive_outcome_class(
            mode="live",
            terminal_state=STATE_LIVE_CANCELLED,
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=None,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=[],
        )
        == OUTCOME_CANCELLED_IN_TRADE
    )


def test_live_cancelled_without_entry_stays_pre_entry():
    # Contrast: a genuine pre-entry cancel (no fill ever) is correctly pre_entry.
    assert (
        derive_outcome_class(
            mode="live",
            terminal_state=STATE_LIVE_CANCELLED,
            entry_occurred=False,
            partial_exit=False,
            realized_pnl_usd=None,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=[],
        )
        == OUTCOME_CANCELLED_PRE_ENTRY
    )
