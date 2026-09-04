"""``live_arm_expired`` must be classified by its real exit, not dropped as unknown.

Since the 2026-09-02 ledger fix ``live_arm_expired`` is terminal for the LEDGER, so
these sessions now produce outcome rows. ``derive_outcome_class`` had no branch for
the state, so every one of them fell through to ``flat_unknown``: of the 16 rows that
exist since that fix, 8 carry money (−$208.71 realised), each one ``reconciled`` and
carrying a real ``exit_reason`` (bailout x4, stop x2, trail_stop, operator_flatten).

What this buys is the CLASS-KEYED surfaces, not the learner's expectancy — evolution
weights return_bps / realized_pnl and never reads outcome_class for the maths
(evolution.py:357-369). It is also GO-FORWARD ONLY: nothing here re-derives a stored
class, and both regrade scripts hardcode ``("live_cancelled", "cancelled")``, so those
16 rows keep flat_unknown until a follow-up widens them.

Three properties are as load-bearing as the happy path and are asserted below:

* **No never-entered leg.** Labelling the remaining rows ``cancelled_pre_entry``
  would replace missing rows with confidently WRONG ones — the exact failure
  ``entry_submission_evidence`` exists to prevent. They stay ``flat_unknown``.
* **Nothing else moves.** The branch is additive and the signature is unchanged, so
  every other terminal state classifies exactly as before.
* **The one credit-set consequence is deliberate and pinned.** ``governance_exit`` is
  in both ``_REAL_EXIT_OUTCOMES`` and ``_NON_STRATEGY_CREDIT_OUTCOMES``, so a
  kill-switch arm-expired row stops contributing to evolution. That is intended, and
  ``test_kill_switch_row_loses_evolution_credit`` asserts it rather than leaving it
  as an unremarked side effect.

DB-free: ``derive_outcome_class`` and ``outcome_evolution_credit_from_extracted`` are
pure functions.
"""

import pytest

from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ARM_EXPIRED,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_FINISHED,
)
from app.services.trading.momentum_neural.outcome_extract import (
    derive_outcome_class,
    outcome_evolution_credit_from_extracted,
)
from app.services.trading.momentum_neural.outcome_labels import (
    OUTCOME_BAILOUT,
    OUTCOME_CANCELLED_IN_TRADE,
    OUTCOME_CANCELLED_PRE_ENTRY,
    OUTCOME_FLAT_UNKNOWN,
    OUTCOME_GOVERNANCE_EXIT,
    OUTCOME_SMALL_WIN,
    OUTCOME_STOP_LOSS,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_EXIT,
)


def _classify(**overrides):
    base = dict(
        mode="live",
        terminal_state=STATE_LIVE_ARM_EXPIRED,
        entry_occurred=True,
        partial_exit=False,
        realized_pnl_usd=None,
        return_bps=None,
        exit_reason=None,
        governance_context={},
        events=[],
    )
    base.update(overrides)
    return derive_outcome_class(**base)


# --- the real round trips: classified by their exit reason -------------------


@pytest.mark.parametrize(
    "exit_reason,expected",
    [
        ("bailout", OUTCOME_BAILOUT),
        ("stop", OUTCOME_STOP_LOSS),
        ("stop_loss", OUTCOME_STOP_LOSS),
        ("trail_stop", OUTCOME_STOP_LOSS),
        ("max_hold", OUTCOME_TIMED_EXIT),
    ],
)
@pytest.mark.parametrize("realized", [-12.5, None])
def test_keyword_exit_reasons_classify_by_their_class(exit_reason, expected, realized):
    """The keyword ladder decides BEFORE the economics, so a row with no P&L at all
    still gets its real class. That shape matters: it is the one that removes a
    ``_LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES`` gap in the loss guard (a real-exit class
    answers "entered" unconditionally at risk_policy.py:1596, where flat_unknown needs
    durable_runtime or economic_nonzero). None of the 16 measured rows has this shape —
    they all carry economics — but the code path is reachable and is asserted here.
    """
    assert _classify(exit_reason=exit_reason, realized_pnl_usd=realized) == expected


def test_reconcile_suffix_is_stripped_before_matching():
    """The broker-zero-reconcile provenance suffix must not hide the exit class."""
    assert (
        _classify(exit_reason="trail_stop_broker_zero_reconcile", realized_pnl_usd=-31.0)
        == OUTCOME_STOP_LOSS
    )
    assert (
        _classify(exit_reason="bailout_retry_cap_broker_zero_reconcile", realized_pnl_usd=-8.0)
        == OUTCOME_BAILOUT
    )


def test_operator_flatten_falls_through_to_economics_not_governance():
    """operator_flatten has no keyword, so the economics decide.

    The LOSS direction is the designed one and is what all 8 measured money rows are:
    mapping it to ``governance_exit`` would bar a real strategy loss from credit. Note
    the asymmetry with ``alpaca_orphan_reconcile``, which the module DOES map to
    governance_exit — that is a broker-safety flatten of a dead session, whereas an
    operator flatten closes a live position the strategy chose.

    The two WIN-direction assertions below pin an UNMEASURED consequence of the
    fall-through, not a desired label. No measured row has this shape; they are here so
    that if someone later decides an operator flatten should never read as a strategy
    success, the change is visible rather than silent.
    """
    assert _classify(exit_reason="operator_flatten", realized_pnl_usd=-47.20) == OUTCOME_STOP_LOSS
    assert _classify(exit_reason="operator_flatten", return_bps=60.0) == OUTCOME_SUCCESS
    assert _classify(exit_reason="operator_flatten", return_bps=5.0) == OUTCOME_SMALL_WIN


