"""[58] RE-DERIVE ``ACCEL_REVERSAL_GIVEBACK_BAND_R`` from the live tape — READ-ONLY.

Why this file is committed (and is not a scratchpad script): the constant
``paper_execution.ACCEL_REVERSAL_GIVEBACK_BAND_R`` ships its provenance into every live
receipt as ``binding``. A provenance that points at an ephemeral per-session temp file is
not provenance — nobody can re-run it. This script IS the derivation, in the repo, so the
next reader can re-derive the number on a fresh window and either confirm it or replace it.

WHAT IT MEASURES
    The give-back ``(H − P) / risk_dist`` at the REAL accel rollover while above entry — the
    "G" trigger of the exit verdict: ``signed_tape_accel`` goes from ``> 0`` to ``<= 0`` on a
    print above the entry fill. ``H`` is the leg's running HIGH PRINT up to that rollover,
    ``P`` is the rollover print, and ``risk_dist`` is the exit helper's OWN risk unit
    (``entry · max(0.003, atr_pct · stop_atr_mult)``).

    The rollover is detected with the SAME function the live gate reads —
    ``entry_gates._signed_tape_features`` — over a PRINT-INDEXED window of
    ``--window-prints`` prints (default: ``chili_momentum_g4_reentry_tape_window_prints``,
    the p50 print count inside the legacy 15-s window at 108 live decision instants). This
    matters: before 2026-09-11 the live exit called ``signed_tape_accel_features`` with NO
    ``window_prints``, so it fell back to a 15-SECOND wall-clock window while this derivation
    was print-indexed — the band was the p90 of a quantity the gate did not measure. The gate
    now passes the same print window, so the two populations are the same population.

``risk_dist`` per leg, in the helper's own unit, first source that resolves:
    1. the ``live_tape_accel_reversal_exit`` receipt itself: ``(high_water_mark − entry) / peak_r``
       (exact — the helper computed ``peak_r`` from the very unit we want back);
    2. ``tranche_oco_placed.stop``  ⇒ ``entry − stop`` (unquantized software stop);
    3. ``momentum_mfe_realized.stop_distance`` (cent-quantized, <= 1 % off).
   A leg that resolves none of the three is SKIPPED and counted in ``legs_no_unit``.

SAFETY: every statement is a SELECT. Every tick read is bounded by symbol AND a time range
(entry fill → min(exit, entry + ``--horizon-min``)), never a bare scan of the 211 M-row
``iqfeed_trade_ticks``. ``statement_timeout`` is set per connection.

USAGE
    conda run -n chili-env python project_ws/AgentOps/timeshare/derive_giveback_band_58_helper_units.py
    conda run -n chili-env python .../derive_giveback_band_58_helper_units.py --days 14 --window-prints 255

MEASURED 2026-09-11 (14 d to 2026-09-10, --window-prints 255, --stride 1): see the header
comment of ``ACCEL_REVERSAL_GIVEBACK_BAND_R`` in
``app/services/trading/momentum_neural/paper_execution.py`` for the distribution this run
produced and the constant it fixes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    _TAPE_GAP_DISCONTINUITY_P90_MULT,
    _signed_tape_features,
)

DEFAULT_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili"
)
# ── THE DERIVATION MUST RUN IN THE GATE'S OWN UNIT ([29], 2026-09-11) ──────────
# Until tonight this script called ``_signed_tape_features(window_s=15.0)`` — the TIME
# split with a ``window_s / 2`` = 7.5 s gap trim — while the live exit called the wrapper
# with ``window_prints``. [29] then made every print-form read a COUNT split with a
# scale-free discontinuity trim, and the accel SIGN flips on 17 of 63 live instants (27%)
# between the two splits. A band derived at rollovers defined under one split is not the
# p90 of the population the gate decides on under the other, so this file now reads the
# SAME unit the gate reads and the constant is re-derived from that run.
GAP_AGE_FLOOR_S = 14.69          # chili_momentum_g4_reentry_max_print_age_seconds
GAP_P90_MULT = float(_TAPE_GAP_DISCONTINUITY_P90_MULT)


def _pct(xs: list[float], q: float) -> float:
    """Linear-interpolated percentile of a sorted-able list (no numpy dependency)."""
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _legs(conn, days: int, horizon_min: int) -> list[dict]:
    """Every live equity entry fill in the window, with its symbol and leg bounds."""
    rows = conn.execute(
        text(
            """
            SELECT e.id, e.session_id, s.symbol, e.ts AS entry_ts,
                   (e.payload_json->>'avg')::numeric AS entry,
                   LEAST(
                     COALESCE((SELECT min(x.ts) FROM trading_automation_events x
                                WHERE x.session_id = e.session_id
                                  AND x.event_type = 'live_exit_filled'
                                  AND x.ts > e.ts),
                              e.ts + make_interval(mins => :h)),
                     e.ts + make_interval(mins => :h)
                   ) AS exit_ts
            FROM trading_automation_events e
            JOIN trading_automation_sessions s ON s.id = e.session_id
            WHERE e.event_type = 'live_entry_filled'
              AND e.ts >= now() - make_interval(days => :d)
              AND (e.payload_json->>'avg') IS NOT NULL
              AND s.symbol NOT LIKE '%%-USD'
            ORDER BY e.ts
            """
        ),
        {"d": days, "h": horizon_min},
    ).fetchall()
    return [
        {
            "id": r[0],
            "session_id": r[1],
            "symbol": r[2],
            "entry_ts": r[3],
            "entry": float(r[4]),
            "exit_ts": r[5],
        }
        for r in rows
        if r[4] is not None and float(r[4]) > 0
    ]


def _risk_dist(conn, leg: dict) -> tuple[float | None, str]:
    """The helper's own risk unit for this leg — first source that resolves."""
    # 1. the receipt itself: peak_r = (hwm − entry) / risk_dist
    r = conn.execute(
        text(
            """
            SELECT (payload_json->>'high_water_mark')::numeric,
                   (payload_json->>'peak_r')::numeric
            FROM trading_automation_events
            WHERE session_id = :sid AND event_type = 'live_tape_accel_reversal_exit'
              AND ts >= :t0 AND (payload_json->>'peak_r') IS NOT NULL
              AND (payload_json->>'peak_r')::numeric > 0
            ORDER BY ts LIMIT 1
            """
        ),
        {"sid": leg["session_id"], "t0": leg["entry_ts"]},
    ).fetchone()
    if r and r[0] is not None and r[1]:
        rd = (float(r[0]) - leg["entry"]) / float(r[1])
        if rd > 0:
            return rd, "receipt_peak_r"
    # 2. the unquantized software stop written with the bracket
    r = conn.execute(
        text(
            """
            SELECT (payload_json->>'stop')::numeric
            FROM trading_automation_events
            WHERE session_id = :sid AND event_type = 'tranche_oco_placed'
              AND (payload_json->>'stop') IS NOT NULL
            ORDER BY abs(EXTRACT(EPOCH FROM (ts - :t0))) LIMIT 1
            """
        ),
        {"sid": leg["session_id"], "t0": leg["entry_ts"]},
    ).fetchone()
    if r and r[0] is not None:
        rd = leg["entry"] - float(r[0])
        if rd > 0:
            return rd, "tranche_oco_stop"
    # 3. the realized-MFE receipt (cent-quantized)
    r = conn.execute(
        text(
            """
            SELECT (payload_json->>'stop_distance')::numeric
            FROM trading_automation_events
            WHERE session_id = :sid AND event_type = 'momentum_mfe_realized'
              AND (payload_json->>'stop_distance') IS NOT NULL
            ORDER BY ts LIMIT 1
            """
        ),
        {"sid": leg["session_id"]},
    ).fetchone()
    if r and r[0] is not None and float(r[0]) > 0:
        return float(r[0]), "mfe_stop_distance"
    return None, "none"


