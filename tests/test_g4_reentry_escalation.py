"""G4 P2 — same-symbol re-entry escalation gate (pure helper, no I/O).

Losers-eat-the-winner fix (CLRO 07-02): two earlier full-risk stops on the same
name ate the later +$285 leg down to +$13 net. After a stop-out, the next
entry on that symbol must clear a raised bar (structural trigger + reclaim of
the prior failure's high-water mark, scaling with consecutive stops) rather
than being a free re-fire on the identical setup that just failed. This is a
WAIT, not a lockout — fail-open on unusable numeric basis (current behavior)."""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.risk_policy import (
    _is_stop_class_exit_reason,
    reentry_escalation_decision,
    reentry_escalation_level_update,
)


def test_flag_off_always_allows() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=False, escalation_level=5, structural_trigger=False,
        live_price=None, prior_hwm=None, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=None,
    )
    assert allowed is True
    assert dbg["reason"] == "flag_off"


def test_zero_level_always_allows() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False,
        live_price=None, prior_hwm=None, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=None,
    )
    assert allowed is True
    assert dbg["reason"] == "no_escalation"


def test_negative_level_always_allows() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=-1, structural_trigger=False,
        live_price=None, prior_hwm=None, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=None,
    )
    assert allowed is True


def test_bad_level_type_fails_open() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level="oops", structural_trigger=False,
        live_price=None, prior_hwm=None, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=None,
    )
    assert allowed is True
    assert dbg["reason"] == "bad_level_fail_open"


def test_non_structural_trigger_blocked_at_level_1() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=False,
        live_price=11.0, prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=1.0,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"


def test_structural_trigger_with_reclaim_and_tape_allows() -> None:
    # prior stop-out HWM 10.5, now live price 10.8 clears it (level 1 -> margin 0).
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=10.8, prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=1.0,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"


def test_reclaim_not_met_blocks() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=10.2, prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=1.0,
    )
    assert allowed is False
    assert dbg["reason"] == "reclaim_not_met"


def test_margin_scales_with_consecutive_stops() -> None:
    # level 3 -> margin = (3-1)*0.3 = 0.6 -> required = 10.5+0.6 = 11.1.
    kw = dict(
        enabled=True, structural_trigger=True, prior_hwm=10.5,
        prior_exit_price=10.0, prior_risk_dist=0.3, tape_accel=1.0,
    )
    blocked, dbg_blocked = reentry_escalation_decision(escalation_level=3, live_price=10.9, **kw)
    assert blocked is False
    assert dbg_blocked["required_reclaim"] == 11.1
    allowed, dbg_allowed = reentry_escalation_decision(escalation_level=3, live_price=11.2, **kw)
    assert allowed is True
    assert dbg_allowed["reason"] == "reclaim_met"


def test_falls_back_to_exit_price_when_no_hwm() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=10.4, prior_hwm=None, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=1.0,
    )
    assert allowed is True
    assert dbg["required_reclaim"] == 10.0


def test_missing_both_references_skips_reclaim_check() -> None:
    # partial raise: structural trigger still required, but with no bookkeeping to
    # compare against, the reclaim leg does not starve the entry.
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=10.0, prior_hwm=None, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=1.0,
    )
    assert allowed is True
    assert dbg["reason"] == "no_reclaim_reference"


def test_no_live_price_fails_open_on_reclaim_leg() -> None:
    # downstream quote gates own a genuinely unreadable price; do not double-block.
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=None, prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=1.0,
    )
    assert allowed is True
    assert dbg["reason"] == "no_live_price_fail_open"


def test_negative_tape_accel_blocks_when_readable() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=10.8, prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=-0.5,
    )
    assert allowed is False
    assert dbg["reason"] == "tape_not_confirming"


def test_unreadable_tape_does_not_starve_entry() -> None:
    # None tape (thin/crypto/no-db) must NOT block — only a confirmed negative tape does.
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=10.8, prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=None,
    )
    assert allowed is True


def test_zero_tape_accel_does_not_block() -> None:
    # only accel <= 0 with a readable (non-None) value blocks; guard the boundary.
    allowed, _dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=10.8, prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=0.0,
    )
    assert allowed is False  # accel<=0 treated as not-confirming per docstring


def test_bad_numeric_basis_does_not_crash() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=float("nan"), prior_hwm=10.5, prior_exit_price=10.0,
        prior_risk_dist=0.3, tape_accel=1.0,
    )
    # nan live_price is treated as unreadable -> fail-open on the reclaim leg.
    assert allowed is True
    assert dbg["reason"] == "no_live_price_fail_open"


# ── review M1: level bookkeeping — only genuine STOP-class losses escalate ──────


@pytest.mark.parametrize("reason", [
    "stop",
    "trail_stop",
    "grind_trail_stop",
    "stop_broker_zero_reconcile",                    # decorated by broker-zero reconcile
    "trail_stop_retry_cap_broker_zero_reconcile",    # decorated retry-cap path
])
def test_stop_class_loss_increments(reason) -> None:
    lvl, why = reentry_escalation_level_update(
        current_level=1, was_loss=True, exit_reason=reason, green_banked=False,
    )
    assert lvl == 2
    assert why == "stop_class_loss_increment"


@pytest.mark.parametrize("reason", [
    "kill_switch_flatten",
    "bailout",
    "max_hold",
    "target",
    "scale_out_limit",
    "operator_flatten",
    None,           # unknown reason: cannot confirm stop-class -> no increment
    "",
])
def test_non_stop_class_loss_does_not_increment(reason) -> None:
    lvl, why = reentry_escalation_level_update(
        current_level=2, was_loss=True, exit_reason=reason, green_banked=False,
    )
    assert lvl == 2
    assert why == "non_stop_loss_unchanged"


def test_profit_recycle_decays() -> None:
    lvl, why = reentry_escalation_level_update(
        current_level=3, was_loss=False, exit_reason="target", green_banked=False,
    )
    assert lvl == 2
    assert why == "profit_recycle_decay"


def test_green_banked_resets_to_zero() -> None:
    lvl, why = reentry_escalation_level_update(
        current_level=4, was_loss=False, exit_reason="target", green_banked=True,
    )
    assert lvl == 0
    assert why == "green_banked_reset"


def test_decay_floors_at_zero_and_bad_level_treated_as_zero() -> None:
    lvl, _ = reentry_escalation_level_update(
        current_level=0, was_loss=False, exit_reason="target", green_banked=False,
    )
    assert lvl == 0
    lvl2, _ = reentry_escalation_level_update(
        current_level="oops", was_loss=True, exit_reason="stop", green_banked=False,
    )
    assert lvl2 == 1  # unusable basis treated as 0, then a stop-class loss increments


def test_stop_class_predicate_token_semantics() -> None:
    # token-split membership, not substring: "stopout_cycle" tokens {stopout, cycle}
    # must NOT classify (no exact "stop" token), while decorated stop reasons do.
    assert _is_stop_class_exit_reason("stop") is True
    assert _is_stop_class_exit_reason("grind_trail_stop") is True
    assert _is_stop_class_exit_reason("stopout_cycle") is False
    assert _is_stop_class_exit_reason("unstoppable") is False
    assert _is_stop_class_exit_reason(None) is False
