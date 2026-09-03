"""Validate a hydrated symbol-day against our own recorded tape, tick by tick.

Phase 3 of the historical hydrator.  ``historical_tick_hydrator.py`` buys tick
history we never recorded; this tool answers the only question that makes that
history usable: **does it agree with what we recorded on the days we recorded
both, and where it disagrees, which side is right?**

It is deliberately unsparing.  A hydrator that silently differs from our
recording would poison every counterfactual built on it, so the defaults here
are the ones most likely to expose a difference rather than hide one:

* Trade rows are aligned as a **multiset** on ``(observed_at, price)``, never a
  set.  Two fills at the same microsecond and price are legal, and set
  semantics would silently collapse them and flatter both sides.
* Comparison is confined to the window our recording actually covers.  Outside
  it, a "difference" is just the coverage gap the hydrator exists to fill.
* Our recording is **frame-deduplicated** before the headline comparison.
  ``iqfeed_trade_ticks.source_frame_sha256`` hashes the exact provider bytes,
  so two rows carrying one hash are the SAME L1 frame written twice -- which
  happens whenever the bridge restarts and re-ingests a window it already
  wrote, because nothing dedupes across ``bridge_run_id``.  Both the raw and
  the deduplicated comparison are reported, because the gap between them is
  itself a finding about our recording.

Reads ``chili`` strictly READ-ONLY under a 30 s statement timeout, with keyset
pagination that backs off to smaller pages rather than raising the timeout.
Writes nothing, anywhere.

Usage::

    python scripts/hydration_fidelity_check.py --symbol-day SSM:2026-09-01
    python scripts/hydration_fidelity_check.py --symbol-day CANF:2026-09-02 \\
        --provider polygon --json out.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:  # pragma: no cover - import shape depends on invocation
    from scripts.historical_tick_hydrator import (
        DEFAULT_HYDRATED_DB,
        SOURCE_IQFEED_NBBO,
        SOURCE_IQFEED_TRADES,
        SOURCE_POLYGON_NBBO,
        SOURCE_POLYGON_TRADES,
        env_file_candidates,
        parse_symbol_day,
        resolve_dsn,
    )
except ModuleNotFoundError:  # pragma: no cover
    from historical_tick_hydrator import (  # type: ignore[no-redef]
        DEFAULT_HYDRATED_DB,
        SOURCE_IQFEED_NBBO,
        SOURCE_IQFEED_TRADES,
        SOURCE_POLYGON_NBBO,
        SOURCE_POLYGON_TRADES,
        env_file_candidates,
        parse_symbol_day,
        resolve_dsn,
    )

RECORDED_TRADE_SOURCE = "iqfeed_l1"

# Prices on both tapes are exact decimals carried through a float column, so
# "agreement" means agreement at 6 decimal places.  Anything looser would hide
# a real half-cent disagreement on a $3 stock.
PRICE_DP = 6
PRICE_TOL = 5e-7

DEFAULT_PAGE = 20_000
MIN_PAGE = 250
STATEMENT_TIMEOUT = "30s"
DEFAULT_NBBO_SAMPLES = 2_000

PROVIDERS = {
    "iqfeed": (SOURCE_IQFEED_TRADES, SOURCE_IQFEED_NBBO),
    "polygon": (SOURCE_POLYGON_TRADES, SOURCE_POLYGON_NBBO),
}

TRADE_COLUMNS = (
    "observed_at", "id", "price", "size", "bid", "ask", "source",
    "timestamp_basis", "provider_event_at", "source_frame_sha256",
    "bridge_run_id", "connection_generation",
)
NBBO_COLUMNS = (
    "observed_at", "id", "bid", "ask", "source", "timestamp_basis",
    "source_frame_sha256", "provider_event_at",
)

T_AT, T_ID, T_PX, T_SZ, T_BID, T_ASK, T_SRC, T_BASIS, T_PEA, T_SHA, T_RUN, T_GEN = range(12)
N_AT, N_ID, N_BID, N_ASK, N_SRC, N_BASIS, N_SHA, N_PEA = range(8)


# --------------------------------------------------------------------------
# pure helpers (unit-tested without a database)
# --------------------------------------------------------------------------
def naive_utc(dt: datetime) -> datetime:
    """Both tapes describe UTC instants; one column is aware and one is not.

    ``iqfeed_trade_ticks.observed_at`` is TIMESTAMP WITHOUT TIME ZONE holding
    UTC; ``momentum_nbbo_spread_tape.observed_at`` is TIMESTAMPTZ.  Comparing
    them requires putting both on the same footing, and the aware one must be
    converted to UTC first -- dropping a non-UTC tzinfo would shift the value.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def quantile(xs: Sequence[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def dedupe_by_frame(rows: Sequence[Sequence[Any]], sha_index: int,
                    run_index: int | None = None) -> tuple[list, dict]:
    """Keep the first row per provider-frame hash; report what was dropped.

    A dropped row is not a suspicious row -- it is a byte-identical re-ingest of
    a frame already on the tape.  Rows with no hash are always kept, because
    absence of provenance is not evidence of duplication.
    """
    seen: dict[str, str] = {}
    kept: list = []
    dropped = 0
    cross_run = 0
    for r in rows:
        sha = r[sha_index]
        if not sha:
            kept.append(r)
            continue
        if sha in seen:
            dropped += 1
            if run_index is not None and seen[sha] != str(r[run_index]):
                cross_run += 1
            continue
        seen[sha] = str(r[run_index]) if run_index is not None else ""
        kept.append(r)
    stats = {
        "rows_in": len(rows),
        "rows_kept": len(kept),
        "rows_dropped": dropped,
        "dropped_rate": round(dropped / len(rows), 6) if rows else None,
        "dropped_frames_first_written_by_a_different_bridge_run": cross_run,
        "rows_without_frame_hash": sum(1 for r in rows if not r[sha_index]),
    }
    return kept, stats


def group_trades(rows: Iterable[Sequence[Any]]) -> dict[tuple, list]:
    g: dict[tuple, list] = defaultdict(list)
    for r in rows:
        g[(naive_utc(r[T_AT]), round(float(r[T_PX]), PRICE_DP))].append(r)
    return g


def multiset_alignment(rec: Sequence[Sequence[Any]],
                       hyd: Sequence[Sequence[Any]]) -> dict:
    gr, gh = group_trades(rec), group_trades(hyd)
    kr = Counter({k: len(v) for k, v in gr.items()})
    kh = Counter({k: len(v) for k, v in gh.items()})
    matched = sum((kr & kh).values())
    return {
        "matched_rows": matched,
        "recorded_only_rows": sum((kr - kh).values()),
        "hydrated_only_rows": sum((kh - kr).values()),
        "recorded_match_rate": round(matched / len(rec), 6) if rec else None,
        "hydrated_match_rate": round(matched / len(hyd), 6) if hyd else None,
        "_groups": (gr, gh, kr, kh),
    }


def extremes(rows: Sequence[Sequence[Any]]) -> dict:
    """Session high/low and the exact instant of each -- what a study consumes.

    If the two tapes disagree here, every reward number computed from the
    recording is wrong, regardless of how well the bulk of the ticks match.
    """
    if not rows:
        return {}
    hi = max(rows, key=lambda r: float(r[T_PX]))
    lo = min(rows, key=lambda r: float(r[T_PX]))
    return {
        "high": round(float(hi[T_PX]), PRICE_DP), "high_at": str(naive_utc(hi[T_AT])),
        "low": round(float(lo[T_PX]), PRICE_DP), "low_at": str(naive_utc(lo[T_AT])),
        "volume": round(sum(float(r[T_SZ] or 0) for r in rows), 4),
        "first": str(naive_utc(rows[0][T_AT])), "last": str(naive_utc(rows[-1][T_AT])),
    }


def miss_rate_by_tape_speed(hyd: Sequence[Sequence[Any]],
                            missing: Sequence[Sequence[Any]]) -> list[dict]:
    """Is what our recording missed biased toward FAST tape?

    This is the question that decides whether the recorded tape can be used at
    all.  A uniform 5 % loss is noise a study can live with.  A loss
    concentrated in the seconds when the tape is fastest is a loss concentrated
    at ignition -- exactly the moment a momentum counterfactual is about.
    """
    per_s = Counter(naive_utc(r[T_AT]).replace(microsecond=0) for r in hyd)
    miss_s = Counter(naive_utc(r[T_AT]).replace(microsecond=0) for r in missing)
    edges = [1, 5, 20, 50, 100, None]
    out: list[dict] = []
    prev = 0
    for hi in edges:
        if hi is None:
            secs = [s for s, n in per_s.items() if n > prev]
            label = f">{prev}"
        else:
            secs = [s for s, n in per_s.items() if prev < n <= hi]
            label = f"{prev + 1}-{hi}" if prev else f"1-{hi}"
            prev = hi
        tot = sum(per_s[s] for s in secs)
        mis = sum(miss_s.get(s, 0) for s in secs)
        out.append({"ticks_per_second": label, "seconds": len(secs),
                    "hydrated_ticks": tot, "missing_from_recording": mis,
                    "miss_rate": round(mis / tot, 6) if tot else None})
    return out


def residual_timestamp_skew(resid_rec: Sequence[Sequence[Any]],
                            resid_hyd: Sequence[Sequence[Any]],
                            window_s: float = 2.0) -> dict:
    """Match leftovers on (price, size) and report the time gap.

    Everything that aligned on an exact timestamp has zero skew by
    construction, so skew can only live in the residual.  A non-zero mode here
    is a systematic clock offset between the tapes; an empty result means the
    residual is genuine presence/absence, not a shifted clock.
    """
    idx: dict[tuple, list[datetime]] = defaultdict(list)
    for r in resid_hyd:
        idx[(round(float(r[T_PX]), PRICE_DP),
             round(float(r[T_SZ] or 0), 6))].append(naive_utc(r[T_AT]))
    for v in idx.values():
        v.sort()
    skews: list[float] = []
    for r in resid_rec:
        cand = idx.get((round(float(r[T_PX]), PRICE_DP),
                        round(float(r[T_SZ] or 0), 6)))
        if not cand:
            continue
        t = naive_utc(r[T_AT])
        i = bisect.bisect_left(cand, t)
        best: float | None = None
        for j in (i - 1, i):
            if 0 <= j < len(cand):
                d = (cand[j] - t).total_seconds()
                if best is None or abs(d) < abs(best):
                    best = d
        if best is not None and abs(best) <= window_s:
            skews.append(best)
    return {
        "recorded_residual_rows": len(resid_rec),
        "hydrated_residual_rows": len(resid_hyd),
        "matched_by_price_size_within_window": len(skews),
        "window_s": window_s,
        "p01": quantile(skews, 0.01), "p50": quantile(skews, 0.5),
        "p99": quantile(skews, 0.99),
        "mean": round(statistics.fmean(skews), 6) if skews else None,
        "abs_gt_1ms": sum(1 for s in skews if abs(s) > 0.001),
    }


def nbbo_structure(rows: Sequence[Sequence[Any]], recorded: bool) -> dict:
    """How much information does this quote tape actually carry?

    Row count flatters a tape that repeats itself.  What a replay consumes is
    the number of distinct instants at which the quote can change.
    """
    if not rows:
        return {"rows": 0}
    ts = Counter(naive_utc(r[N_AT]) for r in rows)
    out: dict = {
        "rows": len(rows),
        "distinct_timestamps": len(ts),
        "max_rows_sharing_one_timestamp": max(ts.values()),
        "distinct_quote_states": len({(naive_utc(r[N_AT]), r[N_BID], r[N_ASK])
                                      for r in rows}),
        "rows_per_distinct_timestamp": round(len(rows) / len(ts), 3),
    }
    if recorded:
        out["basis_mix"] = dict(Counter(r[N_BASIS] for r in rows))
        sha = Counter(r[N_SHA] for r in rows if r[N_SHA])
        out["distinct_frame_hashes"] = len(sha)
        out["rows_repeating_an_already_ingested_frame"] = sum(
            v - 1 for v in sha.values() if v > 1)
        out["rows_with_no_own_quote_clock"] = sum(1 for r in rows if r[N_PEA] is None)
    return out


def compare_nbbo_asof(rec: Sequence[Sequence[Any]], hyd: Sequence[Sequence[Any]],
                      lo: datetime, hi: datetime, samples: int) -> dict:
    """Sample instants and ask each tape "what was the NBBO here?".

    As-of sampling is the honest test: a replay never asks for "the quote row
    with this id", it asks for the last quote at or before an instant.
    """
    out: dict = {"recorded_rows": len(rec), "hydrated_rows": len(hyd)}
    if not rec or not hyd:
        out["skipped"] = "one side empty"
        return out
    ra = [naive_utc(r[N_AT]) for r in rec]
    ha = [naive_utc(r[N_AT]) for r in hyd]
    out["recorded_span"] = [str(ra[0]), str(ra[-1])]
    out["hydrated_span"] = [str(ha[0]), str(ha[-1])]
    lo = max(lo, ra[0], ha[0])
    hi = min(hi, ra[-1], ha[-1])
    if hi <= lo:
        out["skipped"] = "no overlapping span"
        return out
    step = (hi - lo) / samples
    both = same = bid_same = ask_same = 0
    dbids: list[float] = []
    dasks: list[float] = []
    for i in range(samples):
        t = lo + step * i
        ir = bisect.bisect_right(ra, t) - 1
        ih = bisect.bisect_right(ha, t) - 1
        if ir < 0 or ih < 0:
            continue
        rb, rk = rec[ir][N_BID], rec[ir][N_ASK]
        hb, hk = hyd[ih][N_BID], hyd[ih][N_ASK]
        if rb is None or rk is None or hb is None or hk is None:
            continue
        both += 1
        db, da = float(hb) - float(rb), float(hk) - float(rk)
        dbids.append(db)
        dasks.append(da)
        bid_same += abs(db) <= PRICE_TOL
        ask_same += abs(da) <= PRICE_TOL
        same += abs(db) <= PRICE_TOL and abs(da) <= PRICE_TOL
    out.update({
        "sampled_instants": samples, "comparable_instants": both,
        "both_sides_exact": same,
        "exact_rate": round(same / both, 6) if both else None,
        "bid_exact_rate": round(bid_same / both, 6) if both else None,
        "ask_exact_rate": round(ask_same / both, 6) if both else None,
        "bid_diff": {"p01": quantile(dbids, 0.01), "p50": quantile(dbids, 0.5),
                     "p99": quantile(dbids, 0.99),
                     "max_abs": max((abs(d) for d in dbids), default=0.0)},
        "ask_diff": {"p01": quantile(dasks, 0.01), "p50": quantile(dasks, 0.5),
                     "p99": quantile(dasks, 0.99),
                     "max_abs": max((abs(d) for d in dasks), default=0.0)},
    })
    return out


def compare_trades(rec: Sequence[Sequence[Any]], hyd: Sequence[Sequence[Any]],
                   label: str) -> dict:
    out: dict = {"provider": label}
    if not rec or not hyd:
        out["skipped"] = "one side empty"
        return out
    lo, hi = naive_utc(rec[0][T_AT]), naive_utc(rec[-1][T_AT])
    rw = [r for r in rec if lo <= naive_utc(r[T_AT]) <= hi]
    hw = [r for r in hyd if lo <= naive_utc(r[T_AT]) <= hi]
    out["overlap_window"] = {"from": str(lo), "to": str(hi),
                             "recorded_rows": len(rw), "hydrated_rows": len(hw)}
    if not rw or not hw:
        out["skipped"] = "empty overlap"
        return out

    align = multiset_alignment(rw, hw)
    gr, gh, kr, kh = align.pop("_groups")
    out["multiset_ts_price"] = align

    size_pairs = size_mismatch = quote_pairs = quote_exact = 0
    size_diffs: list[float] = []
    bid_diffs: list[float] = []
    ask_diffs: list[float] = []
    rec_no_quote = hyd_no_quote = 0
    for k in (kr & kh):
        rs = sorted(round(float(r[T_SZ] or 0), 6) for r in gr[k])
        hs = sorted(round(float(r[T_SZ] or 0), 6) for r in gh[k])
        for a, b in zip(rs, hs):
            size_pairs += 1
            if a != b:
                size_mismatch += 1
                size_diffs.append(b - a)
        rq = sorted((r[T_BID], r[T_ASK]) for r in gr[k]
                    if r[T_BID] is not None and r[T_ASK] is not None)
        hq = sorted((r[T_BID], r[T_ASK]) for r in gh[k]
                    if r[T_BID] is not None and r[T_ASK] is not None)
        rec_no_quote += len(gr[k]) - len(rq)
        hyd_no_quote += len(gh[k]) - len(hq)
        for (rb, ra), (hb, ha) in zip(rq, hq):
            quote_pairs += 1
            db, da = float(hb) - float(rb), float(ha) - float(ra)
            bid_diffs.append(db)
            ask_diffs.append(da)
            quote_exact += abs(db) <= PRICE_TOL and abs(da) <= PRICE_TOL

    out["size_agreement"] = {
        "compared_pairs": size_pairs, "mismatched": size_mismatch,
        "mismatch_rate": round(size_mismatch / size_pairs, 6) if size_pairs else None,
        "max_abs_diff": max((abs(d) for d in size_diffs), default=0.0),
        "sum_signed_diff": round(sum(size_diffs), 4),
    }
    out["at_trade_quote_agreement"] = {
        "compared_pairs": quote_pairs, "exact_within_tol": quote_exact,
        "exact_rate": round(quote_exact / quote_pairs, 6) if quote_pairs else None,
        "bid_max_abs_diff": max((abs(d) for d in bid_diffs), default=0.0),
        "ask_max_abs_diff": max((abs(d) for d in ask_diffs), default=0.0),
        "bid_p50_diff": quantile(bid_diffs, 0.5),
        "ask_p50_diff": quantile(ask_diffs, 0.5),
        "recorded_rows_without_quote": rec_no_quote,
        "hydrated_rows_without_quote": hyd_no_quote,
    }

    out["recorded_extremes"] = extremes(rw)
    out["hydrated_extremes"] = extremes(hw)
    out["extremes_agree"] = (
        out["recorded_extremes"].get("high") == out["hydrated_extremes"].get("high")
        and out["recorded_extremes"].get("low") == out["hydrated_extremes"].get("low"))
    out["extreme_times_agree"] = (
        out["recorded_extremes"].get("high_at") == out["hydrated_extremes"].get("high_at")
        and out["recorded_extremes"].get("low_at") == out["hydrated_extremes"].get("low_at"))

    resid_r = [r for k, n in (kr - kh).items() for r in gr[k][:n]]
    resid_h = [r for k, n in (kh - kr).items() for r in gh[k][:n]]
    out["residual_timestamp_skew_s"] = residual_timestamp_skew(resid_r, resid_h)
    out["miss_rate_by_tape_speed"] = miss_rate_by_tape_speed(hw, resid_h)
    out["volume_missing_from_recording"] = round(
        sum(float(r[T_SZ] or 0) for r in resid_h), 4)
    out["volume_hydrated_overlap"] = round(sum(float(r[T_SZ] or 0) for r in hw), 4)
    return out


# --------------------------------------------------------------------------
# database access
# --------------------------------------------------------------------------
def live_dsn(env_path: str | None = None) -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        for path in env_file_candidates(env_path):
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if raw.startswith("DATABASE_URL="):
                    dsn = raw.split("=", 1)[1].strip()
                    break
            if dsn:
                break
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set and no .env entry was found")
    return dsn


def paged_scan(conn, table: str, columns: Sequence[str], symbol: str,
               lo: datetime, hi: datetime, source: str | None,
               aware: bool) -> list[tuple]:
    """Keyset scan over (observed_at, id) with timeout-driven page shrinking.

    The 30 s statement timeout on ``chili`` is a hard rule, so a slow page is
    answered by asking for fewer rows -- never by raising the timeout.
    """
    import psycopg2  # local import so the pure helpers are importable without it

    rows: list[tuple] = []
    cur_at: datetime = lo
    cur_id = -1
    page = DEFAULT_PAGE
    src_sql = " AND source = %s" if source else ""
    cols = ", ".join(columns)
    while True:
        params: list[Any] = [symbol, cur_at, cur_id, hi]
        if source:
            params.append(source)
        try:
            with conn.cursor() as c:
                c.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
                c.execute("SET TIME ZONE 'UTC'")
                c.execute(
                    f"SELECT {cols} FROM {table} "
                    "WHERE symbol = %s AND (observed_at, id) > (%s, %s) "
                    f"AND observed_at < %s{src_sql} "
                    f"ORDER BY observed_at, id LIMIT {page}",
                    params,
                )
                batch = c.fetchall()
        except psycopg2.errors.QueryCanceled:
            conn.rollback()
            if page <= MIN_PAGE:
                raise
            page = max(MIN_PAGE, page // 4)
            continue
        if not batch:
            break
        rows.extend(batch)
        cur_at, cur_id = batch[-1][0], batch[-1][1]
        if len(batch) < page:
            break
        if page < DEFAULT_PAGE:
            page = min(DEFAULT_PAGE, page * 2)
    return rows


def check_symbol_day(symbol: str, day: date, providers: Sequence[str],
                     db_name: str, env_path: str | None,
                     nbbo_samples: int) -> dict:
    import psycopg2

    lo_n = datetime.combine(day, datetime.min.time())
    hi_n = lo_n + timedelta(days=1)
    lo_a = lo_n.replace(tzinfo=timezone.utc)
    hi_a = hi_n.replace(tzinfo=timezone.utc)

    live = psycopg2.connect(live_dsn(env_path))
    live.set_session(readonly=True, autocommit=True)
    hyd = psycopg2.connect(resolve_dsn(db_name, env_path))
    hyd.autocommit = True
    try:
        rec = paged_scan(live, "iqfeed_trade_ticks", TRADE_COLUMNS, symbol,
                         lo_n, hi_n, RECORDED_TRADE_SOURCE, aware=False)
        rec_nbbo = paged_scan(live, "momentum_nbbo_spread_tape", NBBO_COLUMNS,
                              symbol, lo_a, hi_a, None, aware=True)

        entry: dict = {
            "symbol": symbol, "day": day.isoformat(),
            "recorded_trade_rows": len(rec),
            "recorded_nbbo_rows": len(rec_nbbo),
        }
        if not rec:
            entry["skipped"] = "no recorded tape for this symbol-day"
            return entry

        entry["recorded_basis_mix"] = dict(Counter(r[T_BASIS] for r in rec))
        entry["recorded_bridge_runs"] = len({str(r[T_RUN]) for r in rec})
        entry["recorded_connection_generations"] = len({r[T_GEN] for r in rec})
        # Is observed_at the exchange clock, or the bridge's own wall clock?
        # Sampled rather than exhaustive: a systematic offset shows up in five
        # rows as clearly as in a million.
        sample = rec[:: max(1, len(rec) // 5000)]
        skew = [(naive_utc(r[T_AT]) - naive_utc(r[T_PEA])).total_seconds()
                for r in sample if r[T_PEA] is not None]
        entry["recorded_observed_at_minus_provider_event_at_s"] = {
            "sampled": len(skew), "min": min(skew, default=None),
            "max": max(skew, default=None), "p50": quantile(skew, 0.5)}

        rec_d, dstats = dedupe_by_frame(rec, T_SHA, T_RUN)
        entry["recorded_frame_duplication"] = dstats
        rec_nbbo_d, ndstats = dedupe_by_frame(rec_nbbo, N_SHA)
        entry["recorded_nbbo_frame_duplication"] = ndstats
        entry["recorded_nbbo_structure"] = nbbo_structure(rec_nbbo, recorded=True)

        entry["trades"] = []
        entry["nbbo"] = []
        for prov in providers:
            tsrc, nsrc = PROVIDERS[prov]
            hyd_t = paged_scan(hyd, "iqfeed_trade_ticks", TRADE_COLUMNS, symbol,
                               lo_n, hi_n, tsrc, aware=False)
            entry["trades"].append(compare_trades(rec, hyd_t, f"{prov}:raw_recording"))
            entry["trades"].append(
                compare_trades(rec_d, hyd_t, f"{prov}:frame_deduped_recording"))
            hyd_n = paged_scan(hyd, "momentum_nbbo_spread_tape", NBBO_COLUMNS,
                               symbol, lo_a, hi_a, nsrc, aware=True)
            entry[f"hydrated_nbbo_structure_{prov}"] = nbbo_structure(hyd_n, recorded=False)
            cmp_n = compare_nbbo_asof(rec_nbbo_d, hyd_n, naive_utc(rec[0][T_AT]),
                                      naive_utc(rec[-1][T_AT]), nbbo_samples)
            cmp_n["provider"] = prov
            cmp_n["recorded_side"] = "frame-deduped recorded NBBO tape"
            entry["nbbo"].append(cmp_n)
        return entry
    finally:
        live.close()
        hyd.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol-day", action="append", type=parse_symbol_day,
                    default=[], metavar="SYM:YYYY-MM-DD", required=True)
    ap.add_argument("--provider", action="append", choices=sorted(PROVIDERS),
                    default=[], help="repeatable; defaults to both")
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--nbbo-samples", type=int, default=DEFAULT_NBBO_SAMPLES)
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args(argv)

    providers = args.provider or sorted(PROVIDERS)
    report = []
    for symbol, day in args.symbol_day:
        entry = check_symbol_day(symbol, day, providers, args.db_name,
                                 args.env_file, args.nbbo_samples)
        report.append(entry)
        headline = {"symbol": symbol, "day": day.isoformat(),
                    "recorded": entry.get("recorded_trade_rows")}
        for t in entry.get("trades", []):
            headline[t["provider"]] = t.get("multiset_ts_price")
        print(json.dumps(headline, default=str), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str),
                                   encoding="utf-8", newline="")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
