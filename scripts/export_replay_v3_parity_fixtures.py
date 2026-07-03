"""Export recorded live-session fixtures for the Replay v3 PARITY test (STEP-3).

Reads (READ-ONLY) the live ``chili`` DB and writes compact JSON fixtures under
``tests/fixtures/replay_v3/`` so ``tests/test_replay_v3_parity.py`` runs on ``chili_test``
(or with no DB at all) — the parity regression must NOT depend on the live DB being up.

For each fixture session it captures:
  * the session row (symbol, mode, execution_family, final state, the confirm-time
    ``live_eligible_at_utc`` anchor from ``risk_snapshot_json``);
  * the ORDERED recorded transition trace — every ``trading_automation_events`` row
    (ts, event_type, and the load-bearing payload facts: fill price/qty, exit reason, pnl);
  * a DOWN-SAMPLED NBBO tape (``momentum_nbbo_spread_tape``) across the session window — the
    recorded quote path the mock broker fills against (capped to a manageable row count).

Usage:
  python scripts/export_replay_v3_parity_fixtures.py
  python scripts/export_replay_v3_parity_fixtures.py --sessions 9920,10397 --max-tape 400
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

# The two canonical parity fixtures (the operator's named sessions).
DEFAULT_SESSIONS = {
    9920: {"symbol": "CELZ", "date": "2026-06-30", "note": "+$40 ORB win"},
    10397: {"symbol": "IPW", "date": "2026-07-02", "note": "the -$58 IPW trade"},
}

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "replay_v3"

# Load-bearing transitions the parity gate asserts on (the ordered decision sequence).
LOAD_BEARING = (
    "live_arm_requested",
    "live_arm_confirmed",
    "live_watch_started",
    "live_entry_candidate_detected",
    "live_entry_submitted",
    "live_entry_filled",
    "live_partial_exit_filled",
    "live_bailout",
    "live_tape_accel_reversal_exit",
    "live_exit_filled",
    "live_cooldown_started",
    "live_cancelled",
    "live_recycled",
)


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _load_bearing_payload(event_type: str, payload: dict) -> dict:
    """Extract only the load-bearing facts from an event payload (keeps fixtures compact +
    stable): fill price/qty, exit reason, pnl, stop/target where present."""
    p = payload or {}
    keep = {}
    for k in ("avg", "filled_size", "fill_price", "reason", "pnl_usd", "unrealized_pnl",
              "bid", "ask", "stop", "target", "peak_r", "order_id"):
        if k in p:
            keep[k] = p[k]
    return keep


def export_session(cur, session_id: int, meta: dict, *, max_tape: int) -> dict:
    cur.execute(
        "SELECT id, symbol, mode, state, execution_family, risk_snapshot_json, created_at "
        "FROM trading_automation_sessions WHERE id=%s",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"session {session_id} not found in live DB")
    (_id, symbol, mode, state, ef, risk_snapshot, created_at) = row
    risk_snapshot = risk_snapshot or {}
    live_eligible_at = None
    if isinstance(risk_snapshot, dict):
        live_eligible_at = risk_snapshot.get("live_eligible_at_utc")

    # --- ORDERED event trace ---
    cur.execute(
        "SELECT ts, event_type, payload_json FROM trading_automation_events "
        "WHERE session_id=%s ORDER BY id ASC",
        (session_id,),
    )
    events = []
    for ts, et, pl in cur.fetchall():
        events.append({
            "ts": _iso(_naive_utc(ts)),
            "event_type": str(et),
            "payload": _load_bearing_payload(str(et), pl or {}),
        })
    if not events:
        raise SystemExit(f"session {session_id} has no events")

    t0 = _naive_utc(min(datetime.fromisoformat(e["ts"]) for e in events))
    t1 = _naive_utc(max(datetime.fromisoformat(e["ts"]) for e in events))
    win_lo = t0 - timedelta(seconds=60)
    win_hi = t1 + timedelta(seconds=60)

    # --- NBBO tape: a uniform down-sample across the window PLUS a DENSE band around each
    #     load-bearing event instant (so quote_at() is FAITHFUL at the fill instants — the
    #     recorded fills happen at true-tick granularity a coarse uniform stride steps over).
    cur.execute(
        "SELECT count(*) FROM momentum_nbbo_spread_tape "
        "WHERE symbol=%s AND observed_at BETWEEN %s AND %s",
        (symbol, win_lo, win_hi),
    )
    total_tape = int(cur.fetchone()[0])
    stride = max(1, total_tape // max_tape) if total_tape > max_tape else 1
    tape_by_ts: dict[str, dict] = {}

    def _ingest(rows):
        for observed_at, bid, ask, mid, day_vol in rows:
            if bid is None or ask is None:
                continue
            key = _iso(_naive_utc(observed_at))
            tape_by_ts[key] = {
                "ts": key,
                "bid": float(bid),
                "ask": float(ask),
                "mid": (float(mid) if mid is not None else (float(bid) + float(ask)) / 2.0),
                "day_volume": (float(day_vol) if day_vol is not None else None),
            }

    # uniform down-sample
    cur.execute(
        "SELECT observed_at, bid, ask, mid, day_volume FROM ("
        "  SELECT observed_at, bid, ask, mid, day_volume, "
        "         row_number() OVER (ORDER BY observed_at) AS rn "
        "  FROM momentum_nbbo_spread_tape "
        "  WHERE symbol=%s AND observed_at BETWEEN %s AND %s"
        ") q WHERE (rn %% %s)=0 OR rn=1 ORDER BY observed_at",
        (symbol, win_lo, win_hi, stride),
    )
    _ingest(cur.fetchall())

    # dense band (±3s, last row per 50ms bucket) around every load-bearing event instant
    for e in events:
        if e["event_type"] not in LOAD_BEARING:
            continue
        ev_ts = datetime.fromisoformat(e["ts"])
        lo = _naive_utc(ev_ts) - timedelta(seconds=3)
        hi = _naive_utc(ev_ts) + timedelta(seconds=3)
        cur.execute(
            "SELECT DISTINCT ON (bucket) observed_at, bid, ask, mid, day_volume FROM ("
            "  SELECT observed_at, bid, ask, mid, day_volume, "
            "         floor(extract(epoch FROM observed_at) * 20) AS bucket "
            "  FROM momentum_nbbo_spread_tape "
            "  WHERE symbol=%s AND observed_at BETWEEN %s AND %s"
            ") q ORDER BY bucket, observed_at DESC",
            (symbol, lo, hi),
        )
        _ingest(cur.fetchall())

    tape = [tape_by_ts[k] for k in sorted(tape_by_ts.keys())]

    # --- per-print trade volume (for the STEP-2 volume-cap fill realism) ---
    cur.execute(
        "SELECT count(*), coalesce(sum(size),0) FROM iqfeed_trade_ticks "
        "WHERE symbol=%s AND observed_at BETWEEN %s AND %s",
        (symbol, win_lo, win_hi),
    )
    tick_count, tick_vol = cur.fetchone()

    return {
        "session_id": session_id,
        "symbol": symbol,
        "date": meta.get("date"),
        "note": meta.get("note"),
        "mode": mode,
        "recorded_final_state": state,
        "execution_family": ef,
        "live_eligible_at_utc": live_eligible_at,
        "window_utc": [_iso(t0), _iso(t1)],
        "recorded_events": events,
        "recorded_transition_trace": [
            e["event_type"] for e in events if e["event_type"] in LOAD_BEARING
        ],
        "tape": tape,
        "tape_meta": {
            "total_rows_in_window": total_tape,
            "exported_rows": len(tape),
            "stride": stride,
            "trade_tick_count": int(tick_count or 0),
            "trade_tick_volume": float(tick_vol or 0.0),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default=",".join(str(s) for s in DEFAULT_SESSIONS))
    ap.add_argument("--max-tape", type=int, default=400,
                    help="max down-sampled NBBO rows per fixture")
    ap.add_argument("--database-url",
                    default="postgresql://chili:chili@localhost:5433/chili")
    args = ap.parse_args()

    session_ids = [int(s.strip()) for s in args.sessions.split(",") if s.strip()]
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(args.database_url)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    try:
        for sid in session_ids:
            meta = DEFAULT_SESSIONS.get(sid, {})
            data = export_session(cur, sid, meta, max_tape=args.max_tape)
            out = FIXTURE_DIR / f"session_{sid}_{data['symbol']}.json"
            out.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(
                f"exported {out.name}: {len(data['recorded_events'])} events, "
                f"{len(data['tape'])}/{data['tape_meta']['total_rows_in_window']} tape rows, "
                f"trace={data['recorded_transition_trace']}"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
