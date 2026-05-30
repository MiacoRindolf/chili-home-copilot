"""Live vs research attribution: closed trades linked to scan patterns vs pattern OOS stats."""

from __future__ import annotations

import heapq
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .management_envelopes import MANAGEMENT_ENVELOPES_RELATION


def _scan_patterns_by_id(db: Session, pattern_ids: set[int]) -> dict[int, Any]:
    ids = sorted({int(pid) for pid in pattern_ids if int(pid) > 0})
    if not ids:
        return {}

    from ...models.trading import ScanPattern

    rows = db.query(ScanPattern).filter(ScanPattern.id.in_(ids)).all()
    return {int(row.id): row for row in rows if row.id is not None}


def _top_high_slippage_trades(
    rows: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or not rows:
        return []
    return heapq.nlargest(limit, rows, key=lambda row: row["total_slippage_bps"])


def _top_pattern_deltas(
    rows: list[dict[str, Any]],
    limit: int,
    *,
    reverse: bool,
) -> list[dict[str, Any]]:
    if limit <= 0 or not rows:
        return []
    key = lambda row: row["delta_pct"] or 0
    if reverse:
        return heapq.nlargest(limit, rows, key=key)
    return heapq.nsmallest(limit, rows, key=key)


def _closed_pattern_live_stats(
    db: Session,
    *,
    user_id: int,
    since: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    """Aggregate closed-trade pattern stats in the database for the attribution endpoint."""
    rows = db.execute(
        text(
            f"""
            SELECT
                scan_pattern_id,
                COUNT(*)::bigint AS live_closed_trades,
                SUM(CASE WHEN COALESCE(pnl, 0) > 0 THEN 1 ELSE 0 END)::bigint AS live_wins,
                SUM(COALESCE(pnl, 0))::double precision AS live_total_pnl,
                AVG(COALESCE(pnl, 0))::double precision AS live_avg_pnl,
                AVG(tca_entry_slippage_bps)::double precision AS live_avg_entry_slippage_bps,
                AVG(tca_exit_slippage_bps)::double precision AS live_avg_exit_slippage_bps
              FROM {MANAGEMENT_ENVELOPES_RELATION}
             WHERE user_id = :user_id
               AND status = 'closed'
               AND scan_pattern_id IS NOT NULL
               AND exit_date IS NOT NULL
               AND exit_date >= :since
             GROUP BY scan_pattern_id
             ORDER BY live_closed_trades DESC, scan_pattern_id ASC
             LIMIT :limit
            """
        ),
        {"user_id": user_id, "since": since, "limit": limit},
    ).mappings().all()

    return [dict(row) for row in rows]


def live_vs_research_by_pattern(
    db: Session,
    user_id: int | None,
    *,
    days: int = 90,
    limit: int = 50,
    include_phase5b_compare: bool = False,
) -> dict[str, Any]:
    """Aggregate closed trades with ``scan_pattern_id`` vs ``ScanPattern`` research fields."""
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
    live_stats = _closed_pattern_live_stats(
        db,
        user_id=int(user_id),
        since=since,
        limit=safe_limit,
    )

    rows: list[dict[str, Any]] = []
    patterns_by_id = _scan_patterns_by_id(
        db,
        {int(row["scan_pattern_id"] or 0) for row in live_stats},
    )
    for stat in live_stats:
        pid = int(stat["scan_pattern_id"] or 0)
        if pid <= 0:
            continue
        pat = patterns_by_id.get(pid)
        n = int(stat["live_closed_trades"] or 0)
        wins = int(stat["live_wins"] or 0)
        total_pnl = round(float(stat["live_total_pnl"] or 0.0), 2)
        avg_pnl = round(float(stat["live_avg_pnl"] or 0.0), 2) if n else 0.0
        entry_slip_avg = stat["live_avg_entry_slippage_bps"]
        exit_slip_avg = stat["live_avg_exit_slippage_bps"]
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
                "live_total_pnl": total_pnl,
                "live_avg_pnl": avg_pnl,
                "live_avg_entry_slippage_bps": round(float(entry_slip_avg), 2)
                if entry_slip_avg is not None
                else None,
                "live_avg_exit_slippage_bps": round(float(exit_slip_avg), 2)
                if exit_slip_avg is not None
                else None,
            }
        )

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

    top_high_slip_trades = _top_high_slippage_trades(high_slip_trades, 5)
    top_outperformers = _top_pattern_deltas(outperformers, 5, reverse=True)
    top_underperformers = _top_pattern_deltas(underperformers, 5, reverse=False)

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
            "high_slippage_trades": top_high_slip_trades,
            "outperforming_patterns": top_outperformers,
            "underperforming_patterns": top_underperformers,
            "takeaways": takeaways,
        },
        "feedback_signals": feedback_signals,
    }
