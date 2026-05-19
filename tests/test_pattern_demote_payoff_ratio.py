"""f-evaluation-function-fix Tier A #2 (2026-05-18).

Pin the payoff-ratio protection in ``_matches_thin_evidence_criteria``.

The 2026-05-18 audit found pattern 585 (the system's only proven alpha:
CPCV 1.41, WR 35%, avg return 1.68%/trade, payoff ratio ~3:1) had been
demoted by ``run_thin_evidence_demote`` -- a gate that uses WR alone.
Skew-driven strategies systematically score below WR floors despite
being positive-expectancy.

This release adds a payoff-ratio protection: when
``payoff_ratio >= chili_pattern_demote_payoff_ratio_floor`` (default
1.5) AND ``payoff_ratio_n >= chili_pattern_demote_payoff_ratio_min_n``
(default 5), the pattern is protected from the realized-WR demote
regardless of WR.

Helper-level (no DB), mirrors ``test_pattern_demote_thresholds.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.learning import _matches_thin_evidence_criteria


def _settings_stub(
    *,
    min_realized: int = 30,
    require_cpcv_degrade: bool = True,
    payoff_floor: float = 1.5,
    payoff_min_n: int = 5,
):
    return SimpleNamespace(
        chili_pattern_demote_min_realized_trades=min_realized,
        chili_pattern_demote_require_cpcv_degrade=require_cpcv_degrade,
        chili_pattern_demote_payoff_ratio_floor=payoff_floor,
        chili_pattern_demote_payoff_ratio_min_n=payoff_min_n,
    )


def _stub(
    *,
    trade_count: int = 86,
    win_rate: float = 0.25,
    oos_win_rate=None,
    cpcv_median_sharpe=None,
    promotion_gate_reasons=("provisional_small_paths",),
    payoff_ratio=None,
    payoff_ratio_n=None,
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
        payoff_ratio=payoff_ratio,
        payoff_ratio_n=payoff_ratio_n,
    )


# ── Payoff-ratio protection (the pattern-585 fingerprint) ────────────


def test_585_post_audit_fingerprint_protected_by_payoff_ratio():
    """Pattern 585 today: n=86, WR=0.25 (under the 0.33 floor), OOS=NULL,
    provisional gate, NO CPCV-passing protection (mid-cycle CPCV
    re-eval pending), but payoff_ratio=3.0 over 86 trades. MUST NOT
    match because the payoff-ratio protection short-circuits to False."""
    p = _stub(
        trade_count=86,
        win_rate=0.25,
        cpcv_median_sharpe=None,  # CPCV-passing protection NOT engaged
        payoff_ratio=3.0,
        payoff_ratio_n=86,
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_payoff_ratio_at_floor_protects():
    """At exactly the floor (1.5), the predicate must protect (>= not >)."""
    p = _stub(payoff_ratio=1.5, payoff_ratio_n=10)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_payoff_ratio_below_floor_still_demotes():
    """A pattern with payoff_ratio=1.49 (below 1.5 floor) AND poor WR
    must still match the demote criteria."""
    p = _stub(payoff_ratio=1.49, payoff_ratio_n=10)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


def test_payoff_ratio_n_below_min_does_not_protect():
    """A pattern with payoff_ratio=10.0 but only 3 trades (below n=5
    floor) must NOT be protected -- a payoff ratio backed by 3 trades
    is noise."""
    p = _stub(payoff_ratio=10.0, payoff_ratio_n=3)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


def test_payoff_ratio_null_does_not_break_predicate():
    """A pattern that has not been backfilled (payoff_ratio=None) must
    fall through to the existing demote path and match (no crash)."""
    p = _stub(payoff_ratio=None, payoff_ratio_n=None)
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


def test_payoff_ratio_n_null_does_not_break_predicate():
    p = _stub(payoff_ratio=2.5, payoff_ratio_n=None)
    s = _settings_stub()
    # payoff_ratio is set but n is None -- predicate cannot enforce
    # the n-floor, falls through to demote.
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


def test_payoff_floor_disabled_via_high_threshold():
    """Operator can disable the protection by setting the floor very
    high (effectively unreachable). With floor=1e9, even a 10:1 payoff
    pattern matches the demote criteria."""
    p = _stub(payoff_ratio=10.0, payoff_ratio_n=20)
    s = _settings_stub(payoff_floor=1.0e9)
    assert _matches_thin_evidence_criteria(p, settings_=s) is True


# ── Layered with the existing CPCV-passing protection ────────────────


def test_cpcv_passing_still_protects_independent_of_payoff_ratio():
    """The CPCV-passing protection (>= 1.0) and the payoff-ratio
    protection are independent. A pattern with CPCV 1.5 and payoff
    ratio NULL is still protected by CPCV alone."""
    p = _stub(
        cpcv_median_sharpe=1.5,
        payoff_ratio=None,
        payoff_ratio_n=None,
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is False


def test_neither_protection_engages_demotes():
    """Pattern with weak CPCV (below 1.0) AND no payoff ratio falls
    through both protections; matches demote criteria."""
    p = _stub(
        cpcv_median_sharpe=0.5,
        payoff_ratio=None,
        payoff_ratio_n=None,
    )
    s = _settings_stub()
    assert _matches_thin_evidence_criteria(p, settings_=s) is True
