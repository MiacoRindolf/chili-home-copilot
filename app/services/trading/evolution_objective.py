"""Configurable fitness for ranking ScanPattern variants during evolution.

Used by ``evolve_pattern_strategies`` so promotion favors Sharpe, win rate, and/or
average return according to ``Settings`` (.env).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...config import Settings


def _settings(s: "Settings | None" = None) -> "Settings":
    from ...config import settings as default_settings

    return s if s is not None else default_settings


def compute_variant_fitness(
    adj_sharpe: float,
    wr: float,
    avg_return_pct: float,
    n_backtests: int,
    *,
    settings: "Settings | None" = None,
) -> float:
    """Single scalar for sorting variants (higher = better).

    *adj_sharpe* should already include insight-based adjustments (fakeout/synergy)
    from the evolution loop. *wr* is 0..1. *avg_return_pct* is mean ticker-level
    return from stored backtests (rough signal).
    """
    cfg = _settings(settings)
    w_s = float(getattr(cfg, "brain_evolution_weight_sharpe", 1.0))
    w_wr = float(getattr(cfg, "brain_evolution_weight_wr", 2.0))
    w_ret = float(getattr(cfg, "brain_evolution_weight_return", 0.01))
    min_t = max(1, int(getattr(cfg, "brain_evolution_min_trades", 5)))
    pen = float(getattr(cfg, "brain_evolution_min_trades_penalty", 0.25))

    base = w_s * float(adj_sharpe) + w_wr * float(wr) + w_ret * float(avg_return_pct)
    if n_backtests < min_t:
        # Softer ranking when evidence thin (fork phase may still compare)
        scale = max(0.05, (n_backtests / min_t) * pen)
        base *= scale
    return base


def aggregate_backtest_metrics(
    backtest_rows: list[Any],
) -> tuple[float, float, float]:
    """From ORM BacktestResult rows: (avg_sharpe or 0, win_rate 0..1, avg return_pct)."""
    if not backtest_rows:
        return 0.0, 0.0, 0.0
    sharpes = [float(bt.sharpe) for bt in backtest_rows if bt.sharpe is not None]
    avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0.0
    wins = sum(1 for bt in backtest_rows if (bt.return_pct or 0) > 0)
    wr = wins / len(backtest_rows)
    avg_ret = sum(float(bt.return_pct or 0) for bt in backtest_rows) / len(backtest_rows)
    return avg_sharpe, wr, avg_ret
