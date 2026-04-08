"""Unit tests for speculative momentum surface (heuristic scan classification)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.models.trading import ScanResult
from app.services.trading.speculative_momentum_surface import build_speculative_momentum_slice


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


def test_build_speculative_momentum_slice_finds_squeeze(db: Session, scan_squeeze: None) -> None:
    out = build_speculative_momentum_slice(db, limit=5)
    assert out["ok"] is True
    assert out["engine"] == "speculative_momentum"
    items = out["items"]
    assert len(items) >= 1
    hit = next((x for x in items if x["ticker"] == "UCAR"), None)
    assert hit is not None
    assert hit["scores"]["speculative_momentum_score"] > 0
    assert "why_not_core_promoted" in hit
    assert any("imminent" in s.lower() for s in hit["why_not_core_promoted"])


def test_build_speculative_momentum_slice_empty_db(db: Session) -> None:
    out = build_speculative_momentum_slice(db, limit=5)
    assert out["ok"] is True
    assert out["items"] == []
