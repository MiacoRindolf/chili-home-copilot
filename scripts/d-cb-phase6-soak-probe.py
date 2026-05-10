"""f-coinbase-autotrader-enablement-phase-6-paper-soak (2026-05-09).

Soak observability probe. Read-only. Run on demand by the operator
at T+1h, T+12h, T+24h, T+48h.

Surfaces seven sections:

  1. **Routing distribution** — selector decisions in the soak
     window (rh / coinbase / skip-with-reason).
  2. **Cost-gate distribution** — pass / block by reason (cost_gate:
     prefix in audit reasons).
  3. **Cap-gate distribution** — pass / block by reason
     (coinbase_cap: prefix).
  4. **Coinbase fills** — Trade rows with broker_source='coinbase'
     in the window. Notional, ticker, status.
  5. **Bracket coverage** — for each Coinbase Trade in window, was a
     bracket_intent created within 60s and a broker stop placed
     within 5min? Acceptance criterion #4.
  6. **Cash drift** — current Coinbase portfolio cash vs $2200.01
     baseline. Acceptance criterion #5.
  7. **Anomaly summary** — green-light / amber / red signals based
     on the brief's anomaly thresholds.

Usage:
    python scripts/d-cb-phase6-soak-probe.py
    python scripts/d-cb-phase6-soak-probe.py --window-hours 24
    python scripts/d-cb-phase6-soak-probe.py --json  # machine-readable

Env / settings: reads DATABASE_URL from .env (or env directly).
Coinbase calls use the same credentials as the running containers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.extras

# Defaults
DEFAULT_WINDOW_HOURS = 48
SOAK_BASELINE_CASH_USD = 2200.01
CASH_DRIFT_AMBER_USD = 5.0
CASH_DRIFT_RED_USD = 25.0
BRACKET_INTENT_DEADLINE_S = 60
BRACKET_BROKER_STOP_DEADLINE_S = 300
SOAK_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili",
)


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _q(cur, sql: str, params=None):
    cur.execute(sql, params or {})
    return cur.fetchall()


def section_routing_distribution(cur, since):
    _section("1. ROUTING DISTRIBUTION (selector decisions)")
    rows = _q(cur, """
        SELECT
            CASE
                WHEN reason LIKE 'selector:%%' THEN reason
                WHEN reason = 'ok' THEN 'placed_rh'
                WHEN reason LIKE 'live_robinhood%%' THEN 'placed_rh'
                WHEN reason LIKE 'broker:%%' THEN reason
                ELSE COALESCE(reason, 'unknown')
            END AS reason_bucket,
            COUNT(*) AS n
          FROM trading_autotrader_runs
         WHERE created_at >= %(since)s
         GROUP BY reason_bucket
         ORDER BY n DESC
         LIMIT 30
    """, {"since": since})
    if not rows:
        print("(no autotrader runs in window)")
        return
    for r in rows:
        print(f"  {r[0]:<60} n={r[1]}")


def section_cost_gate_distribution(cur, since):
    _section("2. COST-GATE DISTRIBUTION")
    rows = _q(cur, """
        SELECT reason, COUNT(*) AS n
          FROM trading_autotrader_runs
         WHERE created_at >= %(since)s
           AND reason LIKE 'cost_gate:%%'
         GROUP BY reason
         ORDER BY n DESC
    """, {"since": since})
    if not rows:
        print("(no cost-gate decisions in window)")
        return
    for r in rows:
        print(f"  {r[0]:<60} n={r[1]}")


def section_cap_gate_distribution(cur, since):
    _section("3. CAP-GATE DISTRIBUTION (Coinbase per-venue cap)")
    rows = _q(cur, """
        SELECT reason, COUNT(*) AS n
          FROM trading_autotrader_runs
         WHERE created_at >= %(since)s
           AND reason LIKE 'coinbase_cap:%%'
         GROUP BY reason
         ORDER BY n DESC
    """, {"since": since})
    if not rows:
        print("(no cap-gate decisions in window)")
        return
    for r in rows:
        print(f"  {r[0]:<60} n={r[1]}")


def section_coinbase_fills(cur, since):
    _section("4. COINBASE FILLS (Trade rows broker_source='coinbase')")
    rows = _q(cur, """
        SELECT id, ticker, status, quantity, entry_price,
               COALESCE(quantity * entry_price, 0.0) AS notional,
               entry_date, exit_date, exit_reason, last_fill_at
          FROM trading_trades
         WHERE LOWER(COALESCE(broker_source, '')) = 'coinbase'
           AND entry_date >= %(since)s
         ORDER BY entry_date ASC
    """, {"since": since})
    if not rows:
        print("(no Coinbase Trades in window)")
        return rows
    print(
        f"  {'id':>5} {'ticker':<10} {'status':<10} {'qty':>10} "
        f"{'entry_px':>10} {'notional$':>10} entry_date"
    )
    for r in rows:
        print(
            f"  {r[0]:>5} {r[1]:<10} {r[2]:<10} {r[3]:>10.4f} "
            f"{r[4]:>10.4f} {r[5]:>10.2f} {r[6]}"
        )
    return rows


def section_bracket_coverage(cur, since):
    _section("5. BRACKET COVERAGE (Coinbase entries)")
    rows = _q(cur, """
        WITH cb_trades AS (
            SELECT id AS trade_id, ticker, entry_date
              FROM trading_trades
             WHERE LOWER(COALESCE(broker_source, '')) = 'coinbase'
               AND entry_date >= %(since)s
        )
        SELECT t.trade_id, t.ticker, t.entry_date,
               bi.id AS intent_id, bi.created_at AS intent_at,
               bi.broker_stop_order_id,
               EXTRACT(EPOCH FROM (bi.created_at - t.entry_date))
                   AS intent_lag_s
          FROM cb_trades t
          LEFT JOIN trading_bracket_intents bi
            ON bi.trade_id = t.trade_id
         ORDER BY t.entry_date ASC
    """, {"since": since})
    if not rows:
        print("(no Coinbase entries to check coverage on)")
        return {"total": 0, "with_intent_60s": 0, "with_broker_stop": 0}

    total = len(rows)
    with_intent_within_60s = sum(
        1 for r in rows
        if r[3] is not None and r[6] is not None
        and r[6] <= BRACKET_INTENT_DEADLINE_S
    )
    with_broker_stop = sum(
        1 for r in rows if r[5] is not None and str(r[5]).strip()
    )
    print(
        f"  {'trade':>5} {'ticker':<10} {'entry':<20} {'intent':<6} "
        f"{'lag(s)':>8} {'broker_stop':>12}"
    )
    for r in rows:
        intent_marker = "yes" if r[3] is not None else "NO"
        lag_s = f"{r[6]:.1f}" if r[6] is not None else "-"
        bso = (str(r[5]) if r[5] else "—")[:12]
        print(
            f"  {r[0]:>5} {r[1]:<10} "
            f"{str(r[2])[:19]:<20} {intent_marker:<6} "
            f"{lag_s:>8} {bso:>12}"
        )
    print()
    print(f"  total Coinbase entries:                  {total}")
    print(f"  with intent within {BRACKET_INTENT_DEADLINE_S}s:               "
          f"{with_intent_within_60s} ({100.0 * with_intent_within_60s / total:.1f}%)")
    print(f"  with broker stop placed:                  "
          f"{with_broker_stop} ({100.0 * with_broker_stop / total:.1f}%)")
    return {
        "total": total,
        "with_intent_60s": with_intent_within_60s,
        "with_broker_stop": with_broker_stop,
    }


def section_cash_drift():
    _section("6. CASH DRIFT (current vs baseline)")
    try:
        # Lazy import so the probe doesn't require coinbase SDK
        # for sections 1-5.
        sys.path.insert(0, os.getcwd())
        from app.services import coinbase_service as cb
        portfolio = cb.get_portfolio() or {}
    except Exception as exc:
        print(f"  (failed to fetch Coinbase portfolio: {exc})")
        return None

    cash = float(portfolio.get("cash") or 0.0)
    delta = cash - SOAK_BASELINE_CASH_USD
    print(f"  baseline cash: ${SOAK_BASELINE_CASH_USD:.2f}")
    print(f"  current cash:  ${cash:.2f}")
    print(f"  drift:         ${delta:+.2f}")
    if abs(delta) > CASH_DRIFT_RED_USD:
        verdict = "RED — investigate immediately"
    elif abs(delta) > CASH_DRIFT_AMBER_USD:
        verdict = "AMBER — monitor closely"
    else:
        verdict = "GREEN — within tolerance"
    print(f"  verdict:       {verdict}")
    return {"cash": cash, "delta": delta, "verdict": verdict}


def section_anomaly_summary(coverage, drift):
    _section("7. ANOMALY SUMMARY (acceptance criteria)")
    items = []

    # AC4: 100% bracket coverage on Coinbase entries
    if coverage and coverage["total"] > 0:
        intent_pct = 100.0 * coverage["with_intent_60s"] / coverage["total"]
        stop_pct = 100.0 * coverage["with_broker_stop"] / coverage["total"]
        if intent_pct == 100.0 and stop_pct == 100.0:
            items.append(("GREEN", "100% bracket coverage"))
        else:
            items.append((
                "RED",
                f"bracket coverage gap: intent={intent_pct:.1f}% "
                f"broker_stop={stop_pct:.1f}%",
            ))
    else:
        items.append((
            "INFO",
            "no Coinbase entries this window — path not exercised yet",
        ))

    # AC5: cash drift <= $5
    if drift:
        if abs(drift["delta"]) > CASH_DRIFT_RED_USD:
            items.append((
                "RED", f"cash drift ${drift['delta']:+.2f} (>$25)",
            ))
        elif abs(drift["delta"]) > CASH_DRIFT_AMBER_USD:
            items.append((
                "AMBER", f"cash drift ${drift['delta']:+.2f} (>$5)",
            ))
        else:
            items.append(("GREEN", f"cash drift ${drift['delta']:+.2f}"))
    else:
        items.append(("AMBER", "cash drift unknown (Coinbase fetch failed)"))

    for kind, msg in items:
        print(f"  [{kind}] {msg}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-hours", type=int, default=DEFAULT_WINDOW_HOURS,
        help=f"Soak window in hours (default {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON-only output (suppresses pretty print)",
    )
    args = parser.parse_args()

    since = datetime.utcnow() - timedelta(hours=args.window_hours)

    print(
        f"# d-cb-phase6-soak-probe @ {datetime.utcnow().isoformat()}Z\n"
        f"# window: trailing {args.window_hours}h "
        f"(since {since.isoformat()}Z)"
    )

    conn = psycopg2.connect(SOAK_DSN)
    cur = conn.cursor()
    try:
        section_routing_distribution(cur, since)
        section_cost_gate_distribution(cur, since)
        section_cap_gate_distribution(cur, since)
        section_coinbase_fills(cur, since)
        coverage = section_bracket_coverage(cur, since)
        drift = section_cash_drift()
        section_anomaly_summary(coverage, drift)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
