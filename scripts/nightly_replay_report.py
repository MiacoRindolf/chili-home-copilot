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
# ⚠️ Ang default ay dating D:\dev\chili-home-copilot -- ang Codex branch, na huling
# na-commit noong 2026-07-16. Ang TUMATAKBONG lane ay nasa E:\dev\wt-window2, kaya
# sinusuri ng ulat ang anim-na-linggong lumang kodigo (2026-08-27).
BUILD = os.environ.get("CHILI_REPLAY_BUILD", r"E:\dev\wt-window2")
# ⚠️ Ang default ay dating replay_window.py -- isang PURPOSE-BUILT na CLRO G4 exit
# A/B script (tingnan ang docstring nito) na ginamit bilang pangkalahatang driver.
# Sa run ng 2026-08-26 ay gumawa ito ng session para sa 4 sa 5 mover na may ZERO
# event. Ang kasalukuyang FSM window driver ay replay_v3_fsm_window.py -- parehong
# env contract (SYMBOL/WIN_START/WIN_END/OHLCV_START/TICK_STRIDE/EQUITY/RISK).
DRIVER = os.environ.get(
    "CHILI_REPLAY_DRIVER",
    str(Path(BUILD) / "scripts" / "replay_v3_fsm_window.py"),
)
PYEXE = sys.executable
OUT_DIR = Path(r"D:\CHILI-Docker\chili-data\nightly_replay")
TOP_N = int(os.environ.get("NIGHTLY_REPLAY_TOP_N", "5"))
MIN_TICKS = 20_000          # kailangan ng totoong tape para may masuri
MIN_MOVE_PCT = 20.0         # Ross-class movers lang
# ⚠️ Ang window ay dating nakapirming 4 na oras (11:30-15:30Z). Ang replay_v3 ay
# nasa pagitan ng 30-45 minutong takbo para sa isang 60-minutong window, kaya ang
# BAWAT simbolo ay lalampas sa 30-minutong timeout. Ang default ngayon ay ang ORAS
# NG OPEN -- doon nangyayari ang ignition (ang DAIC entry ng 2026-08-26 ay 13:16Z)
# -- at ang lahat ay ma-o-override na.
WIN_START_UTC = os.environ.get("NIGHTLY_REPLAY_WIN_START", "13:00:00")  # 09:00 ET
WIN_END_UTC = os.environ.get("NIGHTLY_REPLAY_WIN_END", "14:00:00")      # 10:00 ET
# Ang v3 ay mas mabagal kaysa sa lumang driver at ang CRE (841k tick) ay lumampas
# na sa 1800s kahit sa mabilis na driver.
REPLAY_TIMEOUT_S = int(os.environ.get("NIGHTLY_REPLAY_TIMEOUT_S", "3600"))
TICK_STRIDE = os.environ.get("NIGHTLY_REPLAY_TICK_STRIDE", "8")
# ⚠️ ITO ANG NAGPAPATAY SA ULAT. Kapag OHLCV_START == WIN_START ay nagsisimula ang
# frame sa ZERO bar, kaya lahat ay `insufficient_bars` at walang trigger na
# pumuputok kailanman. NASUKAT 2026-08-26: insufficient_bars x1328, 0 fill sa 5
# mover -- isang ARTIFACT NG HARNESS, hindi natuklasan tungkol sa lane. Ang v3 ay
# may FRAME_WARMUP_MIN na naghihila ng mas malawak na tick load para sa OHLCV seam
# LAMANG (ang mirror at ang grid ay nananatiling nakatali sa window).
FRAME_WARMUP_MIN = os.environ.get("NIGHTLY_REPLAY_FRAME_WARMUP_MIN", "7200")


