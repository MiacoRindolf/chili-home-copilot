"""Live vs research attribution: closed trades linked to scan patterns vs pattern OOS stats."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from .return_math import paper_trade_return_pct, trade_return_pct


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _trade_tca_cost_pct(trade: Any) -> float | None:
    """Return entry+exit TCA cost in percent points, or None if incomplete."""
    entry_bps = _finite_float(getattr(trade, "tca_entry_slippage_bps", None))
    exit_bps = _finite_float(getattr(trade, "tca_exit_slippage_bps", None))
    if entry_bps is None or exit_bps is None:
        return None
    return (entry_bps + exit_bps) / 100.0


def _paper_directional_outcome(pt: Any) -> float | None:
    """Win/loss source for paper attribution, preferring realized dollars."""
    pnl = _finite_float(getattr(pt, "pnl", None))
    if pnl is not None:
        return pnl
    return paper_trade_return_pct(pt)


def _trade_directional_outcome(trade: Any) -> float | None:
    """Win/loss source for live attribution, preferring realized dollars."""
    pnl = _finite_float(getattr(trade, "pnl", None))
    if pnl is not None:
        return pnl
    return trade_return_pct(trade)


def _scan_patterns_by_id(db: Session, pattern_ids: set[int]) -> dict[int, Any]:
    ids = sorted({int(pid) for pid in pattern_ids if int(pid) > 0})
    if not ids:
        return {}

    from ...models.trading import ScanPattern

    rows = db.query(ScanPattern).filter(ScanPattern.id.in_(ids)).all()
    return {int(row.id): row for row in rows if row.id is not None}


def live_vs_research_by_pattern(
    db: Session,
    user_id: int | None,
    *,
    days: int = 90,
    limit: int = 50,
    include_phase5b_compare: bool = False,
) -> dict[str, Any]:
    """Aggregate closed trades with ``scan_pattern_id`` vs ``ScanPattern`` research fields."""
    from ...models.trading import PaperTrade, Trade

    from .backtest_metrics import backtest_win_rate_db_to_display_pct

    if user_id is None:
        out: dict[str, Any] = {"ok": True, "window_days": days, "patterns": []}
        if include_phase5b_compare:
            out["phase5b_compare"] = {
                "enabled": False,
                "reason": "missing_user_id",
            }
        return out

    since = datetime.utcnow() - timedelta(days=max(1, int(days)))
    safe_days = max(1, int(days))
    safe_limit = max(1, min(200, int(limit)))
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
    paper_trades = (
        db.query(PaperTrade)
        .filter(
            PaperTrade.user_id == user_id,
            PaperTrade.status == "closed",
            PaperTrade.scan_pattern_id.isnot(None),
            PaperTrade.exit_date.isnot(None),
            PaperTrade.exit_date >= since,
            or_(
                PaperTrade.paper_shadow_of_alert_id.isnot(None),
                PaperTrade.signal_json.contains({"auto_trader_v1": True}),
                PaperTrade.signal_json.contains({"paper_shadow": True}),
            ),
        )
        .all()
    )
    paper_by_pid: dict[int, list[PaperTrade]] = defaultdict(list)
    for pt in paper_trades:
        paper_by_pid[int(pt.scan_pattern_id or 0)].append(pt)

    rows: list[dict[str, Any]] = []
    pattern_ids = set(by_pid) | set(paper_by_pid)
    patterns_by_id = _scan_patterns_by_id(db, pattern_ids)
    for pid in sorted(pattern_ids):
        if pid <= 0:
            continue
        tlist = by_pid.get(pid, [])
        ptlist = paper_by_pid.get(pid, [])
        pat = patterns_by_id.get(pid)
        pnls = [float(t.pnl or 0) for t in tlist]
        live_directional_outcomes = [
            outcome
            for outcome in (_trade_directional_outcome(t) for t in tlist)
            if outcome is not None
        ]
        wins = sum(1 for outcome in live_directional_outcomes if outcome > 0)
        n = len(tlist)
        live_returns = [
            ret for ret in (trade_return_pct(t) for t in tlist) if ret is not None
        ]
        live_net_returns: list[float] = []
        tca_costs_pct: list[float] = []
        for t in tlist:
            ret = trade_return_pct(t)
            cost_pct = _trade_tca_cost_pct(t)
            if cost_pct is not None:
                tca_costs_pct.append(cost_pct)
            if ret is not None and cost_pct is not None:
                live_net_returns.append(ret - cost_pct)
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
        paper_pnls = [
            p
            for p in (_finite_float(getattr(pt, "pnl", None)) for pt in ptlist)
            if p is not None
        ]
        paper_directional_outcomes = [
            outcome
            for outcome in (_paper_directional_outcome(pt) for pt in ptlist)
            if outcome is not None
        ]
        paper_wins = sum(1 for outcome in paper_directional_outcomes if outcome > 0)
        paper_returns = [
            ret
            for ret in (paper_trade_return_pct(pt) for pt in ptlist)
            if ret is not None
        ]
        paper_tca_costs_pct: list[float] = []
        paper_net_returns: list[float] = []
        for pt in ptlist:
            ret = paper_trade_return_pct(pt)
            cost_pct = _trade_tca_cost_pct(pt)
            if cost_pct is not None:
                paper_tca_costs_pct.append(cost_pct)
            if ret is not None and cost_pct is not None:
                paper_net_returns.append(ret - cost_pct)
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
                "live_win_sample_n": len(live_directional_outcomes),
                "live_win_rate_pct": (
                    round(wins / len(live_directional_outcomes) * 100.0, 1)
                    if live_directional_outcomes
                    else None
                ),
                "live_total_pnl": round(sum(pnls), 2),
                "live_avg_pnl": round(sum(pnls) / n, 2) if n else 0.0,
                "live_return_sample_n": len(live_returns),
                "live_avg_return_pct": (
                    round(sum(live_returns) / len(live_returns), 3)
                    if live_returns
                    else None
                ),
                "live_avg_tca_cost_pct": (
                    round(sum(tca_costs_pct) / len(tca_costs_pct), 4)
                    if tca_costs_pct
                    else None
                ),
                "live_avg_net_return_pct": (
                    round(sum(live_net_returns) / len(live_net_returns), 3)
                    if live_net_returns
                    else None
                ),
                "live_avg_entry_slippage_bps": round(sum(entry_slips) / len(entry_slips), 2)
                if entry_slips
                else None,
                "live_avg_exit_slippage_bps": round(sum(exit_slips) / len(exit_slips), 2)
                if exit_slips
                else None,
                "paper_closed_trades": len(ptlist),
                "paper_win_sample_n": len(paper_directional_outcomes),
                "paper_win_rate_pct": (
                    round(paper_wins / len(paper_directional_outcomes) * 100.0, 1)
                    if paper_directional_outcomes
                    else None
                ),
                "paper_total_pnl": round(sum(paper_pnls), 2)
                if paper_pnls
                else None,
                "paper_avg_pnl": round(sum(paper_pnls) / len(paper_pnls), 2)
                if paper_pnls
                else None,
                "paper_return_sample_n": len(paper_returns),
                "paper_avg_return_pct": (
                    round(sum(paper_returns) / len(paper_returns), 3)
                    if paper_returns
                    else None
                ),
                "paper_avg_tca_cost_pct": (
                    round(sum(paper_tca_costs_pct) / len(paper_tca_costs_pct), 4)
                    if paper_tca_costs_pct
                    else None
                ),
                "paper_avg_net_return_pct": (
                    round(sum(paper_net_returns) / len(paper_net_returns), 3)
                    if paper_net_returns
                    else None
                ),
            }
        )

    rows.sort(
        key=lambda r: (
            r["live_closed_trades"],
            r["paper_closed_trades"],
        ),
        reverse=True,
    )
    rows = rows[:safe_limit]

    out = {"ok": True, "window_days": days, "patterns": rows}
    if include_phase5b_compare:
        out["phase5b_compare"] = _phase5b_pattern_attribution_compare(
            db,
            user_id=int(user_id),
            days=safe_days,
            limit=safe_limit,
        )
    return out


def _phase5b_pattern_attribution_compare(
    db: Session,
    *,
    user_id: int,
    days: int,
    limit: int,
) -> dict[str, Any]:
    """Compare legacy envelope-pattern attribution with Phase 5B decisions."""
    params = {"user_id": user_id, "days": days, "limit": limit}
    grouped_rows = db.execute(text("""
        WITH closed AS (
            SELECT
                decision_scan_pattern_id,
                envelope_scan_pattern_id,
                COALESCE(envelope_pnl, 0)::double precision AS pnl
              FROM trading_phase5b_decision_envelope_position
             WHERE envelope_user_id = :user_id
               AND envelope_status = 'closed'
               AND envelope_exit_date IS NOT NULL
               AND envelope_exit_date >= (NOW() - (:days * INTERVAL '1 day'))
        ),
        attributed AS (
            SELECT
                'envelope'::text AS attribution_source,
                envelope_scan_pattern_id AS scan_pattern_id,
                pnl
              FROM closed
            UNION ALL
            SELECT
                'decision'::text AS attribution_source,
                decision_scan_pattern_id AS scan_pattern_id,
                pnl
              FROM closed
        )
        SELECT
            attribution_source,
            scan_pattern_id,
            COUNT(*)::bigint AS closed_envelopes,
            ROUND(SUM(pnl)::numeric, 4) AS total_pnl,
            ROUND(AVG(pnl)::numeric, 4) AS avg_pnl
          FROM attributed
         GROUP BY attribution_source, scan_pattern_id
         ORDER BY attribution_source, closed_envelopes DESC, total_pnl DESC
    """), params).mappings().all()

    mismatch_rows = db.execute(text("""
        SELECT
            decision_scan_pattern_id,
            envelope_scan_pattern_id,
            COUNT(*)::bigint AS closed_envelopes,
            ROUND(SUM(COALESCE(envelope_pnl, 0))::numeric, 4) AS total_pnl
          FROM trading_phase5b_decision_envelope_position
         WHERE envelope_user_id = :user_id
           AND envelope_status = 'closed'
           AND envelope_exit_date IS NOT NULL
           AND envelope_exit_date >= (NOW() - (:days * INTERVAL '1 day'))
           AND decision_scan_pattern_id IS DISTINCT FROM envelope_scan_pattern_id
         GROUP BY decision_scan_pattern_id, envelope_scan_pattern_id
         ORDER BY ABS(SUM(COALESCE(envelope_pnl, 0))) DESC, closed_envelopes DESC
         LIMIT :limit
    """), params).mappings().all()

    by_source: dict[str, list[dict[str, Any]]] = {"envelope": [], "decision": []}
    source_totals: dict[str, dict[int | None, float]] = {"envelope": {}, "decision": {}}
    source_counts: dict[str, int] = {"envelope": 0, "decision": 0}
    for row in grouped_rows:
        source = str(row["attribution_source"])
        pid = row["scan_pattern_id"]
        pid_key = int(pid) if pid is not None else None
        closed = int(row["closed_envelopes"] or 0)
        total_pnl = round(float(row["total_pnl"] or 0.0), 4)
        payload = {
            "scan_pattern_id": pid_key,
            "closed_envelopes": closed,
            "total_pnl": total_pnl,
            "avg_pnl": round(float(row["avg_pnl"] or 0.0), 4),
        }
        if source in by_source:
            by_source[source].append(payload)
            source_totals[source][pid_key] = total_pnl
            source_counts[source] += closed

    envelope_keys = set(source_totals["envelope"])
    decision_keys = set(source_totals["decision"])
    diff_keys = envelope_keys | decision_keys
    pnl_delta_abs = sum(
        abs(source_totals["envelope"].get(key, 0.0) - source_totals["decision"].get(key, 0.0))
        for key in diff_keys
    )

    mismatches = [
        {
            "decision_scan_pattern_id": (
                int(row["decision_scan_pattern_id"])
                if row["decision_scan_pattern_id"] is not None
                else None
            ),
            "envelope_scan_pattern_id": (
                int(row["envelope_scan_pattern_id"])
                if row["envelope_scan_pattern_id"] is not None
                else None
            ),
            "closed_envelopes": int(row["closed_envelopes"] or 0),
            "total_pnl": round(float(row["total_pnl"] or 0.0), 4),
        }
        for row in mismatch_rows
    ]

    return {
        "enabled": True,
        "source_view": "trading_phase5b_decision_envelope_position",
        "window_days": days,
        "legacy_attribution": "envelope_scan_pattern_id",
        "phase5b_attribution": "decision_scan_pattern_id",
        "summary": {
            "envelope_pattern_groups": len(envelope_keys),
            "decision_pattern_groups": len(decision_keys),
            "envelope_closed_envelopes": source_counts["envelope"],
            "decision_closed_envelopes": source_counts["decision"],
            "mismatched_pattern_groups": len(mismatches),
            "mismatched_closed_envelopes": sum(m["closed_envelopes"] for m in mismatches),
            "absolute_group_pnl_delta": round(pnl_delta_abs, 4),
            "null_decision_pattern_envelopes": sum(
                m["closed_envelopes"]
                for m in mismatches
                if m["decision_scan_pattern_id"] is None
            ),
        },
        "by_envelope_pattern": by_source["envelope"][:limit],
        "by_decision_pattern": by_source["decision"][:limit],
        "attribution_mismatches": mismatches,
    }


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
    from ...models.trading import Trade
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
    directional_outcomes = [
        outcome
        for outcome in (_trade_directional_outcome(t) for t in closed)
        if outcome is not None
    ]
    wins = sum(1 for outcome in directional_outcomes if outcome > 0)
    n = len(pnls)
    directional_n = len(directional_outcomes)
    total_pnl = round(sum(pnls), 2)
    avg_pnl = round(sum(pnls) / n, 2)
    live_win_rate = round(wins / directional_n * 100, 1) if directional_n else None

    # --- Consecutive losses ---
    max_consec_losses = 0
    cur_streak = 0
    for outcome in (_trade_directional_outcome(t) for t in closed):
        if outcome is None:
            cur_streak = 0
        elif outcome < 0:
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
    patterns_by_id = _scan_patterns_by_id(db, set(by_pid))

    for pid, trades in by_pid.items():
        if pid <= 0:
            continue
        pat = patterns_by_id.get(pid)
        trade_pnls = [float(t.pnl or 0) for t in trades]
        trade_directional_outcomes = [
            outcome
            for outcome in (_trade_directional_outcome(t) for t in trades)
            if outcome is not None
        ]
        t_wins = sum(1 for outcome in trade_directional_outcomes if outcome > 0)
        t_n = len(trade_pnls)
        t_directional_n = len(trade_directional_outcomes)
        live_wr = round(t_wins / t_directional_n * 100, 1) if t_directional_n else None

        research_wr = None
        if pat and pat.oos_win_rate is not None:
            research_wr = round(float(backtest_win_rate_db_to_display_pct(pat.oos_win_rate) or 0), 1)
        elif pat and pat.win_rate is not None:
            research_wr = round(float(backtest_win_rate_db_to_display_pct(pat.win_rate) or 0), 1)

        delta = (
            round(live_wr - research_wr, 1)
            if live_wr is not None and research_wr is not None
            else None
        )

        row = {
            "scan_pattern_id": pid,
            "pattern_name": pat.name if pat else None,
            "live_trades": t_n,
            "live_win_sample_n": t_directional_n,
            "live_win_rate_pct": live_wr,
            "research_win_rate_pct": research_wr,
            "delta_pct": delta,
            "live_total_pnl": round(sum(trade_pnls), 2),
        }

        if delta is not None and t_directional_n >= 3:
            if delta >= 5:
                outperformers.append(row)
                feedback_signals.append({
                    "pattern_id": pid,
                    "pattern_name": pat.name if pat else None,
                    "signal": "upweight",
                    "reason": (
                        f"Live win rate {live_wr}% exceeded research {research_wr}% "
                        f"by {delta}pp over {t_directional_n} directional outcomes"
                    ),
                })
            elif delta <= -10:
                underperformers.append(row)
                feedback_signals.append({
                    "pattern_id": pid,
                    "pattern_name": pat.name if pat else None,
                    "signal": "downweight",
                    "reason": (
                        f"Live win rate {live_wr}% lagged research {research_wr}% "
                        f"by {abs(delta)}pp over {t_directional_n} directional outcomes"
                    ),
                })

    outperformers.sort(key=lambda r: r["delta_pct"] or 0, reverse=True)
    underperformers.sort(key=lambda r: r["delta_pct"] or 0)

    # --- Takeaways ---
    takeaways: list[str] = []
    if live_win_rate is not None and live_win_rate >= 60:
        takeaways.append(
            f"Strong period: {live_win_rate}% win rate across "
            f"{directional_n} directional outcomes."
        )
    elif live_win_rate is not None and live_win_rate < 40:
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
            "win_sample_n": directional_n,
            "wins": wins,
            "losses": directional_n - wins,
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
