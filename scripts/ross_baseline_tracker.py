"""Ross Baseline Progress Tracker — CHILI's staged path vs the Ross Cameron curve.

Operator's north star (2026-06-27): baseline CHILI's progress on the Ross small-account
trajectory and equip it to MATCH then SURPASS Ross. This is the STAGE-GATE DASHBOARD — it
reads the LIVE per-round-trip truth (``momentum_fill_outcomes``, realized $ only; the replay
$ is explicitly EXCLUDED because the fidelity audit proved it overstates losses ~5x) and
reports which staged gate CHILI has cleared, so we never scale size before expectancy is proven.

THE STAGED PATH (each gate measurable; stages 2-4 locked behind Stage 0):
  Stage 0 EXPECTANCY  -> profit-factor > 1 over >= MIN_TRADES closed live round-trips
                          AND >= 3 follow-through winners >= 1.5R, losers held ~<= 0.8R.
  Stage 1 CONSISTENCY -> net-positive >= 3 of 4 weeks, max-drawdown < the daily-loss cap.
  Stage 2 COMPOUND    -> verify equity-relative sizing scales (no new build).
  Stage 3 MATCH       -> live %-curve within ~1x the Ross %-of-equity benchmark, win>=60%, R>=1.5.
  Stage 4 SURPASS     -> beat the benchmark on the 6 mechanizable axes over a rolling quarter.

R-multiple is ESTIMATED from the P&L distribution (mean |loss| = 1R, the standard method when a
per-trade risk_usd column is absent). The Ross overlay is a %-OF-EQUITY MODEL (~10%/day goal,
decaying as the liquidity cap binds) — NOT a $2k/day promise (see the 2026-06-27 baseline study:
the FTC found the median Warrior customer LOST money; "$4k->$2k/day in a month" is a stretch ceiling).

READ-ONLY. Run in-container:  docker exec chili-clean-recovery-scheduler python scripts/ross_baseline_tracker.py
Optional: --days N (window, default all-time) | --equity USD (for the Ross %-overlay) | --family NAME.
"""

from __future__ import annotations

import argparse
import os
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

MIN_TRADES = 30                 # Stage-0 sample-size gate (one documented base)
STAGE0_WINNER_R = 1.5           # a "follow-through winner" is >= this R
STAGE0_MIN_WINNERS = 3          # need this many follow-through winners
STAGE0_MAX_LOSER_R = 0.8        # losers must stay controlled (the asymmetry must realize)
ROSS_DAILY_GOAL_PCT = 0.10      # Ross's documented ~10%-of-equity daily goal (the %-overlay model)


def _engine():
    url = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5432/chili")
    return create_engine(url.replace("postgresql+psycopg2", "postgresql"))


def _fetch_round_trips(conn, *, days: int | None, family: str | None):
    where = ["realized_pnl_usd IS NOT NULL"]
    params: dict = {}
    # momentum equity lane: exclude crypto dust (-USD) so the expectancy read is the equity edge
    where.append("(asset_class IS NULL OR asset_class <> 'crypto')")
    where.append("symbol NOT LIKE '%-USD'")
    if family:
        where.append("execution_family = :fam")
        params["fam"] = family
    if days:
        where.append("created_at >= :lo")
        params["lo"] = datetime.now(timezone.utc) - timedelta(days=days)
    q = text(
        "SELECT symbol, realized_pnl_usd, "
        "(created_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date AS d "
        "FROM momentum_fill_outcomes WHERE " + " AND ".join(where) + " ORDER BY created_at"
    )
    return [(r[0], float(r[1]), r[2]) for r in conn.execute(q, params).fetchall()]


def _stats(rows):
    pnls = [p for _s, p, _d in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    r_unit = (statistics.mean([-x for x in losses]) if losses else 0.0)  # mean |loss| = 1R
    winners_r = sorted([(w / r_unit) for w in wins], reverse=True) if r_unit > 0 else []
    avg_loser_r = (statistics.mean([(-x / r_unit) for x in losses]) if (losses and r_unit > 0) else 0.0)
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / n if n else 0.0),
        "net": sum(pnls), "gross_win": gross_win, "gross_loss": gross_loss,
        "avg_win": (statistics.mean(wins) if wins else 0.0),
        "avg_loss": (statistics.mean(losses) if losses else 0.0),
        "profit_factor": (gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0),
        "expectancy": (sum(pnls) / n if n else 0.0),
        "r_unit": r_unit,
        "winners_ge_target_r": sum(1 for r in winners_r if r >= STAGE0_WINNER_R),
        "best_winner_r": (winners_r[0] if winners_r else 0.0),
        "avg_loser_r": avg_loser_r,
    }


