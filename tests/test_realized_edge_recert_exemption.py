"""Tests for the realized-edge recert exemption.

A realized-edge-promoted pilot carries CPCV/OOS recert debt (the evidence its
proven realized EV supersedes). Without an exemption it freezes in observation
and can never place the small reversible live trade the pilot lane exists for.
The exemption is tightly scoped: pilot lifecycle + provably-positive realized
edge + only CPCV/OOS-solvable reasons (never hard execution/data debt or stale
decay, never non-realized patterns).
"""
from types import SimpleNamespace

from app.services.trading import auto_trader as at


def _pilot(
    reason="promotion_gate_not_currently_passed",
    wr=0.449, n=138, payoff=2.87, avg_win=0.03,
    stage="pilot_promoted",
):
    return SimpleNamespace(
        lifecycle_stage=stage,
        recert_required=True,
        recert_reason=reason,
        portfolio_gate_json=None,
        raw_realized_win_rate=wr,
        raw_realized_trade_count=n,
        payoff_ratio=payoff,
        avg_winner_pct=avg_win,
    )


def test_realized_edge_pilot_cpcv_recert_is_exempt():
    pat = _pilot()  # proven winner, promotion_gate_not_currently_passed
    assert at._realized_edge_recert_exempt(pat) is True
    assert at._live_recert_allowance(pat) == at.PILOT_BOOTSTRAP_RECERT_ALLOWANCE


def test_missing_oos_recert_is_exempt():
    pat = _pilot(reason="missing_oos_recert")
    assert at._realized_edge_recert_exempt(pat) is True


def test_stale_oos_recert_not_exempt():
    # stale_oos_recert is deliberately excluded (decay signal), even though it's
    # otherwise CPCV/OOS-solvable.
    pat = _pilot(reason="stale_oos_recert")
    assert at._realized_edge_recert_exempt(pat) is False


def test_mixed_with_hard_reason_not_exempt():
    # Any non-CPCV/OOS (e.g. an execution/data block) in the set blocks exemption.
    pat = _pilot(reason="promotion_gate_not_currently_passed,execution_quality_block")
    assert at._realized_edge_recert_exempt(pat) is False


def test_non_realized_pilot_not_exempt():
    # Loser (realized edge not provably positive) -> not exempt even with CPCV debt.
    pat = _pilot(wr=0.081, n=37, payoff=5.34, avg_win=0.16)
    assert at._realized_edge_recert_exempt(pat) is False
    # And the allowance falls through to None / probation (not pilot-bootstrap here).
    assert at._live_recert_allowance(pat) != at.PILOT_BOOTSTRAP_RECERT_ALLOWANCE


def test_non_pilot_not_exempt():
    pat = _pilot(stage="promoted")
    assert at._realized_edge_recert_exempt(pat) is False


def test_no_recert_debt_returns_none():
    pat = _pilot()
    pat.recert_required = False
    assert at._live_recert_allowance(pat) is None


def test_flag_off_not_exempt(monkeypatch):
    monkeypatch.setattr(
        at.settings, "chili_shadow_vetting_realized_edge_pilot_enabled", False
    )
    pat = _pilot()
    assert at._realized_edge_recert_exempt(pat) is False
