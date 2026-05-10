"""f-promotion-pipeline-rebalance Phase 1 (2026-05-09).

Pin Phase 1's two new guards across both demote paths:

  1. ``learning._matches_thin_evidence_criteria`` (the every-cycle
     thin-evidence sweep).
  2. ``promotion_evidence_audit.run_promotion_evidence_audit`` (the
     daily 02:15 PT cron that demotes for missing-OOS evidence).

Phase 1 protections:

  A. **Sample-size floor**:
     ``settings.chili_pattern_demote_min_realized_trades`` (default 30).
     Patterns with ``trade_count < 30`` are NOT demoted on realized
     stats — gate-laundered noise isn't a valid signal.

  B. **CPCV-passing escape** (when
     ``chili_pattern_demote_require_cpcv_degrade=True``):
     A pattern with ``cpcv_median_sharpe >= 1.0`` is protected from
     BOTH demote paths even if its realized WR is poor or its OOS
     evidence is missing.

The pattern-585 fingerprint (n=8 trades, 25% WR, OOS=NULL,
provisional gate, CPCV sharpe=1.40) is pinned in
``test_pattern_585_protected_*`` — without these guards, 585 dies at
the next 02:15 PT audit run.

Helper-level (no DB except where noted).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.trading.learning import (
    THIN_EVIDENCE_CPCV_PASSING_SHARPE_FLOOR,
    _matches_thin_evidence_criteria,
)


def _settings_stub(
    *,
    min_realized: int = 30,
    require_cpcv_degrade: bool = True,
):
    return SimpleNamespace(
        chili_pattern_demote_min_realized_trades=min_realized,
        chili_pattern_demote_require_cpcv_degrade=require_cpcv_degrade,
    )


def _stub_thin_evidence(
    *,
    trade_count: int = 8,
    win_rate: float = 0.25,
    oos_win_rate=None,
    cpcv_median_sharpe=None,
    promotion_gate_reasons=("provisional_small_paths",),
):
    return SimpleNamespace(
        id=585,
        lifecycle_stage="promoted",
        trade_count=trade_count,
        win_rate=win_rate,
        oos_win_rate=oos_win_rate,
        cpcv_median_sharpe=cpcv_median_sharpe,
        promotion_gate_reasons=list(promotion_gate_reasons),
        promotion_status="promoted",
    )


# ── A. Sample-size floor (PROTECTS pattern 585 with n=8) ────────────


def test_pattern_585_n8_protected_by_sample_floor():
    """Pattern 585's exact fingerprint (n=8 trades, 25% WR, no CPCV
    set) MUST NOT match — the sample-size floor (default 30) returns
    False before the predicate even checks WR."""
    p = _stub_thin_evidence(trade_count=8)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_n_below_floor_protected_at_29():
    p = _stub_thin_evidence(trade_count=29)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_n_at_floor_30_continues_check():
    """At the floor (30), the realized signal becomes admissible.
    With WR=0.25 + OOS NULL + provisional gate + no CPCV passing,
    the predicate matches."""
    p = _stub_thin_evidence(trade_count=30)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


def test_floor_setting_lower_than_30_widens_demote_zone():
    """Operator override: setting min_realized=10 reverts to the
    pre-Phase-1 behavior — n=15 now demotable."""
    p = _stub_thin_evidence(trade_count=15)
    s = _settings_stub(min_realized=10)
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


# ── B. CPCV-passing escape (PROTECTS pattern 585 with sharpe=1.40) ──


def test_pattern_585_full_fingerprint_protected_by_cpcv_passing():
    """Pattern 585 with n>=floor BUT cpcv_median_sharpe=1.40 is
    protected by the CPCV-passing escape."""
    p = _stub_thin_evidence(
        trade_count=30, cpcv_median_sharpe=1.40,
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_cpcv_at_floor_1p0_protected():
    """Floor is >= 1.0 (inclusive)."""
    p = _stub_thin_evidence(
        trade_count=30,
        cpcv_median_sharpe=THIN_EVIDENCE_CPCV_PASSING_SHARPE_FLOOR,
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_cpcv_below_floor_not_protected():
    """Sharpe < 1.0 means CPCV is degraded — protection lifts;
    predicate matches."""
    p = _stub_thin_evidence(trade_count=30, cpcv_median_sharpe=0.95)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


def test_cpcv_null_not_protected():
    """No CPCV record → no protection. Predicate matches if other
    criteria met."""
    p = _stub_thin_evidence(trade_count=30, cpcv_median_sharpe=None)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


def test_require_cpcv_degrade_false_disables_escape():
    """Operator can opt out of the CPCV escape by setting
    chili_pattern_demote_require_cpcv_degrade=False — pattern with
    high CPCV but bad realized still demotes."""
    p = _stub_thin_evidence(trade_count=30, cpcv_median_sharpe=1.40)
    s = _settings_stub(require_cpcv_degrade=False)
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


# ── 02:15 PT audit — CPCV-passing filter ────────────────────────────


def test_02_15_audit_protects_cpcv_passing_pattern():
    """The 02:15 PT promotion_evidence_audit MUST NOT demote a
    pattern with cpcv_median_sharpe=1.40 even when its OOS evidence
    is missing — pattern 585's fingerprint exactly."""
    from app.services.trading.promotion_evidence_audit import (
        _filter_cpcv_passing,
    )

    incomplete_details = [
        {
            "id": 585, "name": "Intraday Squeeze",
            "lifecycle_stage": "promoted", "promotion_status": "promoted",
            "missing": ["oos_win_rate_null", "oos_trade_count_zero_or_null"],
            "cpcv_median_sharpe": 1.40,
        },
        {
            "id": 999, "name": "Bad Pattern",
            "lifecycle_stage": "promoted", "promotion_status": "promoted",
            "missing": ["oos_win_rate_null"],
            "cpcv_median_sharpe": 0.5,  # below floor — not protected
        },
        {
            "id": 1000, "name": "No CPCV Pattern",
            "lifecycle_stage": "promoted", "promotion_status": "promoted",
            "missing": ["cpcv_median_sharpe_null"],
            "cpcv_median_sharpe": None,  # null → not protected
        },
    ]
    actionable, retained = _filter_cpcv_passing(incomplete_details)
    assert 585 not in actionable
    assert 999 in actionable
    assert 1000 in actionable
    # All rows retained in the surfaced report (annotated).
    assert len(retained) == 3
    p585 = next(r for r in retained if r["id"] == 585)
    assert p585["cpcv_protected"] is True


