"""Tests for graph-native speculative momentum engine (opportunity-board payload)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.models.trading import ScanResult
from app.services.trading.speculative_momentum_engine import build_speculative_momentum_slice
from app.services.trading.speculative_momentum_engine.schema import (
    NODE_EVENT_IMPULSE,
    NODE_SQUEEZE_PRESSURE,
    ClusterId,
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
        risk_level="high",
        rationale="Short squeeze with abnormal volume spike intraday parabolic move.",
        indicator_data={"volume_ratio": 4.2},
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
