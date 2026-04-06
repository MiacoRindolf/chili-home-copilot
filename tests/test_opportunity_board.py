"""Light tests for Trading Brain opportunity board (tiers + payload shape)."""
from __future__ import annotations

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

    out = get_trading_opportunity_board(db, 1, include_research=False, include_debug=False)
    assert out["ok"] is True
    assert "generated_at" in out
    assert out["no_trade_now"] is True
    assert "tiers" in out
    assert out["tiers"]["actionable_now"] == []
    assert out["applied_tier_caps"]["A"] >= 1
    assert "debug" not in out

    out_dbg = get_trading_opportunity_board(db, 1, include_debug=True)
    assert "debug" in out_dbg
    assert "skip_reasons" in out_dbg["debug"]
