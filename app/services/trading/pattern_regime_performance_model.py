"""Phase M.1 — Pattern × Regime Performance Ledger (pure functions).

First consumer of the L.17 – L.22 regime snapshot stack. Given a list
of closed paper trades (with ``pattern_id``, ``ticker``, ``entry_date``,
``exit_date``, ``pnl_pct``, ``hold_days``) and a pre-built
``RegimeLookup`` (``dimension × date [× ticker] → label``), produces
per-(pattern, dimension, label) aggregate stats:

- ``n_trades``, ``n_wins``, ``hit_rate``
- ``mean_pnl_pct``, ``median_pnl_pct``, ``sum_pnl``
- ``mean_win_pct``, ``mean_loss_pct``
- ``expectancy`` = ``hit_rate × mean_win - (1 - hit_rate) × |mean_loss|``
- ``profit_factor`` = ``sum_wins / |sum_losses|`` (``None`` on no
  losses; ``0.0`` on no wins)
- ``sharpe_proxy`` = ``mean / std × sqrt(252 / avg_hold_days)``
  (``None`` on degenerate std / hold)
- ``avg_hold_days``
- ``has_confidence`` = ``n_trades >= min_trades_per_cell``

Shadow-only: this pure model has **no side effects**. The service
layer owns the trade fetch, snapshot fetch, and persistence.

Determinism
-----------
``compute_ledger_run_id(as_of_date, window_days)`` returns
``sha256('pattern_regime_perf:' + iso + ':' + w)[:16]`` so a given
(date, window) pair always yields the same ledger id.

Non-goals (M.1)
---------------
- Multi-factor / 2-D interaction cells (``macro_regime × session``).
- Per-backtest-trade stratification (backtest table is aggregated).
- Authoritative consumption (sizing / promotion). Deferred to M.2.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

# ---------------------------------------------------------------------------
# Constants — dimension names (stable identifiers persisted to DB)
# ---------------------------------------------------------------------------

DIMENSION_MACRO_REGIME = "macro_regime"
DIMENSION_BREADTH_LABEL = "breadth_label"
DIMENSION_CROSS_ASSET_LABEL = "cross_asset_label"
DIMENSION_TICKER_REGIME = "ticker_regime"
DIMENSION_VOL_REGIME = "vol_regime"
DIMENSION_DISPERSION_LABEL = "dispersion_label"
DIMENSION_CORRELATION_LABEL = "correlation_label"
DIMENSION_SESSION_LABEL = "session_label"

DEFAULT_DIMENSIONS: Tuple[str, ...] = (
    DIMENSION_MACRO_REGIME,
    DIMENSION_BREADTH_LABEL,
    DIMENSION_CROSS_ASSET_LABEL,
    DIMENSION_TICKER_REGIME,
    DIMENSION_VOL_REGIME,
    DIMENSION_DISPERSION_LABEL,
    DIMENSION_CORRELATION_LABEL,
    DIMENSION_SESSION_LABEL,
)

# Dimensions that require the trade's ``ticker`` to look up the label.
# Everyone else is market-wide and keyed by date only.
TICKER_KEYED_DIMENSIONS: frozenset = frozenset([DIMENSION_TICKER_REGIME])

LABEL_UNAVAILABLE = "regime_unavailable"

TRADING_DAYS_PER_YEAR = 252.0

# ---------------------------------------------------------------------------
# Config + input / output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternRegimePerfConfig:
    """Config echoed into ``payload_json`` for forensics."""

    window_days: int = 90
    min_trades_per_cell: int = 3
    dimensions: Tuple[str, ...] = DEFAULT_DIMENSIONS
    unavailable_label: str = LABEL_UNAVAILABLE

    def as_mapping(self) -> Dict[str, Any]:
        return {
            "window_days": int(self.window_days),
            "min_trades_per_cell": int(self.min_trades_per_cell),
            "dimensions": list(self.dimensions),
            "unavailable_label": self.unavailable_label,
        }


@dataclass(frozen=True)
class ClosedTradeRecord:
    """Per-trade input. ``pnl_pct`` is fractional (0.01 = 1 %)."""

    pattern_id: int
    ticker: str
    entry_date: date
    exit_date: date
    pnl_pct: float
    hold_days: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.pattern_id, int):
            raise TypeError(
                f"pattern_id must be int, got {type(self.pattern_id).__name__}"
            )
        if not self.ticker or not isinstance(self.ticker, str):
            raise ValueError("ticker must be a non-empty string")
        if not isinstance(self.entry_date, date):
            raise TypeError("entry_date must be a date")
        if not isinstance(self.exit_date, date):
            raise TypeError("exit_date must be a date")
        if not isinstance(self.pnl_pct, (int, float)):
            raise TypeError("pnl_pct must be numeric")
        if not math.isfinite(float(self.pnl_pct)):
            raise ValueError("pnl_pct must be finite")


@dataclass
class RegimeLookup:
    """Resolves ``(dimension, date [, ticker]) -> label``.

    Internal storage separates ticker-keyed dimensions from market-wide
    dimensions so the pure model doesn't branch on dimension strings at
    lookup time. The service layer is responsible for populating this
    from the latest-at-or-before-date snapshot per dimension.

    All dates stored here are the ``as_of_date`` of a snapshot row.
    ``resolve()`` walks backwards to find the most recent snapshot at
    or before the trade's ``entry_date``. If none exists, it returns
    the configured unavailable_label.
    """

    # dimension -> sorted list of (as_of_date, label)
    market_wide: Dict[str, List[Tuple[date, str]]] = field(
        default_factory=dict
    )
    # dimension -> ticker -> sorted list of (as_of_date, label)
    ticker_keyed: Dict[str, Dict[str, List[Tuple[date, str]]]] = field(
        default_factory=dict
    )
    unavailable_label: str = LABEL_UNAVAILABLE

    def resolve(
        self, *, dimension: str, as_of_on_or_before: date, ticker: str
    ) -> str:
        """Return the most recent label at or before ``as_of_on_or_before``."""
        if dimension in TICKER_KEYED_DIMENSIONS:
            by_ticker = self.ticker_keyed.get(dimension, {})
            rows = by_ticker.get(ticker, [])
        else:
            rows = self.market_wide.get(dimension, [])
        if not rows:
            return self.unavailable_label
        label = self.unavailable_label
        for snap_date, snap_label in rows:
            if snap_date <= as_of_on_or_before:
                label = snap_label
            else:
                break
        return label

    def sort_inplace(self) -> None:
        """Sort stored (date, label) lists ascending for binary-ish walk."""
        for d in self.market_wide:
            self.market_wide[d].sort(key=lambda r: r[0])
        for d, by_ticker in self.ticker_keyed.items():
            for t in by_ticker:
                by_ticker[t].sort(key=lambda r: r[0])


@dataclass(frozen=True)
class PatternRegimePerfInput:
    as_of_date: date
    trades: Sequence[ClosedTradeRecord]
    lookup: RegimeLookup
    config: PatternRegimePerfConfig = field(
        default_factory=PatternRegimePerfConfig
    )


@dataclass(frozen=True)
class PatternRegimeCell:
    pattern_id: int
    regime_dimension: str
    regime_label: str
    n_trades: int
    n_wins: int
    hit_rate: Optional[float]
    mean_pnl_pct: Optional[float]
    median_pnl_pct: Optional[float]
    sum_pnl: Optional[float]
    expectancy: Optional[float]
    mean_win_pct: Optional[float]
    mean_loss_pct: Optional[float]
    profit_factor: Optional[float]
    sharpe_proxy: Optional[float]
    avg_hold_days: Optional[float]
    has_confidence: bool
    payload: Dict[str, Any]


@dataclass(frozen=True)
class PatternRegimePerfOutput:
    ledger_run_id: str
    as_of_date: date
    window_days: int
    cells: Tuple[PatternRegimeCell, ...]
    unavailable_cells: int
    total_trades_observed: int
    patterns_observed: int
    config: PatternRegimePerfConfig


# ---------------------------------------------------------------------------
# Determinism helpers
# ---------------------------------------------------------------------------


def compute_ledger_run_id(*, as_of_date: date, window_days: int) -> str:
    """Deterministic ``sha256('pattern_regime_perf:' + iso + ':' + w)[:16]``."""
    if not isinstance(as_of_date, date):
        raise TypeError("as_of_date must be a date")
    if not isinstance(window_days, int) or window_days <= 0:
        raise ValueError("window_days must be a positive int")
    payload = (
        f"pattern_regime_perf:{as_of_date.isoformat()}:{int(window_days)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _safe_stdev(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    try:
        s = float(statistics.pstdev(values))
    except statistics.StatisticsError:
        return None
    if not math.isfinite(s) or s <= 0.0:
        return None
    return s


def _aggregate_cell(
    *,
    pattern_id: int,
    dimension: str,
    label: str,
    trades: Sequence[ClosedTradeRecord],
    min_trades_per_cell: int,
) -> PatternRegimeCell:
    """Compute one aggregate row deterministically."""
    n = len(trades)
    if n == 0:
        return PatternRegimeCell(
            pattern_id=pattern_id,
            regime_dimension=dimension,
            regime_label=label,
            n_trades=0,
            n_wins=0,
            hit_rate=None,
            mean_pnl_pct=None,
            median_pnl_pct=None,
            sum_pnl=None,
            expectancy=None,
            mean_win_pct=None,
            mean_loss_pct=None,
            profit_factor=None,
            sharpe_proxy=None,
            avg_hold_days=None,
            has_confidence=False,
            payload={},
        )

    pnls = [float(t.pnl_pct) for t in trades]
    wins = [p for p in pnls if p > 0.0]
    losses = [p for p in pnls if p < 0.0]
    holds_raw = [t.hold_days for t in trades if t.hold_days is not None]
    holds = [float(h) for h in holds_raw if math.isfinite(float(h))]

    n_wins = len(wins)
    hit_rate = float(n_wins) / float(n)
    mean_pnl = float(sum(pnls) / n)
    median_pnl = _median(pnls)
    sum_pnl = float(sum(pnls))
    mean_win = float(sum(wins) / len(wins)) if wins else None
    mean_loss = float(sum(losses) / len(losses)) if losses else None

    # expectancy in pnl_pct units
    if mean_win is not None and mean_loss is not None:
        expectancy = (
            hit_rate * mean_win + (1.0 - hit_rate) * mean_loss
        )
    elif mean_win is not None and not losses:
        # all wins: expectancy = mean_win * hit_rate (hit_rate = 1)
        expectancy = hit_rate * mean_win
    elif mean_loss is not None and not wins:
        # all losses
        expectancy = (1.0 - hit_rate) * mean_loss
    else:
        expectancy = None

    # profit factor: sum_wins / |sum_losses|
    if wins and losses:
        sum_wins = float(sum(wins))
        sum_losses_abs = float(abs(sum(losses)))
        profit_factor: Optional[float] = (
            sum_wins / sum_losses_abs if sum_losses_abs > 0 else None
        )
    elif wins and not losses:
        profit_factor = None  # undefined; divide by zero
    elif losses and not wins:
        profit_factor = 0.0
    else:
        profit_factor = None

    # avg hold days
    avg_hold_days = (
        float(sum(holds) / len(holds)) if holds else None
    )

    # sharpe proxy: mean / std * sqrt(252 / avg_hold)
    std = _safe_stdev(pnls)
    sharpe_proxy: Optional[float]
    if (
        std is not None
        and avg_hold_days is not None
        and avg_hold_days > 0.0
    ):
        try:
            scale = math.sqrt(TRADING_DAYS_PER_YEAR / avg_hold_days)
            sharpe_proxy = (mean_pnl / std) * scale
        except (ValueError, ZeroDivisionError):
            sharpe_proxy = None
    else:
        sharpe_proxy = None

    has_confidence = n >= int(min_trades_per_cell)

    return PatternRegimeCell(
        pattern_id=pattern_id,
        regime_dimension=dimension,
        regime_label=label,
        n_trades=n,
        n_wins=n_wins,
        hit_rate=round(hit_rate, 6),
        mean_pnl_pct=round(mean_pnl, 8) if math.isfinite(mean_pnl) else None,
        median_pnl_pct=(
            round(median_pnl, 8)
            if median_pnl is not None and math.isfinite(median_pnl)
            else None
        ),
        sum_pnl=round(sum_pnl, 8) if math.isfinite(sum_pnl) else None,
        expectancy=(
            round(expectancy, 8)
            if expectancy is not None and math.isfinite(expectancy)
            else None
        ),
        mean_win_pct=(
            round(mean_win, 8)
            if mean_win is not None and math.isfinite(mean_win)
            else None
        ),
        mean_loss_pct=(
            round(mean_loss, 8)
            if mean_loss is not None and math.isfinite(mean_loss)
            else None
        ),
        profit_factor=(
            round(profit_factor, 6)
            if profit_factor is not None
            and math.isfinite(profit_factor)
            else None
        ),
        sharpe_proxy=(
            round(sharpe_proxy, 6)
            if sharpe_proxy is not None
            and math.isfinite(sharpe_proxy)
            else None
        ),
        avg_hold_days=(
            round(avg_hold_days, 4)
            if avg_hold_days is not None
            and math.isfinite(avg_hold_days)
            else None
        ),
        has_confidence=has_confidence,
        payload={},
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def build_pattern_regime_cells(
    inp: PatternRegimePerfInput,
) -> PatternRegimePerfOutput:
    """Stratify closed trades by (pattern, dimension, label) and aggregate.

    Each trade contributes to exactly ONE cell per dimension (never
    more than one), so with the default 8-dimension config a single
    trade with pattern_id = P produces 8 output rows (some of which
    may share ``label = regime_unavailable`` if the corresponding
    snapshot was missing).
    """
    if not isinstance(inp, PatternRegimePerfInput):
        raise TypeError("inp must be a PatternRegimePerfInput")

    config = inp.config
    # dimension -> label -> pattern_id -> list[ClosedTradeRecord]
    grouped: Dict[str, Dict[str, Dict[int, List[ClosedTradeRecord]]]] = {
        d: {} for d in config.dimensions
    }

    unavailable_count = 0
    patterns_seen: set = set()

    for trade in inp.trades:
        patterns_seen.add(trade.pattern_id)
        for dimension in config.dimensions:
            label = inp.lookup.resolve(
                dimension=dimension,
                as_of_on_or_before=trade.entry_date,
                ticker=trade.ticker,
            )
            if label == config.unavailable_label:
                unavailable_count += 1
            grouped.setdefault(dimension, {}).setdefault(
                label, {}
            ).setdefault(trade.pattern_id, []).append(trade)

    cells: List[PatternRegimeCell] = []
    for dimension in config.dimensions:
        by_label = grouped.get(dimension, {})
        for label, by_pattern in by_label.items():
            for pattern_id, cell_trades in by_pattern.items():
                cell = _aggregate_cell(
                    pattern_id=pattern_id,
                    dimension=dimension,
                    label=label,
                    trades=cell_trades,
                    min_trades_per_cell=config.min_trades_per_cell,
                )
                cells.append(cell)

    # Deterministic ordering: pattern ASC, dimension ASC, label ASC.
    cells.sort(
        key=lambda c: (
            int(c.pattern_id),
            str(c.regime_dimension),
            str(c.regime_label),
        )
    )

    return PatternRegimePerfOutput(
        ledger_run_id=compute_ledger_run_id(
            as_of_date=inp.as_of_date, window_days=config.window_days
        ),
        as_of_date=inp.as_of_date,
        window_days=int(config.window_days),
        cells=tuple(cells),
        unavailable_cells=int(unavailable_count),
        total_trades_observed=len(inp.trades),
        patterns_observed=len(patterns_seen),
        config=config,
    )


__all__ = [
    "DIMENSION_MACRO_REGIME",
    "DIMENSION_BREADTH_LABEL",
    "DIMENSION_CROSS_ASSET_LABEL",
    "DIMENSION_TICKER_REGIME",
    "DIMENSION_VOL_REGIME",
    "DIMENSION_DISPERSION_LABEL",
    "DIMENSION_CORRELATION_LABEL",
    "DIMENSION_SESSION_LABEL",
    "DEFAULT_DIMENSIONS",
    "TICKER_KEYED_DIMENSIONS",
    "LABEL_UNAVAILABLE",
    "PatternRegimePerfConfig",
    "ClosedTradeRecord",
    "RegimeLookup",
    "PatternRegimePerfInput",
    "PatternRegimeCell",
    "PatternRegimePerfOutput",
    "compute_ledger_run_id",
    "build_pattern_regime_cells",
]
