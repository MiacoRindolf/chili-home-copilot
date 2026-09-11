#!/usr/bin/env python
"""ROSSBENCH SCORECARD v2 -- three numbers, because one of them cannot see what we never did.

Promoted from the 2026-09-08 session scratchpad (``scorecard_v2.py``, the tool behind the
planner [E] note "ANG LADDER MISMO AY KULANG") for the [E] canon A/B of 2026-09-11, and
extended with the two splits that A/B needs: WHICH exit took each leg, and WHICH cap sized it.

EXIT CAPTURE (what the ladder always reported)::

    realised / (peak_inside_the_leg - avg_entry) * shares

Its denominator is OUR entry at OUR size -- "having taken this leg, how much did we keep".
Blind to the dip we did not buy and the size we did not take.

MOVE CAPTURE fixes the denominator to something we do not control::

    realised / (window_high - window_low) * peak_shares_we_carried

Entering late costs here, exiting early costs here, NOT ADDING costs here.

PARTICIPATION: adds per leg and partial exits per leg. Zero means all-or-nothing, and no exit
rule can fix that.

THE [E] SPLITS
  * EXIT REASON per leg -- the ``reason`` on the ``live_exit_filled`` that closed it:
    ``target``, ``trail_stop``, ``tick_deadman_stop`` (the tick deadman), ``tape_accel_rollover``
    (G, sell into the spike), ``tape_sellers_took_it`` (D, the since-high verdict), ``stop``,
    ``bailout*`` ... reported raw, never re-labelled.
  * SIZING BINDING per entry -- ``sizing.notional_ceiling_source`` / ``_binding``, the frozen
    crossover, and ``risk_mults.realized_over_base`` (= risk_usd / base_max_loss, what the [27]
    multiplier stack left of the loss budget).
  * VERDICT HISTOGRAM -- ``live_exit_verdict_armed / _fired / _unreadable`` and
    ``live_tick_deadman_exit`` straight from the receipt's ``event_histogram``: whether the
    doctrine's exits could decide at all.

DE-DUPLICATION. Several manifest rows of one symbol-day can replay the SAME trades (VEEE
2026-07-13 ml1/ml2/ml3: the three windows are one window). ``--same-window`` names such a
group; it counts ONCE, as the mean of its members per arm, and the report says whether the
members were fill-identical.

READ-ONLY. The only database read is the tape's max/min price inside a leg / a window, symbol-
and time-bounded (``ix_iqfeed_trades_sym_at``), on ``--tape-dsn`` (the bench's ``--source``).

    python scripts/rossbench_scorecard_v2.py \
        --arm A=D:/CHILI-Docker/chili-data/rossbench/E_ab_A_* \
        --arm B=D:/CHILI-Docker/chili-data/rossbench/E_ab_B_* \
        --tape-dsn postgresql://chili:chili@localhost:5433/chili_hydrated \
        --losers DSY,EZRA,INLF,PPBT,PPCB \
        --same-window "VEEE@RZbM0qXOFbc-ml1_2026-07-13,VEEE@RZbM0qXOFbc-ml2_2026-07-13,VEEE@RZbM0qXOFbc-ml3_2026-07-13" \
        --json-out scorecard.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

VERDICT_EVENTS = (
    "live_exit_verdict_armed",
    "live_exit_verdict_fired",
    "live_exit_verdict_unreadable",
    "live_tick_deadman_exit",
)

#: Display order for the exit-reason split; anything else is listed after, raw.
REASON_ORDER = (
    "target", "trail_stop", "tick_deadman_stop", "tape_accel_rollover",
    "tape_sellers_took_it", "stop", "bailout",
)


def _naive_utc(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        t = v
    else:
        s = str(v).strip().replace("Z", "+00:00")
        if not s or s == "None":
            return None
        try:
            t = datetime.fromisoformat(s)
        except ValueError:
            return None
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


# ─── legs ────────────────────────────────────────────────────────────────────────────────

def legs(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """FIFO round trips from the receipt's fills, carrying add / partial counts."""
    out: list[dict[str, Any]] = []
    pos = cost = sh = 0.0
    t0 = None
    adds = parts = 0
    realised = 0.0
    fills = sorted((f for f in (receipt.get("fills") or []) if f.get("px") is not None),
                   key=lambda x: str(x.get("ts")))
    for f in fills:
        q, px = float(f.get("qty") or 0.0), float(f["px"])
        side = str(f.get("side") or "").lower()
        if side in ("buy", "bid", "long"):
            if pos <= 1e-9:
                t0, sh, cost, adds, parts, realised = f.get("ts"), 0.0, 0.0, 0, 0, 0.0
            else:
                adds += 1
            pos += q
            cost += q * px
            sh += q
        else:
            avg = cost / pos if pos else 0.0
            realised += q * (px - avg)
            cost -= q * avg
            pos -= q
            if pos > 1e-9:
                parts += 1
            else:
                # ``pnl`` is the WHOLE leg's realised P&L (every partial + the close). The
                # scratchpad v2 kept only the closing sale's -- harmless while partial/leg was
                # 0.00, wrong the first time a partial fires.
                out.append({"t0": t0, "t1": f.get("ts"), "sh": sh, "avg": avg,
                            "pnl": realised, "adds": adds, "parts": parts, "exit_px": px})
                pos = cost = 0.0
    return out


