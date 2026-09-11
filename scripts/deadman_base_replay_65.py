"""[65] DERIVATION + REPLAY — ang tick deadman BASE na sinukat kasama ang G/D whole sale.

Para sa bawat live Alpaca leg sa isang window (live_entry_filled -> ang susunod na entry ng
parehong sesyon), itinatayo muli ang tape ng symbol-day mula sa ``iqfeed_trade_ticks`` (BOUNDED:
symbol + oras, isang statement kada 20-minutong hiwa, 20 s timeout; mula 04:00 ET ng araw, sa
zoneinfo -- hindi isang EDT na literal) at nilalakad mula sa FILL:

  * ang floor ay sinusuri sa BAWAT print (print <= level => tick deadman);
  * ang SOFTWARE bid-stop sa ``position.stop_price`` (bid <= stop, kinumpirma sa read >= 1 s
    pagkatapos -- ang ``if bid <= stop_px:`` ng runner). Hinahango ito mula sa broker deadman sa
    PAREHONG formula ng runner (``live_runner.deadman_stop_buffer``, ang mga pinangalanang
    ``DEADMAN_STOP_BUFFER_*`` -- hindi kopya ng mga literal), dahil ang broker stop ay nasa
    ILALIM nito at INERT sa premarket;
  * ang broker stop, RTH lamang (09:30-16:00 America/New_York sa zoneinfo, DST-aware);
  * ang C4 viability tighten (``viability_degraded_tighten``) sa AKTUWAL na oras nito -- ⚠️ HANGGANG
    SA AKTUWAL NA EXIT LAMANG: pagkatapos ng aktuwal na exit ay sarado na ang leg, kaya WALANG
    nakikitang C4 doon. Ang variant na humahawak nang mas matagal kaysa sa aktuwal ay hindi
    makakakita ng C4 na sana ay pumutok pagkatapos; ang no-C4 na variant ang hangganan ng epekto;
  * G bawat 25 print sa unang 400, tapos bawat 100; D bawat 100 (ang resolusyon ng mga script of
    record); 60-min na horizon (hangganan ng sukat lamang).

Ang features ay ang IPINADALANG ``entry_gates._signed_tape_features`` (count_v1); ang scanner ay
ang IPINADALANG ``tape_cycles.PullbackCycleScanner(0.5)``; ang base ay ang IPINADALANG
``exit_verdict.tick_deadman_fill_base`` at ang konteksto ay ang IPINADALANG
``exit_verdict.continued_pullback_context`` -- hindi kopya.

Mga variant:
  S0_live        ang lumang base (count-half low sa 255 print sa fill) + ang rolling ratchet + C4
  N1_c4          [65] ANG IPINAPADALA: ang resting stop SA FILL, walang ratchet, C4 gaya ng dati
  N1             pareho, walang C4 (counterfactual lamang; hangganan ng C4)
  N4a_c4         ang unang anyo ng #1419: max(stop, entry - median lalim ng kumpletong cycle) + C4
                 (tinanggihan ng review: ang median ay galing sa cold-start ng scanner)
  N4p_c4         pareho pero scale-free: max(stop, entry x (1 - median (hi - pb_low)/hi)) + C4
                 (konteksto para sa susunod na sukat; hindi ipinapadala)
  N1_ratchet_c4  ang ipinapadalang base + ang lumang rolling ratchet + C4 (ang halaga ng ratchet)

Presyo: sa print / sa bid ng print na nagpasya / sa bid 15.3 s pagkatapos (sinukat na p50 ng
desisyon -> submit sa 2026-09-11). Paired diff = kabuuan kada symbol-day, 2000x cluster bootstrap
(``random.Random(65)``), 90% CI, bilang ng araw +/-, leave-one-cluster-out na minimum.

READ-ONLY. Walang isinusulat sa DB.

    conda run -n chili-env python scripts/deadman_base_replay_65.py \\
        --window today=2026-09-11T08:00,2026-09-12T00:00 \\
        --window 14d=2026-08-28T08:00,2026-09-11T08:00 [--tape-cache DIR] [--out res.pkl]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import pickle
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# app Settings needs a URL at import; the reads below are this script's own bounded engine.
os.environ.setdefault("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.entry_gates import _signed_tape_features  # noqa: E402
from app.services.trading.momentum_neural.exit_verdict import (  # noqa: E402
    continued_pullback_context,
    tick_deadman_fill_base,
)
from app.services.trading.momentum_neural.live_runner import (  # noqa: E402
    DEADMAN_STOP_BUFFER_AVG_FRAC,
    DEADMAN_STOP_BUFFER_MIN_USD,
    DEADMAN_STOP_BUFFER_RISK_FRAC,
)
from app.services.trading.momentum_neural.tape_cycles import PullbackCycleScanner  # noqa: E402

ET = ZoneInfo("America/New_York")

N = 255                      # the shipped count window (chili_momentum_g4_reentry_tape_window_prints)
STEP_FINE, FINE_STEPS, STEP = 25, 16, 100
HORIZON_S = 3600
LAT_S = 15.3                 # measured decision -> broker submit p50, 2026-09-11 (20/20 tick exits)
KEYS = ("swing_low_prev", "swing_low_now", "buy_support_px")

LEGS_SQL = """
WITH ent AS (
  SELECT s.id AS session_id, s.symbol, e.id AS ev_id, e.ts AS t_in_ev,
         coalesce((e.payload_json->>'entry_filled_at_utc')::timestamptz AT TIME ZONE 'UTC', e.ts) AS t_in,
         (e.payload_json->>'avg')::numeric AS entry_px,
         (e.payload_json->>'quantity')::numeric AS qty,
         lead(e.ts) OVER (PARTITION BY s.id ORDER BY e.ts) AS next_in
  FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id=e.session_id
  WHERE s.mode='live' AND s.execution_family='alpaca_spot' AND e.event_type='live_entry_filled'
    AND e.ts >= :a AND e.ts < :b
)
SELECT ent.*,
  (SELECT (x.payload_json->>'stop_price')::numeric FROM trading_automation_events x
     WHERE x.session_id=ent.session_id AND x.event_type='live_deadman_stop_placed'
       AND x.ts > ent.t_in_ev AND (ent.next_in IS NULL OR x.ts < ent.next_in) ORDER BY x.ts LIMIT 1) AS stop_px,
  (SELECT x.ts FROM trading_automation_events x
     WHERE x.session_id=ent.session_id AND x.event_type='live_exit_filled'
       AND x.ts > ent.t_in_ev AND (ent.next_in IS NULL OR x.ts < ent.next_in) ORDER BY x.ts LIMIT 1) AS t_out,
  (SELECT x.payload_json->>'reason' FROM trading_automation_events x
     WHERE x.session_id=ent.session_id AND x.event_type='live_exit_filled'
       AND x.ts > ent.t_in_ev AND (ent.next_in IS NULL OR x.ts < ent.next_in) ORDER BY x.ts LIMIT 1) AS exit_reason,
  (SELECT sum((x.payload_json->>'pnl_usd')::numeric) FROM trading_automation_events x
     WHERE x.session_id=ent.session_id AND x.event_type IN ('live_exit_filled','live_partial_exit_filled')
       AND x.ts > ent.t_in_ev AND (ent.next_in IS NULL OR x.ts < ent.next_in)) AS pnl
