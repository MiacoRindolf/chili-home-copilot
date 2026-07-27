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


def test_rapid_whipsaw_stop_class_loss_double_increments() -> None:
    # L4 (SILO autopsy): consecutive stop-class losses inside the rapid window
    # double-increment so the escalated tier binds by entry #2-3, not #6.
    lvl, why = reentry_escalation_level_update(
        current_level=1, was_loss=True, exit_reason="trail_stop",
        green_banked=False, rapid_stopout=True,
    )
    assert lvl == 3
    assert why == "rapid_whipsaw_double_increment"


def test_rapid_flag_ignored_for_non_stop_class_and_profit_paths() -> None:
    # rapid_stopout must not touch the non-stop-loss / decay / reset semantics.
    lvl, why = reentry_escalation_level_update(
        current_level=2, was_loss=True, exit_reason="bailout",
        green_banked=False, rapid_stopout=True,
    )
    assert (lvl, why) == (2, "non_stop_loss_unchanged")
    lvl2, why2 = reentry_escalation_level_update(
        current_level=3, was_loss=False, exit_reason="target",
        green_banked=False, rapid_stopout=True,
    )
    assert (lvl2, why2) == (2, "profit_recycle_decay")
    lvl3, why3 = reentry_escalation_level_update(
        current_level=4, was_loss=False, exit_reason="target",
        green_banked=True, rapid_stopout=True,
    )
    assert (lvl3, why3) == (0, "green_banked_reset")


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


# ── Review m2: day-leader structural substitute ──────────────────────────────
# A leader whose entries only fire via non-structural (volume-confirmation)
# reasons must not be permanently WAIT-blocked. It may substitute a STRICT
# equivalent: readable POSITIVE tape AND an actual reclaim above the prior
# failure — both actively satisfied (no skip-on-missing). Non-leaders keep the
# strict structural requirement.

def test_leader_substitute_clears_non_structural_with_tape_and_reclaim() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=False,
        live_price=6.98, prior_hwm=6.90, prior_exit_price=6.80,
        prior_risk_dist=0.05, tape_accel=1.2, is_day_leader=True,
    )
    assert allowed is True
    assert dbg.get("leader_structural_substitute") is True


def test_leader_substitute_requires_positive_tape() -> None:
    # leader + reclaim met but tape not lifting (<=0) => substitute fails => block
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=False,
        live_price=6.98, prior_hwm=6.90, prior_exit_price=6.80,
        prior_risk_dist=0.05, tape_accel=-0.3, is_day_leader=True,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    assert dbg.get("leader_structural_substitute") is False


def test_leader_substitute_requires_actual_reclaim_no_skip() -> None:
    # leader + positive tape but price BELOW the reclaim reference => block
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=False,
        live_price=6.70, prior_hwm=6.90, prior_exit_price=6.80,
        prior_risk_dist=0.05, tape_accel=1.2, is_day_leader=True,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"


def test_leader_substitute_requires_a_reference_no_free_pass() -> None:
    # leader + positive tape but NO prior reference at all => substitute cannot
    # be satisfied (unlike step-2 which skips on missing ref) => block.
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=False,
        live_price=6.98, prior_hwm=None, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=1.2, is_day_leader=True,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"


def test_non_leader_still_blocks_on_non_structural_even_with_tape_and_reclaim() -> None:
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=False,
        live_price=6.98, prior_hwm=6.90, prior_exit_price=6.80,
        prior_risk_dist=0.05, tape_accel=1.2, is_day_leader=False,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    assert dbg.get("leader_structural_substitute") is None


def test_leader_with_structural_trigger_unaffected_by_substitute() -> None:
    # structural trigger present => substitute path not taken; normal flow passes.
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True,
        live_price=6.98, prior_hwm=6.90, prior_exit_price=6.80,
        prior_risk_dist=0.05, tape_accel=1.2, is_day_leader=True,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"


def test_leader_substitute_margin_scales_with_level() -> None:
    # level 3 demands ref + (3-1)*risk_dist = 6.90 + 2*0.05 = 7.00; 6.98 < 7.00 => block
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=3, structural_trigger=False,
        live_price=6.98, prior_hwm=6.90, prior_exit_price=6.80,
        prior_risk_dist=0.05, tape_accel=1.2, is_day_leader=True,
    )
    assert allowed is False
    # and at 7.01 it clears
    allowed2, _ = reentry_escalation_decision(
        enabled=True, escalation_level=3, structural_trigger=False,
        live_price=7.01, prior_hwm=6.90, prior_exit_price=6.80,
        prior_risk_dist=0.05, tape_accel=1.2, is_day_leader=True,
    )
    assert allowed2 is True


# ── L4 cadence bookkeeping: rapid_whipsaw_cadence_update (pure helper) ──────────
# Both ends of the "consecutive stop-class losses" pair share the ONE classifier;
# the marker stamps on every stop-class loss regardless of the decision flag and
# window (only the DECISION is gated); the window binds VERBATIM (0 => disabled);
# a corrupt marker is overwritten, never latched.

