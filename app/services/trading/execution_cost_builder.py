"""Rolling execution-cost estimator (Phase F, shadow-safe DB writer).

Reads closed ``Trade`` rows and their TCA slippage columns, computes
median / p90 spread + slippage in bps, pulls average daily volume from a
caller-provided callable (so tests can stay pure), and upserts the
result into ``trading_execution_cost_estimates`` keyed by
``(ticker, side, window_days)``.

Mode gate is ``settings.brain_execution_cost_mode``:
  * ``off``          - writer is a no-op; reads return empty
  * ``shadow``       - writer upserts rows; no one reads them
  * ``authoritative``- writer upserts rows and callers may consume

Everything here is idempotent on the UNIQUE ``uq_execution_cost_estimates``
index — running the estimator twice produces the same state.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.execution_cost_ops_log import (
    format_execution_cost_ops_line,
)

logger = logging.getLogger(__name__)

_ALLOWED_MODES = ("off", "shadow", "authoritative")


# ── Mode helpers ───────────────────────────────────────────────────────

def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_execution_cost_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(override: str | None = None) -> bool:
    return _effective_mode(override) != "off"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_venue_truth_ops_log_enabled", True))


# ── Percentile helper ──────────────────────────────────────────────────

def _percentile(values: list[float], q: float) -> float:
    """Classic linear-interpolation percentile. ``q`` in [0, 1]."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    if len(sorted_v) == 1:
        return sorted_v[0]
    q = max(0.0, min(1.0, q))
    idx = q * (len(sorted_v) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_v[lo]
    frac = idx - lo
    return sorted_v[lo] * (1.0 - frac) + sorted_v[hi] * frac


# ── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class EstimateRow:
    ticker: str
    side: str
    window_days: int
    median_spread_bps: float
    p90_spread_bps: float
    median_slippage_bps: float
    p90_slippage_bps: float
    avg_daily_volume_usd: float
    sample_trades: int


@dataclass
class BuilderReport:
    mode: str
    estimates_written: int = 0
    estimates_skipped: int = 0
    tickers_seen: int = 0
    errors: list[str] = field(default_factory=list)


# ── Core compute ───────────────────────────────────────────────────────

def _ensure_side(direction: str | None) -> str:
    d = (direction or "long").strip().lower()
    return "short" if d == "short" else "long"


def _notional(trade: Any) -> float:
    try:
        px = float(trade.entry_price or 0.0)
        qty = float(trade.quantity or 0.0)
        return abs(px * qty)
    except Exception:
        return 0.0


def compute_rolling_estimate(
    db: Session,
    *,
    ticker: str,
    side: str,
    window_days: int = 30,
    adv_lookup_fn: Callable[[str, int], float] | None = None,
    now: datetime | None = None,
) -> EstimateRow | None:
    """Compute a rolling execution-cost estimate from closed trades.

    Parameters
    ----------
    ticker, side, window_days:
        Key used for both the filter and the output row.
    adv_lookup_fn:
        Optional ``(ticker, window_days) -> avg_daily_volume_usd`` callable.
        When None we fall back to "sum of trade notional / window_days" which
        is a conservative under-estimate of ADV (good for capacity cap).
    now:
        Injected clock for testability; defaults to ``datetime.utcnow()``.

    Returns
    -------
    EstimateRow when at least one closed trade has a usable TCA slippage,
    None otherwise (caller should treat as "no estimate yet").
    """
    from ...models.trading import Trade  # late import to avoid cycles

    _now = now or datetime.utcnow()
    cutoff = _now - timedelta(days=max(1, int(window_days)))
    side_norm = _ensure_side(side)

    rows = (
        db.query(Trade)
        .filter(
            Trade.ticker == ticker,
            Trade.status == "closed",
            Trade.entry_date >= cutoff,
            Trade.direction == side_norm,
        )
        .all()
    )

    if not rows:
        return None

    slip_bps: list[float] = []
    spread_bps: list[float] = []
    notional_sum = 0.0

    for t in rows:
        entry = None
        exit_ = None
        if t.tca_entry_slippage_bps is not None:
            try:
                entry = float(t.tca_entry_slippage_bps)
            except Exception:
                entry = None
        if t.tca_exit_slippage_bps is not None:
            try:
                exit_ = float(t.tca_exit_slippage_bps)
            except Exception:
                exit_ = None

        if entry is not None:
            slip_bps.append(abs(entry))
            # Treat entry slippage as a proxy for spread paid
            spread_bps.append(abs(entry))
        if exit_ is not None:
            slip_bps.append(abs(exit_))
        notional_sum += _notional(t)

    if not slip_bps:
        return None

    # ADV: prefer external lookup, otherwise crude notional / window_days.
    adv_usd = 0.0
    if adv_lookup_fn is not None:
        try:
            adv_usd = float(adv_lookup_fn(ticker, int(window_days)) or 0.0)
        except Exception:
            adv_usd = 0.0
    if adv_usd <= 0 and window_days > 0:
        adv_usd = notional_sum / float(window_days)

    return EstimateRow(
        ticker=ticker,
        side=side_norm,
        window_days=int(window_days),
        median_spread_bps=_percentile(spread_bps, 0.5) if spread_bps else 0.0,
        p90_spread_bps=_percentile(spread_bps, 0.9) if spread_bps else 0.0,
        median_slippage_bps=_percentile(slip_bps, 0.5),
        p90_slippage_bps=_percentile(slip_bps, 0.9),
        avg_daily_volume_usd=max(0.0, adv_usd),
        sample_trades=len(rows),
    )


# ── Persistence ────────────────────────────────────────────────────────

def upsert_estimate(
    db: Session,
    row: EstimateRow,
    *,
    mode_override: str | None = None,
) -> bool:
    """Idempotent UPSERT on ``uq_execution_cost_estimates``.

    Returns True when a row was written / updated, False when mode is
    ``off``.
    """
    mode = _effective_mode(mode_override)
    if mode == "off":
        return False

    sql = text("""
        INSERT INTO trading_execution_cost_estimates (
            ticker, side, window_days,
            median_spread_bps, p90_spread_bps,
            median_slippage_bps, p90_slippage_bps,
            avg_daily_volume_usd, sample_trades,
            last_updated_at
        )
        VALUES (
            :ticker, :side, :window_days,
            :median_spread_bps, :p90_spread_bps,
            :median_slippage_bps, :p90_slippage_bps,
            :avg_daily_volume_usd, :sample_trades,
            NOW()
        )
        ON CONFLICT (ticker, side, window_days) DO UPDATE SET
            median_spread_bps = EXCLUDED.median_spread_bps,
            p90_spread_bps = EXCLUDED.p90_spread_bps,
            median_slippage_bps = EXCLUDED.median_slippage_bps,
            p90_slippage_bps = EXCLUDED.p90_slippage_bps,
            avg_daily_volume_usd = EXCLUDED.avg_daily_volume_usd,
            sample_trades = EXCLUDED.sample_trades,
            last_updated_at = NOW()
    """)

    db.execute(sql, {
        "ticker": row.ticker,
        "side": row.side,
        "window_days": int(row.window_days),
        "median_spread_bps": float(row.median_spread_bps),
        "p90_spread_bps": float(row.p90_spread_bps),
        "median_slippage_bps": float(row.median_slippage_bps),
        "p90_slippage_bps": float(row.p90_slippage_bps),
        "avg_daily_volume_usd": float(row.avg_daily_volume_usd),
        "sample_trades": int(row.sample_trades),
    })
    db.commit()

    if _ops_log_enabled():
        logger.info(
            format_execution_cost_ops_line(
                event="estimate_write",
                mode=mode,
                ticker=row.ticker,
                side=row.side,
                window_days=row.window_days,
                median_spread_bps=row.median_spread_bps,
                p90_spread_bps=row.p90_spread_bps,
                median_slippage_bps=row.median_slippage_bps,
                p90_slippage_bps=row.p90_slippage_bps,
                avg_daily_volume_usd=row.avg_daily_volume_usd,
                sample_trades=row.sample_trades,
            )
        )
    return True


def rebuild_all(
    db: Session,
    *,
    tickers: Iterable[str] | None = None,
    window_days: int = 30,
    sides: Iterable[str] = ("long", "short"),
    adv_lookup_fn: Callable[[str, int], float] | None = None,
    mode_override: str | None = None,
) -> BuilderReport:
    """Batch-compute + upsert estimates for ``tickers × sides``.

    When ``tickers`` is None we pull the distinct set of tickers from
    closed Trade rows in the last ``window_days * 2`` days — enough to
    cover the rolling window plus some breathing room.
    """
    mode = _effective_mode(mode_override)
    report = BuilderReport(mode=mode)

    if mode == "off":
        return report

    if tickers is None:
        from ...models.trading import Trade
        lookback = datetime.utcnow() - timedelta(days=max(1, int(window_days) * 2))
        discovered = (
            db.query(Trade.ticker)
            .filter(Trade.status == "closed", Trade.entry_date >= lookback)
            .distinct()
            .all()
        )
        ticker_list = [t[0] for t in discovered if t[0]]
    else:
        ticker_list = [str(t).strip() for t in tickers if t]

    report.tickers_seen = len(ticker_list)

    for tkr in ticker_list:
        for side in sides:
            try:
                est = compute_rolling_estimate(
                    db,
                    ticker=tkr,
                    side=side,
                    window_days=window_days,
                    adv_lookup_fn=adv_lookup_fn,
                )
                if est is None:
                    report.estimates_skipped += 1
                    continue
                if upsert_estimate(db, est, mode_override=mode_override):
                    report.estimates_written += 1
            except Exception as exc:
                logger.warning(
                    "[execution_cost_builder] failed for %s/%s: %s",
                    tkr, side, exc,
                )
                report.errors.append(f"{tkr}/{side}: {exc}")

    if _ops_log_enabled():
        logger.info(
            format_execution_cost_ops_line(
                event="summary",
                mode=mode,
                window_days=window_days,
                estimates_total=report.estimates_written,
            )
        )

    return report


# ── Diagnostics ────────────────────────────────────────────────────────

def estimates_summary(db: Session, *, stale_threshold_hours: int = 48) -> dict[str, Any]:
    """Summary for the ``/brain/execution-cost/diagnostics`` endpoint.

    Shape is frozen (see plan / docs):
      * mode, estimates_total, tickers, by_side, stale_estimates,
        last_refresh_at
    """
    mode = _effective_mode()

    row_count_q = db.execute(
        text("SELECT COUNT(*) FROM trading_execution_cost_estimates")
    ).fetchone()
    estimates_total = int(row_count_q[0]) if row_count_q else 0

    tickers_q = db.execute(
        text("SELECT COUNT(DISTINCT ticker) FROM trading_execution_cost_estimates")
    ).fetchone()
    tickers = int(tickers_q[0]) if tickers_q else 0

    by_side_rows = db.execute(text("""
        SELECT side, COUNT(*) AS c
        FROM trading_execution_cost_estimates
        GROUP BY side
        ORDER BY side
    """)).fetchall()
    by_side = {r[0]: int(r[1]) for r in by_side_rows}

    stale_cutoff = datetime.utcnow() - timedelta(hours=max(1, int(stale_threshold_hours)))
    stale_q = db.execute(text("""
        SELECT COUNT(*) FROM trading_execution_cost_estimates
        WHERE last_updated_at < :cutoff
    """), {"cutoff": stale_cutoff}).fetchone()
    stale_estimates = int(stale_q[0]) if stale_q else 0

    last_refresh_q = db.execute(text("""
        SELECT MAX(last_updated_at) FROM trading_execution_cost_estimates
    """)).fetchone()
    last_refresh_at = last_refresh_q[0].isoformat() if last_refresh_q and last_refresh_q[0] else None

    return {
        "mode": mode,
        "estimates_total": estimates_total,
        "tickers": tickers,
        "by_side": by_side,
        "stale_estimates": stale_estimates,
        "stale_threshold_hours": int(stale_threshold_hours),
        "last_refresh_at": last_refresh_at,
    }


__all__ = [
    "BuilderReport",
    "EstimateRow",
    "compute_rolling_estimate",
    "estimates_summary",
    "mode_is_active",
    "rebuild_all",
    "upsert_estimate",
]
