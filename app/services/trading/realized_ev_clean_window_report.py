"""Per-asset-class realized-EV clean-window report.

Surfaces the post-floor (>= ``chili_realized_ev_clean_window_since``) clean LIVE
realized EV split by asset class, plus per-promoted-pattern representativeness.
This is the visibility the clean-window floor needs: post-floor coverage is
wildly asymmetric (crypto-heavy; equity is data-starved), so any realized-EV
verdict must be read per asset class, not pooled. Consumed by
``scripts/report_realized_ev_clean_window.py`` and the read-only monitor
endpoint ``GET /api/trading/monitor/realized-ev-clean-window``.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _settings_get(name: str, default: Any) -> Any:
    try:
        from ...config import settings
        return getattr(settings, name, default)
    except Exception:
        return default


def build_clean_window_report(db: Session) -> dict[str, Any]:
    """Return per-asset-class + per-promoted-pattern clean-window realized EV.

    All figures are post-floor, dirty-exit-excluded, LIVE only (paper/shadow
    excluded — same basis the demote pass judges on).
    """
    from ...models.trading import ScanPattern
    from .realized_pnl_sql import (
        clean_live_pattern_ev_exit_filter_sql,
        trade_return_fraction_sql,
    )
    from .realized_ev_demote_pass import _clean_window_live_ev

    since = str(_settings_get("chili_realized_ev_clean_window_since", "2026-05-22"))
    min_trades = int(_settings_get("chili_realized_ev_clean_window_min_trades", 5))
    min_days = int(_settings_get("chili_realized_ev_clean_window_min_days", 5))

    # Per-asset-class aggregate over ALL clean post-floor live pattern trades.
    per_asset_sql = text(
        f"""
        SELECT COALESCE(asset_kind, 'unknown') AS asset_class,
               count(*) AS n,
               avg(frac * 100.0) AS avg_ret_pct,
               sum(CASE WHEN frac > 0 THEN 1 ELSE 0 END) AS wins,
               min(exit_date) AS mind,
               max(exit_date) AS maxd
          FROM (
            SELECT asset_kind, exit_date, {trade_return_fraction_sql()} AS frac
              FROM trading_trades
             WHERE status = 'closed'
               AND scan_pattern_id IS NOT NULL
               AND pnl IS NOT NULL
               AND entry_price > 0
               AND quantity > 0
               AND exit_date IS NOT NULL
               AND exit_date >= :since
               AND {clean_live_pattern_ev_exit_filter_sql()}
          ) s
         GROUP BY 1
         ORDER BY 2 DESC
        """
    )
    per_asset: list[dict[str, Any]] = []
    for r in db.execute(per_asset_sql, {"since": since}).fetchall():
        n = int(r.n or 0)
        wins = int(r.wins or 0)
        span_days = int((r.maxd - r.mind).days) if (r.mind and r.maxd) else 0
        per_asset.append({
            "asset_class": r.asset_class,
            "n": n,
            "win_rate": (wins / n) if n else None,
            "avg_ret_pct": float(r.avg_ret_pct) if r.avg_ret_pct is not None else None,
            "span_days": span_days,
            "representative": n >= min_trades and span_days >= min_days,
        })

    # Per-promoted-pattern detail.
    promoted = (
        db.query(ScanPattern)
        .filter(ScanPattern.lifecycle_stage == "promoted")
        .all()
    )
    cw = _clean_window_live_ev(db, [int(p.id) for p in promoted], since=since)
    patterns: list[dict[str, Any]] = []
    for p in promoted:
        s = cw.get(int(p.id)) or {"n": 0, "avg_ret_pct": None, "win_rate": None, "span_days": 0}
        representative = s["n"] >= min_trades and s["span_days"] >= min_days
        net_negative = (
            (s["avg_ret_pct"] is not None and float(s["avg_ret_pct"]) <= 0.0)
            or (s["win_rate"] is not None and float(s["win_rate"]) <= 0.0)
        )
        patterns.append({
            "pattern_id": int(p.id),
            "name": getattr(p, "name", None),
            "post_floor_n": s["n"],
            "post_floor_avg_ret_pct": s["avg_ret_pct"],
            "post_floor_win_rate": s["win_rate"],
            "post_floor_span_days": s["span_days"],
            "representative": representative,
            "demote_eligible": representative and net_negative,
            "verdict": (
                "demote_eligible" if (representative and net_negative)
                else ("passing" if representative else "data_starved_kept")
            ),
        })

    return {
        "clean_window_since": since,
        "min_trades": min_trades,
        "min_days": min_days,
        "per_asset_class": per_asset,
        "promoted_patterns": patterns,
        "promoted_count": len(promoted),
        "note": (
            "Post-floor, dirty-excluded, LIVE-only realized EV. Coverage is "
            "asymmetric (crypto-heavy; equity data-starved) — read per asset "
            "class, never pooled. Data-starved/unrepresentative patterns are "
            "kept, never demoted on pre-floor churn."
        ),
    }