def _stage0_gates(s):
    return [
        (f"sample >= {MIN_TRADES} trades", s["n"] >= MIN_TRADES, f"{s['n']}"),
        ("profit factor > 1.0", s["profit_factor"] > 1.0, f"{s['profit_factor']:.2f}"),
        (f">= {STAGE0_MIN_WINNERS} winners >= {STAGE0_WINNER_R}R", s["winners_ge_target_r"] >= STAGE0_MIN_WINNERS, f"{s['winners_ge_target_r']}"),
        (f"avg loser <= {STAGE0_MAX_LOSER_R}R", (0 < s["avg_loser_r"] <= STAGE0_MAX_LOSER_R), f"{s['avg_loser_r']:.2f}R"),
    ]


def _bar(ok):
    return "GREEN" if ok else "red  "


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="rolling window in days (default all-time)")
    ap.add_argument("--equity", type=float, default=None, help="account equity for the Ross %%-overlay")
    ap.add_argument("--family", type=str, default=None, help="filter execution_family (e.g. robinhood_agentic_mcp)")
    args = ap.parse_args()

    eng = _engine()
    with eng.connect() as conn:
        db = conn.execute(text("SELECT current_database()")).scalar()
        rows = _fetch_round_trips(conn, days=args.days, family=args.family)

    print("=" * 64)
    print(" CHILI -> ROSS  STAGE-GATE TRACKER   (db=%s, live momentum_fill_outcomes)" % db)
    win_lbl = f"last {args.days}d" if args.days else "all-time"
    print(" window: %s%s | round-trips: %d" % (win_lbl, (" | family=%s" % args.family) if args.family else "", len(rows)))
    print("=" * 64)
    if not rows:
        print(" no closed momentum round-trips yet -> Stage 0 not started (catch the first winner).")
        return

    s = _stats(rows)
    print(" EXPECTANCY (the Stage-0 engine):")
    print("   net $%+.0f | win-rate %.0f%% (%dW/%dL) | profit-factor %.2f | exp/trade $%+.1f"
          % (s["net"], 100 * s["win_rate"], s["wins"], s["losses"], s["profit_factor"], s["expectancy"]))
    print("   avg win $%+.1f | avg loss $%+.1f | 1R ~= $%.1f (mean |loss|)"
          % (s["avg_win"], s["avg_loss"], s["r_unit"]))
    print("   ASYMMETRY: best winner %.1fR | winners >= %.1fR: %d | avg loser %.2fR"
          % (s["best_winner_r"], STAGE0_WINNER_R, s["winners_ge_target_r"], s["avg_loser_r"]))
    print("-" * 64)
    print(" STAGE 0 (EXPECTANCY) gate:")
    gates = _stage0_gates(s)
    for name, ok, val in gates:
        print("   [%s] %-28s = %s" % (_bar(ok), name, val))
    cleared = all(ok for _n, ok, _v in gates)
    print("-" * 64)
    if cleared:
        print(" >> STAGE 0 CLEARED -- expectancy proven. Stage 1 (consistency) unlocks;")
        print("    the idle compounding engine may begin to scale a POSITIVE number.")
    else:
        print(" >> STAGE 0 IN PROGRESS -- at the gate, not on the curve.")
        print("    #1 lever: CATCH A CLEAN FOLLOW-THROUGH WINNER (not more setups, not size).")

    if args.equity:
        ross_daily = ROSS_DAILY_GOAL_PCT * args.equity
        days_traded = len({d for _s, _p, d in rows})
        chili_per_day = s["net"] / days_traded if days_traded else 0.0
        print("-" * 64)
        print(" ROSS %%-OVERLAY (model, NOT a promise):")
        print("   Ross ~10%%/day goal on $%.0f = $%.0f/day target" % (args.equity, ross_daily))
        print("   CHILI actual: $%+.0f over %d trading days = $%+.0f/day (%.1f%% of the Ross goal)"
              % (s["net"], days_traded, chili_per_day, (100 * chili_per_day / ross_daily) if ross_daily else 0.0))
        print("   (the goal scales with equity + decays as the liquidity ceiling binds)")
    print("=" * 64)


if __name__ == "__main__":
    main()
