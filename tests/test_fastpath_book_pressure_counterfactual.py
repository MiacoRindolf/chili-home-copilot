from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.trading.fast_path.book_pressure_counterfactual import (
    BookPressureVariant,
    build_windows,
    derive_quantile_variants,
    evaluate_variants,
    evenly_spaced_quantiles,
    observation_from_book_row,
)


def _row(
    *,
    ticker: str = "TEST-USD",
    at: datetime,
    bid: float,
    ask: float,
    bid_size: float = 10.0,
    ask_size: float = 2.0,
    bid_total: float | None = None,
    ask_total: float | None = None,
    imbalance: float = 0.8,
) -> dict:
    mid = (bid + ask) / 2.0
    return {
        "ticker": ticker,
        "snapshot_at": at,
        "bid_levels": [[bid, bid_size]],
        "ask_levels": [[ask, ask_size]],
        "bid_total_size": bid_total if bid_total is not None else bid_size,
        "ask_total_size": ask_total if ask_total is not None else ask_size,
        "imbalance": imbalance,
        "spread_bps": ((ask - bid) / mid) * 10000.0,
    }


def test_observation_from_book_row_uses_depth_weighted_microprice():
    obs = observation_from_book_row(
        _row(
            at=datetime(2026, 5, 24, 18, 0, 0),
            bid=100.0,
            ask=100.02,
            bid_total=9.0,
            ask_total=1.0,
        )
    )

    assert obs is not None
    assert obs.ticker == "TEST-USD"
    assert obs.microprice_edge_bps > 0.0
    assert obs.top_bid_notional_usd == pytest.approx(1000.0)
    assert obs.top_ask_notional_usd == pytest.approx(200.04)


def test_observation_from_book_row_preserves_zero_imbalance():
    obs = observation_from_book_row(
        _row(
            at=datetime(2026, 5, 24, 18, 0, 0),
            bid=100.0,
            ask=100.02,
            bid_size=1.0,
            ask_size=9.0,
            imbalance=0.0,
        )
    )

    assert obs is not None
    assert obs.imbalance == 0.0


def test_build_windows_computes_reclaim_metrics():
    start = datetime(2026, 5, 24, 18, 0, 0)
    observations = [
        observation_from_book_row(
            _row(at=start + timedelta(seconds=i), bid=100.0 + i * 0.01,
                 ask=100.02 + i * 0.01)
        )
        for i in range(3)
    ]

    windows = build_windows([obs for obs in observations if obs], window_size=3)

    assert len(windows) == 1
    window = windows[0]
    assert window.avg_imbalance == pytest.approx(0.8)
    assert window.mid_move_bps > 0.0
    assert window.best_bid_move_bps > 0.0
    assert window.max_spread_bps > 0.0


def test_quantile_variants_are_derived_from_window_distribution():
    start = datetime(2026, 5, 24, 18, 0, 0)
    observations = [
        observation_from_book_row(
            _row(
                at=start + timedelta(seconds=i),
                bid=100.0 + i * 0.01,
                ask=100.02 + i * 0.01,
                bid_size=5.0 + i,
                ask_size=2.0,
                imbalance=0.55 + i * 0.05,
            )
        )
        for i in range(5)
    ]
    windows = build_windows([obs for obs in observations if obs], window_size=2)

    variants = derive_quantile_variants(
        windows,
        quantiles=evenly_spaced_quantiles(2),
        cooldown_s=30.0,
    )

    assert len(variants) == 2
    assert variants[0].name.startswith("derived_q")
    assert variants[0].cooldown_s == 30.0
    assert variants[0].min_avg_imbalance is not None
    assert variants[0].max_spread_bps is not None


def test_evaluate_variants_uses_executable_ask_to_future_bid_return():
    start = datetime(2026, 5, 24, 18, 0, 0)
    observations = [
        observation_from_book_row(
            _row(
                at=start + timedelta(seconds=i),
                bid=100.00 + i * 0.05,
                ask=100.02 + i * 0.05,
                bid_total=9.0,
                ask_total=1.0,
            )
        )
        for i in range(5)
    ]
    observations = [obs for obs in observations if obs]
    windows = build_windows(observations, window_size=2)
    variant = BookPressureVariant(
        name="all_windows",
        cooldown_s=0.0,
    )

    results = evaluate_variants(
        observations,
        windows,
        [variant],
        horizons_s=[1],
        fee_bps_per_side=0.0,
        min_net_bps=0.0,
    )

    assert len(results) == 1
    result = results[0]
    assert result.sample_count == 3
    assert result.gross_mean_bps is not None
    assert result.gross_mean_bps > 0.0
    assert result.verdict == "positive_edge_candidate"


def test_evaluate_variants_applies_per_ticker_cooldown():
    start = datetime(2026, 5, 24, 18, 0, 0)
    observations = [
        observation_from_book_row(
            _row(
                at=start + timedelta(seconds=i * 5),
                bid=100.00 + i * 0.05,
                ask=100.02 + i * 0.05,
                bid_total=9.0,
                ask_total=1.0,
            )
        )
        for i in range(6)
    ]
    observations = [obs for obs in observations if obs]
    windows = build_windows(observations, window_size=2)
    no_cooldown = BookPressureVariant(name="none", cooldown_s=0.0)
    cooldown = BookPressureVariant(name="cooldown", cooldown_s=11.0)

    results = evaluate_variants(
        observations,
        windows,
        [no_cooldown, cooldown],
        horizons_s=[1],
        fee_bps_per_side=0.0,
    )
    by_name = {result.variant_name: result for result in results}

    assert by_name["none"].sample_count > by_name["cooldown"].sample_count
    assert by_name["cooldown"].triggered_by_ticker["TEST-USD"] == 2
