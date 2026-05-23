"""f-position-identity-phase-5b-soak-and-reader-parity (2026-05-22).

Phase 5B daily soak probe. Read-only. Runs the three SQL queries
defined in docs/STRATEGY/NEXT_TASK.md and prints results in a
human-readable shape so the operator can eyeball drift day over day.

Sections:

  1. **Phase 5A envelope parity** -- valid_trades_missing_decision,
     open_broker_trades_missing_position, orphan_decisions. The
     green state per NEXT_TASK is zeros across the board (the 67
     corrupt legacy dust rows are intentionally skipped).
  2. **Phase 5B linkage status** -- distribution of linkage_status
     across the decision/envelope/position read model.
     Green = 0 hard linkage issues;
     historical_broker_envelope_missing_position can remain nonzero
     until old closed envelopes are backfilled or retired.
  3. **Phase 5B pattern decision performance** -- top 20 patterns by
     total_pnl using the new decision/envelope split.

Usage:
    python scripts/d-phase5b-soak-probe.py
    python scripts/d-phase5b-soak-probe.py --json  # machine-readable

Env: reads DATABASE_URL or defaults to localhost:5433/chili.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili",
)


def _fetch_all(conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def section_parity(conn) -> dict[str, Any]:
    rows = _fetch_all(conn, "SELECT * FROM trading_phase5a_envelope_parity")
    if not rows:
        return {"ok": False, "reason": "view returned no rows"}
    r = rows[0]
    # NEXT_TASK.md references slightly stale names; the live view uses:
    #   trades_missing_decision, open_broker_trades_missing_position,
    #   orphan_decisions. We accept any nonzero trades_missing_decision
    #   because the corrupt legacy dust rows (67 as of 2026-05-22) sit
    #   in this counter and are intentionally skipped.
    return {
        "ok": all(
            (r.get(k) or 0) == 0
            for k in (
                "open_broker_trades_missing_position",
                "orphan_decisions",
            )
        ),
        **r,
    }


def section_linkage(conn) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT linkage_status, COUNT(*) AS n
        FROM trading_phase5b_decision_envelope_position
        GROUP BY linkage_status
        ORDER BY COUNT(*) DESC
        """,
    )


def section_reader_parity(conn) -> dict[str, Any]:
    """Compare old trading_trades-grouped PnL vs Phase 5B view PnL by pattern.

    This is the lightweight first step of reader migration: prove that the
    new decision/envelope/position read model returns the same aggregate
    PnL per pattern as the legacy trading_trades-grouped query, before any
    production reporting reader is pointed at the new view. Mismatches are
    expected for in-flight (open) envelopes and for patterns where envelope
    decision-linkage is incomplete; the goal is to surface them.
    """
    sql_old = """
        SELECT scan_pattern_id,
               COUNT(*)                   AS n_envelopes,
               COUNT(*) FILTER (WHERE status = 'closed') AS n_closed,
               SUM(COALESCE(pnl, 0))      AS total_pnl
        FROM trading_trades
        WHERE scan_pattern_id IS NOT NULL
        GROUP BY scan_pattern_id
    """
    sql_new = """
        SELECT scan_pattern_id, envelopes, closed_envelopes, total_pnl
        FROM trading_phase5b_pattern_decision_performance
    """
    old_rows = {
        r["scan_pattern_id"]: r
        for r in _fetch_all(conn, sql_old)
        if r["scan_pattern_id"] is not None
    }
    new_rows = {
        r["scan_pattern_id"]: r
        for r in _fetch_all(conn, sql_new)
        if r["scan_pattern_id"] is not None
    }

    mismatches: list[dict[str, Any]] = []
    pids = sorted(set(old_rows) | set(new_rows))
    for pid in pids:
        o = old_rows.get(pid)
        n = new_rows.get(pid)
        o_pnl = float(o["total_pnl"]) if o and o["total_pnl"] is not None else 0.0
        n_pnl = float(n["total_pnl"]) if n and n["total_pnl"] is not None else 0.0
        o_env = (o["n_envelopes"] if o else 0) or 0
        n_env = (n["envelopes"] if n else 0) or 0
        if abs(o_pnl - n_pnl) > 0.01 or o_env != n_env:
            mismatches.append({
                "pattern_id": pid,
                "old_envelopes": o_env,
                "new_envelopes": n_env,
                "old_pnl": round(o_pnl, 2),
                "new_pnl": round(n_pnl, 2),
                "delta_pnl": round(n_pnl - o_pnl, 2),
            })
    return {
        "old_pattern_count": len(old_rows),
        "new_pattern_count": len(new_rows),
        "mismatch_count": len(mismatches),
        "top_mismatches_by_abs_delta": sorted(
            mismatches, key=lambda m: abs(m["delta_pnl"]), reverse=True,
        )[:10],
    }


