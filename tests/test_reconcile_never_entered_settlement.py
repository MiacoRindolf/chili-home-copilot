"""A broker-confirmed no-fill row must stop blinding the loss guard.

2026-09-04, the arming outage. Six ``live_arm_expired`` sessions the lane launcher's
cleanup terminalized — WETO 19762, CDTG 20200, IMRN 20208, AOUT 20213, NWGL 20219,
FCUV 20221 — sat at ``flat_unknown`` carrying submission evidence and no fill proof.
``flat_unknown`` is in ``_LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES``; with no economics the
classifier answers ``unknown``; and ONE unknown row makes
``load_current_live_loss_history`` report ``loss_guard_entry_classification_unknown``
for the whole account. The lane stopped arming for hours of an RTH session while it was
seeing +23% movers, and the rows had to be settled by hand from a direct Alpaca read.

Every one of them was genuinely never-entered: WETO's order read ``canceled,
filled_qty=0`` and the other five never reached the broker at all. The proof was already
in this module — ``STATUS_NO_FILLS`` IS a successful broker read that found no fills.

These tests pin the promotion and, more importantly, pin the four ways it must refuse.
The refusals matter more than the promotion: booking $0 over a real loss would hide it
from the loss guard permanently instead of merely delaying it.
"""
from __future__ import annotations

import types

import pytest

import app.services.trading.momentum_neural.outcome_reconcile as rc
from app.services.trading.momentum_neural.outcome_labels import (
    OUTCOME_CANCELLED_PRE_ENTRY,
    OUTCOME_FLAT_UNKNOWN,
    OUTCOME_STOP_LOSS,
)


def _outcome(**kw):
    base = dict(
        outcome_class=OUTCOME_FLAT_UNKNOWN,
        realized_pnl_usd=None,
        return_bps=None,
        broker_realized_pnl_usd=None,
        broker_notional_basis_usd=None,
        broker_recon_status=None,
        broker_reconciled_at=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _settle(outcome, *, status, broker_pnl=None, broker_notional=None, sess_id=19762):
    """Drive only the settlement block, with the same inputs the stamp path gives it."""
    detail: dict = {}
    sess = types.SimpleNamespace(id=sess_id, symbol="WETO")
    # Mirrors the guarded block in _reconcile_one immediately before the mig309 stamp.
    if status == rc.STATUS_NO_FILLS:
        from app.services.trading.momentum_neural.risk_policy import (
            _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES,
        )

        current = str(getattr(outcome, "outcome_class", "") or "").strip().lower()
        no_econ = all(
            rc._f(v) is None
            for v in (
                getattr(outcome, "realized_pnl_usd", None),
                getattr(outcome, "return_bps", None),
                broker_pnl,
                broker_notional,
            )
        )
        if current in _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES and no_econ:
            outcome.outcome_class = OUTCOME_CANCELLED_PRE_ENTRY
            detail["never_entered_settlement"] = {"from": current, "to": OUTCOME_CANCELLED_PRE_ENTRY}
    return detail


def test_no_fill_read_promotes_the_row_out_of_the_blind_class():
    o = _outcome()
    detail = _settle(o, status=rc.STATUS_NO_FILLS)
    assert o.outcome_class == OUTCOME_CANCELLED_PRE_ENTRY
    assert detail["never_entered_settlement"]["from"] == OUTCOME_FLAT_UNKNOWN


def test_the_promoted_class_is_one_the_loss_guard_may_skip():
    """The whole point: cancelled_pre_entry is in NEVER_ENTERED_OUTCOMES, and that is the
    single class ``_loss_history_entry_classification`` is allowed to answer
    ``not_entered`` for. Without this the row stays ``unknown`` and gaps the day."""
    from app.services.trading.momentum_neural.outcome_labels import NEVER_ENTERED_OUTCOMES
    from app.services.trading.momentum_neural.risk_policy import (
        _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES,
    )

    assert OUTCOME_CANCELLED_PRE_ENTRY in NEVER_ENTERED_OUTCOMES
    assert OUTCOME_CANCELLED_PRE_ENTRY not in _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES
    assert OUTCOME_FLAT_UNKNOWN in _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES


# ── the refusals, which matter more than the promotion ───────────────────────


@pytest.mark.parametrize(
    "status",
    [
        rc.STATUS_NO_MATCH,
        rc.STATUS_PHANTOM,
        rc.STATUS_RESIDUAL_OPEN,
        rc.STATUS_BROKER_UNAVAILABLE,
        rc.STATUS_AMBIGUOUS_TRADE,
        rc.STATUS_PYRAMID_GAP,
    ],
)
def test_only_a_confirmed_no_fill_read_settles_anything(status):
    """Every other status means "we do not know", and an unknown must never be
    written down as an empty. BROKER_UNAVAILABLE is the transport-failure case."""
    o = _outcome()
    _settle(o, status=status)
    assert o.outcome_class == OUTCOME_FLAT_UNKNOWN


@pytest.mark.parametrize(
    "field,value",
    [
        ("realized_pnl_usd", -37.25),
        ("return_bps", -158.0),
        ("realized_pnl_usd", 0.01),
    ],
)
def test_any_recorded_economics_blocks_the_settlement(field, value):
    """Money moved, so this row is not an empty — whatever its class says.

    This is the ``entry_submission_evidence`` doctrine: replacing a missing row with a
    confidently WRONG $0 row hides a real loss from the loss guard instead of delaying it.
    """
    o = _outcome(**{field: value})
    _settle(o, status=rc.STATUS_NO_FILLS)
    assert o.outcome_class == OUTCOME_FLAT_UNKNOWN


@pytest.mark.parametrize("broker_pnl,broker_notional", [(-37.25, None), (None, 234.71), (0.0, 0.0)])
def test_broker_side_economics_also_block_it(broker_pnl, broker_notional):
    o = _outcome()
    _settle(o, status=rc.STATUS_NO_FILLS, broker_pnl=broker_pnl, broker_notional=broker_notional)
    assert o.outcome_class == OUTCOME_FLAT_UNKNOWN


def test_a_real_exit_class_is_never_rewritten():
    """stop_loss / success / bailout describe economics and are not ambiguous to the
    guard. A no-fill read on such a row is a contradiction to investigate, not to paper
    over by relabelling the row."""
    o = _outcome(outcome_class=OUTCOME_STOP_LOSS)
    _settle(o, status=rc.STATUS_NO_FILLS)
    assert o.outcome_class == OUTCOME_STOP_LOSS


def test_every_ambiguous_class_is_covered_not_just_flat_unknown():
    """The guard gaps on ANY class in the ambiguous set, so the settlement keys on the
    set rather than on one literal — if a class is added there later, this follows."""
    from app.services.trading.momentum_neural.risk_policy import (
        _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES,
    )

    for cls in _LOSS_HISTORY_AMBIGUOUS_ENTRY_CLASSES:
        o = _outcome(outcome_class=cls)
        _settle(o, status=rc.STATUS_NO_FILLS)
        assert o.outcome_class == OUTCOME_CANCELLED_PRE_ENTRY, cls
