"""Golden-library replay scorecard — turns batch-runner JSONL into the measurement doc.

Consumes ``results.jsonl`` + ``meta.json`` from ``scripts/replay_benchmark_batch.py``
(+ optionally the Ross ground-truth ``manifest.json`` and a read-only prod URL for
golden-table quote paths) and emits (a) machine-readable scorecard JSON and (b) the
markdown report: per-window table, per-setup aggregation, Stage-0-style REPLAY
expectancy, within-trade MFE capture, and the Ross cross-reference in the
ROSS_CAPTURE_PARITY canonical format.

TWO capture denominators, NEVER mixed (both labeled in the report):
  * Label A — within-trade MFE capture: realized vs peak executable bid between
    entry and exit (``evaluate_long_trade_path``; bids only, no post-exit lookahead).
  * Label B — window capture: (pnl_usd / deployed_notional) / window_move_frac,
    where window_move_frac = (window_hi - first_px) / first_px. Sizing-independent
    conversion of the window's first->high move; 1.0 = full conversion.
Mock fills are ~5%% optimistic (ROSS_CAPTURE_PARITY L4) — every dollar section is
deltas-not-absolutes.

    python scripts/replay_scorecard.py \
        --results D:/CHILI-Docker/chili-data/replay_batch/results.jsonl \
        --meta    D:/CHILI-Docker/chili-data/replay_batch/meta.json \
        --manifest project_ws/AgentOps/ross_video_evidence/manifest.json \
        --db postgresql://chili:chili@localhost:5433/chili \
        --out-json D:/CHILI-Docker/chili-data/replay_batch/scorecard.json \
        --out-md   D:/CHILI-Docker/chili-data/replay_batch/scorecard.md

READ-ONLY everywhere (golden tables only when --db is given). Never assumes the
batch completed — reports attempted vs library size.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta

BUILD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BUILD)

from app.services.trading.momentum_neural.post_exit_excursion import (  # noqa: E402
    compute_post_exit_excursion,
)
from app.services.trading.momentum_neural.ross_replay_benchmark import (  # noqa: E402
    TradablePathPoint,
    evaluate_long_trade_path,
    grade_recap_decision,
)

# Stage-0 expectancy constants + math copied from scripts/ross_baseline_tracker.py:67-98
# (scripts/ is not a package, so the ~30 lines are duplicated here with attribution).
STAGE0_WINNER_R = 1.5
STAGE0_MIN_WINNERS = 3
STAGE0_MAX_LOSER_R = 0.8
MIN_TRADES = 30

VERDICT_MARK = {
    "matched_trade": "①✅",          # 1 matched
    "missed_profitable_setup": "①❌",
    "valid_veto": "①✅(veto)",
    "matched_reject": "②✅",          # 2 correct no-trade
    "false_positive_trade": "②❌",
    "wrong_phase_trade": "③❌",       # 3 wrong phase
    "unmatched_trade_outcome": "③❌",
    "unscorable": "?",
}


def load_results(paths: list[str]) -> tuple[list[dict], list[dict]]:
    """Last-wins dedupe on key; split ok vs non-ok."""
    by_key: dict[str, dict] = {}
    for p in paths:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("key"):
                    by_key[rec["key"]] = rec
    ok = [r for r in by_key.values() if r.get("status") == "ok"]
    bad = [r for r in by_key.values() if r.get("status") != "ok"]
    key = lambda r: (r.get("day") or "", r.get("symbol") or "")  # noqa: E731
    return sorted(ok, key=key), sorted(bad, key=key)


def pair_round_trips(fills: list[dict]) -> list[dict]:
    """FIFO netting of BUY/SELL fills into round trips (mirrors the driver's
    buys/sells+MTM convention in replay_ab_dark_flags.py). One trade per SELL,
    matched against outstanding BUY lots."""
    lots: list[list[float]] = []  # [qty_remaining, px]
    trades: list[dict] = []
    for f in fills or []:
        side = str(f.get("side") or "").lower()
        qty = float(f.get("qty") or 0)
        px = float(f.get("px") or 0)
        if qty <= 0 or px <= 0:
            continue
        if side == "buy":
            lots.append([qty, px])
            continue
        if side != "sell":
            continue
        remaining = qty
        cost = 0.0
        matched = 0.0
        while remaining > 1e-9 and lots:
            lot = lots[0]
            take = min(lot[0], remaining)
            cost += take * lot[1]
            matched += take
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots.pop(0)
        if matched > 0:
            entry_px = cost / matched
            trades.append({"qty": matched, "entry_px": round(entry_px, 4), "exit_px": px,
                           "pnl_usd": round((px - entry_px) * matched, 2)})
    return trades


def attach_fill_times(rec: dict, trades: list[dict]) -> None:
    """Best-effort: map sink fill events (live_entry_filled / live_exit_fill, in id
    order) onto FIFO trades sequentially. Mismatched counts -> ts stays None and the
    within-trade MFE degrades to unavailable for that trade."""
    events = (rec.get("sink") or {}).get("fill_events") or []
    entries = [e for e in events if e.get("event_type") == "live_entry_filled"]
    exits = [e for e in events if e.get("event_type") == "live_exit_fill"]
    for i, t in enumerate(trades):
        t["entry_ts"] = entries[i]["ts"] if i < len(entries) else None
        t["exit_ts"] = exits[i]["ts"] if i < len(exits) else None
        t["trigger_reason"] = (entries[i].get("trigger_reason") if i < len(entries) else None)
        t["exit_reason"] = (exits[i].get("exit_reason") if i < len(exits) else None)


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


def nbbo_path(conn, symbol: str, t0: datetime, t1: datetime, max_points: int = 5000):
    cur = conn.cursor()
    cur.execute(
        "SELECT observed_at, bid, ask FROM replay_golden_nbbo "
        "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s "
        "AND bid > 0 AND ask >= bid ORDER BY observed_at ASC, id ASC",
        (symbol, t0 - timedelta(seconds=5), t1 + timedelta(seconds=5)))
    rows = cur.fetchall()
    stride = max(1, len(rows) // max_points)
    return [TradablePathPoint(ts=r[0], bid=float(r[1]), ask=float(r[2]))
            for i, r in enumerate(rows) if i % stride == 0]


def within_trade_metrics(conn, rec: dict, trade: dict) -> dict | None:
    """Label A — within-trade MFE capture via evaluate_long_trade_path. On the
    evaluator's mock-fill guard (fill inside the spread -> ValueError), retry with
    quote-derived fills and mark fill_clamped."""
    t_in, t_out = _parse_ts(trade.get("entry_ts")), _parse_ts(trade.get("exit_ts"))
    if conn is None or t_in is None or t_out is None or t_out < t_in:
        return None
    points = nbbo_path(conn, rec["symbol"], t_in, t_out)
    if not points:
        return None
    base = dict(entry_ts=t_in, exit_ts=t_out, qty=trade["qty"])
    try:
        m = evaluate_long_trade_path(points, **base,
                                     entry_fill_price=trade["entry_px"],
                                     exit_fill_price=trade["exit_px"])
        clamped = False
    except ValueError:
        try:
            m = evaluate_long_trade_path(points, **base)
            clamped = True
        except ValueError:
            return None
    return {"capture_ratio": m.realized_mfe_capture_ratio,
            "giveback_frac": m.open_profit_giveback_fraction,
            "peak_open_profit_usd": round(m.peak_open_profit_usd, 2),
            "seconds_to_peak": round(m.seconds_to_peak, 1),
            "fill_clamped": clamped}


def post_exit(conn, rec: dict, trade: dict) -> dict | None:
    t_out = _parse_ts(trade.get("exit_ts"))
    win_end = _parse_ts(rec.get("win_end"))
    if conn is None or t_out is None or win_end is None or t_out >= win_end:
        return None
    cur = conn.cursor()
    cur.execute("SELECT max(price), min(price) FROM replay_golden_ticks "
                "WHERE symbol = %s AND observed_at >= %s AND observed_at < %s AND price > 0",
                (rec["symbol"], t_out, win_end))
    hi, lo = cur.fetchone()
    if not hi:
        return None
    out = compute_post_exit_excursion(
        entry_price=trade["entry_px"], exit_price=trade["exit_px"],
        original_target=None, original_stop=None, side_long=True,
        future_high=float(hi), future_low=float(lo),
        exit_reason=trade.get("exit_reason"), realized_pnl=trade.get("pnl_usd"))
    if not out.get("ok", True):
        return None
    return {k: out.get(k) for k in ("post_exit_mfe_pct", "post_exit_mae_pct", "outcome_class")}


def window_capture(rec: dict, trades: list[dict]) -> dict:
    """Label B — full-window conversion. Defined for every window; zero-entry
    windows report entered=false and are excluded from the conditional mean."""
    mk = rec.get("market") or {}
    first_px, hi = mk.get("first_px"), mk.get("hi")
    move_frac = ((hi - first_px) / first_px) if (first_px and hi and first_px > 0) else None
    deployed = sum(t["qty"] * t["entry_px"] for t in trades)
    pnl = rec.get("pnl") or 0.0
    ratio = None
    if move_frac and move_frac > 0 and deployed > 0:
        ratio = (pnl / deployed) / move_frac
    return {"window_move_frac": round(move_frac, 4) if move_frac is not None else None,
            "deployed_notional": round(deployed, 2), "entered": bool(trades),
            "window_capture_ratio": round(ratio, 4) if ratio is not None else None}


def stage0_stats(pnls: list[float]) -> dict:
    # copied from scripts/ross_baseline_tracker.py::_stats (empirical-R convention)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_win, gross_loss = sum(wins), -sum(losses)
    r_unit = (statistics.mean([-x for x in losses]) if losses else 0.0)
    winners_r = sorted([w / r_unit for w in wins], reverse=True) if r_unit > 0 else []
    avg_loser_r = (statistics.mean([-x / r_unit for x in losses])
                   if (losses and r_unit > 0) else 0.0)
    return {"n": n, "wins": len(wins), "losses": len(losses),
            "win_rate": (len(wins) / n if n else 0.0), "net": round(sum(pnls), 2),
            "profit_factor": (gross_win / gross_loss if gross_loss > 0
                              else float("inf") if gross_win > 0 else 0.0),
            "expectancy": (sum(pnls) / n if n else 0.0), "r_unit": round(r_unit, 2),
            "winners_ge_target_r": sum(1 for r in winners_r if r >= STAGE0_WINNER_R),
            "best_winner_r": (round(winners_r[0], 2) if winners_r else 0.0),
            "avg_loser_r": round(avg_loser_r, 2)}


def stage0_gates(s: dict) -> list[list]:
    return [[f"sample >= {MIN_TRADES} trades", s["n"] >= MIN_TRADES, str(s["n"])],
            ["profit factor > 1.0", s["profit_factor"] > 1.0, f"{s['profit_factor']:.2f}"],
            [f">= {STAGE0_MIN_WINNERS} winners >= {STAGE0_WINNER_R}R",
             s["winners_ge_target_r"] >= STAGE0_MIN_WINNERS, str(s["winners_ge_target_r"])],
            [f"avg loser <= {STAGE0_MAX_LOSER_R}R",
             0 < s["avg_loser_r"] <= STAGE0_MAX_LOSER_R, f"{s['avg_loser_r']:.2f}R"]]


def _family(reason: str | None) -> str:
    r = (reason or "").lower()
    for fam, keys in (("micro_pullback", ("micro_pullback",)),
                      ("orb/raw_break", ("orb_break", "raw_break", "range_break")),
                      ("vwap/deep_reclaim", ("vwap", "deep_reclaim", "backside")),
                      ("flush_dip/wick", ("flush_dip", "wick_reclaim")),
                      ("flag/abcd", ("bull_flag", "abcd", "flat_top")),
                      ("halt", ("halt",)),
                      ("ihs/reversal", ("head_shoulders", "bottom_reversal", "reversal"))):
        if any(k in r for k in keys):
            return fam
    return "other" if r else "unknown"


def ross_crossref(manifest: dict | None, records: list[dict],
                  trades_by_key: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    if not manifest:
        return [], []
    by_symday = {(r["symbol"], r["day"]): r for r in records}
    matched, manifest_only = [], []
    for w in manifest.get("windows", []):
        rec = by_symday.get((w.get("symbol"), w.get("date")))
        if rec is None:
            manifest_only.append(w)
            continue
        trades = trades_by_key.get(rec["key"], [])
        actual = "trade" if trades else "miss"
        agg = round(sum(t["pnl_usd"] for t in trades), 2)
        grade = grade_recap_decision(
            expected_action=w.get("expected_action") or "trade",
            actual_action=actual,
            phase_window_matched=(True if trades else None),  # operator tier: replay window overlap
            trade_outcome_acceptable=(agg > 0 if trades else None))
        matched.append({"manifest_id": w.get("manifest_id"), "symbol": w["symbol"],
                        "date": w.get("date"), "window_et": w.get("window_et"),
                        "account": w.get("account"), "ross_action": w.get("ross_action"),
                        "ross_net_usd": w.get("ross_net_usd"),
                        "pnl_confidence": w.get("pnl_confidence"),
                        "chili_pnl": agg if trades else 0.0,
                        "chili_entries": len(trades),
                        "grade": grade.status, "credit": grade.credit,
                        "verdict": VERDICT_MARK.get(grade.status, "?"),
                        "reason": grade.reason})
    return matched, manifest_only


def fmt_money(v) -> str:
    return f"{v:+,.2f}" if isinstance(v, (int, float)) else "—"


def render_markdown(sc: dict) -> str:
    m = sc["meta"]
    L: list[str] = []
    L.append("# Golden-library replay baseline scorecard")
    L.append("")
    L.append(f"- generated: {sc['generated_at']} · build sha `{m.get('build_sha', '?')[:12]}` "
             f"· arm `{m.get('arm')}` · GOLDEN=1 · sink `{m.get('sink')}`")
    L.append(f"- equity/exec: {m.get('equity_exec')}")
    L.append(f"- attempted {sc['coverage']['attempted']} / library {sc['coverage']['library']} "
             f"windows (ok={sc['coverage']['ok']}, failed={sc['coverage']['failed']})")
    L.append("- **CAVEAT (deltas, not absolutes)**: mock fills are ~5% optimistic "
             "(ROSS_CAPTURE_PARITY §3 L4). Use this scorecard for BEFORE/AFTER deltas "
             "against the same library, never as live-PnL forecasts.")
    L.append("")
    L.append("## Per-window results")
    L.append("")
    L.append("| symbol | day | window UTC | class | move % | CHILI PnL | entries/exits "
             "| capture B | top trace | top reject |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sc["windows"]:
        mk, wc = r.get("market") or {}, r.get("window_capture") or {}
        st = ((r.get("sink") or {}).get("setup_trace") or {})
        traces = st.get("trace_alias_counts") or {}
        top_trace = max(traces.items(), key=lambda kv: kv[1])[0] if traces else "—"
        rejects = (r.get("sink") or {}).get("top_rejects") or []
        top_reject = rejects[0][0] if rejects else "—"
        L.append(f"| {r['symbol']} | {r['day']} | {r['win_start'][11:16]}–{r['win_end'][11:16]} "
                 f"| {r['class']} | {mk.get('up_pct', '—')} | {fmt_money(r.get('pnl'))} "
                 f"| {r.get('entries', 0)}/{r.get('exits', 0)} "
                 f"| {wc.get('window_capture_ratio') if wc.get('entered') else 'no-entry'} "
                 f"| {top_trace} | {top_reject} |")
    L.append("")
    L.append("## Per-setup aggregation (trigger_reason families, entries from fill events)")
    L.append("")
    L.append("| family | trades | net PnL | wins | losses |")
    L.append("|---|---|---|---|---|")
    for fam, agg in sc["per_setup"].items():
        L.append(f"| {fam} | {agg['n']} | {fmt_money(agg['net'])} "
                 f"| {agg['wins']} | {agg['losses']} |")
    L.append("")
    L.append("## REPLAY expectancy — Stage-0 style (mock fills; deltas only)")
    L.append("")
    s = sc["expectancy"]
    L.append(f"round trips n={s['n']} · net {fmt_money(s['net'])} · PF "
             f"{s['profit_factor'] if s['profit_factor'] != float('inf') else 'inf'} "
             f"· win rate {s['win_rate']:.0%} · empirical 1R = mean|loss| = ${s['r_unit']}")
    L.append("")
    for name, ok, val in sc["expectancy_gates"]:
        L.append(f"- {'GREEN' if ok else 'red'} — {name}: {val}")
    L.append("")
    L.append("## Label A — within-trade MFE capture (entry→exit, bids only)")
    L.append("")
    a = sc["label_a"]
    if a["n"]:
        L.append(f"- trades with quote-path metrics: {a['n']} (fill_clamped on {a['clamped']})")
        L.append(f"- mean capture ratio {a['mean_capture']:.2f} · mean giveback "
                 f"{a['mean_giveback']:.2f}")
        L.append(f"- post-exit outcome classes: {a['outcome_classes']}")
    else:
        L.append("- unavailable (no fill timestamps / no --db quote paths)")
    L.append("")
    L.append("## Ross cross-reference (canonical ①②③ — operator tier)")
    L.append("")
    if sc["crossref"]:
        L.append("| symbol | date | window ET | acct | Ross action | Ross $ (conf) "
                 "| CHILI $ | verdict | reason |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for c in sc["crossref"]:
            ross_usd = (fmt_money(c["ross_net_usd"])
                        if c.get("pnl_confidence") in ("frame_verified", "stated_verbatim")
                        else "n/a")
            L.append(f"| {c['symbol']} | {c['date']} | {c.get('window_et') or '—'} "
                     f"| {c.get('account') or '—'} | {c['ross_action']} "
                     f"| {ross_usd} ({c.get('pnl_confidence')}) | {fmt_money(c['chili_pnl'])} "
                     f"| {c['verdict']} | {c['reason']} |")
        L.append("")
        L.append("Footnote: the strict sealed-coverage tier "
                 "(`grade_recap_phase_window`) is fail-closed by design "
                 "(`sealed_decision_coverage_not_bound`) and is not wired here; "
                 "this table is the operator tier used by the 07-16 scorecard.")
    else:
        L.append("(no manifest windows overlap the replayed set)")
    L.append("")
    L.append(f"## Manifest-only windows (no replay tape): {len(sc['manifest_only'])}")
    for w in sc["manifest_only"][:30]:
        L.append(f"- {w.get('date')} {w.get('symbol')} {w.get('ross_action')} "
                 f"{fmt_money(w.get('ross_net_usd')) if w.get('ross_net_usd') is not None else ''} "
                 f"({w.get('pnl_confidence')})")
    L.append("")
    L.append("## Failed / skipped windows")
    for r in sc["errors"]:
        L.append(f"- {r.get('day')} {r.get('symbol')}: {r.get('status')} "
                 f"(exit={r.get('exit_code')}) log={r.get('log')}")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--library-manifest", default=None,
                    help="window_manifest.json (for the library-size denominator)")
    ap.add_argument("--db", default=None, help="read-only prod URL for golden quote paths")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    args = ap.parse_args()

    with open(args.meta, "r", encoding="utf-8") as f:
        meta = json.load(f)
    ok, bad = load_results(args.results)
    manifest = None
    if args.manifest and os.path.exists(args.manifest):
        with open(args.manifest, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    library = 0
    if args.library_manifest and os.path.exists(args.library_manifest):
        with open(args.library_manifest, "r", encoding="utf-8") as f:
            library = len(json.load(f).get("windows", []))

    conn = None
    if args.db:
        import psycopg2

        conn = psycopg2.connect(args.db)
        conn.autocommit = True  # read-only usage

    trades_by_key: dict[str, list[dict]] = {}
    label_a_rows: list[dict] = []
    for r in ok:
        trades = pair_round_trips(r.get("fills") or [])
        attach_fill_times(r, trades)
        for t in trades:
            wt = within_trade_metrics(conn, r, t)
            if wt:
                t["label_a"] = wt
                pe = post_exit(conn, r, t)
                if pe:
                    t["post_exit"] = pe
                label_a_rows.append(t)
        trades_by_key[r["key"]] = trades
        r["window_capture"] = window_capture(r, trades)
        r["trades"] = trades
    if conn is not None:
        conn.close()

    all_trades = [t for ts in trades_by_key.values() for t in ts]
    pnls = [t["pnl_usd"] for t in all_trades]
    per_setup: dict[str, dict] = {}
    for t in all_trades:
        fam = _family(t.get("trigger_reason"))
        agg = per_setup.setdefault(fam, {"n": 0, "net": 0.0, "wins": 0, "losses": 0})
        agg["n"] += 1
        agg["net"] = round(agg["net"] + t["pnl_usd"], 2)
        agg["wins"] += 1 if t["pnl_usd"] > 0 else 0
        agg["losses"] += 1 if t["pnl_usd"] < 0 else 0

    captures = [t["label_a"]["capture_ratio"] for t in label_a_rows
                if t.get("label_a", {}).get("capture_ratio") is not None]
    givebacks = [t["label_a"]["giveback_frac"] for t in label_a_rows
                 if t.get("label_a", {}).get("giveback_frac") is not None]
    oc: dict[str, int] = {}
    for t in label_a_rows:
        c = (t.get("post_exit") or {}).get("outcome_class")
        if c:
            oc[c] = oc.get(c, 0) + 1
    label_a = {"n": len(label_a_rows),
               "clamped": sum(1 for t in label_a_rows if t["label_a"].get("fill_clamped")),
               "mean_capture": (statistics.mean(captures) if captures else 0.0),
               "mean_giveback": (statistics.mean(givebacks) if givebacks else 0.0),
               "outcome_classes": oc}

    crossref, manifest_only = ross_crossref(manifest, ok, trades_by_key)
    stats = stage0_stats(pnls)
    sc = {"schema": "chili.golden_replay_scorecard.v1",
          "generated_at": datetime.now().isoformat(timespec="seconds"),
          "meta": meta,
          "coverage": {"attempted": len(ok) + len(bad), "ok": len(ok),
                       "failed": len(bad), "library": library},
          "windows": ok, "errors": [{k: r.get(k) for k in
                                     ("key", "symbol", "day", "status", "exit_code", "log")}
                                    for r in bad],
          "per_setup": dict(sorted(per_setup.items(), key=lambda kv: -kv[1]["n"])),
          "expectancy": stats, "expectancy_gates": stage0_gates(stats),
          "label_a": label_a, "crossref": crossref, "manifest_only": manifest_only}

    os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(sc, f, indent=1, default=str)
    md = render_markdown(sc)
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[scorecard] {len(ok)} ok / {len(bad)} failed -> {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
