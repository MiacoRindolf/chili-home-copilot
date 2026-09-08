"""Price a pullback_add change from RECORDED RECEIPTS instead of a fresh replay.

Why this exists
---------------
A full bench run costs hours because it re-simulates whole symbol-days.  But
``pullback_add_decision`` is a PURE function: feed it the inputs a veto recorded
and it reproduces that veto exactly.  So the first pass does not need a replay
engine at all -- it needs the receipts, which are already in the book.

    stage 1 (this tool, seconds)   which DECISIONS flip, exactly
    stage 2 (bench, minutes)       what those flips are WORTH, on the flipped
                                   symbol-days only -- not the whole corpus

Stage 1 is exact for the decision and says nothing about money; stage 2 is the
only thing that may claim money.  Running stage 2 on every case when stage 1 has
already proved most of them cannot change is the waste this removes.

    python scripts/pullback_add_receipt_replay.py --since 2026-08-01
    python scripts/pullback_add_receipt_replay.py --set depth_lo_frac=0.15

Read-only.  Never writes to the book.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.paper_execution import (  # noqa: E402
    pullback_add_decision,
)

# The receipt carries the gate's inputs; anything it does not carry is filled
# with a value that CANNOT by itself flip the decision, so a flip is always
# attributable to a recorded field.
_NEUTRAL = dict(
    enabled=True, is_equity=True, add_count=0, max_adds=99,
    in_flight=False, other_add_in_flight=False,
    a0=1.0, q0=1.0, d0=1.0, stop_px=0.0,
    bounced=True, midday_lull=False, cooldown_active=False,
)

_SQL = """
SELECT e.id, e.ts, s.symbol, e.payload_json
FROM trading_automation_events e
JOIN trading_automation_sessions s ON s.id = e.session_id
WHERE e.event_type = 'live_pullback_add_vetoed'
  AND (:since IS NULL OR e.ts >= CAST(:since AS timestamp))
ORDER BY e.ts
"""


def _f(payload: dict, key: str):
    v = payload.get(key)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _replay_one(payload: dict, overrides: dict) -> dict | None:
    """Re-run the recorded veto through the pure gate. None = not reconstructable."""
    pb_low = _f(payload, "pullback_low")
    prior = _f(payload, "prior_low")
    hwm = _f(payload, "hwm")
    move_range = _f(payload, "move_range")
    if pb_low is None or prior is None:
        return None
    if hwm is None or move_range is None:
        # Receipts written before the depth inputs were recorded. The band cannot
        # be re-priced from them -- say so rather than guessing a move top.
        return None
    kw = dict(_NEUTRAL)
    kw.update(
        bid=pb_low, high_water_mark=hwm, support_level=prior,
        pullback_low=pb_low, prior_pullback_low=prior, move_range=move_range,
        pullback_depth_lo_frac=float(overrides.get("depth_lo_frac",
                                                   payload.get("depth_lo") or 0.20)),
        pullback_depth_hi_frac=float(overrides.get("depth_hi_frac",
                                                   payload.get("depth_hi") or 0.62)),
        front_side_strength=_f(payload, "strength"),
        strength_floor=float(overrides.get("strength_floor",
                                           payload.get("strength_floor") or 0.50)),
        above_vwap_or_reclaiming=bool(payload.get("above_vwap")),
        ofi_level=_f(payload, "ofi_level"), ofi_slope=_f(payload, "ofi_slope"),
    )
    return pullback_add_decision(**kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--since", default=None, help="ISO date; omit for all history")
    ap.add_argument("--set", action="append", default=[], metavar="knob=value",
                    help="depth_lo_frac / depth_hi_frac / strength_floor")
    args = ap.parse_args()
    if not args.database_url:
        print("DATABASE_URL is required.", file=sys.stderr)
        return 2

    overrides: dict[str, float] = {}
    for item in args.set:
        k, _, v = item.partition("=")
        overrides[k.strip()] = float(v)

    eng = create_engine(args.database_url, pool_pre_ping=True)
    with eng.connect() as cx:
        cx.execute(text("SET statement_timeout = '60s'"))
        rows = cx.execute(text(_SQL), {"since": args.since}).fetchall()

    recorded = Counter()
    replayed = Counter()
    flips: list[dict] = []
    unreconstructable = 0

    for _id, ts, symbol, payload in rows:
        if isinstance(payload, str):
            payload = json.loads(payload)
        was = str(payload.get("reason") or "?")
        recorded[was] += 1
        out = _replay_one(payload, overrides)
        if out is None:
            unreconstructable += 1
            replayed["(no depth inputs in receipt)"] += 1
            continue
        now = "FIRE" if out.get("fire") else str(out.get("reason") or "?")
        replayed[now] += 1
        if now != was:
            flips.append({"ts": str(ts), "symbol": symbol, "was": was, "now": now,
                          "depth_was": payload.get("depth_frac"),
                          "depth_now": out.get("pullback_depth_frac")})

    print(f"receipts        : {len(rows)}"
          + (f"  (since {args.since})" if args.since else ""))
    print(f"overrides       : {overrides or '(none — reproduce as recorded)'}")
    if unreconstructable:
        print(f"NOT re-priceable: {unreconstructable} — receipts predate the depth inputs "
              f"(hwm / move_range). Those land from the next live session on.")
    print("\nrecorded -> replayed")
    for reason, n in recorded.most_common():
        print(f"  {reason:<26} {n:>4}")
    print("\nafter")
    for reason, n in replayed.most_common():
        print(f"  {reason:<26} {n:>4}")

    if flips:
        print(f"\nFLIPPED: {len(flips)} — these are the ONLY symbol-days stage 2 needs")
        for f in flips[:40]:
            print(f"  {f['ts'][:19]}  {f['symbol']:<6} {f['was']:>22} -> {f['now']:<22}"
                  f"  depth {f['depth_was']} -> {f['depth_now']}")
        syms = sorted({f["symbol"] for f in flips})
        print(f"\n  bench case set: {','.join(syms)}")
    else:
        print("\nNo decision changed. Stage 2 would prove nothing — do not run it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
