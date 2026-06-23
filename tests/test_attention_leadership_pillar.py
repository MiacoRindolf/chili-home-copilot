"""attention-leadership selection pillar (2026-06-22 Ross study — the TRUE winner/loser
separator: a name's amplitude share+rank of the live mover-field). A re-rank pillar in
score_universe, never a veto. OFF (liquidity-biased weights, no 'attention' key) =
byte-identical; ON (attention weights) = the field leader out-ranks a follower."""
from __future__ import annotations

from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_PILLAR_WEIGHTS_ATTENTION,
    ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
    score_universe,
)


def _sig(rvol, chg, fs, dvol, att=None, dorm=None):
    s = {"rvol": rvol, "daily_change_pct": chg, "float_shares": fs, "dollar_volume": dvol}
    if att is not None:
        s["attention_leadership"] = att
    if dorm is not None:
        s["dormant_rvol"] = dorm
    return s


def test_off_byte_identical_when_pillar_unweighted():
    # With the LIQUIDITY-biased weights (no 'attention'/'dormant' key => weight 0), adding the
    # attention_leadership/dormant_rvol fields must NOT change the score — the pillar never
    # enters the blend. This is the kill-switch OFF=byte-identical guarantee.
    base = {"A": _sig(5, 100, 1e6, 5e6), "B": _sig(3, 50, 2e6, 8e6)}
    withf = {"A": _sig(5, 100, 1e6, 5e6, att=0.9, dorm=10), "B": _sig(3, 50, 2e6, 8e6, att=0.1, dorm=2)}
    r_base = score_universe(base, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
    r_with = score_universe(withf, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
    assert r_base["A"].score == r_with["A"].score
    assert r_base["B"].score == r_with["B"].score


def test_leader_outranks_follower_with_attention_weights():
    # Two names IDENTICAL in base pillars; only attention_leadership differs (the field leader
    # 0.95 vs a follower 0.10). With the ATTENTION weights the leader scores higher + ranks #1.
    sigs = {
        "LEADER": _sig(5, 100, 1e6, 5e6, att=0.95, dorm=8),
        "FOLLOWER": _sig(5, 100, 1e6, 5e6, att=0.10, dorm=8),
    }
    r = score_universe(sigs, weights=ROSS_PILLAR_WEIGHTS_ATTENTION)
    assert r["LEADER"].score > r["FOLLOWER"].score
    assert r["LEADER"].rank == 1


def test_attention_absent_graceful_degrade():
    # ATTENTION weights but NO attention/dormant field -> graceful-degrade (pillar skipped),
    # still scores by the present pillars (no crash) — fail-open.
    sigs = {"A": _sig(5, 100, 1e6, 5e6), "B": _sig(3, 50, 2e6, 8e6)}
    r = score_universe(sigs, weights=ROSS_PILLAR_WEIGHTS_ATTENTION)
    assert "A" in r and "B" in r
    assert r["A"].score >= 0.0
