"""Phase M.2-autopilot pure-model unit tests.

Tests ``evaluate_slice_gates`` against its full decision tree:

* revert (anomaly / approval missing / unhealthy / blocker)
* rate limit (same-day)
* off-stays-off
* terminal authoritative
* order lock (tilt never locked, killswitch waits for tilt, promotion waits for killswitch)
* shadow->compare advance (common gates)
* compare->authoritative advance (common + envelope + approval insert flag)
* hold / gate-fail detail

No I/O. No DB.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.trading.pattern_regime_autopilot_model import (
    ALLOWED_STAGES,
    SLICE_KILLSWITCH,
    SLICE_PROMOTION,
    SLICE_TILT,
    AutopilotConfig,
    SliceEvidence,
    business_days_between,
    compute_order_lock_state,
    evaluate_slice_gates,
)


TODAY = date(2026, 4, 17)  # Friday
YESTERDAY = date(2026, 4, 16)


def _cfg(**overrides) -> AutopilotConfig:
    base = dict(
        shadow_days=5,
        compare_days=10,
        min_decisions=100,
        tilt_mult_min=0.85,
        tilt_mult_max=1.25,
        promo_block_max_ratio=0.10,
        ks_max_fires_per_day=1.0,
        approval_days=30,
    )
    base.update(overrides)
    return AutopilotConfig(**base)


def _all_green_shadow_to_compare(slice_name: str = SLICE_TILT) -> SliceEvidence:
    return SliceEvidence(
        slice_name=slice_name,
        current_mode="shadow",
        days_in_stage=5,
        total_decisions=150,
        last_advance_date=YESTERDAY - timedelta(days=10),
        today_utc=TODAY,
        diagnostics_healthy=True,
        diagnostics_stale_hours=0.1,
        release_blocker_clean=True,
        scan_status_frozen_ok=True,
    )


def _all_green_compare_to_authoritative(slice_name: str = SLICE_TILT) -> SliceEvidence:
    return SliceEvidence(
        slice_name=slice_name,
        current_mode="compare",
        days_in_stage=10,
        total_decisions=500,
        last_advance_date=TODAY - timedelta(days=10),
        today_utc=TODAY,
        diagnostics_healthy=True,
        release_blocker_clean=True,
        scan_status_frozen_ok=True,
        tilt_mean_multiplier=1.05,
        promotion_block_ratio=0.05,
        killswitch_mean_fires_per_day=0.2,
    )


def _unlocked_order() -> object:
    return compute_order_lock_state(
        tilt_mode="authoritative",
        killswitch_mode="authoritative",
        promotion_mode="authoritative",
    )


def _shadow_all_order() -> object:
    return compute_order_lock_state(
        tilt_mode="shadow",
        killswitch_mode="shadow",
        promotion_mode="shadow",
    )


# ---------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------


class TestConfigValidation:
    def test_reject_negative_shadow_days(self):
        with pytest.raises(ValueError):
            _cfg(shadow_days=0)

    def test_reject_bad_tilt_envelope(self):
        with pytest.raises(ValueError):
            _cfg(tilt_mult_min=1.2, tilt_mult_max=1.1)

    def test_reject_bad_block_ratio(self):
        with pytest.raises(ValueError):
            _cfg(promo_block_max_ratio=1.5)

    def test_reject_negative_ks_fires(self):
        with pytest.raises(ValueError):
            _cfg(ks_max_fires_per_day=-0.1)


# ---------------------------------------------------------------------
# Revert (evaluated FIRST)
# ---------------------------------------------------------------------


class TestRevert:
    def test_authoritative_without_approval_reverts(self):
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="authoritative",
            days_in_stage=15,
            total_decisions=500,
            today_utc=TODAY,
            approval_live=False,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "revert"
        assert decision.from_mode == "authoritative"
        assert decision.to_mode == "compare"
        assert decision.reason_code == "authoritative_approval_missing"

    def test_refused_authoritative_reverts(self):
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="authoritative",
            days_in_stage=15,
            total_decisions=500,
            today_utc=TODAY,
            approval_live=True,
            anomaly_refused_authoritative=True,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "revert"
        assert decision.to_mode == "compare"
        assert decision.reason_code == "anomaly_refused_authoritative"

    def test_blocker_fail_reverts_compare_to_shadow(self):
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="compare",
            days_in_stage=10,
            total_decisions=300,
            today_utc=TODAY,
            release_blocker_clean=False,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "revert"
        assert decision.from_mode == "compare"
        assert decision.to_mode == "shadow"
        assert decision.reason_code == "release_blocker_failed"

    def test_blocker_fail_in_shadow_does_not_revert_to_off(self):
        """Autopilot never goes down to off; master-kill only."""
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="shadow",
            days_in_stage=3,
            total_decisions=50,
            today_utc=TODAY,
            release_blocker_clean=False,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert decision.to_mode == "shadow"

    def test_diagnostics_unhealthy_reverts(self):
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="authoritative",
            days_in_stage=15,
            total_decisions=500,
            today_utc=TODAY,
            approval_live=True,
            diagnostics_healthy=False,
            diagnostics_stale_hours=3.0,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "revert"
        assert decision.to_mode == "compare"
        assert decision.reason_code == "diagnostics_unhealthy"


# ---------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------


class TestRateLimit:
    def test_already_advanced_today_holds(self):
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="shadow",
            days_in_stage=5,
            total_decisions=500,
            today_utc=TODAY,
            last_advance_date=TODAY,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert decision.reason_code == "rate_limit_same_day"

    def test_advance_yesterday_does_not_block(self):
        ev = _all_green_shadow_to_compare()
        ev_today = SliceEvidence(
            slice_name=ev.slice_name,
            current_mode=ev.current_mode,
            days_in_stage=ev.days_in_stage,
            total_decisions=ev.total_decisions,
            last_advance_date=YESTERDAY,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
        )
        decision = evaluate_slice_gates(ev_today, _cfg(), _unlocked_order())
        assert decision.action == "advance"


# ---------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------


class TestTerminal:
    def test_off_stays_off(self):
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="off",
            days_in_stage=5,
            total_decisions=150,
            today_utc=TODAY,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert decision.reason_code == "off_stays_off"

    def test_authoritative_with_live_approval_holds_terminal(self):
        evidence = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="authoritative",
            days_in_stage=30,
            total_decisions=9999,
            today_utc=TODAY,
            approval_live=True,
        )
        decision = evaluate_slice_gates(evidence, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert decision.reason_code == "terminal_authoritative"


# ---------------------------------------------------------------------
# Order lock
# ---------------------------------------------------------------------


class TestOrderLock:
    def test_tilt_is_never_locked(self):
        ev = _all_green_shadow_to_compare(slice_name=SLICE_TILT)
        order = compute_order_lock_state(
            tilt_mode="shadow",
            killswitch_mode="shadow",
            promotion_mode="shadow",
        )
        decision = evaluate_slice_gates(ev, _cfg(), order)
        assert decision.action == "advance"

    def test_killswitch_blocked_if_tilt_not_authoritative(self):
        ev = _all_green_shadow_to_compare(slice_name=SLICE_KILLSWITCH)
        order = compute_order_lock_state(
            tilt_mode="compare",
            killswitch_mode="shadow",
            promotion_mode="shadow",
        )
        decision = evaluate_slice_gates(ev, _cfg(), order)
        assert decision.action == "blocked_by_order_lock"
        assert "order_lock" in decision.reason_code

    def test_killswitch_unblocked_when_tilt_authoritative(self):
        ev = _all_green_shadow_to_compare(slice_name=SLICE_KILLSWITCH)
        order = compute_order_lock_state(
            tilt_mode="authoritative",
            killswitch_mode="shadow",
            promotion_mode="shadow",
        )
        decision = evaluate_slice_gates(ev, _cfg(), order)
        assert decision.action == "advance"

    def test_promotion_requires_killswitch_authoritative(self):
        ev = _all_green_shadow_to_compare(slice_name=SLICE_PROMOTION)
        order = compute_order_lock_state(
            tilt_mode="authoritative",
            killswitch_mode="compare",
            promotion_mode="shadow",
        )
        decision = evaluate_slice_gates(ev, _cfg(), order)
        assert decision.action == "blocked_by_order_lock"

    def test_promotion_advances_when_both_prior_authoritative(self):
        ev = _all_green_shadow_to_compare(slice_name=SLICE_PROMOTION)
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "advance"


# ---------------------------------------------------------------------
# Shadow -> compare gate
# ---------------------------------------------------------------------


class TestShadowToCompare:
    def test_happy_path_advances(self):
        ev = _all_green_shadow_to_compare()
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "advance"
        assert decision.from_mode == "shadow"
        assert decision.to_mode == "compare"
        assert decision.requires_approval_insert is False

    def test_insufficient_days_holds(self):
        ev = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="shadow",
            days_in_stage=3,
            total_decisions=200,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert "days_in_stage" in decision.reason_code

    def test_insufficient_decisions_holds(self):
        ev = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="shadow",
            days_in_stage=6,
            total_decisions=50,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert "total_decisions" in decision.reason_code

    def test_diagnostics_unhealthy_holds_in_shadow(self):
        ev = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="shadow",
            days_in_stage=6,
            total_decisions=200,
            today_utc=TODAY,
            diagnostics_healthy=False,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        # In shadow, revert can't go below shadow; it becomes a hold.
        assert decision.action == "hold"
        assert "diagnostics_healthy" in decision.reason_code


# ---------------------------------------------------------------------
# Compare -> authoritative gate
# ---------------------------------------------------------------------


class TestCompareToAuthoritative:
    def test_happy_path_advances_with_approval_insert(self):
        ev = _all_green_compare_to_authoritative(slice_name=SLICE_TILT)
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "advance"
        assert decision.from_mode == "compare"
        assert decision.to_mode == "authoritative"
        assert decision.requires_approval_insert is True

    def test_tilt_envelope_high_blocks(self):
        ev = _all_green_compare_to_authoritative(slice_name=SLICE_TILT)
        ev = SliceEvidence(
            slice_name=ev.slice_name,
            current_mode=ev.current_mode,
            days_in_stage=ev.days_in_stage,
            total_decisions=ev.total_decisions,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
            tilt_mean_multiplier=1.5,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert "envelope_tilt_multiplier" in decision.reason_code

    def test_tilt_envelope_low_blocks(self):
        ev = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="compare",
            days_in_stage=12,
            total_decisions=300,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
            tilt_mean_multiplier=0.60,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert "envelope_tilt_multiplier" in decision.reason_code

    def test_promotion_envelope_ratio_high_blocks(self):
        ev = SliceEvidence(
            slice_name=SLICE_PROMOTION,
            current_mode="compare",
            days_in_stage=12,
            total_decisions=300,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
            promotion_block_ratio=0.35,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert "envelope_promotion_block_ratio" in decision.reason_code

    def test_killswitch_envelope_fires_high_blocks(self):
        ev = SliceEvidence(
            slice_name=SLICE_KILLSWITCH,
            current_mode="compare",
            days_in_stage=12,
            total_decisions=300,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
            killswitch_mean_fires_per_day=5.0,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert "envelope_killswitch_mean_fires" in decision.reason_code

    def test_missing_envelope_blocks_for_tilt(self):
        ev = SliceEvidence(
            slice_name=SLICE_TILT,
            current_mode="compare",
            days_in_stage=12,
            total_decisions=300,
            today_utc=TODAY,
            diagnostics_healthy=True,
            release_blocker_clean=True,
            scan_status_frozen_ok=True,
            tilt_mean_multiplier=None,
        )
        decision = evaluate_slice_gates(ev, _cfg(), _unlocked_order())
        assert decision.action == "hold"
        assert "envelope_tilt_multiplier" in decision.reason_code


# ---------------------------------------------------------------------
# Business-day helper
# ---------------------------------------------------------------------


class TestBusinessDays:
    def test_weekday_span_counts_weekdays(self):
        # 2026-04-13 Mon -> 2026-04-17 Fri exclusive-end inclusive-start
        assert (
            business_days_between(date(2026, 4, 13), date(2026, 4, 17))
            == 4
        )

    def test_spanning_weekend(self):
        # Fri -> next Fri = 5 weekdays
        assert (
            business_days_between(date(2026, 4, 10), date(2026, 4, 17))
            == 5
        )

    def test_zero_or_negative(self):
        assert business_days_between(date(2026, 4, 17), date(2026, 4, 17)) == 0
        assert business_days_between(date(2026, 4, 17), date(2026, 4, 16)) == 0


# ---------------------------------------------------------------------
# Order-lock state validation
# ---------------------------------------------------------------------


class TestOrderLockState:
    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError):
            compute_order_lock_state(
                tilt_mode="nope",
                killswitch_mode="shadow",
                promotion_mode="shadow",
            )
