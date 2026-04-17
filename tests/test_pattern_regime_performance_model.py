"""Unit tests for Phase M.1 pure pattern x regime performance model."""
from __future__ import annotations

import math
from datetime import date

import pytest

from app.services.trading.pattern_regime_performance_model import (
    DEFAULT_DIMENSIONS,
    DIMENSION_MACRO_REGIME,
    DIMENSION_SESSION_LABEL,
    DIMENSION_TICKER_REGIME,
    LABEL_UNAVAILABLE,
    ClosedTradeRecord,
    PatternRegimePerfConfig,
    PatternRegimePerfInput,
    RegimeLookup,
    build_pattern_regime_cells,
    compute_ledger_run_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _single_label_lookup(
    dimension: str, as_of: date, label: str, ticker: str = "AAPL"
) -> RegimeLookup:
    """Build a RegimeLookup with ONE label in ONE dimension."""
    from app.services.trading.pattern_regime_performance_model import (
        TICKER_KEYED_DIMENSIONS,
    )

    lookup = RegimeLookup(unavailable_label=LABEL_UNAVAILABLE)
    if dimension in TICKER_KEYED_DIMENSIONS:
        lookup.ticker_keyed.setdefault(dimension, {}).setdefault(
            ticker, []
        ).append((as_of, label))
    else:
        lookup.market_wide.setdefault(dimension, []).append((as_of, label))
    lookup.sort_inplace()
    return lookup


def _multi_dim_lookup(
    as_of: date, label_per_dim: dict, ticker: str = "AAPL"
) -> RegimeLookup:
    from app.services.trading.pattern_regime_performance_model import (
        TICKER_KEYED_DIMENSIONS,
    )

    lookup = RegimeLookup(unavailable_label=LABEL_UNAVAILABLE)
    for dim, label in label_per_dim.items():
        if dim in TICKER_KEYED_DIMENSIONS:
            lookup.ticker_keyed.setdefault(dim, {}).setdefault(
                ticker, []
            ).append((as_of, label))
        else:
            lookup.market_wide.setdefault(dim, []).append((as_of, label))
    lookup.sort_inplace()
    return lookup


def _mk_trade(
    pattern_id: int,
    ticker: str,
    entry: date,
    exit_: date,
    pnl_pct: float,
    hold_days: float = 3.0,
) -> ClosedTradeRecord:
    return ClosedTradeRecord(
        pattern_id=pattern_id,
        ticker=ticker,
        entry_date=entry,
        exit_date=exit_,
        pnl_pct=pnl_pct,
        hold_days=hold_days,
    )


# ---------------------------------------------------------------------------
# Ledger run id determinism
# ---------------------------------------------------------------------------


def test_ledger_run_id_deterministic():
    a = compute_ledger_run_id(as_of_date=date(2026, 4, 16), window_days=90)
    b = compute_ledger_run_id(as_of_date=date(2026, 4, 16), window_days=90)
    assert a == b
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_ledger_run_id_date_sensitive():
    a = compute_ledger_run_id(as_of_date=date(2026, 4, 16), window_days=90)
    b = compute_ledger_run_id(as_of_date=date(2026, 4, 17), window_days=90)
    assert a != b


def test_ledger_run_id_window_sensitive():
    a = compute_ledger_run_id(as_of_date=date(2026, 4, 16), window_days=90)
    b = compute_ledger_run_id(as_of_date=date(2026, 4, 16), window_days=60)
    assert a != b


def test_ledger_run_id_rejects_bad_window():
    with pytest.raises(ValueError):
        compute_ledger_run_id(as_of_date=date(2026, 4, 16), window_days=0)
    with pytest.raises(ValueError):
        compute_ledger_run_id(
            as_of_date=date(2026, 4, 16), window_days=-1
        )


def test_ledger_run_id_rejects_bad_date():
    with pytest.raises(TypeError):
        compute_ledger_run_id(
            as_of_date="2026-04-16", window_days=90  # type: ignore
        )


# ---------------------------------------------------------------------------
# Trade validation
# ---------------------------------------------------------------------------


def test_trade_validation_rejects_nonfinite():
    with pytest.raises(ValueError):
        _mk_trade(
            1, "AAPL", date(2026, 4, 1), date(2026, 4, 4), float("nan")
        )
    with pytest.raises(ValueError):
        _mk_trade(
            1, "AAPL", date(2026, 4, 1), date(2026, 4, 4), float("inf")
        )


def test_trade_validation_rejects_empty_ticker():
    with pytest.raises(ValueError):
        _mk_trade(1, "", date(2026, 4, 1), date(2026, 4, 4), 0.01)


# ---------------------------------------------------------------------------
# RegimeLookup resolution
# ---------------------------------------------------------------------------


def test_lookup_returns_unavailable_when_empty():
    lookup = RegimeLookup(unavailable_label=LABEL_UNAVAILABLE)
    assert (
        lookup.resolve(
            dimension=DIMENSION_MACRO_REGIME,
            as_of_on_or_before=date(2026, 4, 15),
            ticker="AAPL",
        )
        == LABEL_UNAVAILABLE
    )


def test_lookup_returns_most_recent_at_or_before():
    lookup = RegimeLookup()
    lookup.market_wide[DIMENSION_MACRO_REGIME] = [
        (date(2026, 4, 10), "risk_on"),
        (date(2026, 4, 12), "risk_off"),
        (date(2026, 4, 14), "neutral"),
    ]
    # Exact hit
    assert (
        lookup.resolve(
            dimension=DIMENSION_MACRO_REGIME,
            as_of_on_or_before=date(2026, 4, 12),
            ticker="X",
        )
        == "risk_off"
    )
    # In-between
    assert (
        lookup.resolve(
            dimension=DIMENSION_MACRO_REGIME,
            as_of_on_or_before=date(2026, 4, 13),
            ticker="X",
        )
        == "risk_off"
    )
    # Past last snapshot
    assert (
        lookup.resolve(
            dimension=DIMENSION_MACRO_REGIME,
            as_of_on_or_before=date(2026, 5, 1),
            ticker="X",
        )
        == "neutral"
    )
    # Before first snapshot
    assert (
        lookup.resolve(
            dimension=DIMENSION_MACRO_REGIME,
            as_of_on_or_before=date(2026, 4, 1),
            ticker="X",
        )
        == LABEL_UNAVAILABLE
    )


def test_ticker_keyed_lookup_isolates_tickers():
    lookup = RegimeLookup()
    lookup.ticker_keyed[DIMENSION_TICKER_REGIME] = {
        "AAPL": [(date(2026, 4, 15), "ticker_regime_trend_up")],
        "TSLA": [(date(2026, 4, 15), "ticker_regime_choppy")],
    }
    assert (
        lookup.resolve(
            dimension=DIMENSION_TICKER_REGIME,
            as_of_on_or_before=date(2026, 4, 15),
            ticker="AAPL",
        )
        == "ticker_regime_trend_up"
    )
    assert (
        lookup.resolve(
            dimension=DIMENSION_TICKER_REGIME,
            as_of_on_or_before=date(2026, 4, 15),
            ticker="TSLA",
        )
        == "ticker_regime_choppy"
    )
    # Missing ticker -> unavailable
    assert (
        lookup.resolve(
            dimension=DIMENSION_TICKER_REGIME,
            as_of_on_or_before=date(2026, 4, 15),
            ticker="NVDA",
        )
        == LABEL_UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# Fan-out: one trade -> one cell per dimension
# ---------------------------------------------------------------------------


def test_single_trade_fans_out_to_eight_cells():
    trade = _mk_trade(
        42, "AAPL", date(2026, 4, 1), date(2026, 4, 4), 0.02, 3.0
    )
    # lookup all 8 dimensions to "test_label"
    lookup = _multi_dim_lookup(
        date(2026, 3, 31), {d: f"test_{d}" for d in DEFAULT_DIMENSIONS}
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=[trade],
        lookup=lookup,
        config=PatternRegimePerfConfig(
            window_days=90, min_trades_per_cell=1
        ),
    )
    out = build_pattern_regime_cells(inp)
    assert len(out.cells) == 8
    dims = {c.regime_dimension for c in out.cells}
    assert dims == set(DEFAULT_DIMENSIONS)
    assert out.patterns_observed == 1
    assert out.total_trades_observed == 1
    # unavailable_cells is per-dim-trade; ticker_regime and all market
    # dims hit, so zero unavailable
    assert out.unavailable_cells == 0
    for cell in out.cells:
        assert cell.n_trades == 1
        assert cell.has_confidence  # min_trades_per_cell=1


def test_single_trade_with_no_lookup_produces_unavailable_cells():
    trade = _mk_trade(
        42, "AAPL", date(2026, 4, 1), date(2026, 4, 4), 0.02
    )
    lookup = RegimeLookup(unavailable_label=LABEL_UNAVAILABLE)  # empty
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=[trade],
        lookup=lookup,
        config=PatternRegimePerfConfig(min_trades_per_cell=1),
    )
    out = build_pattern_regime_cells(inp)
    assert len(out.cells) == 8
    assert all(c.regime_label == LABEL_UNAVAILABLE for c in out.cells)
    assert out.unavailable_cells == 8  # 1 trade × 8 dimensions


# ---------------------------------------------------------------------------
# Aggregation math
# ---------------------------------------------------------------------------


def test_aggregate_math_mixed_wins_and_losses():
    pnls = [0.02, 0.04, -0.01, -0.02, 0.03]  # 3 wins, 2 losses
    trades = [
        _mk_trade(
            1,
            "AAPL",
            date(2026, 4, i + 1),
            date(2026, 4, i + 4),
            p,
            3.0,
        )
        for i, p in enumerate(pnls)
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=3,
        ),
    )
    out = build_pattern_regime_cells(inp)
    assert len(out.cells) == 1
    c = out.cells[0]
    assert c.n_trades == 5
    assert c.n_wins == 3
    assert c.hit_rate == pytest.approx(0.6)
    assert c.mean_pnl_pct == pytest.approx(sum(pnls) / 5)
    assert c.sum_pnl == pytest.approx(sum(pnls))
    assert c.mean_win_pct == pytest.approx((0.02 + 0.04 + 0.03) / 3)
    assert c.mean_loss_pct == pytest.approx((-0.01 + -0.02) / 2)
    # expectancy = hit * mean_win + (1-hit) * mean_loss
    exp_expectancy = 0.6 * ((0.02 + 0.04 + 0.03) / 3) + 0.4 * (
        (-0.01 + -0.02) / 2
    )
    assert c.expectancy == pytest.approx(exp_expectancy)
    assert c.profit_factor == pytest.approx(
        (0.02 + 0.04 + 0.03) / abs(-0.01 + -0.02)
    )
    assert c.avg_hold_days == pytest.approx(3.0)
    assert c.has_confidence is True  # 5 >= 3


