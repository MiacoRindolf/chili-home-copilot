# [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
# DATABASE_URL at it privately; every connection sets default_transaction_read_only.
"""[23] slice C — re-derive the [46] chase gate's binding values under the contract that
will SHIP after this PR: the G4 tape read moves from ``legacy_time_split`` to ``count_v1``
(halves split at the print-COUNT midpoint, scale-free gap trim), and the chase gate eats
the SAME ``_g4e_dbg`` tape.

Population = the [46] derivation's own: every ``momentum_reentry_chase_blocked`` row
2026-08-30 .. 2026-09-10 (177 rows / 11 episodes). Both contracts are re-read NOW, side by
side, because publication-eligibility filtering moves tape values across weeks (the [46]
doc says so), so the legacy numbers are reproduced here too rather than copied.

Measured:
  (1) tape+ (accel>0 AND buy_share_delta>0) count per contract, and the disagreement;
  (2) the band UNION hole: rows inside the band on the PRINT basis, and how many are tape-;
  (3) per episode (session_id): tape+ instants per contract, the FIRST admitted instant
      (all 177 are above the union band by construction => admitted iff tape+), its print-
      basis extension, and its forward outcome by the [46] episode method (first touch of
      +2 ATR vs -1 ATR within the next 30 min of prints) AND print-indexed continuation
      (a print strictly above the deciding print within the NEXT 255 prints);
  (4) the size-band check: continuation by extension tercile over the tape+ instants.

READ-ONLY (default_transaction_read_only=on), statement_timeout 20 s, every
iqfeed_trade_ticks read symbol-scoped and bounded (LIMIT 255 or a 30-min window).
"""
from __future__ import annotations

import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

os.environ["CHILI_PYTEST"] = "1"
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    prior_leg_high_print,
    signed_tape_accel_features,
)

SCRATCH = os.path.join(os.environ.get("T23_OUT_DIR", "."), "")
eng = create_engine(
    os.environ["DATABASE_URL"],
    connect_args={"options": "-c statement_timeout=20000 -c default_transaction_read_only=on"},
)
db = sessionmaker(bind=eng)()

CAP_R, ATR, WIN = 1.5, 0.015, 255
CONTRACTS = ("legacy_time_split", "count_v1")

rows = db.execute(text("""
    SELECT e.id, e.session_id, s.symbol, e.ts, e.payload_json
    FROM trading_automation_events e
    JOIN trading_automation_sessions s ON s.id = e.session_id
    WHERE e.event_type = 'momentum_reentry_chase_blocked'
      AND e.ts >= '2026-08-30' AND e.ts < '2026-09-11'
    ORDER BY e.ts, e.id
""")).fetchall()
print("population rows:", len(rows))


def prior_leg(sid, ts):
    ent = db.execute(text("""
        SELECT ts FROM trading_automation_events
        WHERE session_id=:sid AND event_type='live_entry_filled' AND ts < :ts
        ORDER BY ts DESC LIMIT 1"""), {"sid": sid, "ts": ts}).fetchone()
    if ent is None:
        return None, None
    ex = db.execute(text("""
        SELECT ts FROM trading_automation_events
        WHERE session_id=:sid AND event_type='live_exit_filled' AND ts > :ent AND ts < :ts
        ORDER BY ts DESC LIMIT 1"""), {"sid": sid, "ent": ent[0], "ts": ts}).fetchone()
    return (ent[0], ex[0]) if ex is not None else (None, None)


def tape_plus(t):
    if not t or t.get("accel") is None or t.get("bsd") is None:
        return None
    return bool(float(t["accel"]) > 0 and float(t["bsd"]) > 0)


recs = []
for rid, sid, sym, ts, payload in rows:
    p = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    ent_at, exit_at = prior_leg(sid, ts)
    hp = None
    if ent_at is not None:
        hp, _n, _sealed = prior_leg_high_print(sym, db=db, entry_at=ent_at, exit_at=exit_at, as_of=ts)
    rec = {"id": rid, "sid": sid, "sym": sym, "ts": ts,
           "q_px": float(p.get("live_price") or 0) or None,
           "q_anchor": float(p.get("prior_anchor_hwm") or 0) or None,
           "q_risk": float(p.get("risk_unit_atr") or 0) or None,
           "high_print": hp}
    for c in CONTRACTS:
        t = signed_tape_accel_features(sym, db=db, as_of=ts, window_prints=WIN, feature_contract=c)
        rec[c] = None if t is None else {
            "accel": t.get("signed_tape_accel"), "bsd": t.get("buy_share_delta"),
            "n": t.get("n_ticks"), "split": t.get("split"), "last_print": t.get("last_print"),
            "gap_restricted": t.get("gap_restricted"),
        }
    recs.append(rec)

# ── (1) tape+ per contract ──────────────────────────────────────────────────────
for c in CONTRACTS:
    print(f"tape+ under {c}: {sum(1 for r in recs if tape_plus(r[c]))}/{len(recs)}"
          f"  readable: {sum(1 for r in recs if tape_plus(r[c]) is not None)}")
dis = [r for r in recs if tape_plus(r['legacy_time_split']) != tape_plus(r['count_v1'])]
print(f"disagreement: {len(dis)}/{len(recs)}  legacy+/count-: "
      f"{sum(1 for r in dis if tape_plus(r['legacy_time_split']))}  legacy-/count+: "
      f"{sum(1 for r in dis if tape_plus(r['count_v1']))}")
