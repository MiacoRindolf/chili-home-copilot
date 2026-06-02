"""Assemble the CHILI Workspace dashboard from live read-models.

Read-only and defensive: every section is wrapped so a query failure degrades
that section to empty rather than 500-ing the dashboard. Reuses the existing
trading summary builder and the reasoning-research rows; no new schema.
"""
from __future__ import annotations

import json as _json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _fmt_money(v: Any) -> Optional[str]:
    try:
        if v is None:
            return None
        x = float(v)
        return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"
    except (TypeError, ValueError):
        return None


def _fmt_pct(v: Any) -> Optional[str]:
    try:
        return None if v is None else f"{round(float(v) * 100)}%"
    except (TypeError, ValueError):
        return None


def _trading(db: Session, user_id: Optional[int]) -> Dict[str, Any]:
    try:
        from .trading_summary import build_trading_summary
        s = build_trading_summary(db, user_id, window_hours=24) if user_id else {}
    except Exception as e:
        logger.warning("[dashboard] trading summary failed: %s", e)
        s = {}
    closes = s.get("closes") or []
    return {
        "net_pnl": s.get("net_pnl"),
        "net_pnl_fmt": _fmt_money(s.get("net_pnl")),
        "win_rate_fmt": _fmt_pct(s.get("win_rate")),
        "closes": closes,
        "closes_fmt": [
            {"ticker": c.get("ticker"), "pattern": c.get("pattern") or "—",
             "pnl_fmt": _fmt_money(c.get("pnl")), "pnl_up": (c.get("pnl") or 0) >= 0,
             "reason": c.get("reason") or ""}
            for c in closes if isinstance(c, dict)
        ],
        "open_positions": [p for p in (s.get("open_positions") or []) if isinstance(p, dict)],
        "top_patterns": [
            {"id": p.get("id"), "pnl_fmt": _fmt_money(p.get("pnl")),
             "pnl_up": (p.get("pnl") or 0) >= 0, "trades": p.get("trades"),
             "payoff": (f"{float(p['payoff']):.2f}:1" if p.get("payoff") is not None else "—")}
            for p in (s.get("top_patterns") or []) if isinstance(p, dict)
        ],
    }


def _research(db: Session, user_id: Optional[int]) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    try:
        from ..models import ReasoningResearch
        rows = (
            db.query(ReasoningResearch)
            .filter(ReasoningResearch.user_id == user_id, ReasoningResearch.stale.is_(False))
            .order_by(ReasoningResearch.relevance_score.desc(),
                      ReasoningResearch.searched_at.desc())
            .limit(4)
            .all()
        )
        out = []
        for r in rows:
            src = ""
            try:
                s = _json.loads(r.sources or "[]")
                if s and isinstance(s, list) and isinstance(s[0], dict):
                    from urllib.parse import urlparse
                    src = (urlparse(s[0].get("url", "")).hostname or "").replace("www.", "")
            except Exception:
                pass
            out.append({"topic": r.topic, "summary": (r.summary or "")[:90], "source": src})
        return out
    except Exception as e:
        logger.warning("[dashboard] research query failed: %s", e)
        return []


def build_dashboard(db: Session, user_id: Optional[int]) -> Dict[str, Any]:
    """Return the dashboard view-model (KPIs, closes, positions, patterns, research)."""
    trading = _trading(db, user_id)
    research = _research(db, user_id)
    kpis = [
        {"key": "net_pnl", "label": "Net P/L · today", "val": trading["net_pnl_fmt"] or "$0.00",
         "cls": "ws-up" if (trading.get("net_pnl") or 0) >= 0 else "ws-down"},
        {"key": "win_rate", "label": "Win rate · 30d", "val": trading["win_rate_fmt"] or "—", "cls": ""},
        {"key": "open", "label": "Open positions", "val": str(len(trading["open_positions"])), "cls": ""},
        {"key": "patterns", "label": "Top patterns", "val": str(len(trading["top_patterns"])), "cls": ""},
    ]
    return {
        "kpis": kpis,
        "trading": trading,
        "research": research,
        "has_any": bool(trading["closes"] or trading["open_positions"] or research),
    }