def test_all_wins_profit_factor_is_none():
    trades = [
        _mk_trade(
            1, "AAPL", date(2026, 4, 1), date(2026, 4, 4), 0.02
        ),
        _mk_trade(
            1, "AAPL", date(2026, 4, 2), date(2026, 4, 5), 0.03
        ),
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=1,
        ),
    )
    out = build_pattern_regime_cells(inp)
    c = out.cells[0]
    assert c.n_wins == 2
    assert c.hit_rate == 1.0
    assert c.profit_factor is None  # no loss denominator
    assert c.mean_loss_pct is None


def test_all_losses_profit_factor_zero():
    trades = [
        _mk_trade(
            1, "AAPL", date(2026, 4, 1), date(2026, 4, 4), -0.01
        ),
        _mk_trade(
            1, "AAPL", date(2026, 4, 2), date(2026, 4, 5), -0.02
        ),
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=1,
        ),
    )
    out = build_pattern_regime_cells(inp)
    c = out.cells[0]
    assert c.n_wins == 0
    assert c.hit_rate == 0.0
    assert c.profit_factor == 0.0


def test_single_trade_sharpe_is_none():
    trade = _mk_trade(
        1, "AAPL", date(2026, 4, 1), date(2026, 4, 4), 0.02, 3.0
    )
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=[trade],
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=1,
        ),
    )
    out = build_pattern_regime_cells(inp)
    assert out.cells[0].sharpe_proxy is None  # stdev undefined on n=1


