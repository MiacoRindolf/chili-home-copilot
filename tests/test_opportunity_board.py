"""Light tests for Trading Brain opportunity board (tiers + payload shape)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.models.trading import ScanResult
from app.services.trading.opportunity_board import (
    _annotate_desk_fields,
    _apply_board_data_quality_gate,
    _board_data_quality_gate,
    _scanner_fallback_rows,
    get_trading_opportunity_board,
)


def test_opportunity_board_empty_shape(monkeypatch) -> None:
    db = MagicMock()

    def _empty_gather(*_a, **_k):
        meta = {
            "patterns_active": 0,
            "patterns_with_tickers_evaluated": 0,
            "global_ticker_universe": 0,
            "universe_by_source": {},
            "tickers_scored": 0,
            "skip_reasons": {
                "pattern_no_tickers": 0,
                "pattern_no_conditions": 0,
                "score_failed": 0,
                "readiness_unusable": 0,
                "all_conditions_met": 0,
                "readiness_outside_band": 0,
                "eta_too_long": 0,
                "excluded_promotion_lifecycle": 0,
                "insufficient_coverage_main": 0,
                "below_composite_main": 0,
            },
            "top_suppressed": [],
            "equity_session_open": True,
            "for_opportunity_board": True,
            "board_eval_budget_hit": False,
            "board_per_pattern_cap": 10,
            "board_score_budget": 360,
        }
        return [], meta

    monkeypatch.setattr(
        "app.services.trading.opportunity_board.gather_imminent_candidate_rows",
        _empty_gather,
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.get_current_predictions",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.us_stock_session_open",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.describe_us_session_context",
        lambda *_a, **_k: {
            "us_session": "regular_hours",
            "label": "US stocks: regular session",
            "equity_evaluation_active": True,
        },
    )

    monkeypatch.setattr(
        "app.services.trading.opportunity_board.collect_source_freshness",
        lambda *_a, **_k: {
            "predictions_cache_last_updated_utc": "2026-01-01T12:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.compute_board_freshness_status",
        lambda sf: {
            "data_as_of": "2026-01-01T12:00:00+00:00",
            "data_as_of_min_keys": ["predictions_cache_last_updated_utc"],
            "freshness_unknown": False,
            "missing_source_keys": [],
            "invalid_source_keys": [],
            "source_status": {"predictions_cache_last_updated_utc": "complete"},
        },
    )

    out = get_trading_opportunity_board(db, 1, include_research=False, include_debug=False)
    assert out["ok"] is True
    assert "generated_at" in out
    assert out.get("data_as_of") is not None
    assert out.get("board_truncated") is False
    assert out["data_quality_gate"]["learning_lane_enabled"] is True
    assert "source_freshness" in out
    assert out["no_trade_now"] is True
    assert "tiers" in out
    assert out["tiers"]["actionable_now"] == []
    assert out["applied_tier_caps"]["A"] >= 1
    assert "debug" not in out

    out_dbg = get_trading_opportunity_board(db, 1, include_debug=True)
    assert "debug" in out_dbg
    assert "skip_reasons" in out_dbg["debug"]


def test_opportunity_board_truncated_when_budget_hit(monkeypatch) -> None:
    db = MagicMock()

    def _gather(*_a, **_k):
        meta = {
            "patterns_active": 0,
            "patterns_with_tickers_evaluated": 0,
            "global_ticker_universe": 0,
            "universe_by_source": {},
            "tickers_scored": 0,
            "skip_reasons": {},
            "top_suppressed": [],
            "equity_session_open": True,
            "for_opportunity_board": True,
            "board_eval_budget_hit": True,
            "board_per_pattern_cap": 10,
            "board_score_budget": 360,
        }
        return [], meta

    monkeypatch.setattr(
        "app.services.trading.opportunity_board.gather_imminent_candidate_rows",
        _gather,
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.get_current_predictions",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.us_stock_session_open",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.describe_us_session_context",
        lambda *_a, **_k: {
            "us_session": "regular_hours",
            "label": "US stocks: regular session",
            "equity_evaluation_active": True,
        },
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.collect_source_freshness",
        lambda *_a, **_k: {"predictions_cache_last_updated_utc": "2026-01-01T12:00:00+00:00"},
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.compute_board_freshness_status",
        lambda sf: {
            "data_as_of": "2026-01-01T12:00:00+00:00",
            "data_as_of_min_keys": ["predictions_cache_last_updated_utc"],
            "freshness_unknown": False,
            "missing_source_keys": [],
            "invalid_source_keys": [],
            "source_status": {"predictions_cache_last_updated_utc": "complete"},
        },
    )

    out = get_trading_opportunity_board(db, 1, include_research=False, include_debug=False)
    assert out["ok"] is True
    assert out.get("board_truncated") is True


def test_opportunity_board_risk_annotations_are_real() -> None:
    rows = [
        {
            "ticker": "BTC-USD",
            "asset_class": "crypto",
            "tier": "A",
            "sources": ["pattern_imminent", "scan_pattern"],
            "source_strength": "strong",
            "composite": 0.62,
            "score_breakdown": {"overextension_penalty": 0.07},
            "readiness": 0.76,
            "feature_coverage": 0.66,
            "entry": 100.0,
            "stop": 96.0,
            "target": 110.0,
            "price": 100.0,
            "prediction_support": {"direction": "up", "confidence": 0.7},
        }
    ]

    _annotate_desk_fields(rows)
    row = rows[0]
    assert row["extension_risk"]["level"] in {"low", "medium", "high"}
    assert row["execution_risk"]["expected_slippage_bps"] > 0
    assert row["structural_confirmation"]["score"] > 0
    assert row["liquidity_quality"]["score"] > 0
    assert row["net_edge_estimate"]["available"] is True


def test_opportunity_board_data_quality_blocks_capital_not_learning() -> None:
    rows = [
        {
            "ticker": "BTC-USD",
            "asset_class": "crypto",
            "tier": "A",
            "sources": ["pattern_imminent"],
            "source_strength": "strong",
            "composite": 0.7,
            "readiness": 0.8,
            "feature_coverage": 0.7,
            "entry": 100.0,
            "stop": 97.0,
            "target": 108.0,
        }
    ]
    gate = _board_data_quality_gate(
        data_as_of="2026-01-01T12:00:00+00:00",
        age_sec=900.0,
        stale_threshold_seconds=180,
        freshness_unknown=False,
        is_stale=True,
        board_truncated=False,
        data_as_of_min_keys=["scan_results_latest_utc"],
        source_freshness={"scan_results_latest_utc": "2026-01-01T12:00:00+00:00"},
    )

    _annotate_desk_fields(rows)
    _apply_board_data_quality_gate(rows, gate)

    row = rows[0]
    assert gate["status"] == "block"
    assert gate["capital_lane_eligible"] is False
    assert row["learning_lane"]["enabled"] is True
    assert row["capital_lane"]["approved_for_direct_execution"] is False
    assert row["capital_lane"]["hard_block_reason_code"] == "board_data_stale"
    assert row["net_edge_estimate"]["capital_lane"] == "blocked_data_quality"


def test_opportunity_board_data_quality_blocks_missing_source_clock() -> None:
    gate = _board_data_quality_gate(
        data_as_of="2026-01-01T12:00:00+00:00",
        age_sec=30.0,
        stale_threshold_seconds=180,
        freshness_unknown=True,
        is_stale=False,
        board_truncated=False,
        data_as_of_min_keys=["scan_results_latest_utc"],
        source_freshness={
            "scan_results_latest_utc": "2026-01-01T12:00:00+00:00",
            "prescreen_snapshot_finished_latest_utc": None,
        },
        missing_source_keys=["prescreen_snapshot_finished_latest_utc"],
        source_status={
            "scan_results_latest_utc": "complete",
            "prescreen_snapshot_finished_latest_utc": "missing",
        },
    )

    assert gate["status"] == "block"
    assert gate["capital_lane_eligible"] is False
    assert gate["hard_block_reason_code"] == "board_freshness_unknown"
    assert gate["missing_source_keys"] == ["prescreen_snapshot_finished_latest_utc"]
    assert gate["source_status"]["prescreen_snapshot_finished_latest_utc"] == "missing"


def test_scanner_fallback_rows_ignore_stale_scans(db: Session) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add_all(
        [
            ScanResult(
                user_id=None,
                ticker="FRESH",
                score=7.2,
                signal="buy",
                scanned_at=now,
            ),
            ScanResult(
                user_id=None,
                ticker="STALE",
                score=9.1,
                signal="buy",
                scanned_at=now - timedelta(hours=2),
            ),
        ]
    )
    db.commit()

    tier_b, tier_c = _scanner_fallback_rows(
        db,
        set(),
        max_rows=4,
        min_score_b=6.5,
        max_age_seconds=180,
    )

    rows = tier_b + tier_c
    tickers = {row["ticker"] for row in rows}
    assert "FRESH" in tickers
    assert "STALE" not in tickers
    assert rows[0]["scanner_age_seconds"] is not None
