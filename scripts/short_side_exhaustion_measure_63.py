"""[63] BACKSIDE = SHORT? — sukat ng short mula sa PAGOD na spike, sa parehong populasyon ng [62].

Ang tanong ng operator (09-11 01:40Z): "ano ang action kapag nasa backside? mukang di profitable
ang long, pero opportunity para sa short di ba?" Ang [62] ay nagpapaliit LANG ng laki ng long kapag
``cycle_exhaustion_score >= q90`` (continuation 18.5% ⇒ 81.5% ay hindi na gumagawa ng bagong high).
Kung 81.5% ang hindi umaakyat, ang tanong ay: MAGKANO ang nasa ibaba, at kayang-kaya ba ang stop?

Dalawang TRIGGER ang sinusukat, magkatabi, sa PAREHONG tape at PAREHONG exit rules:

  T1 ``rollover_top`` — ang unang panukala (scout, 09-11 02:20Z): short sa G rollover
      (``signed_tape_accel`` prev > 0, ngayon <= 0) habang ang print ay nasa itaas pa ng spike low.
  T2 ``exhausted``   — ang TANONG MISMO ng [63]: short sa UNANG hakbang kung saan ang
      ``cycle_exhaustion_score >= CYCLE_EXHAUSTION_Q90`` para sa kasalukuyang running high.

Bakit dalawa: ang score ay pinangungunahan ng ``pos_in_range`` (sign -1) — MATAAS ito sa tuktok ng
spike kung saan pumuputok ang rollover, kaya ang T1 ay halos HINDI KAILANMAN nasa Q90 band (sinukat:
0 sa 66 setup ng unang 6 na symbol-day). Ang pagod na estado ng [62] ay naaabot HABANG nasa
pullback, hindi sa tuktok. Ang T2 ang sumusukat sa tunay na populasyon ng [63].

Setup (pareho sa dalawa), isa kada running-high index:
  * entry = BID ng printong iyon (short = binebenta sa bid), fallback = presyo ng print
  * stop  = spike HIGH + ang SALAMIN ng resting buffer ng long (live_runner.py:11689 —
            ``max(px*0.0025, 0.25*|px-stop|, 0.01)``); walang bagong literal
  * exit A (structure): buong retrace ng spike (``H - 1.03 x amp``, ang p50 ng [53] sukat 3) o ang
            onset low, alinman ang unang matamaan; stop print >= stop ⇒ fill sa ``max(stop, ask)``
            (gap-through); 60-min na hangganan ng SUKAT lamang
  * exit B (print verdict): salamin ng D — pagkatapos ng pinakamababang print ng short, kapag
            ``signed_tape_accel > 0`` AT ``back_buy_share > front_buy_share`` sa mga print mula sa
            low na iyon (>= 3, capped 458), tinitingnan kada 100 print
  * MAE = squeeze (gaano kalayo umakyat laban sa atin, sa R), MFE = ang pinakamalaking nakuha

BORROW FILTER: ang short ay hindi umiiral sa pangalang hindi shortable. Ang ``--borrow-flags`` ay
isang pickle ng ``(symbol, tradable, shortable, easy_to_borrow, status, exchange)`` na binasa mula
sa Alpaca PAPER ``TradingClient.get_asset`` (ang KAPAREHONG field na inilalabas na ng adapter sa
``alpaca_spot.get_product().raw``). Ang huling talahanayan ay ang net pagkatapos ng filter na iyon.

READ-ONLY. Walang isinusulat sa DB. Bounded ang bawat pagbasa ng tape (symbol + 20-minutong hiwa).

    conda run -n chili-env python scripts/short_side_exhaustion_measure_63.py \\
        --from 2026-08-27 --to 2026-09-11 --tape-cache DIR [--borrow-flags flags.pkl]
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.entry_gates import (  # noqa: E402
    _signed_tape_features,
)
from app.services.trading.momentum_neural.tape_cycles import (  # noqa: E402
    CYCLE_EXHAUSTION_Q50,
    CYCLE_EXHAUSTION_Q90,
    CYCLE_EXHAUSTION_TERMS,
    CYCLE_LEDGER_MAX_CYCLES,
    CYCLE_PULLBACK_FRAC_BASE,
    PullbackCycleScanner,
    cycle_exhaustion_score,
    cycle_features_at,
)

ET = ZoneInfo("America/New_York")

# --- mga halagang HINANGO, hindi pinili (bawat isa ay may pinagmulan) ---------------------
WINDOW_PRINTS = 458   # p50 ng 15-s signed-tape window (sukat 2026-09-10, A script ng [44])
STEP = 25             # resolusyon ng pagsukat ng spike ([53]); hindi ito panuntunan
VERDICT_STEP = 100    # hakbang ng D verdict
HORIZON_MIN = 60      # hangganan ng SUKAT lamang (kapareho ng [62] MFE window)
RETRACE_X = 1.03      # [53] sukat 3: p50 na multiple ng buong retrace ng spike
RISK_USD_P50 = 41.79  # p50 ng TUNAY na (entry - resting stop) x qty ng 84 long leg (09-11)
# Ang buffer ng stop ay ang MISMONG literal ng resting stop ng long (live_runner.py:11689).
BUF_PCT = 0.0025
BUF_FRAC = 0.25
BUF_FLOOR = 0.01


def _q(vals, p):
    v = sorted(x for x in vals if x is not None and math.isfinite(x))
    if not v:
        return None
    k = (len(v) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def _fmt(x, d=2):
    return "  n/a" if x is None else f"{x:.{d}f}"


def _pos_f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def legs(eng, t0, t1):
    """Ang populasyon ng [62]: bawat live Alpaca leg na may fill sa window."""
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        rows = c.execute(
            text(
                """
            SELECT s.symbol, e.ts
              FROM trading_automation_events e
              JOIN trading_automation_sessions s ON s.id = e.session_id
             WHERE s.mode = 'live' AND s.execution_family = 'alpaca_spot'
               AND e.event_type = 'live_entry_filled'
               AND e.ts >= :t0 AND e.ts < :t1
             ORDER BY e.ts
                """
            ),
            {"t0": t0, "t1": t1},
        ).fetchall()
    return [(str(r[0]).upper(), r[1]) for r in rows]


def tape(eng, sym, a, b, cache_dir):
    """Buong-araw na tape ng symbol-day. Bounded: isang statement kada 20-minutong hiwa."""
    fp = os.path.join(cache_dir, f"{sym}_{a:%Y%m%dT%H%M}_{b:%Y%m%dT%H%M}.pkl")
    if os.path.exists(fp):
        with open(fp, "rb") as fh:
            raw = pickle.load(fh)
    else:
        raw = []
        t = a
        with eng.connect() as c:
            c.execute(text("SET statement_timeout='20s'"))
            while t < b:
                t2 = min(b, t + timedelta(minutes=20))
                raw.extend(
                    [
                        tuple(r)
                        for r in c.execute(
                            text(
                                "SELECT observed_at, price, size, bid, ask FROM iqfeed_trade_ticks "
                                "WHERE symbol = :s AND observed_at > :a AND observed_at <= :b "
                                "AND price IS NOT NULL AND price > 0 ORDER BY observed_at, id"
                            ),
                            {"s": sym, "a": t, "b": t2},
                        ).fetchall()
                    ]
                )
                t = t2
        os.makedirs(cache_dir, exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "wb") as fh:
            pickle.dump(raw, fh, protocol=4)
        os.replace(tmp, fp)
    return raw


def _walk_forward(setup, px, ask, ts, frows, n):
    """Dalawang independiyenteng forward walk (A = structure, B = print verdict)."""
    i0 = setup["idx"] + 1
    i_end = bisect.bisect_right(ts, setup["t"] + timedelta(minutes=HORIZON_MIN))
    i_end = min(i_end, n)
    entry, stop, target, risk = setup["entry"], setup["stop"], setup["target"], setup["risk"]

    hi = lo = entry
    lo_i = setup["idx"]
    exA = howA = None
    for k in range(i0, i_end):
        p = px[k]
        if p > hi:
            hi = p
        if p < lo:
            lo, lo_i = p, k
        if p >= stop:
            exA, howA = max(stop, ask[k] or p), "stop"
            break
        if p <= target:
            exA, howA = (ask[k] or p), "target"
            break
    if exA is None:
        k = max(i_end - 1, setup["idx"])
        exA, howA = (ask[k] or px[k]), "60m"

    loB, loB_i = entry, setup["idx"]
    exB = howB = None
    for k in range(i0, i_end):
        p = px[k]
        if p < loB:
            loB, loB_i = p, k
        if p >= stop:
            exB, howB = max(stop, ask[k] or p), "stop"
            break
        m = k - i0
        if m > 0 and m % VERDICT_STEP == 0 and (k - loB_i) >= 3:
            w = frows[max(loB_i + 1, k + 1 - WINDOW_PRINTS) : k + 1]
            f = _signed_tape_features(w, window_s=15.0, tick_rate_floor_pctile=0.0)
            if isinstance(f, dict):
                a2 = f.get("signed_tape_accel")
                fb = f.get("front_buy_share")
                bb = f.get("back_buy_share")
                if a2 is not None and fb is not None and bb is not None and a2 > 0 and bb > fb:
                    exB, howB = (ask[k] or p), "verdict"
                    break
    if exB is None:
        k = max(i_end - 1, setup["idx"])
        exB, howB = (ask[k] or px[k]), "60m"

    setup.update(
        {
            "exA": exA, "howA": howA, "RA": (entry - exA) / risk,
            "exB": exB, "howB": howB, "RB": (entry - exB) / risk,
            "mae_R": (hi - entry) / risk, "mfe_R": (entry - lo) / risk,
        }
    )
    return setup


def walk_day(sym, day, raw):
    """Isang pass sa tape ng symbol-day; parehong trigger ay sinusukat sa PAREHONG hakbang."""
    n = len(raw)
    ts = [r[0] for r in raw]
    px = [float(r[1]) for r in raw]
    bid = [_pos_f(r[3]) for r in raw]
    ask = [_pos_f(r[4]) for r in raw]
    prints = [(r[0], i + 1, r[1], r[2], r[3], r[4]) for i, r in enumerate(raw)]
    frows = [(r[1], r[2], r[3], r[4], r[0].timestamp()) for r in raw]

    sc = PullbackCycleScanner(CYCLE_PULLBACK_FRAC_BASE, max_cycles=CYCLE_LEDGER_MAX_CYCLES)
    prev_acc = None
    last_hod = {"rollover_top": -1, "exhausted": -1}
    setups = []
    i = 0
    while i < n:
        j = min(n, i + STEP)
        sc.feed(prints[i:j])
        idx = j - 1
        i = j
        if sc.spike_low is None or sc.hod is None:
            continue
        p = px[idx]
        f = _signed_tape_features(
            frows[max(0, idx + 1 - WINDOW_PRINTS) : idx + 1],
            window_s=15.0,
            tick_rate_floor_pctile=0.0,
        )
        acc = f.get("signed_tape_accel") if isinstance(f, dict) else None
        rollover = prev_acc is not None and acc is not None and prev_acc > 0 and acc <= 0
        if acc is not None:
            prev_acc = acc

        feats = cycle_features_at(sc, p)
        score, _det = cycle_exhaustion_score(feats, CYCLE_EXHAUSTION_TERMS)
        if score is None:
            continue

        for trig, fires in (
            # T1: rollover ng accel habang nasa itaas pa ng spike low (panukala ng scout)
            ("rollover_top", bool(rollover) and p > float(sc.spike_low)),
            # T2: unang hakbang na PAGOD na ayon sa [62] (ang tanong mismo ng [63])
            ("exhausted", score >= CYCLE_EXHAUSTION_Q90),
        ):
            if not fires or sc.hod_i == last_hod[trig]:
                continue
            H = float(sc.hod)
            amp = H - float(sc.spike_low)
            if amp <= 0:
                continue
            entry = bid[idx] if bid[idx] is not None and bid[idx] <= p else p
            stop = H + max(entry * BUF_PCT, BUF_FRAC * (H - entry), BUF_FLOOR)
            risk = stop - entry
            if risk <= 0:
                continue
            last_hod[trig] = sc.hod_i
            onset = float(sc.onset_low) if sc.onset_low else None
            setups.append(
                _walk_forward(
                    {
                        "sym": sym, "d": str(day), "trig": trig, "t": ts[idx], "idx": idx,
                        "score": score, "cycle": feats.get("cycle_index"),
                        "in_pb": feats.get("in_pullback"), "pos": feats.get("pos_in_range"),
                        "entry": entry, "H": H, "amp": amp, "stop": stop, "risk": risk,
                        "risk_pct": risk / entry * 100.0, "spike_low": float(sc.spike_low),
                        "onset_low": onset,
                        "target": max(H - RETRACE_X * amp, onset if onset is not None else -1.0),
                    },
                    px, ask, ts, frows, n,
                )
            )
    return setups


def report(name, rows):
    if not rows:
        print(f"\n== {name}: n=0")
        return
    n = len(rows)
    days = len({(r["sym"], r["d"]) for r in rows})
    print(f"\n== {name}: n={n} setups, {days} symbol-days")
    for v in ("A", "B"):
        Rs = [r[f"R{v}"] for r in rows]
        hows = defaultdict(int)
        for r in rows:
            hows[r[f"how{v}"]] += 1
        wins = sum(1 for x in Rs if x > 0)
        print(
            f"  exit {v}: sumR {sum(Rs):+8.2f}  meanR {sum(Rs)/n:+.3f}  "
            f"p10 {_fmt(_q(Rs,.1))} p25 {_fmt(_q(Rs,.25))} p50 {_fmt(_q(Rs,.5))} "
            f"p75 {_fmt(_q(Rs,.75))} p90 {_fmt(_q(Rs,.9))}  hit {wins}/{n}={wins/n:.2f}  "
            f"{dict(hows)}  P&L@${RISK_USD_P50} {sum(Rs)*RISK_USD_P50:+.2f}"
        )
    mae = [r["mae_R"] for r in rows]
    mfe = [r["mfe_R"] for r in rows]
    print(
        f"  MAE (squeeze) R: p50 {_fmt(_q(mae,.5))} p75 {_fmt(_q(mae,.75))} "
        f"p90 {_fmt(_q(mae,.9))} max {_fmt(max(mae))}  share>=1R(stop) "
        f"{sum(1 for x in mae if x >= 1)/n:.2f}"
    )
    print(
        f"  MFE R:           p50 {_fmt(_q(mfe,.5))} p75 {_fmt(_q(mfe,.75))} "
        f"p90 {_fmt(_q(mfe,.9))}  share>=1R {sum(1 for x in mfe if x >= 1)/n:.2f}"
    )
    print(
        f"  risk% (stop-entry)/entry: p50 {_fmt(_q([r['risk_pct'] for r in rows],.5))} "
        f"p90 {_fmt(_q([r['risk_pct'] for r in rows],.9))}   "
        f"score p50 {_fmt(_q([r['score'] for r in rows],.5),4)}   "
        f"cycle p50 {_fmt(_q([r['cycle'] for r in rows],.5),1)}"
    )


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t0", default="2026-08-27")
    ap.add_argument("--to", dest="t1", default="2026-09-11")
    ap.add_argument("--tape-cache", default=os.path.join(os.getcwd(), "tape_cache_62"))
    ap.add_argument("--borrow-flags", default="")
    ap.add_argument("--out-pickle", default="")
    ap.add_argument(
        "--db", default=os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
    )
    a = ap.parse_args(argv)

    shortable: set[str] | None = None
    if a.borrow_flags and os.path.exists(a.borrow_flags):
        with open(a.borrow_flags, "rb") as fh:
            flags = pickle.load(fh)
        shortable = {str(r[0]).upper() for r in flags if r[2] is True}
        print(
            f"borrow flags: {len(flags)} symbols, shortable {len(shortable)} "
            f"({sorted(shortable)}), easy_to_borrow "
            f"{sum(1 for r in flags if r[3] is True)}"
        )

    eng = create_engine(a.db, pool_pre_ping=True)
    t0 = datetime.fromisoformat(a.t0)
    t1 = datetime.fromisoformat(a.t1)
    days: dict[tuple[str, object], int] = defaultdict(int)
    for sym, t in legs(eng, t0, t1):
        t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
        days[(sym, t.astimezone(ET).date())] += 1
    print(
        f"legs {sum(days.values())}  symbol-days {len(days)}  "
        f"Q50 {CYCLE_EXHAUSTION_Q50} Q90 {CYCLE_EXHAUSTION_Q90}  "
        f"buffer=max({BUF_PCT:.4%},{BUF_FRAC}x(H-entry),{BUF_FLOOR})  retrace {RETRACE_X}x",
        flush=True,
    )

    allrows = []
    for (sym, d) in sorted(days, key=lambda k: (k[1], k[0])):
        base = datetime.combine(d, datetime.min.time(), tzinfo=ET)
        lo = base.replace(hour=4).astimezone(timezone.utc).replace(tzinfo=None)
        hi = base.replace(hour=16).astimezone(timezone.utc).replace(tzinfo=None)
        raw = tape(eng, sym, lo, hi, a.tape_cache)
        if len(raw) < 10:
            print(f"  {sym} {d}: {len(raw)} prints, skip", flush=True)
            continue
        rows = walk_day(sym, d, raw)
        n1 = [r for r in rows if r["trig"] == "rollover_top"]
        n2 = [r for r in rows if r["trig"] == "exhausted"]
        print(
            f"  {sym} {d}: {len(raw):>7} prints  T1 {len(n1):>3} (>=Q90 "
            f"{sum(1 for r in n1 if r['score'] >= CYCLE_EXHAUSTION_Q90):>2})  "
            f"T2 {len(n2):>3}  T2 sumRA {sum(r['RA'] for r in n2):+7.2f}"
            f"{'  [SHORTABLE]' if shortable and sym in shortable else ''}",
            flush=True,
        )
        allrows.extend(rows)

    if a.out_pickle:
        with open(a.out_pickle, "wb") as fh:
            pickle.dump(allrows, fh, protocol=4)

    t1rows = [r for r in allrows if r["trig"] == "rollover_top"]
    t2rows = [r for r in allrows if r["trig"] == "exhausted"]
    print("\n" + "=" * 78)
    print("TRIGGER T1 — rollover sa tuktok (panukala ng scout)")
    report("T1 lahat", t1rows)
    report("T1 score >= Q90 (ang hinihinging intersection)",
           [r for r in t1rows if r["score"] >= CYCLE_EXHAUSTION_Q90])
    print("\n" + "=" * 78)
    print("TRIGGER T2 — PAGOD na estado (ang tanong ng [63])")
    report("T2 lahat (score >= Q90 sa unang hakbang kada running high)", t2rows)
    rth = [
        r for r in t2rows
        if 13 <= r["t"].hour < 20 and not (r["t"].hour == 13 and r["t"].minute < 30)
    ]
    report("T2, RTH lamang (13:30-20:00Z)", rth)
    if shortable is not None:
        print("\n" + "=" * 78)
        print("PAGKATAPOS NG BORROW FILTER (Alpaca asset.shortable is True)")
        report("T2, shortable lamang", [r for r in t2rows if r["sym"] in shortable])
        report("T1, shortable lamang", [r for r in t1rows if r["sym"] in shortable])
        report("T2, HINDI shortable (hindi maipapatupad — sanggunian lamang)",
               [r for r in t2rows if r["sym"] not in shortable])

    byday = defaultdict(list)
    for r in t2rows:
        byday[(r["sym"], r["d"])].append(r)
    for v in ("A", "B"):
        sums = [sum(x[f"R{v}"] for x in rs) for rs in byday.values()]
        if sums:
            print(
                f"\n  T2 exit {v} kada symbol-day: {len(sums)} araw, positibo "
                f"{sum(1 for s in sums if s > 0)}, p25 {_fmt(_q(sums,.25))} "
                f"p50 {_fmt(_q(sums,.5))} p75 {_fmt(_q(sums,.75))}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
