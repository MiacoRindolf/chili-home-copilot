"""[62] DERIVATION + REPLAY — kung ano ang GINAWA SANA ng cycle-exhaustion conditioning.

Binabasa ang bawat live Alpaca leg sa isang window, itinatayo muli ang tape ng symbol-day nito
mula sa ``iqfeed_trade_ticks`` (BOUNDED: isang statement kada 20-minutong hiwa — ang isang
buong-araw na statement ay tumatama sa 20 s timeout sa BAWAT araw; RDHL 08-31 = 360,297 print),
at pinapakain ang IPINADALANG ``PullbackCycleScanner`` — hindi kopya, ang mismong module na
tumatakbo sa runner. Kaya ang mga numerong iniuulat dito ay ang mismong numerong bubuuin ng
resibo sa live.

⚠️ SA DESISYONG SANDALI, HINDI SA FILL (refuter, 2026-09-11). Ang laki ay napagdedesisyunan
isang tick BAGO pa ipadala ang order; ang fill ay dumarating p50 7.96 s (p90 14.08, max 24.86)
pagkatapos — daan-daang print ang layo sa dating na p90 11.45 print/s. Kaya ang tape ay
pinapakain hanggang sa ``live_entry_submitted.ts`` bawas ang ``place_profile_ms.total`` (ang
simula ng place path, ilang millisegundo pagkatapos ng sizing block), at ang presyong isinusukat
ay ang HULING PRINT — hindi ang fill, hindi ang quote-mid — kapareho ng
``live_runner._cycle_exhaustion_conditioning``.

Iniuulat:
  * distribusyon ng ``cycle_exhaustion_score`` sa mga leg (p10/p25/p50/p75/p90) — ito ang
    ``q50``/``q90`` na binding ng size ramp;
  * P&L / win-rate / continuation kada tercile ng score — ang RATIO ng win-rate ng pinakamataas
    sa pinakamababang tercile ang hinangong ``floor`` ng multiplier;
  * ang replay mismo: ``eff_max_loss × mult`` kada leg at ang delta ng P&L sa fill price
    (risk-first sizing ⇒ ang dami ay linear sa budget, kaya ang P&L ay nag-i-scale ng mult).

READ-ONLY. Walang isinusulat sa DB.

    conda run -n chili-env python scripts/cycle_exhaustion_replay_62.py \\
        --from 2026-08-27 --to 2026-09-11 [--tape-cache DIR]
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.tape_cycles import (  # noqa: E402
    CYCLE_EXHAUSTION_TERMS,
    CYCLE_LEDGER_MAX_CYCLES,
    CYCLE_PULLBACK_FRAC_BASE,
    PullbackCycleScanner,
    cycle_exhaustion_score,
    cycle_exhaustion_size_multiplier,
    cycle_features_at,
)

ET = ZoneInfo("America/New_York")
HORIZON_MIN = 60  # bound ng pagsukat ng MFE/continuation LAMANG — hindi ito panuntunan
FEED_CHUNK = 4999  # kapareho ng hugis ng live feed: paunti-unting pagpapakain, hindi isang batch


def _q(vals, p):
    v = sorted(x for x in vals if x is not None and math.isfinite(x))
    if not v:
        return None
    k = (len(v) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def _fmt(x, d=3):
    return " n/a" if x is None else f"{x:.{d}f}"


def legs(eng, t0, t1):
    """Bawat live Alpaca leg, KASAMA ang DESISYONG SANDALI nito.

    ⚠️ ANG PINAKAMAHALAGANG PAGWAWASTO (refuter, 2026-09-11). Ang unang bersyon ay
    nagpapakain ng tape hanggang sa ``live_entry_filled.ts`` at nag-i-score sa presyo ng
    FILL. Pero ang laki ay napagdedesisyunan SA NAUNANG TICK, bago pa ipadala ang order.
    Sinukat sa mismong populasyon (85 leg, 2026-08-27..2026-09-11): ang puwang mula
    ``live_entry_pending_place`` hanggang fill ay p10 6.93 s / p50 13.76 s / p90 30.24 s /
    max 97.24 s. Sa sinukat na steady-state na dating (p90 11.45 print/s) iyon ay ~158
    print na WALA PA sa desisyong sandali — at iyon mismo ang mga printong gumagalaw ng
    ``pos_in_range``, ``cur_buy_share`` at ``prints_since_high``, at kayang magsara ng
    isa pang cycle (kaya nagpapalit ng ``ext_x_amp0``/``last_pb_depth_ratio`` mula None
    tungo sa nababasa). Kaya ang q50/q90/floor na hinango sa fill instant ay hindi
    tumutugma sa distribusyong makikita ng live.

    ANG DESISYONG SANDALI: ang ``live_entry_submitted`` ay inilalabas sa PAREHONG tick ng
    sizing (pagkatapos bumalik ang broker place), at ang payload nito ay may
    ``place_profile_ms.total`` = ang buong oras ng place path. Kaya
    ``t_dec = submitted.ts - total_ms`` ay ang simula ng place path, ilang millisegundo
    pagkatapos ng sizing block. Fallback: ``submitted.ts``, tapos ``pending_place.ts``.
    """
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        rows = c.execute(
            text(
                """
            WITH ent AS (
              SELECT s.id AS session_id, s.symbol, e.ts AS t_in,
                     (e.payload_json->>'avg')::numeric AS entry_px,
                     (e.payload_json->>'quantity')::numeric AS qty,
                     lag(e.ts) OVER (PARTITION BY s.id ORDER BY e.ts) AS prev_in,
                     lead(e.ts) OVER (PARTITION BY s.id ORDER BY e.ts) AS next_in
              FROM trading_automation_events e JOIN trading_automation_sessions s ON s.id=e.session_id
              WHERE s.mode='live' AND s.execution_family='alpaca_spot' AND e.event_type='live_entry_filled'
                AND e.ts >= :t0 AND e.ts < :t1
            ), sub AS (
              SELECT ent.*, (
                SELECT x.ts - make_interval(secs => coalesce(
                         (x.payload_json->'place_profile_ms'->>'total')::numeric, 0) / 1000.0)
                FROM trading_automation_events x
                WHERE x.session_id=ent.session_id AND x.event_type='live_entry_submitted'
                  AND x.ts <= ent.t_in AND (ent.prev_in IS NULL OR x.ts > ent.prev_in)
                ORDER BY x.ts DESC LIMIT 1) AS t_submit,
              (SELECT max(x.ts) FROM trading_automation_events x
                WHERE x.session_id=ent.session_id AND x.event_type='live_entry_pending_place'
                  AND x.ts <= ent.t_in AND (ent.prev_in IS NULL OR x.ts > ent.prev_in)) AS t_pending
              FROM ent
            )
            SELECT sub.session_id, sub.symbol, sub.t_in, sub.entry_px, sub.qty,
                   coalesce(sub.t_submit, sub.t_pending, sub.t_in) AS t_dec,
                   (sub.t_submit IS NOT NULL) AS dec_from_submit,
                   (SELECT sum((x.payload_json->>'pnl_usd')::numeric) FROM trading_automation_events x
                     WHERE x.session_id=sub.session_id AND x.event_type IN ('live_exit_filled','live_partial_exit_filled')
                       AND x.ts > sub.t_in AND (sub.next_in IS NULL OR x.ts < sub.next_in)) AS pnl,
                   (SELECT count(*) FROM trading_automation_events x
                     WHERE x.session_id=sub.session_id AND x.event_type='live_exit_filled'
                       AND x.ts > sub.t_in AND (sub.next_in IS NULL OR x.ts < sub.next_in)) AS n_exit
            FROM sub ORDER BY sub.t_in
        """
            ),
            {"t0": t0, "t1": t1},
        ).fetchall()
    return [r for r in rows if r.entry_px and r.qty and r.n_exit and r.pnl is not None]


def tape(eng, sym, a, b, cache_dir=None, chunk_min=20):
    """Isang statement kada `chunk_min` minuto (bawat isa ay nasa ilalim ng 20 s timeout)."""
    if cache_dir:
        import pickle

        fp = os.path.join(cache_dir, f"{sym}_{a:%Y%m%dT%H%M}_{b:%Y%m%dT%H%M}.pkl")
        if os.path.exists(fp):
            with open(fp, "rb") as fh:
                raw = pickle.load(fh)
            return [(r[0], i + 1, r[1], r[2], r[3], r[4]) for i, r in enumerate(raw)]
    out = []
    t = a
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        while t < b:
            t2 = min(b, t + timedelta(minutes=chunk_min))
            out.extend(
                [
                    tuple(r)
                    for r in c.execute(
                        text(
                            """
                SELECT observed_at, id, price, size, bid, ask FROM iqfeed_trade_ticks
                WHERE symbol=:s AND observed_at > :a AND observed_at <= :b AND price IS NOT NULL AND price > 0
                ORDER BY observed_at, id
            """
                        ),
                        {"s": sym, "a": t, "b": t2},
                    ).fetchall()
                ]
            )
            t = t2
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t0", default="2026-08-27")
    ap.add_argument("--to", dest="t1", default="2026-09-11")
    ap.add_argument("--tape-cache", dest="cache", default=None)
    ap.add_argument("--frac", type=float, default=CYCLE_PULLBACK_FRAC_BASE)
    ap.add_argument("--floor", type=float, default=None, help="override ang hinangong floor")
    ap.add_argument("--q50", type=float, default=None)
    ap.add_argument("--q90", type=float, default=None)
    a = ap.parse_args()
    t0 = datetime.fromisoformat(a.t0)
    t1 = datetime.fromisoformat(a.t1)
    eng = create_engine(
        os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili"),
        pool_pre_ping=True,
    )
    L = legs(eng, t0, t1)
    print(f"legs: {len(L)}   pullback_frac={a.frac}   terms={list(CYCLE_EXHAUSTION_TERMS)}")

    gaps = sorted(
        (leg.t_in - leg.t_dec).total_seconds()
        for leg in L
        if leg.t_dec is not None and leg.t_in is not None
    )
    if gaps:
        print(
            "desisyon->fill na puwang (s): p10 %s p50 %s p90 %s max %s   (%d/%d mula sa submitted)"
            % (
                _fmt(_q(gaps, 0.10), 2),
                _fmt(_q(gaps, 0.50), 2),
                _fmt(_q(gaps, 0.90), 2),
                _fmt(gaps[-1], 2),
                sum(1 for leg in L if leg.dec_from_submit),
                len(L),
            )
        )

    days = defaultdict(list)
    for leg in L:
        t = leg.t_in.replace(tzinfo=None) if leg.t_in.tzinfo else leg.t_in
        d = t.replace(tzinfo=timezone.utc).astimezone(ET).date()
        days[(leg.symbol, d)].append(leg)

    rows = []
    for (sym, d), Ls in sorted(days.items(), key=lambda kv: kv[1][0].t_in):
        base = datetime.combine(d, datetime.min.time(), tzinfo=ET)
        day_start = base.replace(hour=4).astimezone(timezone.utc).replace(tzinfo=None)
        day_end = base.replace(hour=16).astimezone(timezone.utc).replace(tzinfo=None)
        prints = tape(eng, sym, day_start, day_end, cache_dir=a.cache)
        if len(prints) < 10:
            print(f"  {sym} {d}: {len(prints)} prints, laktawan")
            continue
        ts = [p[0] for p in prints]
        want = sorted(
            (
                (leg.t_dec or leg.t_in).replace(tzinfo=None)
                if (leg.t_dec or leg.t_in).tzinfo
                else (leg.t_dec or leg.t_in),
                leg.t_in.replace(tzinfo=None) if leg.t_in.tzinfo else leg.t_in,
                leg,
            )
            for leg in Ls
        )
        sc = PullbackCycleScanner(a.frac, max_cycles=CYCLE_LEDGER_MAX_CYCLES)
        i = 0
        for t_dec, t_in, leg in want:
            # ipakain lahat ng print HANGGANG sa DESISYONG sandali (hindi sa fill — tingnan
            # ang docstring ng `legs`), paunti-unti, gaya ng live
            j = i
            while j < len(prints) and ts[j] <= t_dec:
                j += 1
            while i < j:
                k = min(j, i + FEED_CHUNK)
                sc.feed(prints[i:k])
                i = k
            if sc.n_prints < 2:
                continue
            entry = float(leg.entry_px)
            # ANG PRESYONG SINUSUKAT AY ANG HULING PRINT, hindi ang fill at hindi ang
            # quote-mid — kapareho ng ginagawa ng `_cycle_exhaustion_conditioning` sa live.
            px_dec = sc.last_px if sc.last_px is not None else entry
            feats = cycle_features_at(sc, px_dec)
            score, detail = cycle_exhaustion_score(feats, CYCLE_EXHAUSTION_TERMS)
            hod = float(sc.hod or entry)
            # ang horizon ng continuation label ay mula sa FILL (ang trade ay nabubuhay doon)
            k_fill = j
            while k_fill < len(prints) and ts[k_fill] <= t_in:
                k_fill += 1
            t_h = t_in + timedelta(minutes=HORIZON_MIN)
            fwd = [float(p[2]) for p in prints[k_fill:] if p[0] <= t_h]
            rows.append(
                {
                    "sym": sym,
                    "d": str(d),
                    "t": t_in,
                    "pnl": float(leg.pnl),
                    "score": score,
                    "n_terms": (detail or {}).get("n_terms"),
                    "reason": (detail or {}).get("reason"),
                    "cycle": feats.get("cycle_index"),
                    "in_pb": feats.get("in_pullback"),
                    "cont": bool(fwd) and max(fwd) > hod,
                    "mfe": ((max(fwd) - entry) / entry * 100.0) if fwd else None,
                    "terms": (detail or {}).get("terms"),
                }
            )
    scored = [r for r in rows if r["score"] is not None]
    unscored = [r for r in rows if r["score"] is None]
    print(f"\nlegs na may score: {len(scored)} / {len(rows)}")
    if unscored:
        by_reason = defaultdict(int)
        for r in unscored:
            by_reason[str(r.get("reason"))] += 1
        # Ang mga leg na ito ay tumatakbo sa mult 1.0 na may PANGALANG dahilan — walang
        # conditioning kapag hindi kumpleto ang hanay ng termino (kulang pa ang cycle).
        print(
            "  walang score: "
            + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items()))
            + f"   (cycle_index p50 {_fmt(_q([r['cycle'] for r in unscored], .5), 1)})"
        )
        n_up = sum(1 for r in unscored if r["pnl"] > 0)
        print(
            f"  P&L ng walang score: {sum(r['pnl'] for r in unscored):.2f} "
            f"(winrate {n_up / len(unscored):.2f}, cont {sum(1 for r in unscored if r['cont']) / len(unscored):.2f})"
        )
    if not scored:
        return 1
    print(
        "  cycle_index sa desisyon (may score): p10 %s p50 %s p90 %s"
        % tuple(_fmt(_q([r["cycle"] for r in scored], p), 1) for p in (0.10, 0.50, 0.90))
    )
    qs = {p: _q([r["score"] for r in scored], p) for p in (0.10, 0.25, 0.50, 0.75, 0.90)}
    print(
        "score quantiles: p10 %s  p25 %s  p50 %s  p75 %s  p90 %s"
        % tuple(_fmt(qs[p], 4) for p in (0.10, 0.25, 0.50, 0.75, 0.90))
    )

    # --- tercile: ang RATIO ng win-rate ng pinakamataas sa pinakamababa = hinangong floor
    srt = sorted(scored, key=lambda r: r["score"])
    n = len(srt)
    cut = n // 3
    terc = [srt[:cut], srt[cut : n - cut], srt[n - cut :]]
    print(f"\n{'tercile':>8} {'n':>3} {'score_p50':>10} {'sumPnL':>10} {'winrate':>8} {'cont%':>6} {'medMFE%':>8} {'medCycle':>9}")
    wr = []
    for name, rs in zip(("low", "mid", "high"), terc):
        if not rs:
            continue
        w = sum(1 for r in rs if r["pnl"] > 0) / len(rs)
        wr.append(w)
        print(
            f"{name:>8} {len(rs):>3} {_fmt(_q([r['score'] for r in rs], .5), 4):>10} "
            f"{sum(r['pnl'] for r in rs):>10.2f} {w:>8.2f} "
            f"{sum(1 for r in rs if r['cont'])/len(rs):>6.2f} {_fmt(_q([r['mfe'] for r in rs], .5), 2):>8} "
            f"{_fmt(_q([r['cycle'] for r in rs], .5), 1):>9}"
        )
    # ANG FLOOR. Ang unang panukala ay win-rate(high)/win-rate(low) — PINABULAANAN ito ng datos:
    # halos patag ang win-rate sa tatlong tercile (ang SARILI nating exit ang pumuputol sa bawat
    # panalo — [46]: 89% ng 189 na natalong leg ay BERDE noong isang sandali), kaya lumalabas
    # ang ratio na > 1 at magiging RESIBO lamang ang buong mekanismo. Ang label na PINAGPILIAN
    # ng mga termino ay CONTINUATION (bagong HOD sa loob ng 60 min) at doon MONOTONE ang score —
    # iyon ang hinahangan ng floor: gaano kadalas pa ibinibigay ng tape ang susunod na bagong
    # high sa pinakapagod na tercile kumpara sa pinakasariwa. Meta-labeling-as-sizing, kapareho
    # ng doktrina ng `chili_momentum_frontside_size_floor`.
    cr = [((sum(1 for r in rs if r["cont"]) / len(rs)) if rs else None) for rs in terc]
    derived_floor = None
    if len(wr) == 3 and wr[0] > 0:
        print(
            f"\nMONEY CHECK win-rate(high)/win-rate(low) = {wr[2]:.4f}/{wr[0]:.4f} = {wr[2] / wr[0]:.4f}"
            "  (patag => HINDI ito ang floor)"
        )
    if cr[0] and cr[2] is not None:
        derived_floor = cr[2] / cr[0]
        print(
            f"continuation(high)/continuation(low) = {cr[2]:.4f}/{cr[0]:.4f} = {derived_floor:.4f}  <= ANG FLOOR"
        )
    floor = a.floor if a.floor is not None else max(0.25, min(1.0, derived_floor or 1.0))
    q50 = a.q50 if a.q50 is not None else qs[0.50]
    q90 = a.q90 if a.q90 is not None else qs[0.90]
    print(f"binding: floor={floor:.4f} (clamped >= chili_momentum_frontside_size_floor 0.25)  q50={q50:.4f}  q90={q90:.4f}")

    # --- REPLAY: risk-first sizing ⇒ dami ∝ budget ⇒ P&L ∝ mult sa parehong fill price
    tot = tot_new = 0.0
    n_down = 0
    mults = []
    for r in scored:
        m, _ = cycle_exhaustion_size_multiplier(r["score"], floor=floor, q50=q50, q90=q90)
        mults.append(m)
        tot += r["pnl"]
        tot_new += r["pnl"] * m
        if m < 1.0:
            n_down += 1
    print(
        f"\nREPLAY sa {len(scored)} leg: actual P&L {tot:.2f} -> conditioned {tot_new:.2f} "
        f"(delta {tot_new - tot:+.2f}); {n_down} leg ang na-size-down "
        f"(mult p10 {_fmt(_q(mults,.1),3)} p50 {_fmt(_q(mults,.5),3)} p90 {_fmt(_q(mults,.9),3)}, min {min(mults):.3f})"
    )
    win = sum(r["pnl"] for r in scored if r["pnl"] > 0)
    los = sum(r["pnl"] for r in scored if r["pnl"] <= 0)
    win_n = sum(r["pnl"] * m for r, m in zip(scored, mults) if r["pnl"] > 0)
    los_n = sum(r["pnl"] * m for r, m in zip(scored, mults) if r["pnl"] <= 0)
    print(f"  winners {win:.2f} -> {win_n:.2f} ({win_n - win:+.2f})   losers {los:.2f} -> {los_n:.2f} ({los_n - los:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