by = {}
for r in dis:
    by[r["sym"]] = by.get(r["sym"], 0) + 1
print("  by symbol:", by)

# ── (2) the band union hole (price-only; last_print is the same under both) ────────
for r in recs:
    t = r["count_v1"] or r["legacy_time_split"] or {}
    px = t.get("last_print")
    anchor = r["high_print"] or r["q_anchor"]
    r["px"], r["anchor"] = px, anchor
    if px and anchor:
        risk = ATR * float(anchor)
        r["ext"] = (float(px) - float(anchor)) / risk
        r["above_print"] = bool(float(px) > float(anchor) + CAP_R * risk)
    else:
        r["ext"], r["above_print"] = None, None
inside = [r for r in recs if r["above_print"] is False]
print(f"inside band on print basis: {len(inside)}; tape- among them: legacy "
      f"{sum(1 for r in inside if not tape_plus(r['legacy_time_split']))}, count_v1 "
      f"{sum(1 for r in inside if not tape_plus(r['count_v1']))}")
lp_same = sum(1 for r in recs if r["legacy_time_split"] and r["count_v1"]
              and r["legacy_time_split"]["last_print"] == r["count_v1"]["last_print"])
print(f"last_print identical across contracts: {lp_same}/{len(recs)}")


# ── (3) episodes ────────────────────────────────────────────────────────────────
def first_touch(sym, ts, entry, anchor):
    atr = ATR * float(anchor)
    up, dn = entry + 2 * atr, entry - 1 * atr
    return db.execute(text("""
        SELECT (array_agg(kind ORDER BY observed_at, id))[1] FROM (
            SELECT observed_at, id, CASE WHEN price >= :up THEN 'UP' ELSE 'DOWN' END kind
            FROM iqfeed_trade_ticks
            WHERE symbol=:s AND observed_at > :t AND observed_at <= :t2
              AND (price >= :up OR price <= :dn)) q"""),
        {"s": sym, "t": ts, "t2": ts + timedelta(minutes=30), "up": up, "dn": dn}).scalar()


def continued(sym, ts, px):
    mx = db.execute(text("""
        SELECT max(price) FROM (
            SELECT price FROM iqfeed_trade_ticks
            WHERE symbol=:s AND observed_at > :t AND observed_at <= :t2
            ORDER BY observed_at ASC, id ASC LIMIT 255) z"""),
        {"s": sym, "t": ts, "t2": ts + timedelta(minutes=30)}).scalar()
    return bool(mx is not None and float(mx) > float(px))


eps = OrderedDict()
for r in recs:
    eps.setdefault(r["sid"], []).append(r)
print(f"episodes: {len(eps)}")
entries = {c: [] for c in CONTRACTS}
for sid, es in eps.items():
    line = f"  {sid} {es[0]['sym']:>5} n={len(es):>3}"
    for c in CONTRACTS:
        tp = [e for e in es if tape_plus(e[c])]
        first = tp[0] if tp else None
        if first is not None and first["px"] and first["anchor"]:
            ft = first_touch(first["sym"], first["ts"], float(first["px"]), float(first["anchor"]))
            ct = continued(first["sym"], first["ts"], float(first["px"]))
            entries[c].append((first, ft, ct))
            line += (f" | {c[:6]} tape+={len(tp):>3} first={first['ts'].strftime('%H:%M:%S')}"
                     f" px={first['px']} ext={first['ext']:.2f} touch={ft} cont={ct}")
        else:
            line += f" | {c[:6]} tape+={len(tp):>3} first=-"
    print(line)
for c in CONTRACTS:
    xs = sorted(round(e[0]["ext"], 2) for e in entries[c])
    ups = sum(1 for e in entries[c] if e[1] == "UP")
    print(f"first-admitted extensions under {c}: {xs}  first-touch UP {ups}/{len(entries[c])}")


# ── (4) continuation tercile over tape+ instants ────────────────────────────────
for c in CONTRACTS:
    tp_rows = [r for r in recs if tape_plus(r[c]) and r["ext"] is not None]
    for r in tp_rows:
        r.setdefault("cont", continued(r["sym"], r["ts"], float(r["px"])))
    tp_rows.sort(key=lambda r: r["ext"])
    n = len(tp_rows)
    lo, hi = tp_rows[: n // 3], tp_rows[-(n // 3):]
    mid = tp_rows[n // 3: n - (n // 3)]

    def rate(g):
        k = sum(1 for r in g if r["cont"])
        return k, len(g), (k / len(g) if g else float("nan"))

    def p50(g):
        xs = sorted(r["ext"] for r in g)
        return xs[len(xs) // 2] if xs else float("nan")

    rl, rm, rh = rate(lo), rate(mid), rate(hi)
    print(f"continuation tercile under {c} (tape+ n={n}): "
          f"low {rl[0]}/{rl[1]}={rl[2]:.4f} (ext p50 {p50(lo):.2f}) | "
          f"mid {rm[0]}/{rm[1]}={rm[2]:.4f} (ext p50 {p50(mid):.2f}) | "
          f"high {rh[0]}/{rh[1]}={rh[2]:.4f} (ext p50 {p50(hi):.2f}) | "
          f"ratio high/low {rh[2] / rl[2]:.4f}")

with open(SCRATCH + "t23_rederive46_count.json", "w", encoding="utf-8") as fh:
    json.dump(recs, fh, indent=1, default=str)
