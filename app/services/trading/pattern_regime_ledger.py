"""Build ``trading_pattern_regime_performance_daily`` from realized trades.

Background (2026-04-28): the trading brain has been computing fresh regime
labels in `trading_ticker_regime_snapshots` (and breadth/cross-asset/vol-
dispersion siblings) for weeks, but
`trading_pattern_regime_performance_daily` is empty (rows=0). That table
is the connector that joins regime evidence to per-pattern outcomes —
once populated, the autotrader can size/gate by "this pattern wins 60%
in trend_up regime, loses in chop". The infrastructure exists; only the
ledger writer is missing.

This module:

* Joins closed `trading_trades` with `trading_ticker_regime_snapshots` on
  ``(ticker, as_of_date)`` so each trade gets the regime label that was
  live when the trade ran.
* Aggregates per ``(pattern_id, regime_dimension="ticker_regime",
  regime_label)`` over a rolling window (default 90d).
* Writes one row per group to the ledger with all stat fields populated.

Idempotent: every run gets a unique ``ledger_run_id`` (UUID4) and the
``ix_pattern_regime_perf_lookup`` index keeps reads fast. Old rows can
be reaped by date if needed; we don't delete on each run.

Future extensions: macro_regime / breadth / vol_dispersion / cross_asset
each becomes another ``regime_dimension`` value once their per-trade
join paths are wired. This first cut covers ticker_regime only — that's
the densest signal (per-ticker, per-day).

Tunable::

    chili_pattern_regime_ledger_enabled        = True
    chili_pattern_regime_ledger_window_days    = 90
    chili_pattern_regime_ledger_min_trades     = 3
    chili_pattern_regime_ledger_dry_run        = False
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


REGIME_DIMENSION = "ticker_regime"


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


@dataclass
class _GroupAcc:
    pattern_id: int
    regime_label: str
    pnls: list[float] = field(default_factory=list)
    rets_pct: list[float] = field(default_factory=list)
    holds_days: list[float] = field(default_factory=list)


def _safe_div(a: float, b: float) -> float | None:
    try:
        if b == 0:
            return None
        return a / b
    except Exception:
        return None


def _stats_for_group(grp: _GroupAcc) -> dict[str, Any]:
    pnls = grp.pnls
    rets = grp.rets_pct
    holds = grp.holds_days
    n = len(rets)
    n_wins = sum(1 for r in rets if r > 0)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    mean_pnl = statistics.fmean(rets) if rets else None
    median_pnl = statistics.median(rets) if rets else None
    sum_pnl_abs = sum(pnls) if pnls else 0.0
    mean_win = statistics.fmean(wins) if wins else None
    mean_loss = statistics.fmean(losses) if losses else None
    hit_rate = (n_wins / n) if n > 0 else None
    expectancy = None
    if mean_win is not None and mean_loss is not None and hit_rate is not None:
        expectancy = hit_rate * mean_win + (1.0 - hit_rate) * mean_loss
    profit_factor = None
    pos_sum = sum(r for r in rets if r > 0)
    neg_sum = abs(sum(r for r in rets if r < 0))
    if neg_sum > 0:
        profit_factor = pos_sum / neg_sum
    sharpe_proxy = None
    if n >= 2:
        sd = statistics.pstdev(rets)
        if sd > 0:
            sharpe_proxy = (mean_pnl or 0.0) / sd
    avg_hold = statistics.fmean(holds) if holds else None
    has_confidence = bool(n >= int(_settings_get("chili_pattern_regime_ledger_min_trades", 3)))
    return {
        "n_trades": n,
        "n_wins": n_wins,
        "hit_rate": hit_rate,
        "mean_pnl_pct": mean_pnl,
        "median_pnl_pct": median_pnl,
        "sum_pnl": sum_pnl_abs,
        "expectancy": expectancy,
        "mean_win_pct": mean_win,
        "mean_loss_pct": mean_loss,
        "profit_factor": profit_factor,
        "sharpe_proxy": sharpe_proxy,
        "avg_hold_days": avg_hold,
        "has_confidence": has_confidence,
    }


def build_ledger(
    sess: Session,
    *,
    as_of: date | None = None,
    window_days: int | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Build one daily ledger snapshot. Returns a small report dict."""
    if not bool(_settings_get("chili_pattern_regime_ledger_enabled", True)):
        return {"skipped": True, "reason": "disabled"}
    if window_days is None:
        window_days = int(_settings_get("chili_pattern_regime_ledger_window_days", 90))
    if dry_run is None:
        dry_run = bool(_settings_get("chili_pattern_regime_ledger_dry_run", False))

    as_of = as_of or date.today()
    run_id = uuid.uuid4().hex[:32]
    mode = str(_settings_get("brain_breadth_relstr_mode", "shadow") or "shadow").lower()

    # Pull (trade, regime_label) for each closed trade in the window.
    # Use a LATERAL join so each trade pulls its OWN regime row by
    # (ticker, as_of_date <= trade exit date) — newest such row per trade.
    rows = sess.execute(text("""
        SELECT t.scan_pattern_id AS pid,
               COALESCE(r.ticker_regime_label, 'unknown') AS regime_label,
               t.pnl AS pnl,
               CASE
                 WHEN t.entry_price IS NOT NULL AND t.entry_price > 0
                      AND t.exit_price IS NOT NULL
                 THEN ((t.exit_price - t.entry_price) / t.entry_price) * 100.0
                 ELSE 0.0
               END AS ret_pct,
               EXTRACT(EPOCH FROM (t.exit_date - t.entry_date))/86400.0 AS hold_days
        FROM trading_trades t
        LEFT JOIN LATERAL (
            SELECT ticker_regime_label
            FROM trading_ticker_regime_snapshots r
            WHERE r.ticker = t.ticker
              AND r.as_of_date <= COALESCE(t.exit_date::date, CURRENT_DATE)
            ORDER BY r.as_of_date DESC
            LIMIT 1
        ) r ON TRUE
        WHERE t.status = 'closed'
          AND t.scan_pattern_id IS NOT NULL
          AND t.exit_date IS NOT NULL
          AND t.exit_date > NOW() - make_interval(days => :wd)
    """), {"wd": int(window_days)}).fetchall()

    groups: dict[tuple[int, str], _GroupAcc] = {}
    for r in rows:
        key = (int(r.pid), str(r.regime_label))
        g = groups.get(key)
        if g is None:
            g = _GroupAcc(pattern_id=int(r.pid), regime_label=str(r.regime_label))
            groups[key] = g
        try:
            ret = float(r.ret_pct) if r.ret_pct is not None else 0.0
        except Exception:
            ret = 0.0
        if math.isfinite(ret):
            g.rets_pct.append(ret)
        try:
            pnl = float(r.pnl) if r.pnl is not None else 0.0
        except Exception:
            pnl = 0.0
        if math.isfinite(pnl):
            g.pnls.append(pnl)
        if r.hold_days is not None:
            try:
                hd = float(r.hold_days)
                if math.isfinite(hd) and hd >= 0:
                    g.holds_days.append(hd)
            except Exception:
                pass

    written = 0
    for (pid, regime_label), grp in groups.items():
        s = _stats_for_group(grp)
        payload = {
            "asset_classes": [],
            "trade_ids_count": s["n_trades"],
            "regime_label_source": REGIME_DIMENSION,
        }
        if dry_run:
            written += 1
            continue
        sess.execute(text("""
            INSERT INTO trading_pattern_regime_performance_daily (
                ledger_run_id, as_of_date, window_days, pattern_id,
                regime_dimension, regime_label,
                n_trades, n_wins, hit_rate,
                mean_pnl_pct, median_pnl_pct, sum_pnl, expectancy,
                mean_win_pct, mean_loss_pct, profit_factor, sharpe_proxy,
                avg_hold_days, has_confidence,
                payload_json, mode, computed_at
            )
            VALUES (
                :run_id, :as_of, :wd, :pid,
                :dim, :label,
                :n, :nw, :hr,
                :mp, :medp, :sp, :exp,
                :mw, :ml, :pf, :sh,
                :ah, :hc,
                CAST(:pj AS jsonb), :mode, CURRENT_TIMESTAMP
            )
        """), {
            "run_id": run_id,
            "as_of": as_of,
            "wd": int(window_days),
            "pid": pid,
            "dim": REGIME_DIMENSION,
            "label": regime_label,
            "n": s["n_trades"],
            "nw": s["n_wins"],
            "hr": s["hit_rate"],
            "mp": s["mean_pnl_pct"],
            "medp": s["median_pnl_pct"],
            "sp": s["sum_pnl"],
            "exp": s["expectancy"],
            "mw": s["mean_win_pct"],
            "ml": s["mean_loss_pct"],
            "pf": s["profit_factor"],
            "sh": s["sharpe_proxy"],
            "ah": s["avg_hold_days"],
            "hc": s["has_confidence"],
            "pj": json.dumps(payload),
            "mode": mode,
        })
        written += 1
    if not dry_run:
        sess.commit()

    return {
        "run_id": run_id,
        "as_of": str(as_of),
        "window_days": window_days,
        "groups": len(groups),
        "rows_written": written,
        "trades_used": len(rows),
        "dry_run": dry_run,
    }