def test_sharpe_proxy_finite_on_real_data():
    pnls = [0.02, 0.01, -0.005, 0.03, -0.01, 0.015]
    trades = [
        _mk_trade(
            1, "AAPL", date(2026, 4, i + 1), date(2026, 4, i + 3), p, 2.0
        )
        for i, p in enumerate(pnls)
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=3,
        ),
    )
    out = build_pattern_regime_cells(inp)
    c = out.cells[0]
    assert c.sharpe_proxy is not None
    assert math.isfinite(c.sharpe_proxy)
    assert c.has_confidence is True


def test_zero_variance_sharpe_is_none():
    # All identical pnl -> pstdev=0 -> sharpe None
    trades = [
        _mk_trade(
            1, "AAPL", date(2026, 4, i + 1), date(2026, 4, i + 3), 0.02
        )
        for i in range(5)
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=1,
        ),
    )
    out = build_pattern_regime_cells(inp)
    assert out.cells[0].sharpe_proxy is None


# ---------------------------------------------------------------------------
# Confidence gate
# ---------------------------------------------------------------------------


def test_confidence_gate_flags_below_threshold():
    trades = [
        _mk_trade(1, "AAPL", date(2026, 4, 1), date(2026, 4, 4), 0.02),
        _mk_trade(1, "AAPL", date(2026, 4, 2), date(2026, 4, 5), 0.03),
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=5,
        ),
    )
    out = build_pattern_regime_cells(inp)
    assert out.cells[0].has_confidence is False


def test_confidence_gate_fires_at_exact_threshold():
    trades = [
        _mk_trade(
            1, "AAPL", date(2026, 4, i + 1), date(2026, 4, i + 3), 0.01
        )
        for i in range(3)
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=3,
        ),
    )
    out = build_pattern_regime_cells(inp)
    assert out.cells[0].has_confidence is True


# ---------------------------------------------------------------------------
# Determinism / ordering
# ---------------------------------------------------------------------------