def _exit_events(receipt: Mapping[str, Any]) -> list[tuple[datetime, dict]]:
    out = []
    for e in receipt.get("events") or []:
        if e.get("event_type") == "live_exit_filled":
            t = _naive_utc(e.get("ts"))
            if t is not None:
                out.append((t, e.get("payload") or {}))
    return sorted(out, key=lambda x: x[0])


def attach_exit_reasons(receipt: Mapping[str, Any], leg_rows: list[dict[str, Any]]) -> None:
    """The closing ``live_exit_filled`` of each leg: the nearest in time at/after the closing
    fill (ties: same fill price first). ``reason`` stays raw; unmatched -> ``unrecorded``."""
    evs = _exit_events(receipt)
    used: set[int] = set()
    for leg in leg_rows:
        t1 = _naive_utc(leg.get("t1"))
        best, best_key = None, None
        for i, (t, pl) in enumerate(evs):
            if i in used or t1 is None:
                continue
            dt = (t - t1).total_seconds()
            if dt < -1.0:          # an exit event cannot precede its fill by more than a tick
                continue
            same_px = pl.get("fill_price") is not None and abs(float(pl["fill_price"]) - float(leg["exit_px"])) < 1e-9
            key = (0 if same_px else 1, abs(dt))
            if best_key is None or key < best_key:
                best, best_key = i, key
        if best is not None:
            used.add(best)
            leg["reason"] = str(evs[best][1].get("reason") or "unrecorded")
        else:
            leg["reason"] = "unrecorded"


