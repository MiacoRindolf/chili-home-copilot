#!/usr/bin/env python3
"""Replay recent pattern-imminent BreakoutAlerts through AutoTrader rules + optional LLM gate.

Read-only on alerts: no broker orders, no new trades, no audit inserts.
Prints decision histogram and projected-profit stats for operator review.

Usage (from repo root, conda env per project rules):
  conda run -n chili-env python scripts/autotrader_shadow.py --limit 50
  conda run -n chili-env python scripts/autotrader_shadow.py --limit 20 --skip-llm
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

# Repo root on path
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoTrader v1 shadow replay (no orders, no DB writes)")
    parser.add_argument("--limit", type=int, default=50, help="Max alerts to scan (newest first)")
    parser.add_argument("--user-id", type=int, default=None, help="Filter BreakoutAlert.user_id (optional)")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM revalidation (rules only)")
    args = parser.parse_args()

    from app.db import SessionLocal
    from app.models.trading import BreakoutAlert
    from app.config import settings
    from app.services.trading.auto_trader_rules import (
        RuleGateContext,
        alert_confidence_from_score,
        projected_profit_pct,
        passes_rule_gate,
    )
    from app.services.trading.auto_trader_llm import run_revalidation_llm
    from app.services.trading.auto_trader import _current_price, _pattern_name

    db = SessionLocal()
    try:
        q = (
            db.query(BreakoutAlert)
            .filter(BreakoutAlert.alert_tier == "pattern_imminent")
            .order_by(BreakoutAlert.id.desc())
            .limit(max(1, args.limit))
        )
        if args.user_id is not None:
            q = q.filter(BreakoutAlert.user_id == int(args.user_id))
        rows = list(q.all())
    finally:
        db.close()

    hist: Counter[str] = Counter()
    for alert in rows:
        db = SessionLocal()
        try:
            px = _current_price(alert.ticker)
            if px is None:
                hist["no_quote"] += 1
                continue
            ctx = RuleGateContext(current_price=px, autotrader_open_count=0, realized_loss_today_usd=0.0)
            ok, reason, _ = passes_rule_gate(
                db,
                alert,
                settings=settings,
                ctx=ctx,
                for_new_entry=True,
            )
            if not ok:
                hist[f"rule:{reason}"] += 1
                continue
            if not args.skip_llm and getattr(settings, "chili_autotrader_llm_revalidation_enabled", True):
                viable, _ = run_revalidation_llm(
                    alert,
                    current_price=px,
                    ohlcv_summary=None,
                    pattern_name=_pattern_name(db, alert.scan_pattern_id),
                    trace_id=f"shadow-{alert.id}",
                )
                if not viable:
                    hist["llm:not_viable"] += 1
                    continue
            hist["would_place"] += 1
        finally:
            db.close()

    print("AutoTrader shadow replay")
    print("  alerts_scanned:", len(rows))
    print("  histogram:")
    for k, v in hist.most_common():
        print(f"    {k}: {v}")
    if rows:
        ppp = [projected_profit_pct(a.entry_price, a.target_price) for a in rows]
        ppp_n = [p for p in ppp if p is not None]
        if ppp_n:
            print("  projected_profit_pct min/median/max:", min(ppp_n), sorted(ppp_n)[len(ppp_n) // 2], max(ppp_n))
        confs = [alert_confidence_from_score(a) for a in rows]
        print("  confidence min/max:", min(confs), max(confs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
