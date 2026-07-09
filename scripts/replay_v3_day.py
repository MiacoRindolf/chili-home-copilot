"""Replay v3 P5 — the DAY RUNNER (the operator's ongoing improvement instrument).

The ONE canonical whole-day replay entrypoint. Orchestrates multi-symbol replays of a
trading day through the STEP-2 realistic fill model + the STEP-3 parity harness, and emits:

  * a per-trade table (symbol, entry/exit, $, fill-confidence),
  * a day PnL BAND — low / point / high via the conservative & optimistic fill modes,
  * a COMPARISON block vs the ACTUAL recorded day (trades taken / missed / avoided).

It reads the live ``chili`` DB READ-ONLY (never writes a trading table): for each symbol it
exports the recorded session in-memory (the same shape ``export_replay_v3_parity_fixtures``
writes to disk), then runs mode-(i) harness parity (the fill band) + mode-(ii) current-code
counterfactual (the expected-divergence note).

Usage:
  python scripts/replay_v3_day.py --date 2026-07-02
  python scripts/replay_v3_day.py --date 2026-07-02 --symbols IPW,CELZ --mode conservative
  python scripts/replay_v3_day.py --date 2026-07-02 --json

Modes: ``--mode conservative|optimistic|band`` (default ``band`` = report both as low/high).
``--code current`` is accepted for parity with the design CLI (the counterfactual mode-ii is
always reported); the recorded-day comparison uses the recorded (as-ran) code.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from app.services.trading.momentum_neural.replay_mock_broker import FillMode  # noqa: E402
from app.services.trading.momentum_neural.replay_parity import (  # noqa: E402
    ParityFixture,
    replay_counterfactual_mode_ii,
    replay_parity_mode_i,
)

# Reuse the on-disk exporter's session-builder so the day runner and the fixture test share
# ONE recorded-session extraction (no divergent readers).
from scripts.export_replay_v3_parity_fixtures import (  # noqa: E402
    LOAD_BEARING,
    export_session,
)

DEFAULT_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili"


def _connect(url: str):
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _day_sessions_that_traded(cur, date: str) -> list[dict]:
    """The day's live sessions that TOOK a trade (a ``live_entry_filled``) — the auto symbol
    set. Each is a real recorded entry→exit we can replay for its PnL band."""
    cur.execute(
        "SELECT DISTINCT s.id, s.symbol FROM trading_automation_sessions s "
        "JOIN trading_automation_events e ON e.session_id=s.id "
        "  AND e.event_type='live_entry_filled' "
        "WHERE s.mode='live' AND s.created_at::date=%s "
        "ORDER BY s.symbol, s.id",
        (date,),
    )
    return [{"session_id": int(r[0]), "symbol": str(r[1])} for r in cur.fetchall()]


def _day_armed_symbols(cur, date: str) -> list[str]:
    """Every symbol ARMED on the day (took a trade or not) — for the missed/avoided compare."""
    cur.execute(
        "SELECT DISTINCT symbol FROM trading_automation_sessions "
        "WHERE mode='live' AND created_at::date=%s ORDER BY symbol",
        (date,),
    )
    return [str(r[0]) for r in cur.fetchall()]


def _fixture_from_session(cur, session_id: int, symbol: str, date: str) -> ParityFixture:
    """Build a ParityFixture in-memory from a live session (reusing export_session)."""
    data = export_session(cur, session_id, {"symbol": symbol, "date": date}, max_tape=600)
    # export_session returns the JSON-shaped dict; ParityFixture.load reads a path, so
    # construct the dataclass directly from the dict.
    return ParityFixture(
        session_id=data["session_id"],
        symbol=data["symbol"],
        date=data.get("date"),
        note=data.get("note"),
        recorded_final_state=str(data.get("recorded_final_state", "")),
        live_eligible_at_utc=data.get("live_eligible_at_utc"),
        recorded_events=list(data.get("recorded_events", [])),
        tape=list(data.get("tape", [])),
        tape_meta=dict(data.get("tape_meta", {})),
    )


def run_day(date: str, *, symbols: list[str] | None, database_url: str) -> dict:
    conn = _connect(database_url)
    cur = conn.cursor()
    try:
        traded = _day_sessions_that_traded(cur, date)
        armed = _day_armed_symbols(cur, date)
        if symbols:
            want = {s.strip().upper() for s in symbols}
            traded = [t for t in traded if t["symbol"].upper() in want]

        per_trade: list[dict] = []
        for t in traded:
            fx = _fixture_from_session(cur, t["session_id"], t["symbol"], date)
            cons = replay_parity_mode_i(fx, fill_mode=FillMode.CONSERVATIVE)
            opt = replay_parity_mode_i(fx, fill_mode=FillMode.OPTIMISTIC)
            diff = replay_counterfactual_mode_ii(fx)
            entry_fill = fx.recorded_entry_fill or {}
            exit_fill = fx.recorded_exit_fill or {}
            per_trade.append({
                "session_id": t["session_id"],
                "symbol": t["symbol"],
                "recorded_final_state": fx.recorded_final_state,
                "trace_matches": cons.trace_matches,
                "recorded_trace": cons.recorded_trace,
                "sim_trace": cons.sim_trace,
                "diffs": cons.diffs,
                "recorded_entry_ts": entry_fill.get("ts"),
                "recorded_entry": cons.recorded_entry_price,
                "recorded_exit_ts": exit_fill.get("ts"),
                "recorded_exit": cons.recorded_exit_price,
                "recorded_pnl_usd": cons.recorded_pnl_usd,
                "sim_entry_conservative": cons.sim_entry_price,
                "sim_entry_optimistic": opt.sim_entry_price,
                "sim_exit_conservative": cons.sim_exit_price,
                "sim_exit_optimistic": opt.sim_exit_price,
                "sim_pnl_conservative": cons.sim_pnl_usd,
                "sim_pnl_optimistic": opt.sim_pnl_usd,
                "entry_in_envelope": cons.entry_within_recorded_envelope,
                "exit_in_envelope": cons.exit_within_recorded_envelope,
                "entry_broker_basis_bps": cons.entry_broker_basis_bps,
                "exit_broker_basis_bps": cons.exit_broker_basis_bps,
                "counterfactual_notes": diff.notes,
            })

        # ── day PnL band + recorded-day comparison ──
        def _sum(key: str) -> float:
            return sum(float(p[key]) for p in per_trade if p.get(key) is not None)

        recorded_total = _sum("recorded_pnl_usd")
        sim_low = _sum("sim_pnl_conservative")
        sim_high = _sum("sim_pnl_optimistic")
        sim_point = (sim_low + sim_high) / 2.0 if per_trade else 0.0

        traded_symbols = sorted({t["symbol"] for t in traded})
        armed_no_trade = sorted(set(armed) - set(traded_symbols))

        return {
            "date": date,
            "symbols_requested": symbols or "auto (armed+traded)",
            "armed_symbol_count": len(armed),
            "traded_session_count": len(traded),
            "per_trade": per_trade,
            "recorded_day_pnl_usd": round(recorded_total, 2),
            "replay_day_pnl_band_usd": {
                "low_conservative": round(sim_low, 2),
                "point": round(sim_point, 2),
                "high_optimistic": round(sim_high, 2),
            },
            "comparison_vs_actual": {
                "trades_taken_symbols": traded_symbols,
                "armed_but_no_trade_symbols": armed_no_trade,
                "note": (
                    "recorded = the day as it actually ran (recorded code). replay band = the "
                    "STEP-2 fill model over the recorded tape (fill-confidence band, low/high). "
                    "mode-ii counterfactual notes flag where CURRENT code (d718991) is expected "
                    "to diverge (e.g. IPW benches)."
                ),
            },
        }
    finally:
        conn.close()


def _print_report(report: dict) -> None:
    print(f"\n===== REPLAY v3 DAY RUNNER — {report['date']} =====")
    print(f"armed symbols: {report['armed_symbol_count']}   traded sessions: {report['traded_session_count']}")
    print("\nPER-TRADE:")
    hdr = f"  {'symbol':<8}{'sid':>7}  {'rec_entry':>10}{'rec_exit':>10}{'rec_pnl':>10}   {'sim_pnl_low':>12}{'sim_pnl_high':>13}  {'trace':>6}  fill"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for p in report["per_trade"]:
        fill = "in-book" if (p["entry_in_envelope"] and p["exit_in_envelope"]) else "OUT-of-book"
        def _n(v): return f"{v:.4f}" if isinstance(v, (int, float)) else "-"
        def _p(v): return f"{v:+.2f}" if isinstance(v, (int, float)) else "-"
        print(
            f"  {p['symbol']:<8}{p['session_id']:>7}  {_n(p['recorded_entry']):>10}{_n(p['recorded_exit']):>10}"
            f"{_p(p['recorded_pnl_usd']):>10}   {_p(p['sim_pnl_conservative']):>12}{_p(p['sim_pnl_optimistic']):>13}"
            f"  {'OK' if p['trace_matches'] else 'DIFF':>6}  {fill}"
        )
    band = report["replay_day_pnl_band_usd"]
    print("\nDAY PnL:")
    print(f"  recorded (as-ran)     : {report['recorded_day_pnl_usd']:+.2f} USD")
    print(f"  replay band  low      : {band['low_conservative']:+.2f} USD  (conservative fills)")
    print(f"               point    : {band['point']:+.2f} USD")
    print(f"               high     : {band['high_optimistic']:+.2f} USD  (optimistic fills)")
    comp = report["comparison_vs_actual"]
    print("\nCOMPARISON vs ACTUAL:")
    print(f"  trades taken   : {', '.join(comp['trades_taken_symbols']) or '(none)'}")
    print(f"  armed, no trade: {len(comp['armed_but_no_trade_symbols'])} symbols "
          f"({', '.join(comp['armed_but_no_trade_symbols'][:12])}{'…' if len(comp['armed_but_no_trade_symbols'])>12 else ''})")
    # surface any counterfactual divergence notes
    notes = [f"{p['symbol']}: {n}" for p in report["per_trade"] for n in p["counterfactual_notes"]
             if "bench" in n.lower() or "expected" in n.lower()]
    if notes:
        print("\nCOUNTERFACTUAL (current-code divergence, mode-ii):")
        for n in notes[:12]:
            print(f"  - {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay v3 whole-day runner")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--symbols", default=None, help="CSV; default = auto (the day's traded set)")
    ap.add_argument("--mode", default="band", choices=["conservative", "optimistic", "band"])
    ap.add_argument("--code", default="current", help="accepted for CLI parity; mode-ii always reported")
    ap.add_argument("--json", action="store_true", help="emit the raw report as JSON")
    ap.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    args = ap.parse_args()

    symbols = [s for s in args.symbols.split(",")] if args.symbols else None
    report = run_day(args.date, symbols=symbols, database_url=args.database_url)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