def section_pattern_perf(conn) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT
            p.scan_pattern_id,
            sp.name AS pattern_name,
            p.decisions,
            p.envelopes,
            p.closed_envelopes,
            p.open_envelopes,
            p.total_pnl,
            p.avg_pnl_closed_or_marked,
            p.avg_entry_slippage_bps,
            p.avg_exit_slippage_bps,
            p.linkage_issues,
            p.historical_linkage_debt
        FROM trading_phase5b_pattern_decision_performance p
        LEFT JOIN scan_patterns sp ON sp.id = p.scan_pattern_id
        ORDER BY p.total_pnl DESC NULLS LAST
        LIMIT 20
        """,
    )


def _print_text(data: dict[str, Any]) -> None:
    parity = data["parity"]
    print("=== Phase 5A envelope parity ===")
    if parity.get("ok"):
        print("  GREEN: all hard-linkage counters are zero.")
    else:
        print("  RED: one or more linkage counters non-zero.")
    for k, v in parity.items():
        if k == "ok":
            continue
        print(f"    {k}: {v}")

    print("\n=== Phase 5B linkage status distribution ===")
    hard_issues = 0
    for r in data["linkage"]:
        status = r["linkage_status"]
        n = r["n"]
        marker = ""
        if status == "linked":
            marker = " (green)"
        elif status == "historical_broker_envelope_missing_position":
            marker = " (debt-ok)"
        else:
            marker = " (HARD ISSUE)"
            hard_issues += int(n)
        print(f"  {status:60s} {n:>6}{marker}")
    if hard_issues == 0:
        print("  GREEN: no hard live linkage issues.")
    else:
        print(f"  RED: {hard_issues} rows in hard-issue statuses.")

    print("\n=== Phase 5B pattern decision performance (top 20 by total_pnl) ===")
    print(
        f"  {'pid':>5}  {'name':40s}  {'dec':>4}  {'env':>4}  {'clz':>4}  "
        f"{'open':>4}  {'total_pnl':>12}  {'avg_pnl':>10}  "
        f"{'ent_bps':>8}  {'ext_bps':>8}  {'iss':>4}"
    )
    for r in data["pattern_perf"]:
        name = (r.get("pattern_name") or "")[:40]
        total = r.get("total_pnl")
        avg = r.get("avg_pnl_closed_or_marked")
        ent_bps = r.get("avg_entry_slippage_bps")
        ext_bps = r.get("avg_exit_slippage_bps")
        total_s = f"{float(total):.2f}" if total is not None else ""
        avg_s = f"{float(avg):.2f}" if avg is not None else ""
        ent_s = f"{float(ent_bps):.1f}" if ent_bps is not None else ""
        ext_s = f"{float(ext_bps):.1f}" if ext_bps is not None else ""
        print(
            f"  {r['scan_pattern_id']:>5}  {name:40s}  "
            f"{r.get('decisions') or 0:>4}  "
            f"{r.get('envelopes') or 0:>4}  "
            f"{r.get('closed_envelopes') or 0:>4}  "
            f"{r.get('open_envelopes') or 0:>4}  "
            f"{total_s:>12}  {avg_s:>10}  "
            f"{ent_s:>8}  {ext_s:>8}  "
            f"{r.get('linkage_issues') or 0:>4}"
        )

    rp = data["reader_parity"]
    print("\n=== Reader parity: old trading_trades vs Phase 5B view ===")
    print(
        f"  patterns in old: {rp['old_pattern_count']}  "
        f"patterns in new: {rp['new_pattern_count']}  "
        f"mismatches: {rp['mismatch_count']}"
    )
    if rp["mismatch_count"] == 0:
        print("  GREEN: pattern-level PnL aggregates match between old and new readers.")
    else:
        print(f"  AMBER: top {len(rp['top_mismatches_by_abs_delta'])} mismatches by |delta_pnl|:")
        print(
            f"    {'pid':>5}  {'o_env':>5}  {'n_env':>5}  "
            f"{'old_pnl':>10}  {'new_pnl':>10}  {'delta':>10}"
        )
        for m in rp["top_mismatches_by_abs_delta"]:
            print(
                f"    {m['pattern_id']:>5}  {m['old_envelopes']:>5}  "
                f"{m['new_envelopes']:>5}  {m['old_pnl']:>10.2f}  "
                f"{m['new_pnl']:>10.2f}  {m['delta_pnl']:>10.2f}"
            )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON for machine consumption")
    args = ap.parse_args(argv[1:])

    try:
        conn = psycopg2.connect(DSN)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot connect to {DSN}: {exc}", file=sys.stderr)
        return 2

    try:
        data = {
            "parity": section_parity(conn),
            "linkage": section_linkage(conn),
            "pattern_perf": section_pattern_perf(conn),
            "reader_parity": section_reader_parity(conn),
        }
    finally:
        conn.close()

    if args.json:
        print(json.dumps(data, default=str, indent=2))
    else:
        _print_text(data)

    hard_linkage_issues = sum(
        int(r["n"])
        for r in data["linkage"]
        if r["linkage_status"]
        not in ("linked", "historical_broker_envelope_missing_position")
    )
    overall_ok = data["parity"].get("ok", False) and hard_linkage_issues == 0
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
