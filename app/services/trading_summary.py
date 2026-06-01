"""Assemble a READ-ONLY daily trading summary for the brief report.

Builds the plain dict that `app.services.trading_brief.build_brief` expects, by
querying existing read-models (the `Trade` ORM, `ScanPattern`). Deliberately:

- read-only (no writes, no broker calls, no live-quote fetches — so the report
  endpoint can never hang or mutate state),
- defensive (every section is wrapped; a query failure degrades that section to
  empty rather than raising — a report must not 500),
- ORM-only (no raw SQL / hardcoded table names — `Trade` maps through the
  compatibility view, so this survives the position-identity rename).

Open-position unrealized P/L is intentionally omitted (it needs live quotes);
the brief simply lists open tickers/sides.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import Trade, ScanPattern

logger = logging.getLogger(__name__)


def _closed_trades(db: Session, user_id: int, since: datetime) -> List[Trade]:
    try:
        return (
            db.query(Trade)
            .filter(
                Trade.user_id == user_id,
                Trade.status == "closed",
                Trade.exit_date.isnot(None),
                Trade.exit_date >= since,
            )
            .order_by(Trade.exit_date.desc())
            .all()
        )
    except Exception as e:
        logger.warning("[trading_summary] closed-trades query failed: %s", e)
        return []


def _open_trades(db: Session, user_id: int) -> List[Trade]:
    try:
        return (
            db.query(Trade)
            .filter(Trade.user_id == user_id, Trade.status == "open")
            .order_by(Trade.entry_date.desc())
            .all()
        )
    except Exception as e:
        logger.warning("[trading_summary] open-trades query failed: %s", e)
        return []


def _pattern_names(db: Session, pattern_ids: set) -> Dict[int, str]:
    ids = {pid for pid in pattern_ids if pid}
    if not ids:
        return {}
    try:
        rows = db.query(ScanPattern.id, ScanPattern.name).filter(ScanPattern.id.in_(ids)).all()
        return {pid: name for pid, name in rows}
    except Exception as e:
        logger.warning("[trading_summary] pattern-name lookup failed: %s", e)
        return {}


def build_trading_summary(db: Session, user_id: Optional[int],
                          window_hours: int = 24) -> Dict[str, Any]:
    """Return a summary dict for build_brief; empty dict if no user.

    Sections: date, net_pnl, closes[], open_positions[], win_rate, top_patterns[].
    """
    if not user_id:
        return {}
    try:
        window_hours = max(1, int(window_hours))
    except (TypeError, ValueError):
        window_hours = 24
    since = datetime.utcnow() - timedelta(hours=window_hours)

    closed = _closed_trades(db, user_id, since)
    opens = _open_trades(db, user_id)

    pat_ids = {t.scan_pattern_id for t in closed if t.scan_pattern_id}
    pat_names = _pattern_names(db, pat_ids)

    def _pat_label(pid) -> str:
        if not pid:
            return ""
        return pat_names.get(pid) or str(pid)

    # Closes + net P/L + win rate
    closes: List[Dict[str, Any]] = []
    net_pnl = 0.0
    wins = 0
    n_with_pnl = 0
    for t in closed:
        pnl = float(t.pnl) if t.pnl is not None else None
        if pnl is not None:
            net_pnl += pnl
            n_with_pnl += 1
            if pnl > 0:
                wins += 1
        closes.append({
            "ticker": t.ticker,
            "pnl": pnl,
            "pattern": _pat_label(t.scan_pattern_id),
            "reason": t.exit_reason or "",
        })

    # Open positions (no unrealized — would need live quotes)
    open_positions = [
        {"ticker": t.ticker, "side": t.direction or ""}
        for t in opens
    ]

    # Top patterns by realized P/L (from this window's closed trades)
    by_pat: Dict[Any, Dict[str, float]] = {}
    for t in closed:
        if not t.scan_pattern_id or t.pnl is None:
            continue
        agg = by_pat.setdefault(t.scan_pattern_id, {"pnl": 0.0, "trades": 0})
        agg["pnl"] += float(t.pnl)
        agg["trades"] += 1
    top_patterns = [
        {"id": _pat_label(pid), "pnl": v["pnl"], "trades": int(v["trades"])}
        for pid, v in by_pat.items()
    ]
    top_patterns.sort(key=lambda p: p["pnl"], reverse=True)
    top_patterns = top_patterns[:5]

    summary: Dict[str, Any] = {
        "date": since.strftime("%Y-%m-%d") if window_hours >= 24 else None,
        "net_pnl": net_pnl if n_with_pnl else None,
        "closes": closes,
        "open_positions": open_positions,
        "win_rate": (wins / n_with_pnl) if n_with_pnl else None,
        "top_patterns": top_patterns,
    }
    return summary
