"""Tests for realized-edge pilot sizing — the sizing-layer completion of the
realized-edge promotion lane.

A pattern promoted to pilot via the realized-edge lane lacks the CPCV pilot_score
the lane bypassed, so pilot_promoted_risk_multiplier used to return 0.0 (blocked
as pilot_promoted_confidence_below_policy) — it could never place a live trade.
The fix sizes such pilots by their realized confidence (small, reversible);
non-realized patterns below the score bar still get 0.0.
"""
from types import SimpleNamespace

from app.services.trading import pattern_shadow_vetting as psv


class _Settings:
    chili_pilot_promoted_enabled = True
    chili_shadow_vetting_realized_edge_pilot_enabled = True
    chili_alpha_portfolio_min_realized_trades = 5
    chili_shadow_vetting_realized_edge_ci_level = 0.90


def _policy(pid, pilot_score):
    return {
        "threshold": 0.77,
        "prior_strength": 5.0,
        "rows": [
            {
                "scan_pattern_id": pid,
                "lifecycle_stage": "pilot_promoted",
                "pilot_score": pilot_score,
                "cpcv_n_paths": 8,
                "evidence_maturity": 0.21,
            }
        ],
    }


class _Db:
    def __init__(self, pattern):
        self._pattern = pattern

    def get(self, _model, _pid):
        return self._pattern


def test_realized_edge_pilot_below_threshold_is_sized_not_blocked(monkeypatch):
    # 1074-like: pilot_score 0.75 < 0.77 threshold, but a proven realized winner.
    monkeypatch.setattr(psv, "_pilot_policy", lambda db, settings_=None: _policy(1074, 0.75))
    pat = SimpleNamespace(
        raw_realized_win_rate=0.449, raw_realized_trade_count=138,
        payoff_ratio=2.87, avg_winner_pct=0.03,
    )
    m = psv.pilot_promoted_risk_multiplier(_Db(pat), 1074, settings_=_Settings())
    assert m is not None
    assert m > 0.0  # sizeable (small pilot), NOT blocked
    assert m < 1.0  # but small/reversible, not full size


def test_non_realized_pattern_below_threshold_still_blocks(monkeypatch):
    # 1011-like loser: pilot_score below threshold AND no realized edge -> 0.0.
    monkeypatch.setattr(psv, "_pilot_policy", lambda db, settings_=None: _policy(1011, 0.50))
    pat = SimpleNamespace(
        raw_realized_win_rate=0.081, raw_realized_trade_count=37,
        payoff_ratio=5.34, avg_winner_pct=0.16,
    )
    m = psv.pilot_promoted_risk_multiplier(_Db(pat), 1011, settings_=_Settings())
    assert m == 0.0  # loser stays blocked


def test_high_pilot_score_unchanged(monkeypatch):
    # pilot_score >= threshold uses the normal pilot_score sizing (no realized path).
    monkeypatch.setattr(psv, "_pilot_policy", lambda db, settings_=None: _policy(585, 0.90))
    pat = SimpleNamespace(
        raw_realized_win_rate=0.5, raw_realized_trade_count=48,
        payoff_ratio=3.5, avg_winner_pct=0.10,
    )
    m = psv.pilot_promoted_risk_multiplier(_Db(pat), 585, settings_=_Settings())
    assert m is not None and m > 0.0  # normal pilot sizing path


def test_flag_off_below_threshold_blocks(monkeypatch):
    # With the realized-edge flag off, below-threshold reverts to 0.0 (legacy).
    monkeypatch.setattr(psv, "_pilot_policy", lambda db, settings_=None: _policy(1074, 0.75))
    pat = SimpleNamespace(
        raw_realized_win_rate=0.449, raw_realized_trade_count=138,
        payoff_ratio=2.87, avg_winner_pct=0.03,
    )

    class _Off(_Settings):
        chili_shadow_vetting_realized_edge_pilot_enabled = False

    m = psv.pilot_promoted_risk_multiplier(_Db(pat), 1074, settings_=_Off())
    assert m == 0.0