def _prints(conn, leg: dict) -> list[tuple]:
    """The leg's executed tape, oldest first — symbol AND time bounded."""
    rows = conn.execute(
        text(
            """
            SELECT price, size, bid, ask, EXTRACT(EPOCH FROM observed_at)
            FROM iqfeed_trade_ticks
            WHERE symbol = :s AND observed_at > :t0 AND observed_at <= :t1
            ORDER BY observed_at ASC, id ASC
            """
        ),
        {"s": leg["symbol"], "t0": leg["entry_ts"], "t1": leg["exit_ts"]},
    ).fetchall()
    return [tuple(r) for r in rows]


def _rollovers(rows: list[tuple], *, entry: float, risk_dist: float,
               window_prints: int, stride: int, arm_r: float,
               all_rollovers: bool) -> dict:
    """Give-back in helper-R at the accel rollovers above entry on this leg's tape.

    THE POPULATION MATTERS, so all three are returned and the caller says which it takes:

    ``first_armed``  the give-back at the FIRST rollover above entry that happens once the
                     leg is a WINNER (running high >= entry + ``arm_r`` · risk_dist). This is
                     the population the live gate actually decides on: gate 1 keeps the helper
                     inert below the arm, and the helper RATCHETS — the first armed rollover is
                     the decision, everything after it is a stop already raised. ``arm_r``
                     here is the FLOOR of the live arm (``max(0.5, arm_frac · rr)``), so this
                     is a lower bound on how late the live arm actually opens.
    ``first``        the first rollover above entry, armed or not.
    ``all``          every rollover above entry — reported for context only. It is NOT the
                     gate's population: late rollovers sit deep under a running high the name
                     already gave back, i.e. exactly the ticks gate 3 exists to REFUSE, so its
                     p90 answers a different question (measured 2026-09-11: p90 2.9 R).
    """
    hits: list[float] = []
    first: float | None = None
    first_armed: float | None = None
    running_high = entry
    prev_accel: float | None = None
    n = len(rows)
    if n < window_prints:
        return {"first": None, "first_armed": None, "all": hits, "evaluated": 0}
    evaluated = 0
    for i in range(window_prints, n + 1, max(1, stride)):
        window = rows[i - window_prints:i]
        try:
            px = float(window[-1][0])
        except (TypeError, ValueError):
            continue
        if px > running_high:
            running_high = px
        feat = _signed_tape_features(
            window,
            tick_rate_floor_pctile=0.0,
            split="count",
            gap_trim_s=GAP_AGE_FLOOR_S,
            gap_discontinuity_mult=GAP_P90_MULT,
        )
        evaluated += 1
        accel = None if feat is None else feat.get("signed_tape_accel")
        if accel is None:
            prev_accel = None
            continue
        accel = float(accel)
        # THE G TRIGGER: the aggressive-buy push was carrying and has now ended/turned,
        # on a print still ABOVE the entry fill (a rollover below entry is the stop's job).
        if prev_accel is not None and prev_accel > 0.0 and accel <= 0.0 and px > entry:
            gb = (running_high - px) / risk_dist
            if gb >= 0.0:
                hits.append(gb)
                if first is None:
                    first = gb
                if first_armed is None and (running_high - entry) >= arm_r * risk_dist:
                    first_armed = gb
                    if not all_rollovers:
                        break
        prev_accel = accel
    return {"first": first, "first_armed": first_armed, "all": hits,
            "evaluated": evaluated}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--horizon-min", type=int, default=60)
    ap.add_argument(
        "--window-prints",
        type=int,
        default=None,
        help="prints per accel window; default = chili_momentum_g4_reentry_tape_window_prints",
    )
    ap.add_argument("--stride", type=int, default=1, help="evaluate every Nth print")
    ap.add_argument("--limit-legs", type=int, default=0, help="0 = all legs")
    ap.add_argument(
        "--arm-r",
        type=float,
        default=0.5,
        help="the FLOOR of the live profit-arm (max(0.5, arm_frac*rr)); a rollover before "
             "the leg reaches it can never be seen by the gate",
    )
    ap.add_argument(
        "--all-rollovers",
        action="store_true",
        help="do not stop at the first armed rollover; also report the full population",
    )
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    if args.window_prints is None:
        from app.config import settings as _s

        args.window_prints = int(
            getattr(_s, "chili_momentum_g4_reentry_tape_window_prints", 255) or 255
        )

    eng = create_engine(args.dsn, pool_pre_ping=True)
    gbs: list[float] = []
    firsts: list[float] = []
    all_gbs: list[float] = []
    per_leg: list[dict] = []
    units: dict[str, int] = {}
    legs_no_unit = 0
    with eng.connect() as conn:
        conn.execute(text("SET statement_timeout='60s'"))
        legs = _legs(conn, args.days, args.horizon_min)
        if args.limit_legs:
            legs = legs[: args.limit_legs]
        print(
            f"[58] legs={len(legs)} window_prints={args.window_prints} "
            f"stride={args.stride} days={args.days} horizon_min={args.horizon_min}",
            flush=True,
        )
        for k, leg in enumerate(legs, 1):
            rd, src = _risk_dist(conn, leg)
            units[src] = units.get(src, 0) + 1
            if rd is None:
                legs_no_unit += 1
                continue
            rows = _prints(conn, leg)
            res = _rollovers(
                rows,
                entry=leg["entry"],
                risk_dist=rd,
                window_prints=args.window_prints,
                stride=args.stride,
                arm_r=args.arm_r,
                all_rollovers=args.all_rollovers,
            )
            if res["first_armed"] is not None:
                gbs.append(res["first_armed"])
            if res["first"] is not None:
                firsts.append(res["first"])
            all_gbs.extend(res["all"])
            per_leg.append(
                {
                    "session_id": leg["session_id"],
                    "symbol": leg["symbol"],
                    "entry_ts": str(leg["entry_ts"]),
                    "entry": leg["entry"],
                    "risk_dist": rd,
                    "risk_dist_source": src,
                    "prints": len(rows),
                    "evaluated": res["evaluated"],
                    "first_giveback_r": res["first"],
                    "first_armed_giveback_r": res["first_armed"],
                    "n_rollovers_seen": len(res["all"]),
                }
            )
            print(
                f"  [{k}/{len(legs)}] {leg['symbol']:<6} {leg['entry_ts']} "
                f"prints={len(rows):>6} unit={rd:.5f} ({src}) "
                f"first_armed={res['first_armed']}",
                flush=True,
            )

    print("")
    print(f"[58] legs_with_unit={len(per_leg)} legs_no_unit={legs_no_unit} units={units}")

    def _report(label: str, xs: list[float]) -> None:
        if not xs:
            print(f"[58] {label}: n=0")
            return
        p90 = _pct(xs, 0.90)
        inside = sum(1 for x in xs if x <= p90)
        print(
            f"[58] {label}: n={len(xs)}  p25 {_pct(xs, 0.25):.3f}  p50 {_pct(xs, 0.50):.3f}  "
            f"p75 {_pct(xs, 0.75):.3f}  p90 {_pct(xs, 0.90):.3f}  max {max(xs):.3f}  "
            f"({inside}/{len(xs)} inside p90)"
        )

    _report("BAND POPULATION (first rollover above entry, per leg)", firsts)
    _report("SENSITIVITY (first ARMED rollover, arm floor %.2f R)" % args.arm_r, gbs)
    if args.all_rollovers:
        _report("every rollover (context only — NOT the gate's population)", all_gbs)
    if firsts:
        print(f"[58] BAND = p90 of the band population = {_pct(firsts, 0.90):.3f} R")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"legs": per_leg, "band_population_giveback_r": gbs, "firsts": firsts},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[58] wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
