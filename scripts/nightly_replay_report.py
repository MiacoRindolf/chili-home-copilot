"""Nightly auto-replay counterfactual (2026-07-10, greenlit #2).

Ginagawang AWTOMATIKO ang mano-manong find-gap loop ng 07-09: tuwing gabi pagkasara,
i-replay ang top movers ng araw (recorded IQFeed tape) sa KASALUKUYANG code gamit ang
FSM replay harness, tapos isulat ang "missed capture report" — per mover: ang replay
PnL, ang mga entries/exits, at ang top binding gate-rejects (alin ang pumigil, gaano
kadalas). Ang report ang nagtuturo kung aling gate ang susunod na titingnan.

Takbo: host python (chili-env), naka-schedule daily pagkasara (CHILI-Nightly-Replay).
Sink: chili_replay2_test (ang dedicated replay DB; isang replay kada pagkakataon).
Output: D:\\CHILI-Docker\\chili-data\\nightly_replay\\YYYY-MM-DD.md
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

PROD = "postgresql://chili:chili@localhost:5433/chili"
SINK = "postgresql://chili:chili@localhost:5433/chili_replay2_test"
BUILD = os.environ.get("CHILI_REPLAY_BUILD", r"D:\dev\chili-home-copilot")
DRIVER = os.environ.get(
    "CHILI_REPLAY_DRIVER",
    str(Path(BUILD) / "scripts" / "replay_window.py"),
)
PYEXE = sys.executable
OUT_DIR = Path(r"D:\CHILI-Docker\chili-data\nightly_replay")
TOP_N = int(os.environ.get("NIGHTLY_REPLAY_TOP_N", "5"))
MIN_TICKS = 20_000          # kailangan ng totoong tape para may masuri
MIN_MOVE_PCT = 20.0         # Ross-class movers lang
WIN_START_UTC = "11:30:00"  # 07:30 ET — ang discovery window
WIN_END_UTC = "15:30:00"    # 11:30 ET — hanggang matapos ang umaga


def _log(msg: str) -> None:
    print(f"[nightly_replay] {datetime.now():%H:%M:%S} {msg}", flush=True)


def top_movers(day: str) -> list[dict]:
    """Ang top movers ng araw MULA SA SARILING TAPE (walang look-ahead sa labas ng
    araw): per symbol na may sapat na ticks sa window, ang intraday move% mula sa
    unang presyo hanggang session high. BRIN-friendly (observed_at range muna)."""
    q = """
        WITH day_ticks AS (
            SELECT symbol, price, observed_at
            FROM iqfeed_trade_ticks
            WHERE observed_at >= %(a)s AND observed_at < %(b)s AND price > 0
              AND symbol NOT LIKE '%%-USD'
        ), agg AS (
            SELECT symbol, count(*) AS n, min(observed_at) AS first_at,
                   (array_agg(price ORDER BY observed_at ASC))[1] AS first_px,
                   max(price) AS hi, min(price) AS lo
            FROM day_ticks GROUP BY symbol
        )
        SELECT symbol, n, first_at, first_px, hi, lo,
               round(((hi - first_px) / first_px * 100)::numeric, 1) AS up_pct
        FROM agg
        WHERE n >= %(min_ticks)s AND first_px > 0
          AND (hi - first_px) / first_px * 100 >= %(min_move)s
        ORDER BY (hi - first_px) / first_px DESC
        LIMIT %(top_n)s
    """
    conn = psycopg2.connect(PROD)
    conn.set_session(readonly=True)
    try:
        cur = conn.cursor()
        cur.execute(q, {
            "a": f"{day} {WIN_START_UTC}", "b": f"{day} {WIN_END_UTC}",
            "min_ticks": MIN_TICKS, "min_move": MIN_MOVE_PCT, "top_n": TOP_N,
        })
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"symbol": r[0], "ticks": int(r[1]), "first_at": r[2],
         "first_px": float(r[3]), "hi": float(r[4]), "lo": float(r[5]),
         "up_pct": float(r[6])}
        for r in rows
    ]


def run_replay(day: str, mover: dict) -> dict:
    """Isang window replay sa kasalukuyang code; ibinabalik ang buod + gate rejects."""
    sym = mover["symbol"]
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": BUILD,
        "DATABASE_URL": PROD,
        "TEST_DATABASE_URL": SINK,
        "SYMBOL": sym,
        "WIN_START": f"{day}T{WIN_START_UTC}",
        "WIN_END": f"{day}T{WIN_END_UTC}",
        "OHLCV_START": f"{day}T{WIN_START_UTC}",
        "ARM": "on", "TICK_STRIDE": "8", "PREPEND_OHLCV": "1",
        "EQUITY": "100000", "RISK": "4000", "EXEC_FAMILY": "alpaca_spot",
    })
    _log(f"replay {sym} ({mover['up_pct']}% mover, {mover['ticks']} ticks)")
    try:
        p = subprocess.run([PYEXE, DRIVER], env=env, cwd=BUILD,
                           capture_output=True, text=True, timeout=1800)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return {"symbol": sym, "error": "timeout_30m"}
    pnl = None
    fills: list[str] = []
    for line in out.splitlines():
        ls = line.strip()
        if ls.startswith(("BUY ", "SELL ")):
            fills.append(ls)
        if "PnL =" in ls:
            try:
                pnl = float(ls.split("PnL =")[1].replace("USD", "").strip().replace("+", ""))
            except ValueError:
                pass
    rejects = _top_rejects(sym)
    return {"symbol": sym, "pnl": pnl, "fills": fills, "rejects": rejects,
            "exit_code": p.returncode}


def _top_rejects(symbol: str) -> list[tuple[str, int]]:
    """Top binding detector-rejects ng pinakabagong replay session sa sink."""
    q = """
        WITH sess AS (
            SELECT max(id) AS sid FROM trading_automation_sessions WHERE symbol = %(s)s
        )
        SELECT r.key || ':' || r.value AS reject, count(*)
        FROM trading_automation_events e,
             jsonb_each_text(e.payload_json->'detector_rejects') r,
             sess
        WHERE e.session_id = sess.sid AND e.event_type = 'live_entry_trigger_wait'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5
    """
    try:
        conn = psycopg2.connect(SINK)
        cur = conn.cursor()
        cur.execute(q, {"s": symbol})
        rows = cur.fetchall()
        conn.close()
        return [(r[0], int(r[1])) for r in rows]
    except Exception as exc:
        _log(f"reject read failed for {symbol}: {exc}")
        return []


def main() -> None:
    day = os.environ.get("NIGHTLY_REPLAY_DAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    movers = top_movers(day)
    _log(f"{day}: {len(movers)} qualifying movers")
    lines = [f"# Nightly replay counterfactual — {day}",
             f"(window {WIN_START_UTC}–{WIN_END_UTC} UTC, kasalukuyang code, "
             f"$100k/$4k risk, fair bars)", ""]
    total = 0.0
    for m in movers:
        r = run_replay(day, m)
        pnl = r.get("pnl")
        total += pnl or 0.0
        lines.append(f"## {m['symbol']} — +{m['up_pct']}% mover "
                     f"({m['first_px']:.2f} → hi {m['hi']:.2f})")
        if r.get("error"):
            lines.append(f"- ERROR: {r['error']}")
        else:
            lines.append(f"- Replay PnL: **{pnl if pnl is not None else 'n/a'}**"
                         f"  (fills: {len(r.get('fills') or [])})")
            for f in (r.get("fills") or [])[:10]:
                lines.append(f"    - {f}")
            if r.get("rejects"):
                lines.append("- Top binding rejects (bakit hindi/naantala ang entry):")
                for rej, n in r["rejects"]:
                    lines.append(f"    - {rej} ×{n}")
        lines.append("")
    lines.append(f"**TOTAL replay PnL sa {len(movers)} movers: {total:+.2f} USD**")
    lines.append("")
    lines.append("_Basahin: ang malaking mover na maliit/negatibo ang replay PnL + "
                 "isang nangingibabaw na reject = ang susunod na gate na susuriin._")
    report = OUT_DIR / f"{day}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    _log(f"report: {report}")


if __name__ == "__main__":
    main()
