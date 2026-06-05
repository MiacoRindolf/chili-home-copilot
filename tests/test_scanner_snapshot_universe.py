"""Snapshot universe coverage for active momentum lanes."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.trading import BreakoutAlert, PrescreenCandidate, ScanResult
from app.services.trading.scanner import (
    build_snapshot_ticker_universe,
    load_top_scan_tickers_for_snapshots,
)


def test_snapshot_universe_prioritizes_recent_stock_imminent_alerts(db) -> None:
    now = datetime.utcnow()
    db.add_all(
        [
            BreakoutAlert(
                ticker="HOT1",
                asset_type="stock",
                alert_tier="pattern_imminent",
                score_at_alert=0.7,
                price_at_alert=3.0,
                alerted_at=now,
            ),
            BreakoutAlert(
                ticker="HOT2",
                asset_type="stock",
                alert_tier="pattern_imminent",
                score_at_alert=0.7,
                price_at_alert=4.0,
                alerted_at=now - timedelta(minutes=1),
            ),
            BreakoutAlert(
                ticker="BTC-USD",
                asset_type="crypto",
                alert_tier="pattern_imminent",
                score_at_alert=0.7,
                price_at_alert=100.0,
                alerted_at=now,
            ),
            ScanResult(
                ticker="SCAN1",
                score=10.0,
                signal="buy",
                risk_level="medium",
                rationale="generic scan",
                scanned_at=now,
            ),
        ]
    )
    db.commit()

    tickers, meta = load_top_scan_tickers_for_snapshots(db, None, limit=3)

    assert tickers == ["HOT1", "HOT2", "SCAN1"]
    assert meta["snapshot_driver"] == "alerts_prescreen_scans"
    assert meta["recent_stock_imminent_alert_tickers"] == 2


def test_snapshot_universe_merges_prescreen_even_when_scan_rows_exist(db) -> None:
    now = datetime.utcnow()
    db.add_all(
        [
            PrescreenCandidate(
                ticker="GAPR",
                ticker_norm="GAPR",
                asset_universe="stock",
                active=True,
                entry_reasons=[],
                sources_json={"tags": ["massive_momentum_gappers"]},
            ),
            ScanResult(
                ticker="SCAN1",
                score=10.0,
                signal="buy",
                risk_level="medium",
                rationale="generic scan",
                scanned_at=now,
            ),
        ]
    )
    db.commit()

    tickers, meta = load_top_scan_tickers_for_snapshots(db, None, limit=3)

    assert tickers[:2] == ["GAPR", "SCAN1"]
    assert meta["prescreen_tickers"] == 1
    assert meta["scan_rows"] == 1


def test_build_snapshot_ticker_universe_keeps_watchlist_after_priority_sources(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.trading.scanner.get_watchlist",
        lambda _db, _user_id: [type("Watch", (), {"ticker": "WATCH"})()],
    )
    now = datetime.utcnow()
    db.add(
        BreakoutAlert(
            ticker="HOT1",
            asset_type="stock",
            alert_tier="pattern_imminent",
            score_at_alert=0.7,
            price_at_alert=3.0,
            alerted_at=now,
        )
    )
    db.commit()

    tickers, _meta = build_snapshot_ticker_universe(db, None, limit=3)

    assert tickers == ["HOT1", "WATCH"]
