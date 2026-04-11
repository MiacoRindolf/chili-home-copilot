"""Tests for graph-native speculative momentum engine (opportunity-board payload)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.models.trading import ScanResult
from app.services.trading.speculative_momentum_engine import build_speculative_momentum_slice
from app.services.trading.speculative_momentum_engine.clusters import resolve_cluster
from app.services.trading.speculative_momentum_engine.features import build_features
from app.services.trading.speculative_momentum_engine.nodes import evaluate_all_nodes
from app.services.trading.speculative_momentum_engine.reasoning import build_non_promotion
from app.services.trading.speculative_momentum_engine.scoring import build_scoring_plane
from app.services.trading.speculative_momentum_engine.schema import (
    NODE_EVENT_IMPULSE,
    NODE_SQUEEZE_PRESSURE,
    ClusterId,
    ReasonCode,
)


@pytest.fixture
def scan_squeeze(db: Session) -> None:
    r = ScanResult(
        user_id=None,
        ticker="UCAR",
        score=8.5,
        signal="buy",
        entry_price=1.0,
        stop_loss=0.9,
        take_profit=1.2,
        risk_level="medium",
        rationale="Short squeeze with abnormal volume spike intraday parabolic move.",
        indicator_data={"volume_ratio": 2.6},
        scanned_at=datetime.utcnow(),
    )
    db.add(r)
    db.commit()


@pytest.fixture
def scan_event_only(db: Session) -> None:
    r = ScanResult(
        user_id=None,
        ticker="XYZ",
        score=7.5,
        signal="buy",
        risk_level="medium",
        rationale="FDA catalyst gap with unusual activity and strong momentum.",
        indicator_data={"volume_ratio": 2.6},
        scanned_at=datetime.utcnow(),
    )
    db.add(r)
    db.commit()


def test_build_speculative_momentum_slice_graph_native_fields(db: Session, scan_squeeze: None) -> None:
    out = build_speculative_momentum_slice(db, limit=5)
    assert out["ok"] is True
    assert out["engine"] == "speculative_momentum"
    assert out.get("engine_version") == 1
    assert out.get("methodology") == "graph_native_heuristic_v1"
    items = out["items"]
    assert len(items) >= 1
    hit = next((x for x in items if x["ticker"] == "UCAR"), None)
    assert hit is not None
    assert hit["scores"]["speculative_momentum_score"] > 0
    assert "why_not_core_promoted" in hit
    assert any("imminent" in s.lower() for s in hit["why_not_core_promoted"])
    assert hit.get("cluster_id")
    assert hit.get("cluster_label")
    assert hit.get("graph_trace", {}).get("hub_node_id") == "nm_speculative_momentum_hub"
    assert hit["graph_trace"].get("cluster_id") == hit["cluster_id"]
    active = {n["node_id"] for n in hit.get("active_nodes", [])}
    assert NODE_SQUEEZE_PRESSURE in active
    assert "non_promotion" in hit and hit["non_promotion"].get("codes")


def test_cluster_event_driven_spike(db: Session, scan_event_only: None) -> None:
    out = build_speculative_momentum_slice(db, limit=5)
    hit = next((x for x in out["items"] if x["ticker"] == "XYZ"), None)
    assert hit is not None
    assert hit["cluster_id"] == ClusterId.event_driven_spike.value
    ids = {n["node_id"] for n in hit["active_nodes"]}
    assert NODE_EVENT_IMPULSE in ids


def test_build_speculative_momentum_slice_empty_db(db: Session) -> None:
    out = build_speculative_momentum_slice(db, limit=5)
    assert out["ok"] is True
    assert out["items"] == []


def test_repeatability_confidence_discriminates_rows() -> None:
    """Repeatability must not be capped ~0.30; strong structure can clear low-repeatability threshold."""
    hi = ScanResult(
        user_id=None,
        ticker="RHI",
        score=8.0,
        signal="buy",
        risk_level="medium",
        rationale="First pullback reclaim vwap hold with abnormal volume expansion.",
        indicator_data={"volume_ratio": 2.8},
        scanned_at=datetime.utcnow(),
    )
    lo = ScanResult(
        user_id=None,
        ticker="RLO",
        score=6.5,
        signal="buy",
        risk_level="medium",
        rationale="Sympathy mover watch.",
        indicator_data={"volume_ratio": 1.0},
        scanned_at=datetime.utcnow(),
    )
    f_hi = build_features(hi)
    acts_hi = evaluate_all_nodes(f_hi)
    cl_hi = resolve_cluster(acts_hi, scanner_score=f_hi.scanner_score)
    out_hi = build_scoring_plane(f_hi, acts_hi, cl_hi)

    f_lo = build_features(lo)
    acts_lo = evaluate_all_nodes(f_lo)
    cl_lo = resolve_cluster(acts_lo, scanner_score=f_lo.scanner_score)
    out_lo = build_scoring_plane(f_lo, acts_lo, cl_lo)

    assert out_hi["repeatability_confidence"] > out_lo["repeatability_confidence"]
    codes_hi, _, _ = build_non_promotion(scores=out_hi, cluster=cl_hi)
    assert ReasonCode.low_repeatability_signature.value not in codes_hi


def test_high_scanner_without_extension_lexicon_not_too_extended() -> None:
    sr = ScanResult(
        user_id=None,
        ticker="HOTX",
        score=9.2,
        signal="buy",
        risk_level="medium",
        rationale="Strong momentum breakout trending stock leader.",
        indicator_data={"volume_ratio": 1.1},
        scanned_at=datetime.utcnow(),
    )
    f = build_features(sr)
    acts = evaluate_all_nodes(f)
    cl = resolve_cluster(acts, scanner_score=f.scanner_score)
    assert cl.cluster_id not in (
        ClusterId.too_extended.value,
        ClusterId.blow_off_risk.value,
    )


def test_severe_execution_risk_overrides_squeeze_cluster() -> None:
    """High squeeze + extreme liquidity stress → execution_risk_high, not speculative_squeeze."""
    r = ScanResult(
        user_id=None,
        ticker="EXECO",
        score=8.0,
        signal="buy",
        risk_level="high",
        rationale="Short squeeze gamma ramp abnormal volume spike halt resume.",
        indicator_data={"volume_ratio": 4.5},
        scanned_at=datetime.utcnow(),
    )
    f = build_features(r)
    acts = evaluate_all_nodes(f)
    cl = resolve_cluster(acts, scanner_score=f.scanner_score)
    assert cl.cluster_id == ClusterId.execution_risk_high.value


def test_core_opportunity_board_untouched_import(db: Session, scan_squeeze: None) -> None:
    """Regression: board helper still attaches speculative payload without touching tiers."""
    from app.services.trading.opportunity_board import get_trading_opportunity_board

    data = get_trading_opportunity_board(db, None, include_research=False, include_debug=False)
    assert data.get("ok") is True
    assert "tiers" in data
    assert "speculative_movers" in data
    sm = data["speculative_movers"]
    assert sm.get("ok") is True
    assert any(x.get("ticker") == "UCAR" for x in sm.get("items", []))