def _entry_sizing(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in receipt.get("events") or []:
        if e.get("event_type") != "live_entry_submitted":
            continue
        pl = e.get("payload") or {}
        sz = pl.get("sizing") if isinstance(pl.get("sizing"), dict) else {}
        rm = pl.get("risk_mults") if isinstance(pl.get("risk_mults"), dict) else {}
        out.append({
            "ceiling_source": sz.get("notional_ceiling_source"),
            "ceiling_binding": sz.get("notional_ceiling_binding"),
            "ceiling_usd": sz.get("notional_ceiling_usd"),
            "frozen_crossover": sz.get("notional_ceiling_frozen_crossover_stop_pct"),
            "risk_usd": sz.get("risk_usd"),
            "base_max_loss": rm.get("base_max_loss"),
            "realized_over_base": rm.get("realized_over_base"),
            "mult_binding": rm.get("binding"),
        })
    return out


# ─── tape ────────────────────────────────────────────────────────────────────────────────

class Tape:
    """max/min price in [a, b] for one symbol -- bounded by symbol + time."""

    SQL = ("SELECT max(price), min(price) FROM iqfeed_trade_ticks "
           "WHERE symbol = %s AND observed_at >= %s AND observed_at <= %s AND price > 0")

    def __init__(self, dsn: str, sources: Optional[Sequence[str]] = None):
        import psycopg2

        self._cn = psycopg2.connect(dsn, options="-c statement_timeout=60000")
        self._cn.set_session(readonly=True)
        self._sources = list(sources or [])
        self._cache: dict[tuple, tuple] = {}

    def hilo(self, sym: str, a: Any, b: Any) -> tuple[Optional[float], Optional[float]]:
        ta, tb = _naive_utc(a), _naive_utc(b)
        if ta is None or tb is None:
            return None, None
        key = (sym, ta, tb)
        if key not in self._cache:
            sql, args = self.SQL, [sym, ta, tb]
            if self._sources:
                sql += " AND source = ANY(%s)"
                args.append(self._sources)
            cur = self._cn.cursor()
            cur.execute(sql, args)
            hi, lo = cur.fetchone()
            cur.close()
            self._cn.rollback()
            self._cache[key] = (float(hi) if hi is not None else None,
                                float(lo) if lo is not None else None)
        return self._cache[key]


# ─── score ───────────────────────────────────────────────────────────────────────────────

def load_runs(pattern: str) -> dict[str, dict[str, Any]]:
    """case_dirname -> receipt, for every ``<bench>/<case>/<arm>/run.json`` under the glob."""
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(glob.glob(os.path.join(pattern, "*", "*", "run.json"))):
        case = os.path.basename(os.path.dirname(os.path.dirname(f)))
        with open(f, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["_path"] = f
        if case in out:
            raise SystemExit(f"two receipts for {case} under {pattern!r}: {out[case]['_path']} / {f}")
        out[case] = doc
    return out


def score_case(case: str, receipt: Mapping[str, Any], tape: Tape) -> dict[str, Any]:
    sym = case.split("@")[0].split("_")[0]
    env = receipt.get("env") or {}
    L = legs(receipt)
    attach_exit_reasons(receipt, L)
    exit_num = exit_den = 0.0
    for leg in L:
        hi, _lo = tape.hilo(sym, leg["t0"], leg["t1"])
        leg["leg_high"] = hi
        leg["exit_den"] = max((hi - leg["avg"]) * leg["sh"], 0.0) if hi is not None else None
        if leg["exit_den"] is not None:
            exit_num += leg["pnl"]
            exit_den += leg["exit_den"]
    wh, wl = tape.hilo(sym, env.get("WIN_START"), env.get("WIN_END"))
    peak_sh = max((x["sh"] for x in L), default=0.0)
    move_den = (wh - wl) * peak_sh if (wh is not None and wl is not None and wh > wl) else None
    hist = receipt.get("event_histogram") or {}
    sizing = _entry_sizing(receipt)
    nc = receipt.get("notional_ceiling") or {}
    pub = receipt.get("publication_clock") or {}
    return {
        "case": case,
        "symbol": sym,
        "pnl_usd": float(receipt.get("pnl_usd") or 0.0),
        "realised_usd": round(sum(x["pnl"] for x in L), 2),
        "legs": L,
        "n_legs": len(L),
        "adds": sum(x["adds"] for x in L),
        "partials": sum(x["parts"] for x in L),
        "exit_num": exit_num, "exit_den": exit_den,
        "move_num": sum(x["pnl"] for x in L) if move_den else 0.0,
        "move_den": move_den or 0.0,
        "window_high": wh, "window_low": wl, "peak_shares": peak_sh,
        "verdict_hist": {k: int(hist.get(k, 0) or 0) for k in VERDICT_EVENTS},
        "sizing": sizing,
        "frozen_ceiling_usd": nc.get("frozen_usd"),
        "frozen_ceiling_source": nc.get("source"),
        "frozen_crossover": nc.get("crossover_stop_pct"),
        "publication_clock": {k: pub.get(k) for k in ("recv_lag_s", "avail_lag_s", "clock_rows")},
        "tree_head": (receipt.get("tree") or {}).get("head"),
        "fills_fingerprint": json.dumps([(f.get("ts"), f.get("side"), f.get("px"), f.get("qty"))
                                         for f in receipt.get("fills") or []], default=str),
    }


def _pct(num: float, den: float) -> Optional[float]:
    return round(100.0 * num / den, 1) if den else None


def aggregate(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    cs = list(cases)
    L = [leg for c in cs for leg in c["legs"]]
    n = len(L)
    by_reason: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "num": 0.0, "den": 0.0})
    for leg in L:
        r = by_reason[leg.get("reason") or "unrecorded"]
        r["n"] += 1
        r["pnl"] += leg["pnl"]
        if leg.get("exit_den") is not None:
            r["num"] += leg["pnl"]
            r["den"] += leg["exit_den"]
    sizing = [s for c in cs for s in c["sizing"]]
    rob = [float(s["realized_over_base"]) for s in sizing if s.get("realized_over_base") is not None]
    return {
        "cases": len(cs),
        "pnl_usd": round(sum(c["pnl_usd"] for c in cs), 2),
        "realised_usd": round(sum(c["realised_usd"] for c in cs), 2),
        "legs": n,
        "exit_capture_pct": _pct(sum(c["exit_num"] for c in cs), sum(c["exit_den"] for c in cs)),
        "move_capture_pct": _pct(sum(c["move_num"] for c in cs), sum(c["move_den"] for c in cs)),
        "add_per_leg": round(sum(c["adds"] for c in cs) / n, 2) if n else None,
        "partial_per_leg": round(sum(c["partials"] for c in cs) / n, 2) if n else None,
        "exit_reasons": {k: {"n": v["n"], "pnl_usd": round(v["pnl"], 2),
                             "exit_capture_pct": _pct(v["num"], v["den"])}
                         for k, v in sorted(by_reason.items(), key=lambda kv: _reason_rank(kv[0]))},
        "verdict_hist": dict(sum((Counter(c["verdict_hist"]) for c in cs), Counter())),
        "ceiling_sources": dict(Counter(str(s.get("ceiling_source")) for s in sizing)),
        "ceiling_bindings": dict(Counter(str(s.get("ceiling_binding")) for s in sizing)),
        "frozen_crossovers": sorted({c["frozen_crossover"] for c in cs if c["frozen_crossover"] is not None}),
        "risk_over_base_mean": round(sum(rob) / len(rob), 4) if rob else None,
        "entries_sized": len(sizing),
    }


def _reason_rank(reason: str) -> tuple:
    for i, r in enumerate(REASON_ORDER):
        if reason == r or reason.startswith(r):
            return (i, reason)
    return (len(REASON_ORDER), reason)


def dedupe(cases: dict[str, dict[str, Any]], groups: Sequence[Sequence[str]]) -> tuple[float, list[dict]]:
    """P&L with each ``--same-window`` group counted ONCE (its mean)."""
    seen: set[str] = set()
    total = 0.0
    notes = []
    for g in groups:
        members = [m for m in g if m in cases]
        if not members:
            continue
        pnls = [cases[m]["pnl_usd"] for m in members]
        identical = len({cases[m]["fills_fingerprint"] for m in members}) == 1
        total += sum(pnls) / len(pnls)
        seen.update(members)
        notes.append({"group": members, "pnl_each": pnls, "counted_as": round(sum(pnls) / len(pnls), 2),
                      "fill_identical": identical})
    total += sum(c["pnl_usd"] for k, c in cases.items() if k not in seen)
    return round(total, 2), notes


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return str(v)


def render(result: Mapping[str, Any]) -> str:
    out: list[str] = []
    arms = list(result["arms"])
    out.append("## Headline (per arm)\n")
    out.append("| arm | set | cases | legs | P&L raw | P&L dedup | EXIT cap | MOVE cap | add/leg | partial/leg | risk/base | ceiling source | frozen crossover |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for arm in arms:
        a = result["arms"][arm]
        for key in ("all", "winners", "losers"):
            g = a[key]
            out.append(
                f"| {arm} | {key} | {g['cases']} | {g['legs']} | {_fmt(g['pnl_usd'])} | "
                f"{_fmt(a['dedup'][key]['pnl_usd']) if key in a['dedup'] else '-'} | "
                f"{_fmt(g['exit_capture_pct'], 1)}% | {_fmt(g['move_capture_pct'], 1)}% | "
                f"{_fmt(g['add_per_leg'])} | {_fmt(g['partial_per_leg'])} | {_fmt(g['risk_over_base_mean'], 3)} | "
                f"{g['ceiling_sources']} | {g['frozen_crossovers']} |")
    out.append("\n## Exit-reason split (per arm, all cases)\n")
    out.append("| arm | reason | legs | P&L | EXIT cap |")
    out.append("|---|---|---:|---:|---:|")
    for arm in arms:
        for reason, r in result["arms"][arm]["all"]["exit_reasons"].items():
            out.append(f"| {arm} | {reason} | {r['n']} | {_fmt(r['pnl_usd'])} | {_fmt(r['exit_capture_pct'], 1)}% |")
    out.append("\n## Verdict histogram (per arm)\n")
    out.append("| arm | " + " | ".join(VERDICT_EVENTS) + " |")
    out.append("|---|" + "---:|" * len(VERDICT_EVENTS))
    for arm in arms:
        h = result["arms"][arm]["all"]["verdict_hist"]
        out.append(f"| {arm} | " + " | ".join(str(h.get(k, 0)) for k in VERDICT_EVENTS) + " |")
    out.append("\n## Per case\n")
    out.append("| case | " + " | ".join(f"{a} P&L | {a} legs | {a} EXIT | {a} exits" for a in arms) + " |")
    out.append("|---|" + "---:|---:|---:|---|" * len(arms))
    all_cases = sorted({c for a in arms for c in result["arms"][a]["cases"]})
    for case in all_cases:
        cells = []
        for a in arms:
            c = result["arms"][a]["cases"].get(case)
            if c is None:
                cells.append("- | - | - | -")
                continue
            reasons = dict(Counter(leg.get("reason") for leg in c["legs"]))
            cells.append(f"{_fmt(c['pnl_usd'])} | {c['n_legs']} | {_fmt(_pct(c['exit_num'], c['exit_den']), 1)}% | {reasons}")
        out.append(f"| {case} | " + " | ".join(cells) + " |")
    if result.get("dedup_notes"):
        out.append("\n## De-duplication\n")
        for arm, notes in result["dedup_notes"].items():
            for n in notes:
                out.append(f"- {arm}: {n['group']} -> counted once as {n['counted_as']} "
                           f"(each {n['pnl_each']}; fill-identical: {n['fill_identical']})")
    return "\n".join(out) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="rossbench_scorecard_v2", description=__doc__.splitlines()[0])
    ap.add_argument("--arm", action="append", required=True, metavar="NAME=GLOB",
                    help="bench output directories (glob) for one arm; repeat per arm")
    ap.add_argument("--tape-dsn", required=True, help="the bench's --source (read-only)")
    ap.add_argument("--tape-sources", default="",
                    help="comma list: restrict the hi/lo read to these tape sources")
    ap.add_argument("--losers", default="", help="comma list of symbols that were Ross LOSERS")
    ap.add_argument("--same-window", action="append", default=[],
                    help="comma list of case dirnames that are ONE window (counted once)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    losers = {s.strip().upper() for s in args.losers.split(",") if s.strip()}
    groups = [[m.strip() for m in g.split(",") if m.strip()] for g in args.same_window]
    tape = Tape(args.tape_dsn, [s for s in args.tape_sources.split(",") if s.strip()])
    result: dict[str, Any] = {"arms": {}, "dedup_notes": {}, "losers": sorted(losers), "same_window": groups}
    for spec in args.arm:
        name, _, pattern = spec.partition("=")
        runs: dict[str, dict[str, Any]] = {}
        for pat in sorted(glob.glob(pattern)) or [pattern]:
            for case, doc in load_runs(pat).items():
                if case in runs:
                    raise SystemExit(f"arm {name}: {case} appears in two bench directories")
                runs[case] = doc
        scored = {case: score_case(case, doc, tape) for case, doc in runs.items()}
        win = {k: v for k, v in scored.items() if v["symbol"].upper() not in losers}
        los = {k: v for k, v in scored.items() if v["symbol"].upper() in losers}
        dd_all, notes = dedupe(scored, groups)
        dd_win, _ = dedupe(win, groups)
        dd_los, _ = dedupe(los, groups)
        result["arms"][name] = {
            "cases": {k: {kk: vv for kk, vv in v.items() if kk != "fills_fingerprint"} for k, v in scored.items()},
            "all": aggregate(scored.values()),
            "winners": aggregate(win.values()),
            "losers": aggregate(los.values()),
            "dedup": {"all": {"pnl_usd": dd_all}, "winners": {"pnl_usd": dd_win}, "losers": {"pnl_usd": dd_los}},
            "tree_heads": sorted({str(v["tree_head"]) for v in scored.values()}),
        }
        result["dedup_notes"][name] = notes
    text = render(result)
    sys.stdout.write(text)
    if args.json_out:
        with open(args.json_out, "w", newline="\n", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        with open(os.path.splitext(args.json_out)[0] + ".md", "w", newline="\n", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