def test_partial_exit_alone_is_enough_to_reclassify():
    """Defensive contract on the public function only.

    ``extract_momentum_session_outcome`` cannot actually emit this combination: the
    partial-exit event types are themselves live entry markers, read from the same
    events list in the same pass (outcome_extract.py:134-135 vs :462-464), so
    partial_exit=True implies entry_occurred=True there. The assertion guards
    ``derive_outcome_class`` for any other caller that constructs the pair directly.
    """
    assert (
        _classify(entry_occurred=False, partial_exit=True, exit_reason="stop", realized_pnl_usd=-3.0)
        == OUTCOME_STOP_LOSS
    )


def test_kill_switch_still_wins_over_the_exit_reason():
    assert (
        _classify(
            exit_reason="stop",
            realized_pnl_usd=-5.0,
            governance_context={"kill_switch_exit": True},
        )
        == OUTCOME_GOVERNANCE_EXIT
    )


def test_kill_switch_row_loses_evolution_credit():
    """The ONE credit-set consequence of this branch, asserted rather than implied.

    ``governance_exit`` sits in both ``_REAL_EXIT_OUTCOMES`` (so the new branch can
    return it) and ``_NON_STRATEGY_CREDIT_OUTCOMES`` (so it is barred from evolution).
    A kill-switch arm-expired row therefore moves from flat_unknown, which credits, to
    governance_exit, which does not. That is intended — a kill-switch flatten is not
    strategy — and for ``alpaca_orphan_reconcile`` it is idempotent with the repair
    path that already forces the same pair. Pinning it here means a future change to
    either frozenset shows up as a failing test instead of a silent credit shift.
    """
    base = {
        "entry_occurred": True,
        "entry_decision_packet_id": 4242,
        "realized_pnl_usd": -5.0,
        "return_bps": -30.0,
        "mode": "live",
        "quote_source_at_entry": "iqfeed_l1",
    }

    stop_row = outcome_evolution_credit_from_extracted({**base, "outcome_class": OUTCOME_STOP_LOSS})
    assert stop_row["contributes_to_evolution"] is True
    assert stop_row["reason_codes"] == []

    unknown_row = outcome_evolution_credit_from_extracted({**base, "outcome_class": OUTCOME_FLAT_UNKNOWN})
    assert unknown_row["contributes_to_evolution"] is True, (
        "flat_unknown is NOT in _NON_STRATEGY_CREDIT_OUTCOMES — these rows credited before "
        "this branch existed, which is why the credit set is only touched via governance_exit"
    )

    gov_row = outcome_evolution_credit_from_extracted({**base, "outcome_class": OUTCOME_GOVERNANCE_EXIT})
    assert gov_row["contributes_to_evolution"] is False
    assert f"non_strategy_outcome_{OUTCOME_GOVERNANCE_EXIT}" in gov_row["reason_codes"]


# --- the honest unknowns: NOT invented as cancelled_pre_entry ----------------


def test_no_exit_reason_stays_flat_unknown_never_cancelled_pre_entry():
    """The 8 rows with submission evidence but no readable exit must stay unknown.

    Booking them as ``cancelled_pre_entry`` with $0 is the documented "9 confidently
    WRONG rows" failure: all of them carry entry-submission evidence and 4 carry
    broker P&L (−$442.46). An honest unknown beats a false zero.
    """
    out = _classify(entry_occurred=False, partial_exit=False, exit_reason=None)
    assert out == OUTCOME_FLAT_UNKNOWN
    assert out != OUTCOME_CANCELLED_PRE_ENTRY


def test_entered_but_unreadable_exit_stays_flat_unknown():
    assert _classify(entry_occurred=True, exit_reason=None) == OUTCOME_FLAT_UNKNOWN
    assert _classify(entry_occurred=True, exit_reason="") == OUTCOME_FLAT_UNKNOWN


def test_ambiguous_exit_reason_with_no_economics_stays_flat_unknown():
    """An unrecognised reason with no P&L cannot be upgraded to a real exit class."""
    assert _classify(exit_reason="session_cancelled", realized_pnl_usd=None, return_bps=None) == (
        OUTCOME_FLAT_UNKNOWN
    )


# --- nothing else moves ------------------------------------------------------


def test_live_cancelled_branch_is_untouched():
    assert (
        derive_outcome_class(
            mode="live",
            terminal_state=STATE_LIVE_CANCELLED,
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=-9.0,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=[],
        )
        == OUTCOME_CANCELLED_IN_TRADE
    )
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


def test_live_finished_branch_is_untouched():
    assert (
        derive_outcome_class(
            mode="live",
            terminal_state=STATE_LIVE_FINISHED,
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=-20.0,
            return_bps=-90.0,
            exit_reason="bailout",
            governance_context={},
            events=[],
        )
        == OUTCOME_BAILOUT
    )


def test_paper_mode_arm_expired_is_not_a_thing_and_still_falls_through():
    """``live_arm_expired`` is a LIVE state; a paper row carrying it is not special-cased
    into a real exit unless it genuinely has one, and never becomes a false zero."""
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state=STATE_LIVE_ARM_EXPIRED,
            entry_occurred=False,
            partial_exit=False,
            realized_pnl_usd=None,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=[],
        )
        == OUTCOME_FLAT_UNKNOWN
    )
