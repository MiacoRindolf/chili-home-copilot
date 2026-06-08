"""Reconcile-exit outcome classification (EIGEN 57/64 follow-up to #517).

A live momentum session that completes a REAL round-trip — a full position-
closing exit (stop / bailout / trail / max_hold / target) recorded via the
broker-zero-reconcile path — can terminate in FSM state ``live_cancelled``
(the recycled post-exit watcher is reaped, or a duplicate claimant is cleaned
up) rather than ``live_finished``. Before this fix ``derive_outcome_class``
labeled it ``cancelled_in_trade`` — a non-strategy outcome
(``contributes_to_evolution=False``) that the selection learner only readmits
via the ``stop_too_tight`` shake-out back-door. So a clean reconcile WIN or a
non-shakeout reconcile LOSS was invisible to the strategy learner.

Fix: when a live_cancelled/cancelled terminal carries entry + a recorded FULL
exit reason, classify it by its TRUE exit class (stripping the
``_broker_zero_reconcile`` provenance suffix first) — identical to how the
finished branch would label the same exit. A position-neutral operator/dup
cancel of a still-open position (no full exit reason) stays
``cancelled_in_trade``.
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_CANCELLED,
    STATE_LIVE_FINISHED,
)
from app.services.trading.momentum_neural.outcome_extract import (
    _classify_real_exit,
    _strip_reconcile_suffix,
    derive_outcome_class,
    outcome_evolution_credit_from_extracted,
)
from app.services.trading.momentum_neural.outcome_labels import (
    OUTCOME_BAILOUT,
    OUTCOME_CANCELLED_IN_TRADE,
    OUTCOME_CANCELLED_PRE_ENTRY,
    OUTCOME_GOVERNANCE_EXIT,
    OUTCOME_SMALL_WIN,
    OUTCOME_STOP_LOSS,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_EXIT,
)


def _classify(state, **kw):
    base = dict(
        mode="live",
        terminal_state=state,
        entry_occurred=True,
        partial_exit=False,
        realized_pnl_usd=None,
        return_bps=None,
        exit_reason=None,
        governance_context={},
        events=[],
    )
    base.update(kw)
    return derive_outcome_class(**base)


# ── suffix stripping (pure) ──────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("trail_stop_broker_zero_reconcile", "trail_stop"),
        ("bailout_broker_zero_reconcile", "bailout"),
        ("stop_broker_zero_reconcile", "stop"),
        ("max_hold_broker_zero_reconcile", "max_hold"),
        # retry-cap is the longer suffix and must be stripped whole.
        ("stop_retry_cap_broker_zero_reconcile", "stop"),
        ("bailout_retry_cap_broker_zero_reconcile", "bailout"),
        # no suffix → unchanged
        ("stop", "stop"),
        ("trail_stop", "trail_stop"),
        ("", ""),
        (None, None),
    ],
)
def test_strip_reconcile_suffix(raw, clean):
    assert _strip_reconcile_suffix(raw) == clean


# ── the live_cancelled reroute classifies by true exit class ─────────


@pytest.mark.parametrize(
    "exit_reason,return_bps,realized,expected",
    [
        ("bailout_broker_zero_reconcile", -52.9, -1.13, OUTCOME_BAILOUT),
        ("trail_stop_broker_zero_reconcile", -52.9, -1.13, OUTCOME_STOP_LOSS),
        ("stop_broker_zero_reconcile", -258.7, -6.54, OUTCOME_STOP_LOSS),
        ("max_hold_broker_zero_reconcile", None, None, OUTCOME_TIMED_EXIT),
        ("stop_retry_cap_broker_zero_reconcile", -120.0, -3.0, OUTCOME_STOP_LOSS),
        # clean (non-reconcile) full exit reasons land here too when the
        # session ends cancelled instead of finished.
        ("stop", -92.7, -2.19, OUTCOME_STOP_LOSS),
        ("trail_stop", -23.6, -0.59, OUTCOME_STOP_LOSS),
        # a winning reconcile/late-cancel round-trip is a real win.
        ("target", 30.0, 5.0, OUTCOME_SUCCESS),
        ("target", 8.0, 1.0, OUTCOME_SMALL_WIN),
    ],
)
def test_live_cancelled_reconcile_round_trip_classifies_by_exit(
    exit_reason, return_bps, realized, expected
):
    assert (
        _classify(
            STATE_LIVE_CANCELLED,
            entry_occurred=True,
            exit_reason=exit_reason,
            return_bps=return_bps,
            realized_pnl_usd=realized,
        )
        == expected
    )


@pytest.mark.parametrize(
    "exit_reason,return_bps,realized",
    [
        ("bailout_broker_zero_reconcile", -52.9, -1.13),
        ("trail_stop_broker_zero_reconcile", -52.9, -1.13),
        ("stop_broker_zero_reconcile", -258.7, -6.54),
        ("max_hold_broker_zero_reconcile", None, None),
        ("stop", -92.7, -2.19),
        ("target", 30.0, 5.0),
        ("target", 8.0, 1.0),
    ],
)
def test_live_cancelled_matches_live_finished_for_real_exits(
    exit_reason, return_bps, realized
):
    """Parity: a real round-trip is labeled IDENTICALLY whether the session
    terminated in live_finished or got cancelled after the exit reconciled."""
    common = dict(
        entry_occurred=True,
        exit_reason=exit_reason,
        return_bps=return_bps,
        realized_pnl_usd=realized,
    )
    assert _classify(STATE_LIVE_CANCELLED, **common) == _classify(
        STATE_LIVE_FINISHED, **common
    )


def test_kill_switch_reconcile_exit_is_governance():
    # A kill-switch-driven exit that reconciled to cancelled is governance_exit,
    # consistent with the finished branch (still non-contributing, but truthful).
    assert (
        _classify(
            STATE_LIVE_CANCELLED,
            entry_occurred=True,
            exit_reason="stop_broker_zero_reconcile",
            realized_pnl_usd=-1.0,
            return_bps=-40.0,
            governance_context={"kill_switch_exit": True},
        )
        == OUTCOME_GOVERNANCE_EXIT
    )


# ── boundary: position-neutral cancels STAY cancelled ────────────────


def test_holding_cancel_without_exit_reason_stays_in_trade():
    # Operator / dup cleanup cancels a still-open position position-neutrally:
    # entry occurred but NO full exit reason was recorded → cancelled_in_trade.
    assert (
        _classify(STATE_LIVE_CANCELLED, entry_occurred=True, exit_reason=None)
        == OUTCOME_CANCELLED_IN_TRADE
    )


def test_partial_then_cancel_holding_stays_in_trade():
    # A scale-out (partial) banked realized P&L but the runner was cancelled
    # while still open: a partial sets last_partial_exit_reason, NOT exit_reason,
    # so exit_reason is None here → the round-trip is NOT complete → in_trade.
    assert (
        _classify(
            STATE_LIVE_CANCELLED,
            entry_occurred=True,
            partial_exit=True,
            exit_reason=None,
            realized_pnl_usd=3.5,
            return_bps=40.0,
        )
        == OUTCOME_CANCELLED_IN_TRADE
    )


def test_pre_entry_cancel_stays_pre_entry():
    assert (
        _classify(STATE_LIVE_CANCELLED, entry_occurred=False, exit_reason=None)
        == OUTCOME_CANCELLED_PRE_ENTRY
    )


def test_reconcile_reason_without_entry_stays_pre_entry():
    # A reconcile reason is present but entry can't be proven (no durable fill
    # evidence, aged-out events) and there is no economic result: stay pre-entry
    # rather than fabricating a strategy outcome (task gate: entry_occurred AND).
    assert (
        _classify(
            STATE_LIVE_CANCELLED,
            entry_occurred=False,
            exit_reason="max_hold_broker_zero_reconcile",
            realized_pnl_usd=None,
            return_bps=None,
        )
        == OUTCOME_CANCELLED_PRE_ENTRY
    )


def test_unrecognized_reason_no_economic_stays_in_trade():
    # A non-empty but unrecognized exit reason with no economic result must not
    # be upgraded to a flat/unknown class — the reroute only emits genuine exit
    # classes, otherwise it falls through to cancelled_in_trade.
    assert (
        _classify(
            STATE_LIVE_CANCELLED,
            entry_occurred=True,
            exit_reason="some_unmapped_reason",
            realized_pnl_usd=None,
            return_bps=None,
        )
        == OUTCOME_CANCELLED_IN_TRADE
    )


# ── paper parity (STATE_CANCELLED) ───────────────────────────────────


def test_paper_cancelled_reconcile_round_trip_classifies():
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state="cancelled",
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=-11.69,
            return_bps=-180.0,
            exit_reason="bailout",
            governance_context={},
            events=[],
        )
        == OUTCOME_BAILOUT
    )


# ── real-data regression (live momentum_automation_outcomes rows) ────


def test_regression_real_live_cancelled_rows():
    """Exact (exit_reason, return_bps, pnl) tuples observed on live
    momentum_automation_outcomes rows that were mislabeled cancelled_*."""
    cases = [
        # session 64 (EIGEN) — bailout reconcile
        ("bailout_broker_zero_reconcile", -52.94, -1.128, OUTCOME_BAILOUT),
        # session 57 (EIGEN) — trail-stop reconcile
        ("trail_stop_broker_zero_reconcile", -52.94, -1.133, OUTCOME_STOP_LOSS),
        # session 53 — trail-stop reconcile
        ("trail_stop_broker_zero_reconcile", -17.55, -0.410, OUTCOME_STOP_LOSS),
        # session 18 / 14 / 13 — stop reconcile
        ("stop_broker_zero_reconcile", -258.69, -6.541, OUTCOME_STOP_LOSS),
        # session 52 / 22 / 19 / 12 / 10 / 9 — clean stop, cancelled
        ("stop", -182.78, -4.762, OUTCOME_STOP_LOSS),
    ]
    for reason, rbps, pnl, expected in cases:
        assert (
            _classify(
                STATE_LIVE_CANCELLED,
                entry_occurred=True,
                exit_reason=reason,
                return_bps=rbps,
                realized_pnl_usd=pnl,
            )
            == expected
        ), reason


def test_partial_then_full_stop_is_stop_loss():
    # session 25: a partial scale-out happened AND a full trail_stop exit closed
    # the runner → the completed round-trip is a stop_loss.
    assert (
        _classify(
            STATE_LIVE_CANCELLED,
            entry_occurred=True,
            partial_exit=True,
            exit_reason="trail_stop",
            return_bps=-530.14,
            realized_pnl_usd=-48.46,
        )
        == OUTCOME_STOP_LOSS
    )


# ── the evolution-credit flip (why this matters) ─────────────────────


def _extracted(outcome_class):
    return {
        "entry_occurred": True,
        "entry_decision_packet_id": 4321,
        "return_bps": -52.94,
        "realized_pnl_usd": -1.128,
        "outcome_class": outcome_class,
        "mode": "live",
        "quote_source_at_entry": None,
    }


def test_reconcile_exit_now_contributes_to_evolution():
    """The whole point: relabeling cancelled_in_trade → stop_loss/bailout flips
    contributes_to_evolution True (entry + packet + economic result + a strategy
    outcome) so the strategy learner sees the real win/loss."""
    # Old (wrong) label was a non-strategy cancel → blocked.
    blocked = outcome_evolution_credit_from_extracted(_extracted(OUTCOME_CANCELLED_IN_TRADE))
    assert blocked["contributes_to_evolution"] is False
    assert "non_strategy_outcome_cancelled_in_trade" in blocked["reason_codes"]

    # New (correct) labels contribute.
    for oc in (OUTCOME_STOP_LOSS, OUTCOME_BAILOUT):
        credit = outcome_evolution_credit_from_extracted(_extracted(oc))
        assert credit["contributes_to_evolution"] is True, oc
        assert credit["reason_codes"] == []


def test_classify_real_exit_helper_direct():
    # The shared helper, exercised directly (used by both terminal branches).
    assert (
        _classify_real_exit(
            exit_reason="trail_stop_broker_zero_reconcile",
            return_bps=-52.9,
            realized_pnl_usd=-1.13,
            entry_occurred=True,
            governance_context={},
        )
        == OUTCOME_STOP_LOSS
    )


# ── aggregate is invariant to the relabel for shake-out rows ─────────


def test_aggregate_invariant_to_relabel_for_shakeout_row():
    """A too-tight-stop shake-out row was ALREADY in the selection aggregate via
    the stop_too_tight back-door (``_contributes_or_shakeout_filter``). After this
    fix it is in via ``contributes_to_evolution=True`` instead — but it is still
    counted ONCE. ``_aggregate_rows`` keys its return/setup-adjusted channels on
    ``return_bps`` + the post-exit label, NOT on ``outcome_class``, so relabeling
    cancelled_in_trade → stop_loss must not change the aggregate (no double-count,
    no value drift). The net change for these rows is only the contributes flag.
    """
    from types import SimpleNamespace

    from app.services.trading.momentum_neural.evolution import _aggregate_rows

    def _row(oc):
        return SimpleNamespace(
            evidence_weight=1.0,
            return_bps=-52.9,
            realized_pnl_usd=-1.13,
            outcome_class=oc,
            extracted_summary_json={
                "post_exit_label": {
                    "stop_too_tight": True,
                    "setup_quality": 0.6,
                    "post_exit_mfe_pct": 1.6,
                    "outcome_class": "premature_stop",
                }
            },
        )

    before = _aggregate_rows([_row(OUTCOME_CANCELLED_IN_TRADE)])
    after = _aggregate_rows([_row(OUTCOME_STOP_LOSS)])
    assert before == after
