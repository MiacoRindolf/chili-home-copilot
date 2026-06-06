"""Forensic edge-leakage attribution — why proven patterns lose money live.

Decomposes realized per-trade / per-pattern loss into an additive waterfall and
emits a verdict: how much of the loss is (a) entry-estimator over-optimism,
(b) involuntary / reconciler exits, (c) execution cost (TCA), vs (d) thin alpha.
Read-only. Reuses the existing attribution helpers + return_math (the canonical
signed-return) so the numbers match the rest of the realized-EV stack.

Usage:
    conda run -n chili-env python scripts/forensic_edge_leakage_attribution.py [--days 180] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CHILI_PYTEST", "1")  # never run migrations from this read-only probe


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xa = [p[0] for p in pairs]
    ya = [p[1] for p in pairs]
    try:
        return statistics.correlation(xa, ya)
    except Exception:
        return None


def _tstat_nonzero(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return None
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return None
    return m / (sd / math.sqrt(n))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from sqlalchemy import text
    from app.db import SessionLocal
    from app.models.trading import Trade, ScanPattern
    from app.services.trading.attribution_service import (
        _expected_net_pct_from_payload,
        _trade_tca_cost_pct,
        _trade_directional_outcome,
        _normalized_exit_reason,
        _exit_reason_family,
        _is_low_confidence_exit_family,
        _exit_reason_quality_rows,
        _json_dict,
    )

    db = SessionLocal()
    cutoff = datetime.utcnow() - timedelta(days=args.days)
    try:
        trades = (
            db.query(Trade)
            .filter(Trade.status == "closed", Trade.pnl.isnot(None), Trade.exit_date >= cutoff)
            .all()
        )
        # latest entry-edge snapshot per trade
        ids = [int(t.id) for t in trades]
        snap_by_tid: dict[int, dict] = {}
        if ids:
            rows = db.execute(text(
                """
                SELECT DISTINCT ON (r.trade_id) r.trade_id, r.rule_snapshot
                  FROM trading_autotrader_runs r
                 WHERE r.trade_id = ANY(:ids)
                   AND r.rule_snapshot IS NOT NULL
                 ORDER BY r.trade_id, r.created_at DESC
                """
            ), {"ids": ids}).fetchall()
            for tid, snap in rows:
                snap_by_tid[int(tid)] = snap if isinstance(snap, dict) else _json_dict(snap)

        # per-trade records
        recs: list[dict[str, Any]] = []
        for t in trades:
            payload = snap_by_tid.get(int(t.id)) or {}
            realized = _trade_directional_outcome(t)
            expected = _expected_net_pct_from_payload(payload) if payload else None
            edge = _json_dict(payload.get("entry_edge")) if payload else {}
            assumed_cost_pct = None
            for k in ("empirical_cost_fraction", "cost_fraction"):
                v = edge.get(k)
                if v is not None:
                    try:
                        assumed_cost_pct = float(v) * 100.0
                        break
                    except (TypeError, ValueError):
                        pass
            fam = _exit_reason_family(_normalized_exit_reason(t))
            recs.append({
                "id": int(t.id),
                "pattern_id": int(t.scan_pattern_id) if t.scan_pattern_id else None,
                "asset": (t.asset_kind or "unknown"),
                "realized": realized,
                "expected": expected,
                "tca_cost_pct": _trade_tca_cost_pct(t),
                "assumed_cost_pct": assumed_cost_pct,
                "exit_family": fam,
                "involuntary": _is_low_confidence_exit_family(fam) or fam in ("reconciler_or_broker_cleanup", "unknown"),
            })

        edge_linked = [r for r in recs if r["expected"] is not None and r["realized"] is not None]

        out: dict[str, Any] = {"window_days": args.days}

        # ---- Section 0: coverage ----
        by_asset_all = defaultdict(int)
        for r in recs:
            by_asset_all[r["asset"]] += 1
        out["coverage"] = {
            "closed_trades": len(recs),
            "edge_linked": len(edge_linked),
            "by_asset": dict(by_asset_all),
            "tca_available": sum(1 for r in recs if r["tca_cost_pct"] is not None),
        }

        # ---- Section 1: estimator calibration (expected vs realized) ----
        calib = {}
        for asset in sorted({r["asset"] for r in edge_linked}):
            grp = [r for r in edge_linked if r["asset"] == asset]
            exp = [r["expected"] for r in grp]
            rea = [r["realized"] for r in grp]
            gaps = [e - a for e, a in zip(exp, rea)]
            sign_agree = _mean([1.0 if (e > 0) == (a > 0) else 0.0 for e, a in zip(exp, rea)])
            calib[asset] = {
                "n": len(grp),
                "mean_expected_pct": _mean(exp),
                "mean_realized_pct": _mean(rea),
                "mean_optimism_pp": _mean(gaps),
                "sign_agreement": sign_agree,
                "pearson": _pearson(exp, rea),
                "tstat_optimism": _tstat_nonzero(gaps),
            }
        out["estimator_calibration"] = calib

        # ---- Section 2: execution cost (TCA where available) ----
        execrows = {}
        for asset in sorted({r["asset"] for r in recs}):
            grp = [r for r in recs if r["asset"] == asset]
            realized_cost = [r["tca_cost_pct"] for r in grp if r["tca_cost_pct"] is not None]
            assumed_cost = [r["assumed_cost_pct"] for r in grp if r["assumed_cost_pct"] is not None]
            execrows[asset] = {
                "n_tca": len(realized_cost),
                "mean_realized_tca_cost_pct": _mean(realized_cost),
                "n_assumed": len(assumed_cost),
                "mean_assumed_cost_pct": _mean(assumed_cost),
                "cost_under_estimate_pp": (
                    (_mean(realized_cost) - _mean(assumed_cost))
                    if (_mean(realized_cost) is not None and _mean(assumed_cost) is not None) else None
                ),
            }
        out["execution_cost"] = execrows

        # ---- Section 3: exit attribution ----
        exit_rows, exit_summary = _exit_reason_quality_rows(trades)
        out["exit_summary"] = exit_summary
        out["exit_families"] = exit_rows[:14]
        invol = [r["realized"] for r in recs if r["involuntary"] and r["realized"] is not None]
        planned = [r["realized"] for r in recs if not r["involuntary"] and r["realized"] is not None]
        out["exit_involuntary_vs_planned"] = {
            "involuntary_n": len(invol),
            "involuntary_mean_realized_pct": _mean(invol),
            "planned_n": len(planned),
            "planned_mean_realized_pct": _mean(planned),
            "involuntary_share_pct": round(100.0 * len(invol) / max(1, len(recs)), 1),
        }

        # ---- Section 4: per-pattern waterfall (n>=3) ----
        patt_ids = {r["pattern_id"] for r in recs if r["pattern_id"]}
        patterns = {int(p.id): p for p in db.query(ScanPattern).filter(ScanPattern.id.in_(patt_ids)).all()} if patt_ids else {}
        per_pattern = []
        byp = defaultdict(list)
        for r in recs:
            if r["pattern_id"]:
                byp[r["pattern_id"]].append(r)
        for pid, grp in byp.items():
            rs = [r["realized"] for r in grp if r["realized"] is not None]
            if len(rs) < 3:
                continue
            p = patterns.get(pid)
            backtest_edge = None
            for attr in ("corrected_avg_return_pct", "oos_avg_return_pct", "raw_realized_avg_return_pct"):
                v = getattr(p, attr, None) if p else None
                if v is not None:
                    backtest_edge = float(v)
                    break
            mean_real = _mean(rs)
            invol_share = round(100.0 * sum(1 for r in grp if r["involuntary"]) / len(grp), 0)
            per_pattern.append({
                "pattern_id": pid,
                "n": len(rs),
                "backtest_edge_pct": round(backtest_edge, 2) if backtest_edge is not None else None,
                "mean_realized_pct": round(mean_real, 2) if mean_real is not None else None,
                "gap_pp": round(backtest_edge - mean_real, 2) if (backtest_edge is not None and mean_real is not None) else None,
                "involuntary_exit_pct": invol_share,
            })
        per_pattern.sort(key=lambda x: (x["gap_pp"] if x["gap_pp"] is not None else -999), reverse=True)
        out["per_pattern_waterfall"] = per_pattern

    finally:
        db.close()

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0

    # ----- human report -----
    def pct(v, w=8):
        return (f"{v:+.2f}" if isinstance(v, (int, float)) else "   -  ").rjust(w)

    def f2(v, nd=2):
        return (f"{v:.{nd}f}" if isinstance(v, (int, float)) else "-")

    c = out["coverage"]
    print("=" * 78)
    print(f"FORENSIC EDGE-LEAKAGE ATTRIBUTION  (last {args.days}d)")
    print("=" * 78)
    print(f"closed={c['closed_trades']}  edge-linked={c['edge_linked']}  tca-available={c['tca_available']}  by_asset={c['by_asset']}")

    print("\n[1] ENTRY-ESTIMATOR CALIBRATION (expected vs realized) — the over-optimism")
    print(f"  {'asset':8} {'n':>4} {'exp%':>8} {'real%':>8} {'optimism_pp':>12} {'sign_agree':>10} {'corr':>6} {'t':>6}")
    for a, v in out["estimator_calibration"].items():
        sa, pe, ts = f2(v["sign_agreement"]), f2(v["pearson"]), f2(v["tstat_optimism"], 1)
        print(f"  {a:8} {v['n']:>4} {pct(v['mean_expected_pct'])} {pct(v['mean_realized_pct'])} {pct(v['mean_optimism_pp'],12)} "
              f"{sa:>10} {pe:>6} {ts:>6}")

    print("\n[2] EXECUTION COST (realized TCA vs assumed)")
    print(f"  {'asset':8} {'n_tca':>6} {'real_cost%':>10} {'n_asm':>6} {'asm_cost%':>10} {'under_est_pp':>12}")
    for a, v in out["execution_cost"].items():
        print(f"  {a:8} {v['n_tca']:>6} {pct(v['mean_realized_tca_cost_pct'],10)} {v['n_assumed']:>6} "
              f"{pct(v['mean_assumed_cost_pct'],10)} {pct(v['cost_under_estimate_pp'],12)}")

    print("\n[3] EXIT ATTRIBUTION")
    es = out["exit_summary"]
    print(f"  reconciler/cleanup exits: {es.get('reconciler_exit_count')} | low-confidence: {es.get('low_confidence_exit_count')} "
          f"| planned: {es.get('planned_exit_count')} ({es.get('planned_exit_rate_pct')}%)")
    iv = out["exit_involuntary_vs_planned"]
    print(f"  INVOLUNTARY exits: {iv['involuntary_n']} ({iv['involuntary_share_pct']}% of closed), mean realized {pct(iv['involuntary_mean_realized_pct'])}%")
    print(f"  PLANNED     exits: {iv['planned_n']}, mean realized {pct(iv['planned_mean_realized_pct'])}%")
    print(f"  {'exit_family':30} {'reason':28} {'trades':>6} {'rate%':>6} {'win%':>6} {'tot_pnl':>9}")
    for r in out["exit_families"]:
        print(f"  {(r['exit_family'] or '')[:30]:30} {(r['exit_reason'] or '')[:28]:28} {r['trades']:>6} "
              f"{(r['trade_rate_pct'] or 0):>6} {(r['win_rate_pct'] if r['win_rate_pct'] is not None else 0):>6} "
              f"{(r['total_pnl'] if r['total_pnl'] is not None else 0):>9}")

    print("\n[4] PER-PATTERN WATERFALL (n>=3 closed; backtest edge vs realized)")
    print(f"  {'pid':>5} {'n':>4} {'backtest%':>10} {'realized%':>10} {'gap_pp':>8} {'invol_exit%':>11}")
    for r in out["per_pattern_waterfall"][:20]:
        print(f"  {r['pattern_id']:>5} {r['n']:>4} {pct(r['backtest_edge_pct'],10)} {pct(r['mean_realized_pct'],10)} "
              f"{pct(r['gap_pp'])} {(r['involuntary_exit_pct'] or 0):>11}")
    print("\n(verdict: read [1] for estimator optimism, [3] for involuntary-exit drag, [2] for cost, [4] for per-pattern)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
