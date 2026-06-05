"""Tests for the realized-aware empirical-Bayes edge prior.

When shrinking the regime-conditioned hit rate for the expected-edge gate, a
well-sampled overall realized win rate anchors the estimate so a noisy regime
cell can't bury a proven pattern's edge — while losers (low realized WR) stay
below break-even. Numbers mirror live patterns (1074 winner, 1011 loser).
"""
from types import SimpleNamespace

from app.services.trading import auto_trader_rules as R


class _Settings:
    chili_edge_realized_aware_prior_enabled = True
    chili_realized_ev_min_trades = 5


def _pattern(wr, n):
    return SimpleNamespace(raw_realized_win_rate=wr, raw_realized_trade_count=n)


def _prob(pat_ctx, pattern, settings):
    # scan_pattern_id=0 makes _directional_edge_probability short-circuit to
    # None, so no DB is touched and the realized prior is the only adjustment.
    return R._pattern_probability(
        None,
        alert=SimpleNamespace(scan_pattern_id=0),
        pat_ctx=pat_ctx,
        confidence=0.5,
        settings=settings,
        pattern=pattern,
        reward=0.04,
        loss=0.02,
    )


def test_overall_realized_winrate_helper():
    assert R._overall_realized_winrate(_pattern(0.449, 138)) == (0.449, 138)
    assert R._overall_realized_winrate(_pattern(None, 138)) is None
    assert R._overall_realized_winrate(_pattern(0.4, 0)) is None
    assert R._overall_realized_winrate(None) is None


def test_proven_pattern_pulled_up_toward_realized():
    # 1074-like: regime hit rate 0.30 (n=102), overall realized 0.449 (n=138).
    p, source, n, details = _prob(
        {"hit_rate": 0.30, "n_trades_effective": 102}, _pattern(0.449, 138), _Settings()
    )
    # 50/50 blend (prior_n capped at regime n=102): (0.30+0.449)/2 = 0.3745,
    # which clears the ~0.337 break-even that the raw 0.30 failed.
    assert 0.36 < p < 0.39
    assert "realized_prior_blend" in source
    assert details["realized_prior_mean"] == 0.449


def test_flag_off_reverts_to_neutral_prior():
    class _Off(_Settings):
        chili_edge_realized_aware_prior_enabled = False

    p, source, n, details = _prob(
        {"hit_rate": 0.30, "n_trades_effective": 102}, _pattern(0.449, 138), _Off()
    )
    # Neutral 0.5 prior with the tiny default weight (5) barely moves 0.30.
    assert 0.29 < p < 0.32
    assert "shrunk" in source and "realized_prior_blend" not in source


def test_loser_stays_below_breakeven():
    # 1011-like: regime hit rate ~0.10, overall realized 0.081 (n=37).
    p, source, n, details = _prob(
        {"hit_rate": 0.10, "n_trades_effective": 40}, _pattern(0.081, 37), _Settings()
    )
    # Blends toward 0.081 -> stays well below any sane break-even (~0.33).
    assert p < 0.12


def test_high_regime_pattern_not_inflated_above_realized():
    # If the regime rate already exceeds the overall realized WR, the blend pulls
    # it DOWN toward realized (conservative) rather than inflating it.
    p, _, _, _ = _prob(
        {"hit_rate": 0.60, "n_trades_effective": 100}, _pattern(0.40, 100), _Settings()
    )
    assert 0.49 < p < 0.51  # (0.60 + 0.40)/2


def test_cold_start_pattern_uses_neutral_prior():
    # No realized record -> falls back to neutral 0.5 prior unchanged.
    p, source, n, details = _prob(
        {"hit_rate": 0.30, "n_trades_effective": 102}, _pattern(None, 0), _Settings()
    )
    assert "realized_prior_blend" not in source
    assert "realized_prior_mean" not in details


def test_thin_realized_record_ignored_uses_neutral():
    # Tiny realized sample (n=3 < floor 5) is too noisy to anchor on -> falls
    # back to the neutral 0.5 prior, so a single lucky win can't inflate the edge.
    p, source, _, details = _prob(
        {"hit_rate": 0.30, "n_trades_effective": 100}, _pattern(0.9, 3), _Settings()
    )
    # (0.30*100 + 0.5*5)/105 = 0.3095 — neutral prior, thin realized WR ignored.
    assert 0.30 < p < 0.32
    assert "realized_prior_blend" not in source
    assert "realized_prior_mean" not in details
