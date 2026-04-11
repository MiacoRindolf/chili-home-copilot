"""Tests for edge-vs-luck v1 evidence (weak-null permutations)."""
from __future__ import annotations

import pytest

from app.services.trading.edge_evidence import (
    CHALLENGE_VERSION,
    apply_edge_evidence_veto,
    build_edge_evidence,
    collect_walk_forward_fold_returns,
    permutation_mean_one_sided_p,
    resolve_gated_lifecycle_stage,
)


def test_permutation_mean_deterministic():
    rng_a = __import__("random").Random(7)
    rng_b = __import__("random").Random(7)
    vals = [52.0, 48.0, 55.0, 51.0]
    p1, _ = permutation_mean_one_sided_p(vals, n_perm=200, rng=rng_a)
    p2, _ = permutation_mean_one_sided_p(vals, n_perm=200, rng=rng_b)
    assert p1 == p2
    assert 0.0 <= p1 <= 1.0


def test_permutation_insufficient_units():
    p, skip = permutation_mean_one_sided_p([60.0], n_perm=100, rng=__import__("random").Random(1))
    assert p is None
    assert skip == "insufficient_units"


def test_collect_walk_forward_fold_returns():
    raw = {
        "tickers": {
            "SPY": {
                "windows": [
                    {"ok": True, "return_pct": 1.2},
                    {"ok": True, "return_pct": -0.5},
                ]
            }
        }
    }
    xs = collect_walk_forward_fold_returns(raw)
    assert xs == [1.2, -0.5]


def test_build_edge_evidence_shape():
    ee = build_edge_evidence(
        mean_is_wr_pct=55.0,
        is_wrs=[54.0, 56.0, 55.0],
        mean_oos_wr_pct=53.0,
        oos_wrs=[52.0, 54.0, 53.0],
        oos_ticker_hits=3,
        tickers_tested=3,
        oos_trade_sum=40,
        bench_raw=None,
        n_perm=100,
        seed=99,
    )
    assert ee["challenge_version"] == CHALLENGE_VERSION
    assert "weak_null_disclaimer" in ee
    assert ee["in_sample_score"] == 55.0
    assert ee["in_sample_perm_p"] is not None
    assert ee["walk_forward_evidence_source"] == "oos_ticker_pool_proxy_v1"
    assert isinstance(ee["promotion_block_codes"], list)


def test_apply_edge_evidence_veto_mutates_blocks():
    ee = {
        "in_sample_perm_p": 0.5,
        "oos_perm_p": 0.99,
        "walk_forward_perm_p": 0.5,
        "walk_forward_perm_skip": None,
        "promotion_block_codes": [],
    }
    veto, codes = apply_edge_evidence_veto(
        ee,
        max_is_perm_p=0.1,
        max_oos_perm_p=0.2,
        max_wf_perm_p=0.2,
        require_wf_when_available=False,
    )
    assert veto is True
    assert "weak_null_oos_perm_p" in codes
    assert "weak_null_in_sample_perm_p" in ee["promotion_block_codes"]


def test_resolve_gated_lifecycle_stage():
    assert (
        resolve_gated_lifecycle_stage(
            promotion_status="promoted", edge_gate_ran=True, edge_veto=False
        )
        == "promoted"
    )
    assert (
        resolve_gated_lifecycle_stage(
            promotion_status="pending_oos", edge_gate_ran=True, edge_veto=False
        )
        == "validated"
    )
    assert (
        resolve_gated_lifecycle_stage(
            promotion_status="promoted", edge_gate_ran=True, edge_veto=True
        )
        == "challenged"
    )
    assert resolve_gated_lifecycle_stage(
        promotion_status="rejected_oos", edge_gate_ran=True, edge_veto=False
    ) is None