def _trading_day_et() -> str:
    """Ang petsa ng KALAKALAN sa US/Eastern, hindi ang petsa sa UTC.

    ⚠️ Ang task ay tumatakbo sa 17:30 PT = 00:30Z, kaya LAGPAS NA ang UTC date at
    hinahanap ng ulat ang movers ng BUKAS -- laging "0 qualifying movers"
    (napatunayan 2026-08-27: ET 08-26 21:25 laban sa UTC 08-27 01:25). Ang araw ng
    kalakalan ay tinutukoy ng ET, kaya doon dapat magmula ang petsa.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        # Fail-safe: ET ay UTC-4/-5, kaya ang pagbawas ng 5 oras ay hindi kailanman
        # umaabante nang lampas sa araw ng kalakalan.
        return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")


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
        "ARM": "on", "TICK_STRIDE": TICK_STRIDE, "PREPEND_OHLCV": "1",
        "FRAME_WARMUP_MIN": FRAME_WARMUP_MIN,
        "EQUITY": "100000", "RISK": "4000", "EXEC_FAMILY": "alpaca_spot",
    })
    _log(f"replay {sym} ({mover['up_pct']}% mover, {mover['ticks']} ticks)")
    try:
        p = subprocess.run([PYEXE, DRIVER], env=env, cwd=BUILD,
                           capture_output=True, text=True,
                           timeout=REPLAY_TIMEOUT_S)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return {"symbol": sym, "error": f"timeout_{REPLAY_TIMEOUT_S}s"}
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
    gates = _binding_gates(sym)
    return {"symbol": sym, "pnl": pnl, "fills": fills, "rejects": rejects,
            "gates": gates,
            "exit_code": p.returncode}


def _binding_gates(symbol: str) -> list[tuple[str, int, float]]:
    """Ang gate na TUMANGGI, mula sa pinakabagong replay session sa sink.

    ⚠️ ITO ANG FIELD NA MAHALAGA, at hindi ito ipinapakita ng ulat hanggang
    2026-08-27. Ang `payload_json->>'reason'` ang nagdadala ng `_trigger_reason`
    -- ang halagang kapares ng `_trigger_ok == False` sa sandaling kinuha ang wait
    branch. Ang `detector_rejects` ay isang telemetry-only na side-map na
    isinusulat LAMANG ng pullback ladder.

    NASUKAT 2026-08-26: para sa XPON (225 wait) at OLOX (74 wait) ang tumanggi ay
    ang 15m fallback leg na `momentum_volume_confirmation`
    (live_runner.py:29608) -- at ang call site na iyon ay HINDI KAILANMAN
    sumusulat sa `_reject_map`. Kaya kapag ang fallback leg ang tumanggi, ang
    `detector_rejects` ay HINDI KAYANG maglaman ng tunay na dahilan. Hindi ito
    paminsan-minsang nakaliligaw; sa dalawang session na iyon ay 100% itong mali:
    naiulat na `premarket_tickbreak_unconfirmed x102` gayong ang totoo ay
    `volume_below_1p5x_avg` sa 225 sa 225.
    """
    q = """
        WITH sess AS (
            SELECT max(id) AS sid FROM trading_automation_sessions WHERE symbol = %(s)s
        ),
        w AS (
            SELECT e.payload_json->>'reason' AS reason
            FROM trading_automation_events e, sess
            WHERE e.session_id = sess.sid AND e.event_type = 'live_entry_trigger_wait'
        )
        SELECT coalesce(reason, '(walang reason)') AS reason,
               count(*) AS n,
               100.0 * count(*) / NULLIF(sum(count(*)) OVER (), 0) AS pct
        FROM w GROUP BY 1 ORDER BY 2 DESC LIMIT 5
    """
    try:
        conn = psycopg2.connect(SINK)
        cur = conn.cursor()
        cur.execute(q, {"s": symbol})
        rows = cur.fetchall()
        conn.close()
        return [(r[0], int(r[1]), float(r[2] or 0.0)) for r in rows]
    except Exception as exc:
        _log(f"gate read failed for {symbol}: {exc}")
        return []


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
        -- NOTE: ang isang wait ay nag-e-emit ng ~3 detector reject, kaya ang raw
        -- na "x102" ay mukhang mayorya gayong halos kalahati lang ito ng mga
        -- wait. Ang render ang naghahati laban sa bilang ng WAIT, hindi sa
        -- kabuuan ng mga reject. (Walang porsyentong simbolo rito: binabasa iyon
        -- ng psycopg2 bilang placeholder at sasabog ang naka-pangalang params.)
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
    day = os.environ.get("NIGHTLY_REPLAY_DAY") or _trading_day_et()
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
            _gates = r.get("gates") or []
            _waits = sum(int(g[1]) for g in _gates) or 0
            if _gates:
                lines.append(
                    f"- **ANG GATE NA TUMANGGI** (`reason`, {_waits} wait):")
                for gate, n, pct in _gates:
                    lines.append(f"    - **{gate}** ×{n}  ({pct:.0f}%)")
            if r.get("rejects"):
                lines.append(
                    "- Upstream na detalye ng detector (`detector_rejects` — BAKIT "
                    "hindi pumutok ang pullback ladder, kaya ang fallback leg ang "
                    "nagpasya; HINDI ito ang gate sa itaas):")
                for rej, n in r["rejects"]:
                    _p = (100.0 * n / _waits) if _waits else 0.0
                    lines.append(f"    - {rej} ×{n}  ({_p:.0f}% ng wait)")
        lines.append("")
    lines.append(f"**TOTAL replay PnL sa {len(movers)} movers: {total:+.2f} USD**")
    lines.append("")
    lines.append(
        "_Basahin: ang malaking mover na maliit/negatibo ang replay PnL + isang "
        "nangingibabaw na **GATE** = ang susunod na susuriin. Ang gate ang "
        "tumanggi; ang detector detail sa ilalim nito ay nagsasabi kung bakit "
        "hindi pumutok ang ladder. Hanggang 2026-08-27 ay ang detalye LAMANG ang "
        "ipinapakita ng ulat, at para sa XPON/OLOX noong 08-26 ay 100% itong "
        "mali -- naiulat na `premarket_tickbreak_unconfirmed` gayong ang totoong "
        "gate ay `volume_below_1p5x_avg` sa 225 sa 225._")
    report = OUT_DIR / f"{day}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    _log(f"report: {report}")


if __name__ == "__main__":
    main()