def test_output_ordering_is_deterministic_under_reshuffled_input():
    pnls = [0.01, 0.02, -0.01]
    trades_a = [
        _mk_trade(
            1, "AAPL", date(2026, 4, i + 1), date(2026, 4, i + 3), p
        )
        for i, p in enumerate(pnls)
    ]
    trades_b = list(reversed(trades_a))
    lookup = _multi_dim_lookup(
        date(2026, 3, 31),
        {
            DIMENSION_MACRO_REGIME: "risk_on",
            DIMENSION_SESSION_LABEL: "session_trending_up",
        },
    )
    cfg = PatternRegimePerfConfig(
        dimensions=(DIMENSION_MACRO_REGIME, DIMENSION_SESSION_LABEL),
        min_trades_per_cell=1,
    )
    out_a = build_pattern_regime_cells(
        PatternRegimePerfInput(
            as_of_date=date(2026, 4, 16),
            trades=trades_a,
            lookup=lookup,
            config=cfg,
        )
    )
    out_b = build_pattern_regime_cells(
        PatternRegimePerfInput(
            as_of_date=date(2026, 4, 16),
            trades=trades_b,
            lookup=lookup,
            config=cfg,
        )
    )
    # Cell order must match
    assert [
        (c.pattern_id, c.regime_dimension, c.regime_label)
        for c in out_a.cells
    ] == [
        (c.pattern_id, c.regime_dimension, c.regime_label)
        for c in out_b.cells
    ]
    # And aggregate stats must match
    for a, b in zip(out_a.cells, out_b.cells):
        assert a.mean_pnl_pct == b.mean_pnl_pct
        assert a.hit_rate == b.hit_rate


def test_multiple_patterns_separate_cells():
    trades = [
        _mk_trade(
            1, "AAPL", date(2026, 4, 1), date(2026, 4, 4), 0.02
        ),
        _mk_trade(
            2, "AAPL", date(2026, 4, 2), date(2026, 4, 5), -0.01
        ),
        _mk_trade(
            1, "AAPL", date(2026, 4, 3), date(2026, 4, 6), 0.03
        ),
    ]
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=1,
        ),
    )
    out = build_pattern_regime_cells(inp)
    assert len(out.cells) == 2
    by_pattern = {c.pattern_id: c for c in out.cells}
    assert by_pattern[1].n_trades == 2
    assert by_pattern[2].n_trades == 1
    assert out.patterns_observed == 2


def test_multi_label_separate_cells():
    """Two trades same pattern, different regime labels at entry -> 2 cells."""
    lookup = RegimeLookup(unavailable_label=LABEL_UNAVAILABLE)
    lookup.market_wide[DIMENSION_MACRO_REGIME] = [
        (date(2026, 4, 1), "risk_on"),
        (date(2026, 4, 5), "risk_off"),
    ]
    lookup.sort_inplace()
    trades = [
        # Entry 2026-04-03 -> risk_on (snap 04-01)
        _mk_trade(1, "AAPL", date(2026, 4, 3), date(2026, 4, 4), 0.02),
        # Entry 2026-04-06 -> risk_off (snap 04-05)
        _mk_trade(1, "AAPL", date(2026, 4, 6), date(2026, 4, 7), -0.01),
    ]
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=trades,
        lookup=lookup,
        config=PatternRegimePerfConfig(
            dimensions=(DIMENSION_MACRO_REGIME,),
            min_trades_per_cell=1,
        ),
    )
    out = build_pattern_regime_cells(inp)
    labels = sorted([c.regime_label for c in out.cells])
    assert labels == ["risk_off", "risk_on"]


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_trades_produces_empty_output():
    lookup = RegimeLookup()
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=[],
        lookup=lookup,
        config=PatternRegimePerfConfig(),
    )
    out = build_pattern_regime_cells(inp)
    assert out.cells == ()
    assert out.total_trades_observed == 0
    assert out.patterns_observed == 0


def test_output_echoes_config_and_date():
    trade = _mk_trade(
        7, "AAPL", date(2026, 4, 1), date(2026, 4, 4), 0.02
    )
    lookup = _single_label_lookup(
        DIMENSION_MACRO_REGIME, date(2026, 3, 31), "risk_on"
    )
    cfg = PatternRegimePerfConfig(
        window_days=45,
        min_trades_per_cell=1,
        dimensions=(DIMENSION_MACRO_REGIME,),
    )
    inp = PatternRegimePerfInput(
        as_of_date=date(2026, 4, 16),
        trades=[trade],
        lookup=lookup,
        config=cfg,
    )
    out = build_pattern_regime_cells(inp)
    assert out.as_of_date == date(2026, 4, 16)
    assert out.window_days == 45
    assert out.config is cfg
    assert out.ledger_run_id == compute_ledger_run_id(
        as_of_date=date(2026, 4, 16), window_days=45
    )
