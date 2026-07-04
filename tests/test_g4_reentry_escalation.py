"""G4 P2 — same-symbol re-entry escalation gate (pure helper, no I/O).

Losers-eat-the-winner fix (CLRO 07-02): two earlier full-risk stops on the same
name ate the later +$285 leg down to +$13 net. After a stop-out, the next
entry on that symbol must clear a raised bar (structural trigger + reclaim of
the prior failure's high-water mark, scaling with consecutive stops) rather
than being a free re-fire on the identical setup that just failed. This is a
WAIT, not a lockout — fail-open on unusable numeric basis (current behavior)."""

from __future__ import annotations

from app.services.trading.momentum_neural.risk_policy import reentry_escalation_decision


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
