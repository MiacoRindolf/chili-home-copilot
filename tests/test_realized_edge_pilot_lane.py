"""Tests for the adaptive realized-edge shadow->pilot promotion lane.

The lane graduates provably-profitable "grinder" shadow patterns that the
CPCV/quality-weighted pilot score under-credits, while never graduating
losers. Conservatism comes from a win-rate lower-confidence bound on the
realized geometry (already net of fees), so the bar is a provably positive
realized expectancy. Numbers below mirror live patterns observed in prod
(1074 winner, 1011/1248 losers, 1250 marginal).
"""
from types import SimpleNamespace

from app.services.trading.pattern_shadow_vetting import _realized_edge_pilot_eligible


class _Settings:
    chili_alpha_portfolio_min_realized_trades = 5
    chili_shadow_vetting_realized_edge_ci_level = 0.90


def _pattern(**kw):
    base = dict(
        raw_realized_trade_count=None,
        raw_realized_win_rate=None,
        payoff_ratio=None,
        avg_winner_pct=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_proven_grinder_graduates():
    # 1074-like: n=138, wr=0.449, payoff=2.87, avg_win=3% -> ev_lcb > 0.
    pat = _pattern(
        raw_realized_trade_count=138,
        raw_realized_win_rate=0.449,
        payoff_ratio=2.87,
        avg_winner_pct=0.03,
    )
    eligible, detail = _realized_edge_pilot_eligible(pat, settings_=_Settings())
    assert eligible is True
    assert detail["ev_lcb"] > 0
    # LCB haircut is applied (bound below the raw win rate).
    assert 0 < detail["wr_lcb"] < 0.449


def test_realized_loser_never_graduates():
    # 1011-like: n=37, wr=0.081 (8% WR), payoff=5.34, avg_win=16% -> ev_lcb < 0.
    pat = _pattern(
        raw_realized_trade_count=37,
        raw_realized_win_rate=0.081,
        payoff_ratio=5.34,
        avg_winner_pct=0.16,
    )
    eligible, detail = _realized_edge_pilot_eligible(pat, settings_=_Settings())
    assert eligible is False
    assert detail["ev_lcb"] <= 0


def test_low_payoff_low_winrate_rejected():
    # 1248-like: n=171, wr=0.216, payoff=0.87 (<1) -> negative expectancy.
    pat = _pattern(
        raw_realized_trade_count=171,
        raw_realized_win_rate=0.216,
        payoff_ratio=0.87,
        avg_winner_pct=0.02,
    )
    eligible, _ = _realized_edge_pilot_eligible(pat, settings_=_Settings())
    assert eligible is False


def test_thin_sample_rejected():
    # Strong-looking but sample below the floor -> insufficient evidence.
    pat = _pattern(
        raw_realized_trade_count=3,
        raw_realized_win_rate=0.7,
        payoff_ratio=3.0,
        avg_winner_pct=0.05,
    )
    eligible, detail = _realized_edge_pilot_eligible(pat, settings_=_Settings())
    assert eligible is False
    assert detail["reason"] == "insufficient_realized_evidence"


def test_missing_fields_rejected():
    pat = _pattern(raw_realized_trade_count=50, raw_realized_win_rate=0.5)
    eligible, detail = _realized_edge_pilot_eligible(pat, settings_=_Settings())
    assert eligible is False
    assert detail["reason"] == "insufficient_realized_evidence"


def test_higher_ci_is_stricter():
    # 1250-like marginal grinder: passes at 90% CI, fails at 99.99% (wider haircut).
    pat = _pattern(
        raw_realized_trade_count=73,
        raw_realized_win_rate=0.411,
        payoff_ratio=2.33,
        avg_winner_pct=0.02,
    )
    eligible90, d90 = _realized_edge_pilot_eligible(pat, settings_=_Settings())

    class _Strict(_Settings):
        chili_shadow_vetting_realized_edge_ci_level = 0.9999

    eligible_strict, d_strict = _realized_edge_pilot_eligible(pat, settings_=_Strict())
    assert eligible90 is True
    # Stricter CI lowers the win-rate LCB, so EV is more conservative (lower).
    assert d_strict["wr_lcb"] <= d90["wr_lcb"]
    assert d_strict["ev_lcb"] <= d90["ev_lcb"]
    assert eligible_strict is False
