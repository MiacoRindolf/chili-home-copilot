"""Walk-forward simulation for the f-phase3-stop-bleed D1 monthly DD breaker.

Replays 2026-03-10 → 2026-05-16 day-by-day against the live CHILI DB
(or the chili_staging copy), computing on each as-of date:

  * the empirical Gaussian lower-bound threshold from the rolling 180d
    of CHILI-attributed history (scan_pattern_id NOT NULL AND != -1)
    as of that date
  * the realized 30-day PnL as of that date
  * whether the breaker would have tripped (30d PnL <= threshold)

Reports the first-trip date for K ∈ {1.5, 2.0, 2.5, 3.0} (sigma).

Per the brief, the reasonable target is "tripped on or around
2026-04-22" -- the cumulative-PnL trough date from the 2026-05-15
audit. If K=2.0 trips much earlier or never, the methodology needs
revisiting before the operator flips chili_pattern_dd_breaker_enabled
(renamed from chili_monthly_dd_breaker_enabled by
f-portfolio-vs-pattern-breaker-separation, 2026-05-16; legacy env var
remains honored via AliasChoices for one release).

Usage:
    set DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
    python scripts/walkforward_monthly_dd_breaker.py

Optional --user-id <int> to scope (default: scan all users -- intended
for single-user prod where uid is implicit). The script is read-only.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import create_engine, text


START = date(2026, 3, 10)
END = date(2026, 5, 16)
SIGMAS = [1.5, 2.0, 2.5, 3.0]
HISTORY_WINDOW_DAYS = 180
LOOKBACK_30D = 30
HISTORY_MIN_DAYS = 30


def _load_daily_pnls(
    db_url: str,
    user_id: Optional[int],
    asof_max: date,
) -> dict[date, float]:
    """Pull per-day SUM(pnl) on CHILI-attributed closed trades up through
    ``asof_max``. Single query; in-memory replay does the rest."""
    sql = """
        SELECT DATE_TRUNC('day',
                   COALESCE(exit_date, last_fill_at, filled_at))::date AS d,
               COALESCE(SUM(pnl), 0)::float AS daily_pnl
          FROM trading_trades
         WHERE status = 'closed'
           AND pnl IS NOT NULL
           AND scan_pattern_id IS NOT NULL
           AND scan_pattern_id != -1
           AND COALESCE(exit_date, last_fill_at, filled_at) <= :asof_max
    """
    params: dict[str, object] = {"asof_max": asof_max}
    if user_id is not None:
        sql += " AND user_id = :uid"
        params["uid"] = user_id
    sql += " GROUP BY 1 ORDER BY 1"

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return {r.d: float(r.daily_pnl or 0.0) for r in rows}


def _threshold(daily_window: list[float], k_sigma: float) -> Optional[float]:
    """Same arithmetic as portfolio_risk._monthly_dd_threshold."""
    n = len(daily_window)
    if n < HISTORY_MIN_DAYS:
        return None
    mean_d = sum(daily_window) / n
    var_d = sum((p - mean_d) ** 2 for p in daily_window) / max(n - 1, 1)
    std_d = var_d ** 0.5
    return (30.0 * mean_d) - k_sigma * ((30.0 ** 0.5) * std_d)


def simulate(by_day: dict[date, float], k_sigma: float):
    """Return list of (asof, monthly_pnl, threshold, n_history, tripped)."""
    rows = []
    asof = START
    while asof <= END:
        win_lo = asof - timedelta(days=HISTORY_WINDOW_DAYS)
        daily_window = [v for d, v in by_day.items() if win_lo <= d < asof]
        threshold = _threshold(daily_window, k_sigma)
        win30_lo = asof - timedelta(days=LOOKBACK_30D)
        monthly = sum(v for d, v in by_day.items() if win30_lo <= d < asof)
        tripped = (
            threshold is not None and float(monthly) <= float(threshold)
        )
        rows.append((asof, monthly, threshold, len(daily_window), tripped))
        asof += timedelta(days=1)
    return rows


def first_trip(rows) -> Optional[tuple]:
    for r in rows:
        if r[4]:
            return r
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres URL. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Scope to a single user_id (default: all users).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the full day-by-day table for each sigma.",
    )
    args = parser.parse_args()

    if not args.db_url:
        print("ERROR: no DATABASE_URL set (use --db-url or env).", file=sys.stderr)
        return 2

    print(f"# walkforward_monthly_dd_breaker — {START} → {END}")
    print(f"# db_url={args.db_url!r}  user_id={args.user_id!r}")
    print(f"# K-sigma sweep: {SIGMAS}")
    print("")

    by_day = _load_daily_pnls(args.db_url, args.user_id, END)
    print(f"# Loaded {len(by_day)} distinct CHILI-attributed close-days "
          f"through {END}.")
    print("")

    summary_rows = []
    for k in SIGMAS:
        rows = simulate(by_day, k)
        ft = first_trip(rows)
        if ft is None:
            summary_rows.append((k, None, None, None, None))
            print(f"K={k:.1f}σ  first_trip=NEVER over {START}..{END}")
        else:
            asof, monthly, threshold, n_obs, _ = ft
            summary_rows.append((k, asof, monthly, threshold, n_obs))
            print(
                f"K={k:.1f}σ  first_trip={asof}  "
                f"monthly_pnl=${monthly:,.2f}  "
                f"threshold=${threshold:,.2f}  "
                f"n_history={n_obs}d"
            )

        if args.verbose:
            print("")
            print(f"  date         monthly       threshold     n_hist  tripped")
            for asof, monthly, threshold, n_obs, tripped in rows:
                t = "" if threshold is None else f"${threshold:>11,.2f}"
                m = f"${monthly:>10,.2f}"
                print(f"  {asof}  {m}  {t:>13}  {n_obs:>6}  {'YES' if tripped else ''}")
            print("")

    print("")
    print("# Summary (paste into CC_REPORT):")
    print("# K-sigma | first_trip | monthly_pnl_at_trip | threshold_at_trip | n_history")
    for k, asof, monthly, threshold, n_obs in summary_rows:
        if asof is None:
            print(f"# {k:.1f} | NEVER | - | - | -")
        else:
            print(
                f"# {k:.1f} | {asof} | "
                f"${monthly:,.2f} | "
                f"${threshold:,.2f} | "
                f"{n_obs}d"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
