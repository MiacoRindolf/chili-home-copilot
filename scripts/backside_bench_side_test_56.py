"""[56] DERIVATION — may binibili ba ang backside bench sa TAPE?

Ang tanong ng bench ay "nasa likod na ba ng galaw ang pangalan — HINDI na ba ito gagawa ng
bagong high?". Ang tape ang sumasagot: pagkatapos ng sandali ng desisyon, alin ang UNANG
nangyari sa mga PRINT — umabot ba sa high ng araw, o bumaba nang PAREHONG layo sa ilalim?

    H  = ang running high ng tape hanggang t (default: ``run_hi`` = pinakamataas na print mula
         04:00 ET — ang katumbas sa tape ng ``cur_hod`` ng bench, premarket-inclusive)
    px = ang HULING PRINT sa t (hindi quote, hindi bar close)
    UP   = ang unang print na >= H ay nauna sa unang print na <= px - (H - px)
    DOWN = kabaligtaran;  NONE = walang naabot bago matapos ang tape (20:00 ET)

Sa martingale ang null ay 0.5. Kung TAMA ang bench, ang mga sandaling tinanggihan nito ay
dapat MAS MADALANG mag-UP kaysa sa mga sandaling PINAPASOK natin. Kaya dalawang populasyon,
IISANG label, IISANG tape:

  * BENCH   = bawat ``live_entry_backside_bench_veto`` (ang trigger na PUMUTOK at kinain ng
              bench) sa live alpaca_spot, kasama ang ``benched_at_hod`` ng payload;
  * CONTROL = bawat live alpaca_spot entry DECISION instant
              (``live_entry_submitted.ts`` bawas ``place_profile_ms.total`` — ang simula ng
              place path, kapareho ng [62]).

Clustered = mean ng up-rate kada symbol-day (ang isang pangalang tumanggi nang 2,000 beses ay
IISANG boto, hindi 2,000). Ang derived size mult ng bench = ``min(1, bench_up / control_up)``
— size-DOWN lang, at kapag >= 1 ay walang sariling conditioning ang bench.

Iniuulat din:
  * ``--anchor scanner_hod`` = ang anchor ng scout (``PullbackCycleScanner.hod``, na bago ang
    unang cycle ay nagre-reset pababa sa onset low) — para mareproduce ang planner row;
  * ang BAR-anchor scorecard (``benched_at_hod`` ng payload bilang barrier, ``current_px``
    bilang presyo) na may binomial sa tama/maling cluster;
  * ang hati bago/pagkatapos ng 86ed59aaf (``--post-fix-date``, ET date);
  * ang [62] ``cycle_exhaustion`` mult band sa loob ng bawat populasyon (IPINADALANG scanner
    at score — hindi kopya).

READ-ONLY. Bawat tape read ay may symbol + 20-minutong bound (``iqfeed_trade_ticks`` ay 211M
hilera) at ``statement_timeout=20s``. Walang isinusulat.

    conda run -n chili-env python scripts/backside_bench_side_test_56.py \\
        --from 2026-09-01 --to 2026-09-11 [--tape-cache DIR] [--anchor run_hi|scanner_hod]
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.services.trading.momentum_neural.tape_cycles import (  # noqa: E402
    CYCLE_EXHAUSTION_FLOOR,
    CYCLE_EXHAUSTION_Q50,
    CYCLE_EXHAUSTION_Q90,
    CYCLE_EXHAUSTION_TERMS,
    CYCLE_PULLBACK_FRAC_BASE,
    PullbackCycleScanner,
    cycle_exhaustion_score,
    cycle_exhaustion_size_multiplier,
    cycle_features_at,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
CHUNK_MIN = 20  # isang statement kada 20 minuto — ang buong-araw na statement ay tumatama sa 20 s


def _q(vals: Iterable[float | None], p: float) -> float | None:
    v = sorted(x for x in vals if x is not None and math.isfinite(x))
    if not v:
        return None
    k = (len(v) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def _binom_two_sided(k: int, n: int) -> float:
    """Exact two-sided binomial p (p=0.5): kabuuan ng lahat ng kinalabasang kasing-dalang o
    mas madalang pa kaysa sa nakita. Walang scipy."""
    if n <= 0:
        return 1.0
    pmf = [math.comb(n, i) / 2.0**n for i in range(n + 1)]
    obs = pmf[k]
    return min(1.0, sum(x for x in pmf if x <= obs * (1.0 + 1e-9)))


def bench_vetoes(eng, t0: datetime, t1: datetime) -> list[Any]:
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        return c.execute(
            text(
                """
            SELECT e.session_id, s.symbol, e.ts,
                   coalesce((e.payload_json->>'current_px')::float,
                            (e.payload_json->>'price')::float) AS px,
                   (e.payload_json->>'benched_at_hod')::float AS hod,
                   e.payload_json->>'reason' AS reason,
                   e.payload_json->>'blocked_trigger' AS trig
            FROM trading_automation_events e
            JOIN trading_automation_sessions s ON s.id = e.session_id
            WHERE e.event_type = 'live_entry_backside_bench_veto'
              AND s.mode = 'live' AND s.execution_family = 'alpaca_spot'
              AND e.ts >= :t0 AND e.ts < :t1
            ORDER BY s.symbol, e.ts"""
            ),
            {"t0": t0, "t1": t1},
        ).fetchall()


def control_decisions(eng, t0: datetime, t1: datetime) -> list[Any]:
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        return c.execute(
            text(
                """
            SELECT s.id AS session_id, s.symbol,
                   x.ts - make_interval(secs => coalesce(
                       (x.payload_json->'place_profile_ms'->>'total')::numeric, 0) / 1000.0) AS ts,
                   NULL::float AS px, NULL::float AS hod, 'control' AS reason, NULL AS trig
            FROM trading_automation_events x
            JOIN trading_automation_sessions s ON s.id = x.session_id
            WHERE s.mode = 'live' AND s.execution_family = 'alpaca_spot'
              AND x.event_type = 'live_entry_submitted'
              AND x.ts >= :t0 AND x.ts < :t1
            ORDER BY s.symbol, x.ts"""
            ),
            {"t0": t0, "t1": t1},
        ).fetchall()


def tape(eng, sym: str, day: date, cache_dir: str | None) -> list[tuple]:
    """Ang tape ng symbol-day, 04:00–20:00 ET, sa 20-minutong hiwa. Ang cache (kung meron) ay
    ``{sym}_{day}.pkl`` na may parehong hugis ng hilera ``(observed_at, id, price, size, bid,
    ask)`` — ang anyo ng cache ng scout (scratchpad/m56/tape)."""
    fp = os.path.join(cache_dir, f"{sym}_{day.isoformat()}.pkl") if cache_dir else None
    if fp and os.path.exists(fp):
        with open(fp, "rb") as fh:
            return pickle.load(fh)
    a = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET).astimezone(UTC).replace(tzinfo=None)
    b = a + timedelta(hours=16)
    out: list[tuple] = []
    t = a
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        while t < b:
            t2 = min(b, t + timedelta(minutes=CHUNK_MIN))
            out.extend(
                tuple(r)
                for r in c.execute(
                    text(
                        "SELECT observed_at, id, price, size, bid, ask FROM iqfeed_trade_ticks "
                        "WHERE symbol = :s AND observed_at > :a AND observed_at <= :b "
                        "AND price IS NOT NULL AND price > 0 ORDER BY observed_at, id"
                    ),
                    {"s": sym, "a": t, "b": t2},
                ).fetchall()
            )
            t = t2
    if fp:
        os.makedirs(cache_dir, exist_ok=True)
        with open(fp, "wb") as fh:
            pickle.dump(out, fh)
    return out


def first_passage(after: np.ndarray, up: float, dn: float) -> str:
    hu = after >= up
    hd = after <= dn
    ju = int(hu.argmax()) if hu.any() else None
    jd = int(hd.argmax()) if hd.any() else None
    if ju is None and jd is None:
        return "NONE"
    if jd is None or (ju is not None and ju < jd):
        return "UP"
    return "DOWN"


def label_population(eng, pop, cache_dir: str | None, anchor: str) -> list[dict[str, Any]]:
    """``anchor``: ``run_hi`` (tape day-high), ``scanner_hod`` (ang anchor ng scout), o
    ``payload`` (``benched_at_hod`` + ``current_px`` ng veto payload — ang BAR anchor)."""
    by: dict[tuple[str, date], list[Any]] = defaultdict(list)
    for r in pop:
        by[(r.symbol, r.ts.replace(tzinfo=UTC).astimezone(ET).date())].append(r)
    rows: list[dict[str, Any]] = []
    for (sym, d), vs in sorted(by.items()):
        tp = tape(eng, sym, d, cache_dir)
        if not tp:
            continue
        npx = np.asarray([float(r[2]) for r in tp])
        ts_arr = [r[0] for r in tp]
        n = len(tp)
        sc = PullbackCycleScanner(CYCLE_PULLBACK_FRAC_BASE)
        i = 0
        memo: dict[Any, dict[str, Any]] = {}
        for v in sorted(vs, key=lambda x: x.ts):
            while i < n and ts_arr[i] <= v.ts:
                sc.feed([tp[i]])
                i += 1
            if i == 0:
                continue
            if anchor == "payload":
                if not (v.px and v.hod and v.hod > v.px > 0):
                    continue
                H, px = float(v.hod), float(v.px)
                key = (i, round(H, 6), round(px, 6))
            else:
                px = float(sc.last_px)
                H = float(sc.run_hi if anchor == "run_hi" else sc.hod)
                key = i
            if key in memo:
                rows.append({**memo[key], "cl": (sym, d), "reason": v.reason})
                continue
            if not H > px:
                lab = "ATHIGH"
            else:
                lab = first_passage(npx[i:], H, px - (H - px))
            feats = cycle_features_at(sc, sc.last_px)
            score, _ = cycle_exhaustion_score(feats, CYCLE_EXHAUSTION_TERMS)
            mult, _ = cycle_exhaustion_size_multiplier(
                score, floor=CYCLE_EXHAUSTION_FLOOR, q50=CYCLE_EXHAUSTION_Q50, q90=CYCLE_EXHAUSTION_Q90
            )
            memo[key] = {"lab": lab, "mult": mult, "retr": (H - px) / H if H > 0 else None}
            rows.append({**memo[key], "cl": (sym, d), "reason": v.reason})
    return rows


def summarize(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    lab: dict[str, int] = defaultdict(int)
    for r in rows:
        lab[r["lab"]] += 1
    cl: dict[Any, list[float]] = defaultdict(list)
    for r in rows:
        if r["lab"] in ("UP", "DOWN"):
            cl[r["cl"]].append(1.0 if r["lab"] == "UP" else 0.0)
    rates = [sum(x) / len(x) for x in cl.values() if x]
    res = sum(len(x) for x in cl.values())
    pooled = sum(sum(x) for x in cl.values()) / res if res else None
    clustered = sum(rates) / len(rates) if rates else None
    right = sum(1 for x in rates if x < 0.5)  # bench TAMA = mas madalas bumaba kaysa umakyat
    wrong = sum(1 for x in rates if x > 0.5)
    out = {
        "name": name, "n": len(rows), "labels": dict(lab), "clusters": len(rates),
        "resolved": res, "pooled_up": pooled, "clustered_up": clustered,
        "right": right, "wrong": wrong,
        "binom_p": _binom_two_sided(right, right + wrong) if (right + wrong) else None,
    }
    print(
        f"[{name}] n={len(rows)} labels={dict(lab)} clusters={len(rates)} resolved={res} "
        f"pooled_up={_fmt(pooled)} clustered_up={_fmt(clustered)} "
        f"p25={_fmt(_q(rates, .25))} p50={_fmt(_q(rates, .5))} p75={_fmt(_q(rates, .75))} "
        f"right={right} wrong={wrong} binom_p={_fmt(out['binom_p'])}"
    )
    bands = (
        (0.999, 2.0, "mult=1.0"),
        (CYCLE_EXHAUSTION_FLOOR + 1e-9, 0.999, "ramp"),
        (-1.0, CYCLE_EXHAUSTION_FLOOR + 1e-9, "floor"),
    )
    out["bands"] = {}
    for lo, hi, nm in bands:
        c2: dict[Any, list[float]] = defaultdict(list)
        for r in rows:
            m = r["mult"] if r["mult"] is not None else 1.0
            if r["lab"] in ("UP", "DOWN") and lo <= m < hi:
                c2[r["cl"]].append(1.0 if r["lab"] == "UP" else 0.0)
        rr = [sum(x) / len(x) for x in c2.values()]
        cu = sum(rr) / len(rr) if rr else None
        out["bands"][nm] = {"n": sum(len(x) for x in c2.values()), "clusters": len(rr), "clustered_up": cu}
        print(f"    [62] band {nm:9s} n={out['bands'][nm]['n']} clusters={len(rr)} clustered_up={_fmt(cu)}")
    return out


def _fmt(x: float | None, d: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{d}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t0", default="2026-09-01")
    ap.add_argument("--to", dest="t1", default="2026-09-11", help="exclusive, UTC ts ng event")
    ap.add_argument("--tape-cache", dest="cache", default=None)
    ap.add_argument("--anchor", choices=("run_hi", "scanner_hod"), default="run_hi")
    ap.add_argument("--post-fix-date", default="2026-09-10", help="ET date ng unang araw na may 86ed59aaf")
    a = ap.parse_args()
    t0 = datetime.fromisoformat(a.t0)
    t1 = datetime.fromisoformat(a.t1)
    post = date.fromisoformat(a.post_fix_date)
    eng = create_engine(
        os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili"),
        pool_pre_ping=True,
    )
    V = bench_vetoes(eng, t0, t1)
    C = control_decisions(eng, t0, t1)
    print(f"window {a.t0}..{a.t1}  anchor={a.anchor}  bench_vetoes={len(V)}  control_decisions={len(C)}")

    def _et(r):
        return r.ts.replace(tzinfo=UTC).astimezone(ET).date()

    ctrl = summarize(label_population(eng, C, a.cache, a.anchor), "control_entries")
    ctrl_pre = summarize(label_population(eng, [r for r in C if _et(r) < post], a.cache, a.anchor), "control_entries_prefix")
    ctrl_post = summarize(label_population(eng, [r for r in C if _et(r) >= post], a.cache, a.anchor), "control_entries_postfix")
    bench = summarize(label_population(eng, V, a.cache, a.anchor), "bench_vetoes")
    bench_pre = summarize(label_population(eng, [r for r in V if _et(r) < post], a.cache, a.anchor), "bench_vetoes_prefix")
    bench_post = summarize(label_population(eng, [r for r in V if _et(r) >= post], a.cache, a.anchor), "bench_vetoes_postfix")
    print("-- BAR anchor (payload benched_at_hod / current_px):")
    summarize(label_population(eng, V, a.cache, "payload"), "bench_bar_anchor")
    summarize(label_population(eng, [r for r in V if _et(r) < post], a.cache, "payload"), "bench_bar_anchor_prefix")
    summarize(label_population(eng, [r for r in V if _et(r) >= post], a.cache, "payload"), "bench_bar_anchor_postfix")

    def _ratio(b, c):
        bu, cu = b["clustered_up"], c["clustered_up"]
        return (bu / cu) if (bu is not None and cu) else None

    # ANG PANUNTUNAN: ang mult ay para sa bench na TUMATAKBO — ang populasyong ginagawa ng
    # kasalukuyang code (pagkatapos ng 86ed59aaf, na nagbago kung KAILAN nagla-latch) laban sa
    # kontrol ng PAREHONG mga araw. Ang buong window (ang numero ng scout) at ang bago-86ed59aaf
    # na hati ay iniuulat bilang konteksto — ang buong window ay HALO ng dalawang magkaibang latch.
    r_all, r_pre, r_post = _ratio(bench, ctrl), _ratio(bench_pre, ctrl_pre), _ratio(bench_post, ctrl_post)
    rule = r_post if r_post is not None else r_all
    mult = min(1.0, rule) if rule is not None else 1.0
    print(
        "BINDING: whole bench_up=%s control_up=%s ratio=%s | prefix bench_up=%s control_up=%s ratio=%s "
        "| postfix bench_up=%s control_up=%s ratio=%s | derived_mult=min(1, postfix ratio)=%s "
        "n_veto=%d clusters=%d control_n=%d control_clusters=%d"
        % (
            _fmt(bench["clustered_up"]), _fmt(ctrl["clustered_up"]), _fmt(r_all),
            _fmt(bench_pre["clustered_up"]), _fmt(ctrl_pre["clustered_up"]), _fmt(r_pre),
            _fmt(bench_post["clustered_up"]), _fmt(ctrl_post["clustered_up"]), _fmt(r_post),
            _fmt(mult), bench["n"], bench["clusters"], ctrl["n"], ctrl["clusters"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
