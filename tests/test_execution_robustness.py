"""Phase 4 execution robustness: deterministic scoring, skip contracts, readiness merge."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.trading import execution_robustness as er_mod
from app.services.trading.execution_robustness import (
    build_skip_contract,
    compute_execution_robustness_contract,
    execution_robustness_summary,
    merge_repeatable_edge_robustness_into_readiness,
)
from app.services.trading.momentum_neural.operator_readiness import build_momentum_operator_readiness


def _settings_mod(**kwargs):
    base = dict(
        brain_execution_robustness_window_days=120,
        brain_execution_robustness_min_orders=5,
        brain_execution_robustness_warn_fill_rate=0.65,
        brain_execution_robustness_critical_fill_rate=0.45,
        brain_execution_robustness_warn_slippage_bps=35.0,
        brain_execution_robustness_critical_slippage_bps=65.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _patch_market_settings(monkeypatch, *, polygon=False, massive_key="", coinbase=False):
    monkeypatch.setattr(
        er_mod,
        "settings",
        SimpleNamespace(
            use_polygon=polygon,
            massive_api_key=massive_key,
            chili_coinbase_spot_adapter_enabled=coinbase,
        ),
    )


def test_build_skip_contract_has_skip_reason_and_null_rates():
    c = build_skip_contract(skip_reason="insufficient_trade_sample", evaluation_window_days=90)
    assert c["skip_reason"] == "insufficient_trade_sample"
    assert c["fill_rate"] is None
    assert c["robustness_tier"] == "n/a"
    assert c["evaluation_window"]["days"] == 90


def test_compute_skips_non_repeatable_origin():
    p = SimpleNamespace(origin="imported")
    c = compute_execution_robustness_contract(
        pattern=p,
        stats={"n_orders": 99},
        settings_mod=_settings_mod(),
    )
    assert c["skip_reason"] == "not_repeatable_edge_origin"


def test_compute_skips_low_sample():
    p = SimpleNamespace(origin="web_discovered")
    c = compute_execution_robustness_contract(
        pattern=p,
        stats={"n_orders": 3},
        settings_mod=_settings_mod(),
    )
    assert c["skip_reason"] == "insufficient_trade_sample"


def test_compute_healthy_polygon_aggregated(monkeypatch):
    _patch_market_settings(monkeypatch, polygon=True)
    p = SimpleNamespace(origin="brain_discovered")
    stats = dict(
        n_orders=10,
        n_filled=9,
        n_partial=0,
        n_miss=0,
        slippages_abs_bps=[10.0, 12.0],
        dominant_broker_source="coinbase",
    )
    c = compute_execution_robustness_contract(pattern=p, stats=stats, settings_mod=_settings_mod())
    assert c["skip_reason"] is None
    assert c["robustness_tier"] == "healthy"
    assert c["fill_rate"] == 0.9
    assert c["provider_truth_mode"] == "aggregated"
    assert c["market_data_source"] == "polygon"
    assert c["source_truth_tier"] == "medium"
    assert "poor_fill_rate" not in (c.get("readiness_impact_flags") or [])


def test_compute_critical_low_fill(monkeypatch):
    _patch_market_settings(monkeypatch, polygon=True)
    p = SimpleNamespace(origin="web_discovered")
    stats = dict(
        n_orders=10,
        n_filled=4,
        n_partial=0,
        n_miss=6,
        slippages_abs_bps=[],
        dominant_broker_source="manual",
    )
    c = compute_execution_robustness_contract(pattern=p, stats=stats, settings_mod=_settings_mod())
    assert c["robustness_tier"] == "critical"
    assert "poor_fill_rate" in c["readiness_impact_flags"]
    assert "review_required" in c["readiness_impact_flags"]


def test_compute_critical_slippage(monkeypatch):
    _patch_market_settings(monkeypatch, polygon=True)
    p = SimpleNamespace(origin="web_discovered")
    stats = dict(
        n_orders=8,
        n_filled=8,
        n_partial=0,
        n_miss=0,
        slippages_abs_bps=[70.0, 72.0],
        dominant_broker_source="manual",
    )
    c = compute_execution_robustness_contract(pattern=p, stats=stats, settings_mod=_settings_mod())
    assert c["robustness_tier"] == "critical"
    assert "high_slippage" in c["readiness_impact_flags"]


def test_provider_truth_exchange_aware(monkeypatch):
    _patch_market_settings(monkeypatch, coinbase=True)
    p = SimpleNamespace(origin="web_discovered")
    stats = dict(
        n_orders=6,
        n_filled=6,
        n_partial=0,
        n_miss=0,
        slippages_abs_bps=[1.0],
        dominant_broker_source="coinbase",
    )
    c = compute_execution_robustness_contract(pattern=p, stats=stats, settings_mod=_settings_mod())
    assert c["provider_truth_mode"] == "exchange_aware"
    assert c["source_truth_tier"] == "strong"


def test_execution_robustness_summary_shape():
    s = execution_robustness_summary(
        {"robustness_tier": "warning", "fill_rate": 0.5, "skip_reason": None, "readiness_impact_flags": ["x"]}
    )
    assert s["robustness_tier"] == "warning"
    assert s["fill_rate"] == 0.5
    assert execution_robustness_summary(None) is None


def test_merge_repeatable_edge_idempotent_and_clears_block(monkeypatch):
    """Second merge with same pattern does not stack keys; missing pattern id clears internal block flag."""

    monkeypatch.setattr(
        er_mod,
        "settings",
        SimpleNamespace(
            brain_execution_robustness_live_not_recommended=True,
            brain_execution_robustness_flag_weak_truth_live=True,
            brain_execution_robustness_hard_block_live_enabled=False,
            brain_execution_robustness_min_orders=5,
        ),
    )
    pat = SimpleNamespace(
        oos_validation_json={
            "execution_robustness": {
                "robustness_tier": "critical",
                "readiness_impact_flags": ["review_required"],
                "skip_reason": None,
                "sample_count_orders": 10,
                "provider_truth_mode": "aggregated",
            }
        }
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = pat

    base = build_momentum_operator_readiness()

    m1 = merge_repeatable_edge_robustness_into_readiness(dict(base), db, scan_pattern_id=1)
    m2 = merge_repeatable_edge_robustness_into_readiness(dict(m1), db, scan_pattern_id=1)
    assert m1["repeatable_edge_live_not_recommended"] is m2["repeatable_edge_live_not_recommended"]
    assert m1["repeatable_edge_execution_robustness"] == m2["repeatable_edge_execution_robustness"]

    cleared = merge_repeatable_edge_robustness_into_readiness(
        {"_repeatable_edge_block_live": "execution_robustness_critical"},
        db,
        scan_pattern_id=None,
    )
    assert cleared.get("repeatable_edge_execution_robustness") is None
    assert "_repeatable_edge_block_live" not in cleared


def test_merge_hard_block_when_enabled(monkeypatch):
    monkeypatch.setattr(
        er_mod,
        "settings",
        SimpleNamespace(
            brain_execution_robustness_live_not_recommended=True,
            brain_execution_robustness_flag_weak_truth_live=True,
            brain_execution_robustness_hard_block_live_enabled=True,
            brain_execution_robustness_min_orders=5,
        ),
    )
    pat = SimpleNamespace(
        oos_validation_json={
            "execution_robustness": {
                "robustness_tier": "critical",
                "readiness_impact_flags": [],
                "skip_reason": None,
                "sample_count_orders": 10,
            }
        }
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = pat
    out = merge_repeatable_edge_robustness_into_readiness({}, db, scan_pattern_id=42)
    assert out.get("_repeatable_edge_block_live") == "execution_robustness_critical"