FROM ent ORDER BY ent.t_in
"""


def _engine():
    return create_engine(os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili"),
                         pool_pre_ping=True)


def _utc(s):
    d = datetime.fromisoformat(str(s))
    return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone(timezone.utc).replace(tzinfo=None)


def read_legs(eng, a, b):
    out = []
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        for r in c.execute(text(LEGS_SQL), {"a": a, "b": b}).mappings():
            out.append({
                "session_id": r["session_id"], "symbol": r["symbol"], "t_in": r["t_in"].isoformat(),
                "entry_px": float(r["entry_px"] or 0), "qty": float(r["qty"] or 0),
                "stop_px": float(r["stop_px"]) if r["stop_px"] is not None else None,
                "t_out": r["t_out"].isoformat() if r["t_out"] else None,
                "exit_reason": r["exit_reason"], "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            })
    return out


def session_start_utc(day):
    """04:00 America/New_York of ``day`` (YYYY-MM-DD) as naive UTC -- the feed's own session
    key, DST-aware (08:00Z in EDT, 09:00Z in EST)."""
    d = datetime.fromisoformat(str(day)[:10])
    return datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET).astimezone(timezone.utc).replace(tzinfo=None)


def day_tape(eng, cache, sym, day, until):
    """(epoch, id, price, size, bid, ask) ascending, 04:00 ET .. until (cached per symbol-day-until)."""
    fn = os.path.join(cache, f"{sym}_{day}_{until:%H%M}.pkl") if cache else None
    if fn and os.path.exists(fn):
        with open(fn, "rb") as fh:
            return pickle.load(fh)
    t = session_start_utc(day)
    out = []
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        while t < until:
            t2 = min(until, t + timedelta(minutes=20))
            rows = c.execute(text(
                "SELECT EXTRACT(EPOCH FROM observed_at)::float8, id, price, size, bid, ask FROM iqfeed_trade_ticks "
                "WHERE symbol=:s AND observed_at > :a AND observed_at <= :b AND price > 0 "
                "ORDER BY observed_at, id"), {"s": sym, "a": t, "b": t2}).fetchall()
            out.extend(tuple(r) for r in rows)
            t = t2
    if fn:
        with open(fn, "wb") as fh:
            pickle.dump(out, fh)
    return out


def feats(rows):
    if len(rows) < 3:
        return None
    rr = [(r[2], r[3], r[4], r[5], r[0]) for r in rows]
    return _signed_tape_features(rr, window_s=None, tick_rate_floor_pctile=0.0, split="count",
                                 gap_trim_s=14.69, gap_discontinuity_mult=7.82,
                                 as_of_ts=rows[-1][0], window_mode="prints")


def first_key(f, below=None):
    if not isinstance(f, dict):
        return None, None
    for k in KEYS:
        v = f.get(k)
        if v is None:
            continue
        v = float(v)
        if v > 0 and (below is None or v < below):
            return v, k
    return None, None


def d_verdict(rows_since_high):
    if len(rows_since_high) < 4:
        return False
    f = feats(rows_since_high)
    if not isinstance(f, dict):
        return False
    a, b, sn, sp = (f.get(k) for k in ("signed_tape_accel", "buy_share_delta", "swing_low_now", "swing_low_prev"))
    if None in (a, b, sn, sp):
        return False
    return a < 0 and b < 0 and sn < sp


def software_stop(broker, avg):
    """position.stop_price from the broker deadman stop: invert the runner's OWN
    ``deadman_px = sw - live_runner.deadman_stop_buffer(avg, sw)`` per branch of its max
    (quantization ~1c). The constants are imported, never copied."""
    if broker is None or avg is None or avg <= 0:
        return None
    a_frac, r_frac, m_usd = DEADMAN_STOP_BUFFER_AVG_FRAC, DEADMAN_STOP_BUFFER_RISK_FRAC, DEADMAN_STOP_BUFFER_MIN_USD
    cands = []
    sw = (broker + r_frac * avg) / (1.0 + r_frac)          # the |avg - sw| x RISK_FRAC branch
    if r_frac * abs(avg - sw) >= max(avg * a_frac, m_usd) - 1e-9:
        cands.append(sw)
    sw = broker + avg * a_frac                              # the avg x AVG_FRAC branch
    if avg * a_frac >= max(r_frac * abs(avg - sw), m_usd) - 1e-9:
        cands.append(sw)
    sw = broker + m_usd                                     # the MIN_USD branch
    if m_usd >= max(r_frac * abs(avg - sw), avg * a_frac) - 1e-9:
        cands.append(sw)
    return max(cands) if cands else None


def c4_lifts(eng, sid, t_in, t_out):
    """The ACTUAL `viability_degraded_tighten` lifts inside the leg, (epoch, new_stop).

    ⚠️ Bounded by the ACTUAL exit: after it the leg is closed and C4 never evaluated, so a
    variant that holds longer than the actual leg cannot see a C4 lift that would have landed
    later. That is unobservable, not zero; the no-C4 variant bounds C4's effect."""
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        rows = c.execute(text(
            "SELECT EXTRACT(EPOCH FROM ts)::float8, payload_json::text FROM trading_automation_events "
            "WHERE session_id=:s AND ts > :a AND ts <= :b AND event_type='viability_degraded_tighten' ORDER BY ts"),
            {"s": sid, "a": t_in, "b": t_out}).fetchall()
    out = []
    for ep, pj in rows:
        try:
            out.append((float(ep), float(json.loads(pj)["new_stop"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def rth(ts):
    """09:30 <= America/New_York wall time < 16:00, via zoneinfo (DST-aware; the live runner
    derives its session key the same way)."""
    d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
    m = d.hour * 60 + d.minute
    return 9 * 60 + 30 <= m < 16 * 60


def reclaim_after(rows, j, entry, secs):
    t0 = rows[j][0]
    for q in range(j + 1, len(rows)):
        if rows[q][0] > t0 + secs:
            return False
        if rows[q][2] > entry:
            return True
    return False


def ledger_base(rows, i_at, entry, sw):
    """The SHIPPED base (the resting stop at the fill) with the SHIPPED context, on the SHIPPED
    scanner fed from the session start to print index ``i_at``."""
    sc = PullbackCycleScanner(0.5)
    sc.feed((datetime.fromtimestamp(r[0], tz=timezone.utc), r[1], r[2], r[3], r[4], r[5]) for r in rows[:i_at])
    st = sc.to_dict()
    st["feed"] = {"caught_up": True}
    return tick_deadman_fill_base(entry_px=entry, resting_stop=sw,
                                  resting_stop_source="replay_inverted_broker_deadman", cycle_state=st)


def max_floor(sw, cand, entry):
    """The #1419-draft rule: max(resting stop, candidate) when the candidate is inside (0, entry)."""
    if cand is None or not (0.0 < cand < entry):
        return sw
    return cand if sw is None else max(sw, cand)


def walk(rows, i0, i_end, entry, base, *, ratchet, sw, broker, lifts):
    level, sw_now, li, pend = base, sw, 0, None
    acc_prev = hi = hi_i = None
    mfe = entry
    for j in range(i0, i_end):
        ts, px, bid = rows[j][0], rows[j][2], rows[j][4]
        k = j - i0
        while li < len(lifts) and ts >= lifts[li][0]:
            if sw_now is None or lifts[li][1] > sw_now:
                sw_now = lifts[li][1]
            li += 1
        if level is not None and px <= level:
            return {"px": px, "how": "deadman", "j": j, "ts": ts, "mfe": mfe}
        if broker is not None and rth(ts) and px <= broker:
            return {"px": px, "how": "broker_stop", "j": j, "ts": ts, "mfe": mfe}
        if sw_now is not None and bid and bid > 0:
            if bid <= sw_now:
                if pend is None:
                    pend = ts
                elif ts - pend >= 1.0:
                    return {"px": px, "how": "sw_stop", "j": j, "ts": ts, "mfe": mfe}
            else:
                pend = None
        mfe = max(mfe, px)
        if hi is None or px > hi:
            hi, hi_i = px, j
        step = STEP_FINE if k < STEP_FINE * FINE_STEPS else STEP
        if k > 0 and k % step == 0:
            f = feats(rows[max(0, j + 1 - N): j + 1])
            if ratchet:
                cand, _ = first_key(f)
                if cand is not None and cand < px and (level is None or cand > level):
                    level = cand
            acc = f.get("signed_tape_accel") if isinstance(f, dict) else None
            if acc is not None:
                if acc_prev is not None and acc_prev > 0 and acc <= 0 and px > entry:
                    return {"px": px, "how": "G", "j": j, "ts": ts, "mfe": mfe}
                acc_prev = acc
            if k % STEP == 0 and hi_i is not None and d_verdict(rows[hi_i + 1: j + 1]):
                return {"px": px, "how": "D", "j": j, "ts": ts, "mfe": mfe}
    j = i_end - 1
    return {"px": rows[j][2], "how": "60m", "j": j, "ts": rows[j][0], "mfe": mfe}


def simulate(eng, legs, cache, label):
    legs = [L for L in legs if L["pnl"] is not None and L["t_out"] and L["entry_px"] and L["qty"]]
    by_day = {}
    for L in legs:
        by_day.setdefault((L["symbol"], L["t_in"][:10]), []).append(L)
    recs = []
    for (sym, day), ls in sorted(by_day.items()):
        until = max(_utc(L["t_in"]) for L in ls) + timedelta(seconds=HORIZON_S + 60 * 16)
        rows = day_tape(eng, cache, sym, day, until)
        eps = [r[0] for r in rows]
        for L in ls:
            t_in = _utc(L["t_in"]).replace(tzinfo=timezone.utc).timestamp()
            i0 = bisect.bisect_right(eps, t_in)
            i_end = bisect.bisect_right(eps, t_in + HORIZON_S)
            if i0 < 3 or i_end - i0 < 3:
                recs.append({"leg": L, "skip": "no_tape"})
                continue
            entry, qty, broker = L["entry_px"], L["qty"], L["stop_px"]
            sw = software_stop(broker, entry)
            lifts = c4_lifts(eng, L["session_id"], _utc(L["t_in"]), _utc(L["t_out"]))
            shipped, shipped_key = first_key(feats(rows[max(0, i0 - N): i0]), below=entry)
            old_base = shipped if shipped is not None else sw
            b = ledger_base(rows, i0, entry, sw)
            ctx = b["cont_context"]
            n4a = max_floor(sw, ctx["cont_candidate"], entry)
            n4p = max_floor(sw, ctx["cont_candidate_pct"], entry)
            variants = {
                "S0_live": (old_base, True, lifts),
                "N1_c4": (b["level"], False, lifts),
                "N1": (b["level"], False, []),
                "N4a_c4": (n4a, False, lifts),
                "N4p_c4": (n4p, False, lifts),
                "N1_ratchet_c4": (b["level"], True, lifts),
            }
            out = {"leg": L, "bases": {"sw": sw, "shipped": shipped, "shipped_key": shipped_key,
                                       "base": b, "n4a": n4a, "n4p": n4p,
                                       "n_c4": len(lifts), "c4_first_s": (lifts[0][0] - t_in) if lifts else None},
                   "var": {}}
            for name, (base, rat, lf) in variants.items():
                w = walk(rows, i0, i_end, entry, base, ratchet=rat, sw=sw, broker=broker, lifts=lf)
                jj = w.pop("j")
                w["pnl"] = (w["px"] - entry) * qty
                w["giveback"] = (w["mfe"] - w["px"]) * qty
                w["hold_s"] = w["ts"] - t_in
                b0 = rows[jj][4]
                w["bid0"] = b0 if (b0 and b0 > 0) else w["px"]
                w["pnl_bid"] = (w["bid0"] - entry) * qty
                if w["how"] == "broker_stop":
                    w["bid_lat"] = w["bid0"]
                else:
                    q2 = bisect.bisect_left(eps, rows[jj][0] + LAT_S)
                    w["bid_lat"] = rows[q2][4] if (q2 < len(rows) and rows[q2][4] and rows[q2][4] > 0) else w["bid0"]
                w["pnl_lat"] = (w["bid_lat"] - entry) * qty
                if w["how"] in ("deadman", "sw_stop", "broker_stop"):
                    w["reclaim5"] = reclaim_after(rows, jj, entry, 300)
                    w["reclaim15"] = reclaim_after(rows, jj, entry, 900)
                out["var"][name] = w
            recs.append(out)
            print(label, sym, L["t_in"][5:19], "ok", file=sys.stderr)
    return recs


def _q(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


PAIRS = [("N1_c4", "S0_live"), ("N1", "S0_live"), ("N1_c4", "N1"), ("N1_c4", "N4a_c4"),
         ("N1_c4", "N4p_c4"), ("N1_c4", "N1_ratchet_c4")]
PRICES = ("pnl", "pnl_bid", "pnl_lat")


def report(label, recs):
    ok = [r for r in recs if "var" in r]
    if not ok:
        print(f"\n===== {label}: no simulated legs")
        return
    days = {(r["leg"]["symbol"], r["leg"]["t_in"][:10]) for r in ok}
    print(f"\n===== {label}: {len(ok)} legs / {len(days)} symbol-days; actual {sum(r['leg']['pnl'] for r in ok):+.2f}")
    B = [r["bases"] for r in ok]
    print("shipped base binding:", dict(Counter(b["base"]["binding"] for b in B)),
          "| fallback reasons:", dict(Counter(b["base"]["fallback_reason"] for b in B)))
    ctxs = [b["base"]["cont_context"] for b in B]
    print("context no_candidate_reason:", dict(Counter(c["no_candidate_reason"] for c in ctxs)))
    print(f"context candidate above the resting stop: dollar {sum(1 for b in B if b['n4a'] != b['sw'])}/{len(B)}, "
          f"scale-free {sum(1 for b in B if b['n4p'] != b['sw'])}/{len(B)}")
    trunc = [c["ledger"] for c in ctxs if c.get("ledger")]
    print(f"ledger at the fill: max_cycles binding (n_cycles_total > max_cycles) on "
          f"{sum(1 for g in trunc if g.get('max_cycles_binding'))}/{len(B)}; n_cycles_total p50 "
          f"{_q([g.get('n_cycles_total') for g in trunc], .5)} p90 {_q([g.get('n_cycles_total') for g in trunc], .9)} "
          f"max {max((g.get('n_cycles_total') or 0) for g in trunc) if trunc else None}")
    d_old, d_new, d_4a, d_4p = [], [], [], []
    for r in ok:
        b, e = r["bases"], r["leg"]["entry_px"]
        if b["sw"] is not None and e > b["sw"]:
            R = e - b["sw"]
            if b["shipped"] is not None:
                d_old.append((e - b["shipped"]) / R)
            if b["base"]["level"] is not None:
                d_new.append((e - b["base"]["level"]) / R)
            if b["n4a"] is not None:
                d_4a.append((e - b["n4a"]) / R)
            if b["n4p"] is not None:
                d_4p.append((e - b["n4p"]) / R)
    for name, xs in (("old count-half", d_old), ("shipped (resting at fill)", d_new),
                     ("#1419 draft (dollar median)", d_4a), ("scale-free median", d_4p)):
        print(f"(entry - base)/R, R = entry - position stop: {name:28} p10 {_q(xs,.1)} p50 {_q(xs,.5)} "
              f"p90 {_q(xs,.9)} (n={len(xs)})")
    print(f"C4 lift inside the actual leg (observable up to the actual exit only): {sum(1 for b in B if b['n_c4'])}/{len(B)}")
    print(f"\n{'variant':15} {'print':>9} {'bid':>9} {'bid+lat':>9} {'win':>4} {'gb$':>7} {'hold_p50':>8}  how | reclaim5/15")
    for name in ok[0]["var"]:
        v = [r["var"][name] for r in ok]
        hits = [x for x in v if x["how"] in ("deadman", "sw_stop", "broker_stop")]
        print(f"{name:15} {sum(x['pnl'] for x in v):>+9.2f} {sum(x['pnl_bid'] for x in v):>+9.2f} "
              f"{sum(x['pnl_lat'] for x in v):>+9.2f} {sum(1 for x in v if x['pnl_lat'] > 0):>4} "
              f"{sum(x['giveback'] for x in v):>7.0f} {_q([x['hold_s'] for x in v], .5):>8}  "
              f"{dict(Counter(x['how'] for x in v))} | {sum(1 for h in hits if h.get('reclaim5'))}/{len(hits)} "
              f"{sum(1 for h in hits if h.get('reclaim15'))}/{len(hits)}")
    print("PAIRED diff = A - B per symbol-day; 2000x cluster bootstrap 90% CI; LOO-min")
    rnd = random.Random(65)
    for a, b in PAIRS:
        for p in PRICES:
            cl = {}
            for r in ok:
                key = (r["leg"]["symbol"], r["leg"]["t_in"][:10])
                cl[key] = cl.get(key, 0.0) + r["var"][a][p] - r["var"][b][p]
            vals = list(cl.values())
            tot = sum(vals)
            bs = sorted(sum(rnd.choice(vals) for _ in vals) for _ in range(2000))
            print(f"  {a:15} - {b:15} {p:8} {tot:+9.2f}  days +{sum(1 for x in vals if x > 1e-9)}/"
                  f"-{sum(1 for x in vals if x < -1e-9)}  CI90 [{bs[100]:+.0f}, {bs[1899]:+.0f}]  "
                  f"LOO-min {min(tot - x for x in vals):+.0f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window", action="append", default=[],
                    help="label=FROM,TO (UTC, on live_entry_filled.ts); repeatable")
    ap.add_argument("--legs-json", action="append", default=[],
                    help="label=path of a legs JSON (the shape read_legs returns); repeatable")
    ap.add_argument("--tape-cache", default=None, help="directory for per-symbol-day tape pickles")
    ap.add_argument("--out", default=None, help="pickle of every simulated leg")
    args = ap.parse_args(argv)
    if args.tape_cache:
        os.makedirs(args.tape_cache, exist_ok=True)
    eng = _engine()
    samples = []
    for w in args.window:
        label, rng = w.split("=", 1)
        a, b = rng.split(",", 1)
        samples.append((label, read_legs(eng, a, b)))
    for w in args.legs_json:
        label, path = w.split("=", 1)
        with open(path, encoding="utf-8") as fh:
            samples.append((label, json.load(fh)))
    res = {}
    for label, legs in samples:
        res[label] = simulate(eng, legs, args.tape_cache, label)
        report(label, res[label])
    if args.out:
        with open(args.out, "wb") as fh:
            pickle.dump(res, fh)


if __name__ == "__main__":
    main()