from datetime import datetime, timedelta  # noqa: E402

from app.services.trading.momentum_neural.risk_policy import (  # noqa: E402
    rapid_whipsaw_cadence_update,
)

_NOW = datetime(2026, 7, 27, 13, 30, 0)


def _cadence(**kw):
    base = dict(
        was_loss=True, exit_reason="trail_stop",
        prev_marker_raw=None, now=_NOW,
        window_seconds=120.0, decision_enabled=True,
    )
    base.update(kw)
    return rapid_whipsaw_cadence_update(**base)


def test_cadence_stop_to_stop_within_window_is_rapid_and_restamps() -> None:
    prev = (_NOW - timedelta(seconds=70)).isoformat()
    rapid, marker = _cadence(prev_marker_raw=prev)
    assert rapid is True
    assert marker == _NOW.isoformat()


def test_cadence_stop_to_stop_outside_window_not_rapid_but_stamps() -> None:
    prev = (_NOW - timedelta(seconds=121)).isoformat()
    rapid, marker = _cadence(prev_marker_raw=prev)
    assert rapid is False
    assert marker == _NOW.isoformat()


def test_cadence_window_zero_disables_decision_but_still_stamps() -> None:
    # Config contract: 0 => disabled — must bind VERBATIM (the `or 120.0`
    # falsy-coercion defect this test pins against regression).
    prev = (_NOW - timedelta(seconds=5)).isoformat()
    rapid, marker = _cadence(prev_marker_raw=prev, window_seconds=0.0)
    assert rapid is False
    assert marker == _NOW.isoformat()


def test_cadence_decision_flag_off_still_stamps_marker() -> None:
    # Bookkeeping runs regardless of the flag so a later flag flip never
    # compares against a stale marker; only the decision is gated.
    prev = (_NOW - timedelta(seconds=5)).isoformat()
    rapid, marker = _cadence(prev_marker_raw=prev, decision_enabled=False)
    assert rapid is False
    assert marker == _NOW.isoformat()


def test_cadence_non_stop_class_loss_neither_stamps_nor_measures() -> None:
    for reason in ("bailout", "max_hold", "kill_switch_flatten", None, ""):
        rapid, marker = _cadence(
            exit_reason=reason,
            prev_marker_raw=(_NOW - timedelta(seconds=5)).isoformat(),
        )
        assert (rapid, marker) == (False, None), reason


def test_cadence_profit_exit_neither_stamps_nor_measures() -> None:
    rapid, marker = _cadence(was_loss=False)
    assert (rapid, marker) == (False, None)


def test_cadence_absent_marker_not_rapid_but_stamps_first() -> None:
    rapid, marker = _cadence(prev_marker_raw=None)
    assert rapid is False
    assert marker == _NOW.isoformat()


def test_cadence_corrupt_marker_fails_open_and_restamps() -> None:
    # A corrupt marker must be OVERWRITTEN, never latched — otherwise rapid
    # detection dies silently for the rest of the session.
    rapid, marker = _cadence(prev_marker_raw="not-a-date")
    assert rapid is False
    assert marker == _NOW.isoformat()


def test_cadence_z_suffixed_marker_parses() -> None:
    prev = (_NOW - timedelta(seconds=30)).isoformat() + "Z"
    rapid, marker = _cadence(prev_marker_raw=prev)
    assert rapid is True
    assert marker == _NOW.isoformat()


def test_cadence_future_marker_negative_gap_not_rapid() -> None:
    prev = (_NOW + timedelta(seconds=30)).isoformat()
    rapid, marker = _cadence(prev_marker_raw=prev)
    assert rapid is False
    assert marker == _NOW.isoformat()


def test_cadence_bail_between_stops_does_not_reset_the_pair() -> None:
    # Composition per the documented contract: stop at t0, bailout at t0+60
    # (no stamp), stop at t0+90 -> gap measured from t0 (90s <= 120s) => rapid.
    t0 = _NOW - timedelta(seconds=90)
    _, marker0 = rapid_whipsaw_cadence_update(
        was_loss=True, exit_reason="trail_stop", prev_marker_raw=None,
        now=t0, window_seconds=120.0, decision_enabled=True,
    )
    assert marker0 == t0.isoformat()
    rapid_bail, marker_bail = rapid_whipsaw_cadence_update(
        was_loss=True, exit_reason="bailout", prev_marker_raw=marker0,
        now=_NOW - timedelta(seconds=30), window_seconds=120.0,
        decision_enabled=True,
    )
    assert (rapid_bail, marker_bail) == (False, None)
    rapid, _ = rapid_whipsaw_cadence_update(
        was_loss=True, exit_reason="trail_stop", prev_marker_raw=marker0,
        now=_NOW, window_seconds=120.0, decision_enabled=True,
    )
    assert rapid is True
