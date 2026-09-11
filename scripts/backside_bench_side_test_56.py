"""[56] DERIVATION — may binibili ba ang backside bench sa TAPE?

Ang tanong ng bench ay "nasa likod na ba ng galaw ang pangalan — HINDI na ba ito gagawa ng
bagong high?". Ang tape ang sumasagot: pagkatapos ng sandali ng desisyon, alin ang UNANG
nangyari sa mga PRINT — umabot ba sa high ng araw, o bumaba nang PAREHONG layo sa ilalim?

    H  = ang running high ng tape hanggang t (default: ``run_hi`` = pinakamataas na print mula
         04:00 ET — ang katumbas sa tape ng ``cur_hod`` ng bench, premarket-inclusive)
    px = ang HULING PRINT sa t (hindi quote, hindi bar close)
    UP    = ang unang print na >= H ay nauna sa unang print na <= px - (H - px)
    DOWN  = kabaligtaran;  NONE = walang naabot bago matapos ang tape (20:00 ET)
    DEGEN = px <= H/2: ang DOWN barrier ay <= 0, kaya ang label ay UP o NONE LAMANG (walang
            DOWN na posible). Hindi ito kasama sa alinmang sukat, at BINIBILANG.

DALAWANG SUKAT, parehong iniuulat (review 2026-09-11 — ang una lamang ang iniulat dati):
  * ``p_up``          = clustered P(UP), ang NONE ay HINDI-UP. Ito ang tanong ng bench mismo:
                        "gumawa ba ulit ng bagong high ang pangalan bago matapos ang tape?".
                        Ang NONE ay ang eksaktong sinasabi ng bench (walang bagong high).
  * ``p_up_resolved`` = clustered P(UP | UP o DOWN) — ang martingale-null (0.5) na sukat. Ang
                        cluster na puro NONE ay NAHUHULOG dito; iniuulat ang bilang.
Ang ``p_up`` ay confounded ng DISTANSYA (ang malalim na retrace ay mas malayo sa H, kaya mas
madalas NONE). Kaya ang binding ay KINONDISYON SA RETRACE: sa loob ng isang stratum, ang
bench at ang control ay parehong layo sa high.

Kung TAMA ang bench, ang mga sandaling tinanggihan nito ay dapat MAS MADALANG mag-UP kaysa sa
mga sandaling PINAPASOK natin sa PAREHONG retrace. Kaya dalawang populasyon, IISANG label,
IISANG tape:

  * BENCH   = bawat trigger na PUMUTOK habang benched ang pangalan, sa live alpaca_spot:
              ``live_entry_backside_bench_veto`` (bago ang [56]: kinain ang trigger) at
              ``live_entry_backside_bench_conditioned`` (mula [56]: pinanatili, may resibo);
  * CONTROL = bawat live alpaca_spot entry DECISION instant
              (``live_entry_submitted.ts`` bawas ``place_profile_ms.total`` — ang simula ng
              place path, kapareho ng [62]) na HINDI benched na pasok (mula [56] ang benched
              na pasok ay may ``backside_bench.action = 'conditioned'`` sa payload — bench iyon).

Clustered = mean kada symbol-day (ang isang pangalang tumanggi nang 2,000 beses ay IISANG
boto, hindi 2,000).

ANG BINDING (``BACKSIDE_BENCH_MEASURED["size_by_retrace"]``): ang mga stratum ay ang QUARTILES
ng retrace-at-decision ng CONTROL (ang pinangalanang distribusyon: kung saan tayo PUMAPASOK),
at sa bawat stratum ``mult = min(1, bench p_up / control p_up)`` — size-DOWN lamang. Buong
window (bago + pagkatapos ng 86ed59aaf): ang post-fix ay 4–5 control cluster bawat stratum —
hindi kayang magdala ng sariling hatol; iniuulat ang post-fix na hati bilang tseke.

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
from typing import Any, Iterable, Sequence
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

# Ang dalawang event na parehong nangangahulugang "pumutok ang trigger habang benched".
BENCH_EVENT_TYPES = ("live_entry_backside_bench_veto", "live_entry_backside_bench_conditioned")
# Ang mga label na sinusukat (ang ATHIGH = nasa high na, walang tanong; ang DEGEN = walang DOWN).
MEASURED_LABELS = ("UP", "DOWN", "NONE")
# Ang mga quantile ng control retrace na naghahati ng mga stratum (quartiles).
STRATUM_QUANTILES = (0.25, 0.5, 0.75)


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


BENCH_SQL = """
    SELECT e.session_id, s.symbol, e.ts,
           coalesce((e.payload_json->>'current_px')::float,
                    (e.payload_json->>'price')::float) AS px,
           (e.payload_json->>'benched_at_hod')::float AS hod,
           e.payload_json->>'reason' AS reason,
           coalesce(e.payload_json->>'blocked_trigger',
                    e.payload_json->>'preserved_trigger') AS trig,
           e.event_type AS source
    FROM trading_automation_events e
    JOIN trading_automation_sessions s ON s.id = e.session_id
    WHERE e.event_type IN :bench_types
      AND s.mode = 'live' AND s.execution_family = 'alpaca_spot'
      AND e.ts >= :t0 AND e.ts < :t1
    ORDER BY s.symbol, e.ts"""

# Ang benched na pasok mula [56] ay BENCH, hindi control: ang submit payload ay may resibo.
CONTROL_SQL = """
    SELECT s.id AS session_id, s.symbol,
           x.ts - make_interval(secs => coalesce(
               (x.payload_json->'place_profile_ms'->>'total')::numeric, 0) / 1000.0) AS ts,
           NULL::float AS px, NULL::float AS hod, 'control' AS reason, NULL AS trig,
           x.event_type AS source
    FROM trading_automation_events x
    JOIN trading_automation_sessions s ON s.id = x.session_id
    WHERE s.mode = 'live' AND s.execution_family = 'alpaca_spot'
      AND x.event_type = 'live_entry_submitted'
      AND coalesce(x.payload_json->'backside_bench'->>'action', '') <> 'conditioned'
      AND x.ts >= :t0 AND x.ts < :t1
    ORDER BY s.symbol, x.ts"""


def bench_vetoes(eng, t0: datetime, t1: datetime) -> list[Any]:
    """Ang BENCH population: bawat trigger na pumutok habang benched — ang makasaysayang veto at
    ang [56] na conditioned fire. Ang ``source`` column ang nagsasabi kung alin."""
    from sqlalchemy import bindparam

    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        return c.execute(
            text(BENCH_SQL).bindparams(bindparam("bench_types", expanding=True)),
            {"t0": t0, "t1": t1, "bench_types": list(BENCH_EVENT_TYPES)},
        ).fetchall()


def control_decisions(eng, t0: datetime, t1: datetime) -> list[Any]:
    with eng.connect() as c:
        c.execute(text("SET statement_timeout='20s'"))
        return c.execute(text(CONTROL_SQL), {"t0": t0, "t1": t1}).fetchall()


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


def label_instant(after: np.ndarray, H: float, px: float) -> str:
    """Ang label ng isang sandali. ``ATHIGH`` kapag nasa high na; ``DEGEN`` kapag ang DOWN
    barrier ``px - (H - px)`` ay <= 0 (retrace >= 50%): walang print na <= 0, kaya ang label ay
    UP o NONE LAMANG — isang barya na isang panig lang ang kayang lumapag. Hindi ito sinusukat."""
    if not H > px:
        return "ATHIGH"
    dn = px - (H - px)
    if dn <= 0:
        return "DEGEN"
    return first_passage(after, H, dn)


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
            src = getattr(v, "source", None)
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
                rows.append({**memo[key], "cl": (sym, d), "reason": v.reason, "source": src})
                continue
            lab = label_instant(npx[i:], H, px)
            feats = cycle_features_at(sc, sc.last_px)
            score, _ = cycle_exhaustion_score(feats, CYCLE_EXHAUSTION_TERMS)
            mult, _ = cycle_exhaustion_size_multiplier(
                score, floor=CYCLE_EXHAUSTION_FLOOR, q50=CYCLE_EXHAUSTION_Q50, q90=CYCLE_EXHAUSTION_Q90
            )
            memo[key] = {"lab": lab, "mult": mult, "retr": (H - px) / H if H > 0 else None}
            rows.append({**memo[key], "cl": (sym, d), "reason": v.reason, "source": src})
    return rows


def cluster_rates(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Ang DALAWANG clustered na sukat sa isang hanay ng hilera, kasama ang mga nahuhulog.

    ``p_up``          = mean kada cluster ng UP / (UP+DOWN+NONE) — NONE ay hindi-UP;
    ``p_up_resolved`` = mean kada cluster ng UP / (UP+DOWN), sa mga cluster na may kahit isa;
    ``dropped_all_none_clusters`` = mga cluster na puro NONE (wala sa ``p_up_resolved``)."""
    cl: dict[Any, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        if r["lab"] in MEASURED_LABELS:
            cl[r["cl"]][MEASURED_LABELS.index(r["lab"])] += 1
    n = len(cl)
    res = [c for c in cl.values() if c[0] + c[1] > 0]
    tot_rows = sum(sum(c) for c in cl.values())
    tot_res = sum(c[0] + c[1] for c in res)
    return {
        "clusters": n,
        "rows": tot_rows,
        "p_up": (sum(c[0] / sum(c) for c in cl.values()) / n) if n else None,
        "p_none": (sum(c[2] / sum(c) for c in cl.values()) / n) if n else None,
        "p_up_resolved": (sum(c[0] / (c[0] + c[1]) for c in res) / len(res)) if res else None,
        "clusters_resolved": len(res),
        "dropped_all_none_clusters": n - len(res),
        "pooled_up": (sum(c[0] for c in cl.values()) / tot_rows) if tot_rows else None,
        "pooled_up_resolved": (sum(c[0] for c in res) / tot_res) if tot_res else None,
        "rates_resolved": [c[0] / (c[0] + c[1]) for c in res],
    }


def summarize(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    lab: dict[str, int] = defaultdict(int)
    for r in rows:
        lab[r["lab"]] += 1
    cr = cluster_rates(rows)
    rates = cr.pop("rates_resolved")
    right = sum(1 for x in rates if x < 0.5)  # bench TAMA = mas madalas bumaba kaysa umakyat
    wrong = sum(1 for x in rates if x > 0.5)
    out = {
        "name": name, "n": len(rows), "labels": dict(lab), **cr,
        # ang dating pangalan ng resolved-only na sukat (planner row / PR v1)
        "clustered_up": cr["p_up_resolved"],
        "right": right, "wrong": wrong,
        "binom_p": _binom_two_sided(right, right + wrong) if (right + wrong) else None,
    }
    print(
        f"[{name}] n={len(rows)} labels={dict(lab)} clusters={cr['clusters']} "
        f"P(UP)={_fmt(cr['p_up'])} P(NONE)={_fmt(cr['p_none'])} "
        f"P(UP|resolved)={_fmt(cr['p_up_resolved'])} on {cr['clusters_resolved']} cl "
        f"(all-NONE clusters dropped from the resolved metric: {cr['dropped_all_none_clusters']}) "
        f"pooled={_fmt(cr['pooled_up'])}/{_fmt(cr['pooled_up_resolved'])} "
        f"right={right} wrong={wrong} binom_p={_fmt(out['binom_p'])}"
    )
    bands = (
        (0.999, 2.0, "mult=1.0"),
        (CYCLE_EXHAUSTION_FLOOR + 1e-9, 0.999, "ramp"),
        (-1.0, CYCLE_EXHAUSTION_FLOOR + 1e-9, "floor"),
    )
    out["bands"] = {}
    for lo, hi, nm in bands:
        sub = [r for r in rows if lo <= (r["mult"] if r["mult"] is not None else 1.0) < hi]
        b = cluster_rates(sub)
        b.pop("rates_resolved")
        out["bands"][nm] = b
        print(
            f"    [62] band {nm:9s} clusters={b['clusters']} P(UP)={_fmt(b['p_up'])} "
            f"P(UP|resolved)={_fmt(b['p_up_resolved'])} ({b['clusters_resolved']} cl)"
        )
    return out


def stratum_edges(control_rows: Sequence[dict[str, Any]]) -> list[float]:
    """Ang mga hangganan ng stratum = QUARTILES ng retrace-at-decision ng CONTROL (ang mga
    sinusukat na hilera lamang). Ang pinangalanang distribusyon: kung GAANO KALAYO sa high ng
    tape tayo PUMAPASOK. Bukas ang itaas (walang kisame ang retrace)."""
    retr = [r["retr"] for r in control_rows if r["lab"] in MEASURED_LABELS and r.get("retr") is not None]
    qs = [_q(retr, p) for p in STRATUM_QUANTILES]
    return [0.0] + [float(x) for x in qs if x is not None] + [math.inf]


def stratified_binding(
    bench_rows: Sequence[dict[str, Any]],
    control_rows: Sequence[dict[str, Any]],
    edges: Sequence[float] | None = None,
) -> list[dict[str, Any]]:
    """Kada stratum ng retrace: ang ``p_up`` ng bench laban sa control sa PAREHONG layo sa high,
    at ``mult = min(1, bench/control)`` — size-DOWN lamang. ``None`` na ratio (walang bench, o
    walang/zero na control) ⇒ mult 1.0 na PINANGALANAN (``mult_reason``), hindi hula."""
    edges = list(edges) if edges is not None else stratum_edges(control_rows)
    out: list[dict[str, Any]] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        def _in(r, lo=lo, hi=hi):
            return r.get("retr") is not None and lo <= r["retr"] < hi
        b = cluster_rates([r for r in bench_rows if _in(r)])
        c = cluster_rates([r for r in control_rows if _in(r)])
        ratio = (b["p_up"] / c["p_up"]) if (b["p_up"] is not None and c["p_up"]) else None
        ratio_res = (
            (b["p_up_resolved"] / c["p_up_resolved"])
            if (b["p_up_resolved"] is not None and c["p_up_resolved"]) else None
        )
        out.append({
            "lo_pct": round(lo * 100.0, 2),
            "hi_pct": (None if math.isinf(hi) else round(hi * 100.0, 2)),
            "bench_p_up": _r(b["p_up"]), "control_p_up": _r(c["p_up"]),
            "bench_p_up_resolved": _r(b["p_up_resolved"]),
            "control_p_up_resolved": _r(c["p_up_resolved"]),
            "bench_clusters": b["clusters"], "control_clusters": c["clusters"],
            "bench_rows": b["rows"], "control_rows": c["rows"],
            "ratio": _r(ratio), "ratio_resolved": _r(ratio_res),
            "mult": (round(min(1.0, ratio), 3) if ratio is not None else 1.0),
            "mult_reason": (
                None if ratio is not None
                else "no_bench_in_stratum" if b["p_up"] is None
                else "no_control_in_stratum"
            ),
        })
    return out


def _r(x: float | None, d: int = 3) -> float | None:
    return None if x is None else round(float(x), d)


def _fmt(x: float | None, d: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{d}f}"


def _print_strata(title: str, strata: list[dict[str, Any]]) -> None:
    print(f"-- {title}")
    for s in strata:
        hi = "inf" if s["hi_pct"] is None else f"{s['hi_pct']:.2f}"
        print(
            f"   retrace [{s['lo_pct']:.2f}%, {hi}%)  bench P(UP)={_fmt(s['bench_p_up'])} "
            f"({s['bench_clusters']} cl, {s['bench_rows']} rows)  control P(UP)="
            f"{_fmt(s['control_p_up'])} ({s['control_clusters']} cl, {s['control_rows']} rows)  "
            f"ratio={_fmt(s['ratio'])}  | resolved {_fmt(s['bench_p_up_resolved'])}/"
            f"{_fmt(s['control_p_up_resolved'])} ratio={_fmt(s['ratio_resolved'])}  "
            f"=> mult={s['mult']:.3f}"
        )


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
    by_src: dict[str, int] = defaultdict(int)
    for r in V:
        by_src[str(r.source)] += 1
    print(
        f"window {a.t0}..{a.t1}  anchor={a.anchor}  bench={len(V)} {dict(by_src)}  "
        f"control_decisions={len(C)}"
    )

    # Isang label kada sandali (hindi nakadepende ang label sa ibang hilera), tapos hinahati.
    lc = label_population(eng, C, a.cache, a.anchor)
    lb = label_population(eng, V, a.cache, a.anchor)

    def _pre(r):
        return r["cl"][1] < post

    ctrl = summarize(lc, "control_entries")
    ctrl_pre = summarize([r for r in lc if _pre(r)], "control_entries_prefix")
    ctrl_post = summarize([r for r in lc if not _pre(r)], "control_entries_postfix")
    bench = summarize(lb, "bench")
    bench_pre = summarize([r for r in lb if _pre(r)], "bench_prefix")
    bench_post = summarize([r for r in lb if not _pre(r)], "bench_postfix")
    degen = sum(1 for r in lb if r["lab"] == "DEGEN")
    print(f"DEGEN (retrace >= 50%: DOWN barrier <= 0, excluded from every metric): bench={degen} "
          f"control={sum(1 for r in lc if r['lab'] == 'DEGEN')}")
    print("-- BAR anchor (payload benched_at_hod / current_px):")
    lbar = label_population(eng, V, a.cache, "payload")
    summarize(lbar, "bench_bar_anchor")
    summarize([r for r in lbar if _pre(r)], "bench_bar_anchor_prefix")
    summarize([r for r in lbar if not _pre(r)], "bench_bar_anchor_postfix")

    def _ratio(b, c, k):
        bu, cu = b[k], c[k]
        return (bu / cu) if (bu is not None and cu) else None

    print(
        "UNCONDITIONAL (the bench's own question, NONE = no new high):  whole %s/%s ratio=%s | "
        "prefix %s/%s ratio=%s | postfix %s/%s ratio=%s"
        % (
            _fmt(bench["p_up"]), _fmt(ctrl["p_up"]), _fmt(_ratio(bench, ctrl, "p_up")),
            _fmt(bench_pre["p_up"]), _fmt(ctrl_pre["p_up"]), _fmt(_ratio(bench_pre, ctrl_pre, "p_up")),
            _fmt(bench_post["p_up"]), _fmt(ctrl_post["p_up"]), _fmt(_ratio(bench_post, ctrl_post, "p_up")),
        )
    )
    print(
        "RESOLVED-ONLY (martingale null 0.5; all-NONE clusters dropped: bench %d/%d, control %d/%d): "
        "whole %s/%s ratio=%s | prefix %s/%s ratio=%s | postfix %s/%s ratio=%s"
        % (
            bench["dropped_all_none_clusters"], bench["clusters"],
            ctrl["dropped_all_none_clusters"], ctrl["clusters"],
            _fmt(bench["p_up_resolved"]), _fmt(ctrl["p_up_resolved"]),
            _fmt(_ratio(bench, ctrl, "p_up_resolved")),
            _fmt(bench_pre["p_up_resolved"]), _fmt(ctrl_pre["p_up_resolved"]),
            _fmt(_ratio(bench_pre, ctrl_pre, "p_up_resolved")),
            _fmt(bench_post["p_up_resolved"]), _fmt(ctrl_post["p_up_resolved"]),
            _fmt(_ratio(bench_post, ctrl_post, "p_up_resolved")),
        )
    )

    # ANG BINDING: kondisyon sa retrace (kung saan ang bench at control ay parehong layo sa high).
    edges = stratum_edges(lc)
    strata = stratified_binding(lb, lc, edges)
    _print_strata(
        f"BINDING size_by_retrace (strata = control retrace quartiles {[round(e * 100, 2) for e in edges[1:-1]]}%, "
        f"whole window)", strata,
    )
    _print_strata("check: prefix, same edges", stratified_binding(
        [r for r in lb if _pre(r)], [r for r in lc if _pre(r)], edges))
    _print_strata("check: postfix (86ed59aaf), same edges — thin", stratified_binding(
        [r for r in lb if not _pre(r)], [r for r in lc if not _pre(r)], edges))
    print("BINDING size_by_retrace = [")
    for s in strata:
        print(f"    {s!r},")
    print("]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
