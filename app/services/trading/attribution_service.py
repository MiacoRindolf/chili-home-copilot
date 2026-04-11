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

    from .backtest_metrics import backtest_win_rate_db_to_display_pct

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
                "research_win_rate_pct": (
                    round(float(backtest_win_rate_db_to_display_pct(pat.win_rate)), 2)
                    if pat and pat.win_rate is not None
                    else None
                ),
                "research_oos_win_rate_pct": (
                    round(float(backtest_win_rate_db_to_display_pct(pat.oos_win_rate)), 2)
                    if pat and pat.oos_win_rate is not None
                    else None
                ),
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


# ── Post-trade review loop ──────────────────────────────────────────────────

def post_trade_review(
    db: Session,
    user_id: int | None,
    *,
    days: int = 30,
) -> dict[str, Any]:
    """Produce a structured "what worked, what failed, and why" review.

    Aggregates closed trades over the last *days* and returns:
    - Top-performing patterns (live win-rate vs research expectation)
    - Underperforming patterns (where live results lagged research)
    - Slippage outliers (high TCA entry/exit cost)
    - Consecutive-loss streaks (execution timing issues)
    - Key takeaways for the learning loop
    - Pattern feedback signals (which patterns should be up/down-weighted)

    The returned dict is additive and does not mutate DB state.
    """
    from datetime import datetime, timedelta
    from ...models.trading import ScanPattern, Trade
    from .backtest_metrics import backtest_win_rate_db_to_display_pct

    if user_id is None:
        return {"ok": True, "window_days": days, "review": {}, "feedback_signals": []}

    since = datetime.utcnow() - timedelta(days=max(1, int(days)))

    closed = (
        db.query(Trade)
        .filter(
            Trade.user_id == user_id,
            Trade.status == "closed",
            Trade.exit_date >= since,
        )
        .order_by(Trade.exit_date.asc())
        .all()
    )

    if not closed:
        return {
            "ok": True,
            "window_days": days,
            "review": {"total_trades": 0},
            "feedback_signals": [],
        }

    pnls = [float(t.pnl or 0) for t in closed]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    total_pnl = round(sum(pnls), 2)
    avg_pnl = round(sum(pnls) / n, 2)
    live_win_rate = round(wins / n * 100, 1)

    # --- Consecutive losses ---
    max_consec_losses = 0
    cur_streak = 0
    for p in pnls:
        if p < 0:
            cur_streak += 1
            max_consec_losses = max(max_consec_losses, cur_streak)
        else:
            cur_streak = 0

    # --- Slippage outliers ---
    high_slip_trades = []
    for t in closed:
        entry_slip = float(t.tca_entry_slippage_bps or 0)
        exit_slip = float(t.tca_exit_slippage_bps or 0)
        total_slip = entry_slip + exit_slip
        if total_slip > 50:  # >50 bps total is notable
            high_slip_trades.append({
                "ticker": t.ticker,
                "entry_slippage_bps": round(entry_slip, 1),
                "exit_slippage_bps": round(exit_slip, 1),
                "total_slippage_bps": round(total_slip, 1),
                "pnl": float(t.pnl or 0),
            })
    high_slip_trades.sort(key=lambda x: x["total_slippage_bps"], reverse=True)

    # --- Pattern performance ---
    from collections import defaultdict
    by_pid: dict[int, list[Trade]] = defaultdict(list)
    for t in closed:
        if t.scan_pattern_id:
            by_pid[int(t.scan_pattern_id)].append(t)

    outperformers: list[dict[str, Any]] = []
    underperformers: list[dict[str, Any]] = []
    feedback_signals: list[dict[str, Any]] = []

    for pid, trades in by_pid.items():
        if pid <= 0:
            continue
        pat = db.query(ScanPattern).filter(ScanPattern.id == pid).first()
        trade_pnls = [float(t.pnl or 0) for t in trades]
        t_wins = sum(1 for p in trade_pnls if p > 0)
        t_n = len(trade_pnls)
        live_wr = round(t_wins / t_n * 100, 1) if t_n else 0

        research_wr = None
        if pat and pat.oos_win_rate is not None:
            research_wr = round(float(backtest_win_rate_db_to_display_pct(pat.oos_win_rate) or 0), 1)
        elif pat and pat.win_rate is not None:
            research_wr = round(float(backtest_win_rate_db_to_display_pct(pat.win_rate) or 0), 1)

        delta = round(live_wr - research_wr, 1) if research_wr is not None else None

        row = {
            "scan_pattern_id": pid,
            "pattern_name": pat.name if pat else None,
            "live_trades": t_n,
            "live_win_rate_pct": live_wr,
            "research_win_rate_pct": research_wr,
            "delta_pct": delta,
            "live_total_pnl": round(sum(trade_pnls), 2),
        }

        if delta is not None and t_n >= 3:
            if delta >= 5:
                outperformers.append(row)
                feedback_signals.append({
                    "pattern_id": pid,
                    "pattern_name": pat.name if pat else None,
                    "signal": "upweight",
                    "reason": f"Live win rate {live_wr}% exceeded research {research_wr}% by {delta}pp over {t_n} trades",
                })
            elif delta <= -10:
                underperformers.append(row)
                feedback_signals.append({
                    "pattern_id": pid,
                    "pattern_name": pat.name if pat else None,
                    "signal": "downweight",
                    "reason": f"Live win rate {live_wr}% lagged research {research_wr}% by {abs(delta)}pp over {t_n} trades",
                })

    outperformers.sort(key=lambda r: r["delta_pct"] or 0, reverse=True)
    underperformers.sort(key=lambda r: r["delta_pct"] or 0)

    # --- Takeaways ---
    takeaways: list[str] = []
    if live_win_rate >= 60:
        takeaways.append(f"Strong period: {live_win_rate}% win rate across {n} trades.")
    elif live_win_rate < 40:
        takeaways.append(f"Challenging period: {live_win_rate}% win rate — review entry criteria.")
    if max_consec_losses >= 4:
        takeaways.append(
            f"Max consecutive loss streak was {max_consec_losses} — consider pausing after {max_consec_losses - 1} losses."
        )
    if high_slip_trades:
        avg_slip = round(
            sum(t["total_slippage_bps"] for t in high_slip_trades) / len(high_slip_trades), 1
        )
        takeaways.append(
            f"{len(high_slip_trades)} trades had high slippage (avg {avg_slip} bps) — review order type/timing."
        )
    if outperformers:
        takeaways.append(
            f"{len(outperformers)} pattern(s) beat research expectations — consider increasing allocation."
        )
    if underperformers:
        takeaways.append(
            f"{len(underperformers)} pattern(s) underperformed research — review for market-regime mismatch."
        )

    return {
        "ok": True,
        "window_days": days,
        "review": {
            "total_trades": n,
            "wins": wins,
            "losses": n - wins,
            "live_win_rate_pct": live_win_rate,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "max_consecutive_losses": max_consec_losses,
            "high_slippage_trades": high_slip_trades[:5],
            "outperforming_patterns": outperformers[:5],
            "underperforming_patterns": underperformers[:5],
            "takeaways": takeaways,
        },
        "feedback_signals": feedback_signals,
    }
