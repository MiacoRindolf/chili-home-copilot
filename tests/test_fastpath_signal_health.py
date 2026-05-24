"""Tests for fast-path signal-health diagnostics."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.services.trading.fast_path.settings import FastPathSettings
from app.services.trading.fast_path.signal_health import (
    build_signal_health_report,
    summarize_maker_attempt_group,
    summarize_signal_group,
)


def _row(
    *,
    ticker: str = "BTC-USD",
    alert_type: str = "imbalance_long",
    score_bucket: str = "high",
    horizon_s: int = 30,
    sample_count: int = 30,
    mean_return: float = 0.0,
    m2_return: float = 0.0,
    spread_bps: float | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "alert_type": alert_type,
        "score_bucket": score_bucket,
        "horizon_s": horizon_s,
        "sample_count": sample_count,
        "mean_return": mean_return,
        "m2_return": m2_return,
        "status": "shadow",
        "rank": 1,
        "spread_bps": spread_bps,
    }


def _attempt(
    *,
    ticker: str = "BTC-USD",
    alert_type: str = "book_pressure_reclaim_long",
    signal_score: float = 0.42,
    side: str = "buy",
    fill_outcome: str | None = "filled",
    time_to_fill_ms: int | None = 500,
    spread_at_placement_bps: float | None = 1.0,
    spread_at_fill_bps: float | None = 1.5,
    mid_drift_bps: float | None = -2.0,
) -> dict:
    return {
        "ticker": ticker,
        "alert_type": alert_type,
        "signal_score": signal_score,
        "side": side,
        "fill_outcome": fill_outcome,
        "time_to_fill_ms": time_to_fill_ms,
        "spread_at_placement_bps": spread_at_placement_bps,
        "spread_at_fill_bps": spread_at_fill_bps,
        "mid_drift_bps": mid_drift_bps,
    }


def test_signal_health_flags_negative_edge_without_sample_quota():
    out = summarize_signal_group(
        [
            _row(
                sample_count=6,
                mean_return=-0.0012,
                m2_return=0.0000001,
            ),
        ],
        table="fast_signal_decay_maker_filled",
        scope="ticker",
        fee_bps=1.0,
        spread_bps=0.5,
    )

    assert out["verdict"] == "negative_edge"
    assert out["action"] == "suppress"
    assert out["worst_negative"]["upper_bps"] < 0.0


def test_signal_health_marks_cost_cleared_candidate_by_lower_bound():
    out = summarize_signal_group(
        [
            _row(
                alert_type="spread_squeeze",
                sample_count=30,
                mean_return=0.01,
                m2_return=0.00000001,
            ),
        ],
        table="fast_signal_decay_maker_filled",
        scope="pooled",
        fee_bps=1.0,
        spread_bps=0.5,
    )

    assert out["verdict"] == "positive_edge_candidate"
    assert out["action"] == "maker_shadow_candidate"
    assert out["best_lower_net"]["lower_net_bps"] > 0.0


def test_signal_health_marks_below_cost_when_upper_bound_cannot_clear():
    out = summarize_signal_group(
        [
            _row(
                alert_type="volume_breakout_pullback_long",
                sample_count=30,
                mean_return=0.0002,
                m2_return=0.00000001,
            ),
        ],
        table="fast_signal_decay_maker_filled",
        scope="pooled",
        fee_bps=8.0,
        spread_bps=2.0,
    )

    assert out["verdict"] == "below_cost"
    assert out["action"] == "keep_shadow_or_drop"
    assert out["best_upper_net"]["upper_net_bps"] < 0.0


def test_signal_health_keeps_wide_interval_observe_only():
    out = summarize_signal_group(
        [
            _row(
                alert_type="spread_squeeze",
                sample_count=5,
                mean_return=0.002,
                m2_return=0.0001,
            ),
        ],
        table="fast_signal_decay_maker_filled",
        scope="pooled",
        fee_bps=1.0,
        spread_bps=0.5,
    )

    assert out["verdict"] == "uncertain"
    assert out["action"] == "observe_only"


def test_maker_attempt_health_flags_adverse_fills_and_missed_moves():
    out = summarize_maker_attempt_group(
        [
            _attempt(fill_outcome="filled", mid_drift_bps=-3.0),
            _attempt(
                fill_outcome="cancelled",
                time_to_fill_ms=None,
                mid_drift_bps=4.0,
            ),
            _attempt(
                fill_outcome="replaced",
                time_to_fill_ms=None,
                mid_drift_bps=2.0,
            ),
        ],
        scope="ticker",
    )

    assert out["ticker"] == "BTC-USD"
    assert out["score_bucket"] == "med"
    assert out["attempts"] == 3
    assert out["fills"] == 1
    assert out["fill_rate"] == pytest.approx(1.0 / 3.0)
    assert out["filled_adverse_rate"] == 1.0
    assert out["unfilled_favorable_rate"] == 1.0
    assert "filled_after_adverse_move" in out["pain_points"]
    assert "unfilled_when_move_was_favorable" in out["pain_points"]


def test_maker_attempt_health_side_adjusts_sell_drift():
    out = summarize_maker_attempt_group(
        [
            _attempt(
                side="sell",
                fill_outcome="filled",
                mid_drift_bps=2.0,
            ),
        ],
        scope="ticker",
    )

    assert out["filled_avg_side_mid_drift_bps"] == -2.0
    assert out["filled_adverse_rate"] == 1.0


def test_build_signal_health_report_uses_execution_mode_table_and_spread_cost():
    settings = FastPathSettings(
        execution_mode="maker_only",
        cost_aware_maker_fee_bps=1.0,
        live_alpha_min_net_bps=0.0,
    )
    pooled = [
        _row(
            ticker=None,
            alert_type="imbalance_long",
            score_bucket="high",
            sample_count=6,
            mean_return=-0.0012,
            m2_return=0.0000001,
        ),
    ]
    ticker = [
        _row(
            ticker="ETH-USD",
            alert_type="spread_squeeze",
            score_bucket="med",
            sample_count=30,
            mean_return=0.01,
            m2_return=0.00000001,
            spread_bps=0.25,
        ),
    ]

    with patch(
        "app.services.trading.fast_path.signal_health._fetch_median_universe_spread_bps",
        return_value=0.5,
    ), patch(
        "app.services.trading.fast_path.signal_health._fetch_pooled_decay_rows",
        return_value=pooled,
    ) as pooled_lookup, patch(
        "app.services.trading.fast_path.signal_health._fetch_ticker_decay_rows",
        return_value=ticker,
    ), patch(
        "app.services.trading.fast_path.signal_health._fetch_maker_attempt_rows",
        return_value=[
            _attempt(
                ticker="ETH-USD",
                alert_type="spread_squeeze",
                signal_score=0.5,
                fill_outcome="filled",
                mid_drift_bps=-1.0,
            ),
        ],
    ) as maker_attempt_lookup:
        report = build_signal_health_report(
            object(),
            settings=settings,
            include_tickers=True,
            limit=10,
        )

    assert report["ok"] is True
    assert report["settings"]["decay_table"] == "fast_signal_decay_maker_filled"
    assert report["settings"]["pooled_cost_bps"] == 3.0
    assert report["pooled"][0]["verdict"] == "negative_edge"
    assert report["tickers"][0]["verdict"] == "positive_edge_candidate"
    assert report["maker_attempts"]["summary"]["attempts"] == 1
    assert report["maker_attempts"]["summary"]["filled_adverse_rate"] == 1.0
    assert pooled_lookup.call_args.kwargs["table"] == "fast_signal_decay_maker_filled"
    assert maker_attempt_lookup.call_args.kwargs["window_hours"] == 24


def test_signal_health_endpoint_delegates_to_report_builder():
    from app.routers.trading_sub import fast_path_api as mod

    with patch(
        "app.services.trading.fast_path.signal_health.build_signal_health_report",
        return_value={"ok": True, "pooled": [], "tickers": []},
    ) as builder:
        resp = mod.get_signal_health(limit=7, include_tickers=False)

    payload = json.loads(bytes(resp.body).decode("utf-8"))
    assert payload == {"ok": True, "pooled": [], "tickers": []}
    assert builder.call_args.args[0] is mod.engine
    assert builder.call_args.kwargs["limit"] == 7
    assert builder.call_args.kwargs["include_tickers"] is False
    assert builder.call_args.kwargs["include_maker_attempts"] is True