def test_02_15_audit_no_cpcv_filter_when_setting_false():
    from app.config import settings
    from app.services.trading.promotion_evidence_audit import (
        _filter_cpcv_passing,
    )

    incomplete_details = [
        {
            "id": 585, "name": "Intraday Squeeze",
            "lifecycle_stage": "promoted", "promotion_status": "promoted",
            "missing": ["oos_win_rate_null"],
            "cpcv_median_sharpe": 1.40,
        },
    ]
    with patch.object(
        settings, "chili_pattern_demote_require_cpcv_degrade",
        False, create=True,
    ):
        actionable, retained = _filter_cpcv_passing(incomplete_details)
    # When require=False, CPCV protection is bypassed → 585 actionable.
    assert 585 in actionable
    assert retained[0]["cpcv_protected"] is False


# ── Sanity: existing predicate fields still apply when all guards pass ──


def test_lifecycle_not_promoted_short_circuits():
    p = _stub_thin_evidence(trade_count=30, cpcv_median_sharpe=0.5)
    p.lifecycle_stage = "challenged"
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_high_win_rate_short_circuits():
    p = _stub_thin_evidence(
        trade_count=30, win_rate=0.7, cpcv_median_sharpe=0.5,
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_oos_present_short_circuits():
    p = _stub_thin_evidence(
        trade_count=30, oos_win_rate=0.65, cpcv_median_sharpe=0.5,
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_no_provisional_gate_reason_short_circuits():
    p = _stub_thin_evidence(
        trade_count=30, cpcv_median_sharpe=0.5,
        promotion_gate_reasons=["sharpe_above_threshold"],
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


# ── Settings defaults pinned ────────────────────────────────────────


def test_phase_1_settings_defaults():
    """Pin Phase 1's safe defaults so an operator-side override
    requires explicit env action."""
    from app.config import settings
    assert settings.chili_pattern_demote_min_realized_trades == 30
    assert settings.chili_pattern_demote_require_cpcv_degrade is True
