"""Read-only Massive.com equity ROI monitor.

Answers one operator question on an ongoing basis: **is the Massive.com data
subscription paying for itself through the equity (Robinhood) trade flow it
feeds?** Crypto (Coinbase / RH-crypto) is priced off Coinbase OHLCV + CoinGecko
and does not consume Massive, so equity realized PnL is the honest numerator for
Massive ROI; the crypto book is reported separately as a PnL-leak monitor.

Design notes (per the project's standing "no magic numbers" rule):
  * The only dollar input is ``--massive-monthly-usd`` — the *actual* invoiced
    subscription cost ($249/mo by default). It is a real-world cost input, not a
    tuning threshold, and it is prorated to the analysis window by calendar days.
  * Every "flag" in this report is *relative* (a pattern vs its own longer-window
    baseline, or vs the eligible-cohort), never a hardcoded cliff. The report
    ranks and annotates; it does not gate. It changes no live trading behavior.

Sections:
  1. Massive equity ROI scorecard      — equity PnL vs prorated Massive cost
  2. Equity conversion funnel          — supply -> eligible -> placed -> closed
  3. Equity realized PnL by pattern    — where the equity edge actually is
  4. Crypto PnL-leak monitor           — degrading / non-eligible-trading-live
  5. Shadow graduation candidates      — ranked by REALIZED evidence first

Usage:
  conda run -n chili-env python scripts/analyze_massive_equity_value.py \
      --window-days 7 --baseline-days 30
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CHILI_APP_NAME", "chili-massive-equity-value")

from app.db import SessionLocal  # noqa: E402

# Crypto tickers are quoted in USD/USDC on the venues; equity is everything else.
CRYPTO_PREDICATE = "(ticker LIKE '%-USD' OR ticker LIKE '%-USDC')"
EQUITY_PREDICATE = "(ticker NOT LIKE '%-USD' AND ticker NOT LIKE '%-USDC')"
DAYS_PER_MONTH = 365.25 / 12.0  # calendar proration, not a tunable


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    with SessionLocal() as db:
        return [dict(r._mapping) for r in db.execute(text(sql), params or {}).fetchall()]


def _scalar(sql: str, params: dict | None = None):
    with SessionLocal() as db:
        return db.execute(text(sql), params or {}).scalar()


def _print_table(title: str, rows: Iterable[dict]) -> None:
    rows = list(rows)
    print(f"\n## {title}")
    if not rows:
        print("(no rows)")
        return
    keys = list(rows[0].keys())
    widths = {k: max(len(k), *(len(str(r.get(k, ""))) for r in rows)) for k in keys}
    print(" | ".join(k.ljust(widths[k]) for k in keys))
    print("-+-".join("-" * widths[k] for k in keys))
    for row in rows:
        print(" | ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys))


def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out


# ── 1. Massive equity ROI scorecard ──────────────────────────────────────────
def scorecard(window_days: int, baseline_days: int, massive_monthly_usd: float) -> None:
    print("\n# Massive.com equity ROI scorecard")
    daily_cost = massive_monthly_usd / DAYS_PER_MONTH
    for days in sorted({window_days, baseline_days}):
        row = _rows(
            f"""
            SELECT count(*) n,
                   COALESCE(sum(pnl), 0) gross_pnl,
                   COALESCE(avg(pnl), 0) avg_pnl,
                   COALESCE(avg((pnl > 0)::int), 0) win_rate
            FROM trading_management_envelopes
            WHERE status IN ('closed', 'expired') AND pnl IS NOT NULL
              AND exit_date > now() - make_interval(days => :days)
              AND {EQUITY_PREDICATE}
            """,
            {"days": days},
        )[0]
        prorated_cost = daily_cost * days
        gross = _f(row["gross_pnl"])
        net = gross - prorated_cost
        verdict = "PAYS FOR ITSELF" if net >= 0 else "SUBSIDIZED BY OPERATOR"
        print(
            f"\n  [{days}d] equity closes={row['n']}  "
            f"win_rate={_f(row['win_rate']) * 100:.1f}%  avg=${_f(row['avg_pnl']):.2f}"
        )
        print(f"        gross equity PnL ............ ${gross:>9.2f}")
        print(
            f"        Massive cost ({days}d @ ${daily_cost:.2f}/d) ... "
            f"-${prorated_cost:>8.2f}"
        )
        print(f"        net Massive-attributable ROI  ${net:>9.2f}  -> {verdict}")
    print(
        "\n  NOTE: equity-only is a conservative LOWER BOUND on Massive's value — "
        "the\n  same feed also powers regime detection and the scanner that seeds "
        "crypto.\n  This scorecard isolates the directly-attributable equity book."
    )


# ── 2. Equity conversion funnel ───────────────────────────────────────────────
def equity_funnel(window_days: int) -> None:
    supply = _rows(
        """
        SELECT count(*) alerts,
               count(*) FILTER (WHERE sp.lifecycle_stage IN ('promoted','pilot_promoted')
                                  AND COALESCE(sp.recert_required, false) = false) eligible_alerts
        FROM trading_breakout_alerts ba
        LEFT JOIN scan_patterns sp ON sp.id = ba.scan_pattern_id
        WHERE ba.alerted_at > now() - make_interval(days => :days)
          AND UPPER(COALESCE(ba.ticker,'')) NOT LIKE '%-USD'
          AND UPPER(COALESCE(ba.ticker,'')) NOT LIKE '%-USDC'
        """,
        {"days": window_days},
    )[0]
    decided = _rows(
        f"""
        SELECT r.decision, count(*) n
        FROM trading_autotrader_runs r
        WHERE r.created_at > now() - make_interval(days => :days) AND {EQUITY_PREDICATE}
        GROUP BY 1 ORDER BY 2 DESC
        """,
        {"days": window_days},
    )
    print(f"\n# Equity conversion funnel (last {window_days}d)")
    print(f"  equity alerts (supply) ........ {supply['alerts']}")
    print(f"  from trade-eligible patterns .. {supply['eligible_alerts']}")
    placed = sum(d["n"] for d in decided if d["decision"] in ("placed", "scaled_in"))
    print(f"  autotrader placed/scaled ...... {placed}")
    _print_table(
        f"Equity autotrader decisions (last {window_days}d)",
        decided,
    )
    _print_table(
        f"Top equity rejection reasons on TRADE-ELIGIBLE patterns (last {window_days}d)",
        _rows(
            f"""
            SELECT r.decision, left(r.reason, 52) reason, count(*) n,
                   count(distinct r.ticker) tickers
            FROM trading_autotrader_runs r
            JOIN scan_patterns sp ON sp.id = r.scan_pattern_id
                 AND sp.lifecycle_stage IN ('promoted','pilot_promoted')
            WHERE r.created_at > now() - make_interval(days => :days)
              AND {EQUITY_PREDICATE} AND r.decision IN ('blocked','skipped')
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12
            """,
            {"days": window_days},
        ),
    )


# ── 3. Equity realized PnL by pattern ─────────────────────────────────────────
def equity_pnl_by_pattern(baseline_days: int) -> None:
    _print_table(
        f"Equity realized PnL by pattern (last {baseline_days}d)",
        _rows(
            f"""
            SELECT e.scan_pattern_id, left(sp.name, 34) name, sp.lifecycle_stage stage,
                   count(*) n, round(sum(e.pnl)::numeric, 2) total_pnl,
                   round(avg(e.pnl)::numeric, 2) avg_pnl,
                   round((100.0 * avg((e.pnl > 0)::int))::numeric, 0) win_rate
            FROM trading_management_envelopes e
            LEFT JOIN scan_patterns sp ON sp.id = e.scan_pattern_id
            WHERE e.status IN ('closed','expired') AND e.pnl IS NOT NULL
              AND e.exit_date > now() - make_interval(days => :days)
              AND {EQUITY_PREDICATE}
            GROUP BY 1,2,3 ORDER BY total_pnl DESC
            """,
            {"days": baseline_days},
        ),
    )


# ── 4. Crypto PnL-leak monitor ────────────────────────────────────────────────
def crypto_leak_monitor(window_days: int, baseline_days: int) -> None:
    # Per-pattern window PnL with the pattern's own longer-baseline win-rate, so
    # "degrading" is measured against the pattern itself, never a fixed cliff.
    rows = _rows(
        f"""
        WITH win AS (
            SELECT scan_pattern_id, count(*) n, sum(pnl) total_pnl,
                   avg((pnl > 0)::int) wr
            FROM trading_management_envelopes
            WHERE status IN ('closed','expired') AND pnl IS NOT NULL
              AND exit_date > now() - make_interval(days => :win) AND {CRYPTO_PREDICATE}
            GROUP BY 1
        ), base AS (
            SELECT scan_pattern_id, avg((pnl > 0)::int) wr
            FROM trading_management_envelopes
            WHERE status IN ('closed','expired') AND pnl IS NOT NULL
              AND exit_date > now() - make_interval(days => :base) AND {CRYPTO_PREDICATE}
            GROUP BY 1
        )
        SELECT win.scan_pattern_id, left(sp.name, 30) name, sp.lifecycle_stage stage,
               win.n, round(win.total_pnl::numeric, 2) win_pnl,
               round((100.0 * win.wr)::numeric, 0) win_wr,
               round((100.0 * base.wr)::numeric, 0) base_wr
        FROM win
        LEFT JOIN base ON base.scan_pattern_id = win.scan_pattern_id
        LEFT JOIN scan_patterns sp ON sp.id = win.scan_pattern_id
        WHERE win.total_pnl < 0
        ORDER BY win.total_pnl ASC LIMIT 12
        """,
        {"win": window_days, "base": baseline_days},
    )
    for r in rows:
        flags = []
        if r["base_wr"] is not None and r["win_wr"] is not None and r["win_wr"] < r["base_wr"]:
            flags.append("DEGRADING_VS_BASELINE")
        if r["stage"] not in ("promoted", "pilot_promoted", None):
            # A non-trade-eligible lifecycle stage placing live crypto = leak.
            flags.append(f"NON_ELIGIBLE_TRADING_LIVE({r['stage']})")
        r["flags"] = ",".join(flags) or "-"
    _print_table(
        f"Crypto PnL-leak monitor — net-negative patterns (last {window_days}d, "
        f"win-rate vs own {baseline_days}d baseline)",
        rows,
    )


# ── 5. Shadow graduation candidates (realized-evidence-first) ─────────────────
def graduation_candidates(baseline_days: int) -> None:
    rows = _rows(
        f"""
        WITH live AS (
            SELECT scan_pattern_id, count(*) n, sum(pnl) pnl, avg((pnl>0)::int) wr
            FROM trading_management_envelopes
            WHERE status IN ('closed','expired') AND pnl IS NOT NULL
              AND exit_date > now() - make_interval(days => :days)
            GROUP BY 1
        ), paper AS (
            SELECT scan_pattern_id, count(*) n, sum(pnl) pnl, avg((pnl>0)::int) wr
            FROM trading_paper_trades
            WHERE status IN ('closed','expired') AND pnl IS NOT NULL
              AND exit_date > now() - make_interval(days => :days)
            GROUP BY 1
        )
        SELECT sp.id, left(sp.name, 30) name,
               sp.promotion_gate_passed gate,
               round(sp.cpcv_median_sharpe::numeric, 2) cpcv,
               round(sp.payoff_ratio::numeric, 2) payoff,
               sp.payoff_ratio_n payoff_n,
               COALESCE(live.n, 0) live_n,
               round(COALESCE(live.pnl, 0)::numeric, 2) live_pnl,
               round((100.0 * live.wr)::numeric, 0) live_wr,
               COALESCE(paper.n, 0) paper_n,
               round(COALESCE(paper.pnl, 0)::numeric, 2) paper_pnl,
               round((100.0 * paper.wr)::numeric, 0) paper_wr
        FROM scan_patterns sp
        LEFT JOIN live ON live.scan_pattern_id = sp.id
        LEFT JOIN paper ON paper.scan_pattern_id = sp.id
        WHERE sp.lifecycle_stage = 'shadow_promoted'
        ORDER BY COALESCE(paper.pnl, 0) + COALESCE(live.pnl, 0) DESC, sp.cpcv_median_sharpe DESC
        """,
        {"days": baseline_days},
    )
    _print_table(
        f"Shadow graduation candidates — REALIZED-first ranking (last {baseline_days}d "
        "paper+live PnL; CPCV shown but NOT the sort key)",
        rows,
    )
    print(
        "\n  READ: rank by realized paper+live PnL, then sanity-check CPCV. A high "
        "CPCV\n  with negative/empty realized evidence (e.g. pid 1267: CPCV 8.01, "
        "0-win live)\n  is a graduation TRAP, not a candidate. Only patterns with "
        "positive realized\n  evidence AND a passed gate belong in a pilot."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-days", type=int, default=7, help="recent analysis window")
    ap.add_argument("--baseline-days", type=int, default=30, help="longer baseline window")
    ap.add_argument(
        "--massive-monthly-usd",
        type=float,
        default=249.0,
        help="actual invoiced Massive.com subscription cost (real input, not a threshold)",
    )
    args = ap.parse_args()

    scorecard(args.window_days, args.baseline_days, args.massive_monthly_usd)
    equity_funnel(args.window_days)
    equity_pnl_by_pattern(args.baseline_days)
    crypto_leak_monitor(args.window_days, args.baseline_days)
    graduation_candidates(args.baseline_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
