"""Unit tests for Phase 2 repeatable-edge hygiene (stability tickers, slice keys, soft nudges)."""

from __future__ import annotations

from app.services.trading.edge_evidence import apply_phase2_hygiene_nudges
from app.services.trading.parameter_stability import pick_stability_tickers
from app.services.trading.selection_bias import (
    build_outcome_fingerprint,
    build_research_run_key,
    build_validation_slice_key,
    selection_bias_skip_contract,
)


def test_pick_stability_tickers_order_independent():
    tickers = ["ZZZ", "AAA", "MMM"]
    a, _reason = pick_stability_tickers(tickers, k=2, seed=7)
    b, _ = pick_stability_tickers(list(reversed(tickers)), k=2, seed=7)
    assert a == b
    assert len(a) == 2
    assert set(a) <= {x.upper() for x in tickers}


def test_validation_slice_key_uses_eval_rows_not_order():
    rows_a = [
        {
            "ticker": "SPY",
            "chart_time_from": "a",
            "chart_time_to": "b",
            "ohlc_bars": 100,
            "in_sample_bars": 80,
            "out_of_sample_bars": 20,
            "oos_holdout_fraction": 0.2,
            "period": "1y",
            "interval": "1d",
            "spread_used": 0.01,
            "commission_used": 0.0,
        },
        {
            "ticker": "QQQ",
            "chart_time_from": "c",
            "chart_time_to": "d",
            "ohlc_bars": 90,
            "in_sample_bars": 70,
            "out_of_sample_bars": 20,
            "oos_holdout_fraction": 0.2,
            "period": "1y",
            "interval": "1d",
            "spread_used": 0.01,
            "commission_used": 0.0,
        },
    ]
    rows_b = list(reversed(rows_a))
    k1 = build_validation_slice_key(
        origin="brain_discovered",
        asset_class="stocks",
        timeframe="1d",
        hypothesis_family=None,
        eval_rows=rows_a,
    )
    k2 = build_validation_slice_key(
        origin="brain_discovered",
        asset_class="stocks",
        timeframe="1d",
        hypothesis_family=None,
        eval_rows=rows_b,
    )
    assert k1 == k2


def test_research_run_key_stable_for_same_inputs():
    sk = "a" * 64
    r1 = build_research_run_key(
        slice_key=sk,
        scan_pattern_id=3,
        rules_fingerprint="rf1",
        outcome_fingerprint="of1",
    )
    r2 = build_research_run_key(
        slice_key=sk,
        scan_pattern_id=3,
        rules_fingerprint="rf1",
        outcome_fingerprint="of1",
    )
    assert r1 == r2
    assert len(r1) == 64


def test_outcome_fingerprint_changes_with_wr():
    base = {
        "ticker": "SPY",
        "chart_time_to": "x",
        "in_sample_bars": 10,
        "out_of_sample_bars": 5,
        "oos_holdout_fraction": 0.2,
        "is_win_rate": 50.0,
        "oos_win_rate": 55.0,
        "trade_count": 3,
    }
    a = build_outcome_fingerprint([base])
    b = build_outcome_fingerprint([{**base, "oos_win_rate": 56.0}])
    assert a != b


def test_selection_bias_skip_contract_shape():
    c = selection_bias_skip_contract("unit_test")
    assert c["skip_reason"] == "unit_test"
    assert c["validation_slice_key"] is None
    assert c["usage_count"] == 0


def test_apply_phase2_hygiene_nudges_soft_tier_downgrade():
    ee = {"evidence_tier": "A"}
    ov: dict = {}
    apply_phase2_hygiene_nudges(
        ee,
        parameter_stability={"stability_tier": "fragile"},
        selection_bias={"burn_tier": "high"},
        oos_validation=ov,
    )
    assert ee["evidence_tier"] == "C"
    assert "phase2_fragile_parameter_neighborhood" in ov["research_hygiene_flags"]
    assert "phase2_high_validation_slice_burn" in ov["research_hygiene_flags"]
