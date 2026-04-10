"""Light tests for Trading Brain opportunity board (tiers + payload shape)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.services.trading.opportunity_board import get_trading_opportunity_board


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
        "app.services.trading.opportunity_board.compute_board_data_as_of",
        lambda sf, **_k: ("2026-01-01T12:00:00+00:00", ["predictions_cache_last_updated_utc"]),
    )

    out = get_trading_opportunity_board(db, 1, include_research=False, include_debug=False)
    assert out["ok"] is True
    assert "generated_at" in out
    assert out.get("data_as_of") is not None
    assert out.get("board_truncated") is False
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
        "app.services.trading.opportunity_board.compute_board_data_as_of",
        lambda sf, **_k: ("2026-01-01T12:00:00+00:00", ["predictions_cache_last_updated_utc"]),
    )

    out = get_trading_opportunity_board(db, 1, include_research=False, include_debug=False)
    assert out["ok"] is True
    assert out.get("board_truncated") is True


def test_opportunity_board_freshness_ignores_old_scan_results_when_scanner_not_rendered(
    monkeypatch,
) -> None:
    db = MagicMock()
    now = datetime.now(timezone.utc)
    fresh_core = (now - timedelta(seconds=45)).isoformat()
    stale_scan = (now - timedelta(hours=1)).isoformat()

    def _gather(*_a, **_k):
        meta = {
            "patterns_active": 1,
            "patterns_with_tickers_evaluated": 1,
            "global_ticker_universe": 1,
            "universe_by_source": {},
            "tickers_scored": 1,
            "skip_reasons": {},
            "top_suppressed": [],
            "equity_session_open": True,
            "for_opportunity_board": True,
            "board_eval_budget_hit": False,
            "board_per_pattern_cap": 10,
            "board_score_budget": 360,
        }
        return [{"ticker": "AAPL"}], meta

    monkeypatch.setattr(
        "app.services.trading.opportunity_board.gather_imminent_candidate_rows",
        _gather,
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board._tier_a_b_c_from_pattern_rows",
        lambda *_a, **_k: (
            [{
                "ticker": "AAPL",
                "tier": "A",
                "sources": ["pattern_imminent", "scan_pattern"],
                "composite": 0.71,
                "scanner_score": None,
            }],
            [],
            [],
        ),
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.get_current_predictions",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board._scanner_fallback_rows",
        lambda *_a, **_k: ([], []),
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board._prescreener_fallback_rows",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.build_speculative_momentum_slice",
        lambda *_a, **_k: {"ok": True, "items": [], "generated_at": now.isoformat()},
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
            "scan_results_latest_utc": stale_scan,
            "prescreen_snapshot_finished_latest_utc": fresh_core,
            "prescreen_candidate_last_seen_latest_utc": fresh_core,
            "imminent_job_ok_latest_utc": fresh_core,
            "predictions_cache_last_updated_utc": None,
        },
    )

    out = get_trading_opportunity_board(db, 1, include_research=False, include_debug=False)
    assert out["ok"] is True
    assert out["is_stale"] is False
    assert "scan_results_latest_utc" not in out["data_as_of_considered_keys"]
    assert "scan_results_latest_utc" not in out["data_as_of_min_keys"]


def test_opportunity_board_freshness_includes_scan_results_when_scanner_rows_rendered(
    monkeypatch,
) -> None:
    db = MagicMock()
    now = datetime.now(timezone.utc)
    fresh_core = (now - timedelta(seconds=45)).isoformat()
    stale_scan = (now - timedelta(hours=1)).isoformat()

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
            "board_eval_budget_hit": False,
            "board_per_pattern_cap": 10,
            "board_score_budget": 360,
        }
        return [], meta

    monkeypatch.setattr(
        "app.services.trading.opportunity_board.gather_imminent_candidate_rows",
        _gather,
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board._tier_a_b_c_from_pattern_rows",
        lambda *_a, **_k: ([], [], []),
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.get_current_predictions",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board._scanner_fallback_rows",
        lambda *_a, **_k: (
            [{
                "ticker": "SMCI",
                "tier": "B",
                "sources": ["scanner"],
                "composite": None,
                "scanner_score": 7.2,
            }],
            [],
        ),
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board._prescreener_fallback_rows",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "app.services.trading.opportunity_board.build_speculative_momentum_slice",
        lambda *_a, **_k: {"ok": True, "items": [], "generated_at": now.isoformat()},
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
            "scan_results_latest_utc": stale_scan,
            "prescreen_snapshot_finished_latest_utc": fresh_core,
            "prescreen_candidate_last_seen_latest_utc": fresh_core,
            "imminent_job_ok_latest_utc": fresh_core,
            "predictions_cache_last_updated_utc": None,
        },
    )

    out = get_trading_opportunity_board(db, 1, include_research=False, include_debug=False)
    assert out["ok"] is True
    assert out["is_stale"] is True
    assert "scan_results_latest_utc" in out["data_as_of_considered_keys"]
    assert "scan_results_latest_utc" in out["data_as_of_min_keys"]
