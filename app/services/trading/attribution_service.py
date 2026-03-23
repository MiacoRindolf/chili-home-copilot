"""Live vs research attribution: closed trades linked to scan patterns vs pattern OOS stats."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session


def live_vs_research_by_pattern(
    db: Session,
    user_id: int | None,
    *,
    days: int = 90,
    limit: int = 50,
) -> dict[str, Any]:
    """Aggregate closed trades with ``scan_pattern_id`` vs ``ScanPattern`` research fields."""
    from ...models.trading import ScanPattern, Trade

    if user_id is None:
        return {"ok": True, "window_days": days, "patterns": []}

    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.scan_pattern_id.isnot(None),
            Trade.exit_date.isnot(None),
            Trade.exit_date >= since,
        )
        .all()
    )
    by_pid: dict[int, list[Trade]] = defaultdict(list)
    for t in trades:
        by_pid[int(t.scan_pattern_id or 0)].append(t)

    rows: list[dict[str, Any]] = []
    for pid, tlist in by_pid.items():
        if pid <= 0:
            continue
        pat = db.query(ScanPattern).filter(ScanPattern.id == pid).first()
        pnls = [float(t.pnl or 0) for t in tlist]
        wins = sum(1 for p in pnls if p > 0)
        n = len(tlist)
        entry_slips = [
            float(t.tca_entry_slippage_bps)
            for t in tlist
            if t.tca_entry_slippage_bps is not None
        ]
        exit_slips = [
            float(t.tca_exit_slippage_bps)
            for t in tlist
            if t.tca_exit_slippage_bps is not None
        ]
        rows.append(
            {
                "scan_pattern_id": pid,
                "pattern_name": pat.name if pat else None,
                "promotion_status": pat.promotion_status if pat else None,
                "research_win_rate_pct": round(float(pat.win_rate), 2) if pat and pat.win_rate is not None else None,
                "research_oos_win_rate_pct": round(float(pat.oos_win_rate), 2)
                if pat and pat.oos_win_rate is not None
                else None,
                "research_oos_avg_return_pct": round(float(pat.oos_avg_return_pct), 3)
                if pat and pat.oos_avg_return_pct is not None
                else None,
                "live_closed_trades": n,
                "live_win_rate_pct": round(wins / n * 100.0, 1) if n else 0.0,
                "live_total_pnl": round(sum(pnls), 2),
                "live_avg_pnl": round(sum(pnls) / n, 2) if n else 0.0,
                "live_avg_entry_slippage_bps": round(sum(entry_slips) / len(entry_slips), 2)
                if entry_slips
                else None,
                "live_avg_exit_slippage_bps": round(sum(exit_slips) / len(exit_slips), 2)
                if exit_slips
                else None,
            }
        )

    rows.sort(key=lambda r: r["live_closed_trades"], reverse=True)
    rows = rows[: max(1, min(200, int(limit)))]

    return {"ok": True, "window_days": days, "patterns": rows}
