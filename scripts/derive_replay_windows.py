"""Derive diagnostic replay bounds from an explicitly selected golden archive.

The replay driver (``scripts/replay_ab_dark_flags.py``) needs
``WIN_START/WIN_END`` (a 2h-class intraday burst) plus ``OHLCV_START`` (warmup
lead). ``derive_window()`` finds the activity burst deterministically from the
GOLDEN per-minute tick histogram (never the live table): the minimal contiguous
span holding >=85%% of the day's ticks, always extended to include the day-high
minute, capped/floored, padded.

This is deliberately POST-SESSION/HINDSIGHT window selection. It is useful for a
stable diagnostic regression library, but it is not causal setup discovery,
walk-forward/OOS evidence, or Ross-profitability proof.

Three legacy diagnostic windows (CLRO 07-02 / QTTB 07-13 / PLSM 07-13) keep
their hand-picked bounds verbatim solely to compare with earlier A/B output;
derived bounds are printed beside them as a sanity check.

    python scripts/derive_replay_windows.py --out-dir D:/CHILI-Docker/chili-data/replay_batch

Output: ``window_manifest.json`` (schema chili.replay-window-manifest.v2) with a
``tier`` per window: "baseline" (legacy tie-backs + the largest retained windows,
max 2 per calendar day) or "library" (everything else). The DB transaction is
REPEATABLE READ and READ ONLY. No provider/network fallback exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from diagnostic_replay_db import (  # noqa: E402
    content_receipt_sha256,
    golden_window_content_receipt,
    guarded_database_identity,
    query_contract_sha256,
    verify_connected_endpoint,
)

BUILD = os.path.dirname(SCRIPT_DIR)
RECEIPT_HELPER = os.path.join(SCRIPT_DIR, "diagnostic_replay_db.py")
HIST_SQL = """
SELECT date_trunc('minute', observed_at) AS m, count(*) AS n
FROM replay_golden_ticks
WHERE symbol = %(s)s AND observed_at >= %(day)s::date
  AND observed_at < (%(day)s::date + 1) AND price > 0
GROUP BY 1 ORDER BY 1
"""

HI_SQL = """
SELECT observed_at FROM replay_golden_ticks
WHERE symbol = %(s)s AND observed_at >= %(day)s::date
  AND observed_at < (%(day)s::date + 1) AND price > 0
ORDER BY price DESC, observed_at ASC LIMIT 1
"""

CENSUS_SQL = """
SELECT t.symbol, t.day::text, t.ticks, coalesce(n.nbbo, 0)
FROM (SELECT symbol, observed_at::date AS day, count(*) AS ticks
      FROM replay_golden_ticks GROUP BY 1, 2) t
LEFT JOIN (SELECT symbol, observed_at::date AS day, count(*) AS nbbo
           FROM replay_golden_nbbo GROUP BY 1, 2) n USING (symbol, day)
ORDER BY t.day, t.symbol
"""

# Legacy tie-back windows preserve comparisons to earlier diagnostic runs. They
# carry no Ross/economic/causal credit and are never OOS selection.
LEGACY_TIEBACK = {
    ("CLRO", "2026-07-02"): {"win_start": "2026-07-02T14:00:00", "win_end": "2026-07-02T16:00:00",
                             "ohlcv_start": "2026-07-02T13:00:00", "prepend": False},
    ("QTTB", "2026-07-13"): {"win_start": "2026-07-13T13:00:00", "win_end": "2026-07-13T16:00:00",
                             "ohlcv_start": "2026-07-13T11:00:00", "prepend": False},
    ("PLSM", "2026-07-13"): {"win_start": "2026-07-13T13:00:00", "win_end": "2026-07-13T16:00:00",
                             "ohlcv_start": "2026-07-13T12:05:00", "prepend": True},
}

# Ross-PHASE cross-reference windows (E2): hand-set bounds matching the frame-
# verified Ross windows (manifest window_et, EDT+4h -> naive UTC) so the ①②③
# grading judges the SAME leg Ross traded — the derived activity-burst windows
# for these days replay a different phase (CETX opens post-squeeze; CLRO both
# days replay the later leg). Emitted ONLY via --rossxref into a SEPARATE
# manifest (one (symbol, day) row per manifest). Diagnostic evidence only —
# carries no Ross/economic/causal credit by itself.
ROSS_PHASE_XREF = {
    ("CETX", "2026-07-02"): {"win_start": "2026-07-02T12:25:00", "win_end": "2026-07-02T13:35:00",
                             "ohlcv_start": "2026-07-02T11:25:00", "prepend": False},
    ("CLRO", "2026-07-02"): {"win_start": "2026-07-02T12:15:00", "win_end": "2026-07-02T13:00:00",
                             "ohlcv_start": "2026-07-02T11:15:00", "prepend": False},
    ("CLRO", "2026-07-07"): {"win_start": "2026-07-07T12:40:00", "win_end": "2026-07-07T13:35:00",
                             "ohlcv_start": "2026-07-07T11:40:00", "prepend": False},
}

# 2026-08-15: 22 -> 48. Ang canon-v3 cohort (22 windows, Hun-Hul) ay natabunan
# ng 16+ bagong higanteng pins (08-06..08-13, 600k-1.2M ticks) sa largest-N
# promotion — bumagsak sa 'library' tier at HINDI na runnable ng batch runner
# ("only the content-hashed baseline tier is runnable"), kaya imposible ang
# canon re-run/reprice sa sariwang manifest. 48 = tiebacks + canon cohort +
# bagong pins + margin; ang MAX_PER_DAY=2 ay eksaktong kasya sa canon days.
BASELINE_TARGET = 48          # legacy tie-backs + top-gold fill
MAX_PER_DAY = 2               # diversity: no single day monopolizes the baseline


def estimate_runtime_s(ticks: int) -> int:
    # CLRO 860k ~ 40-48 min, QTTB 378k ~ 25 min  =>  ~8 min fixed + ~2.8 ms/tick
    return 480 + int(ticks * 0.0028)


def guard_source_database_url(url: str) -> tuple[str, str]:
    identity = guarded_database_identity(url, sink=False)
    return url, identity.dbname


def clean_build_sha() -> str:
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=BUILD,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=BUILD,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if dirty.strip():
        raise SystemExit(
            "[derive] REFUSING dirty worktree; manifest provenance requires "
            "an immutable clean commit"
        )
    return rev


def file_sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@dataclass
class DerivedWindow:
    win_start: datetime
    win_end: datetime
    ohlcv_start: datetime
    evidence: dict


def derive_window(minute_counts: list[tuple[datetime, int]], hi_at: datetime, *,
                  coverage: float = 0.85, cap_min: int = 165, hard_cap_min: int = 150,
                  min_min: int = 45, pad_pre_min: int = 5, pad_post_min: int = 10,
                  ohlcv_lead_min: int = 60) -> DerivedWindow:
    """Deterministic burst finder on a per-minute tick histogram (pure — unit-testable)."""
    assert minute_counts, "empty histogram"
    t0 = minute_counts[0][0]
    t1 = minute_counts[-1][0]
    n_min = int((t1 - t0).total_seconds() // 60) + 1
    dense = [0] * n_min
    for m, n in minute_counts:
        dense[int((m - t0).total_seconds() // 60)] = int(n)
    total = sum(dense)
    hi_idx = min(max(int((hi_at.replace(second=0, microsecond=0) - t0).total_seconds() // 60), 0),
                 n_min - 1)

    def minimal_span(cov: float) -> tuple[int, int]:
        # two-pointer minimal contiguous span with sum >= cov*total; earliest tie-break
        target = cov * total
        best = (0, n_min - 1)
        best_len = n_min
        s = 0
        lo = 0
        for hi in range(n_min):
            s += dense[hi]
            while s - dense[lo] >= target:
                s -= dense[lo]
                lo += 1
            if s >= target and (hi - lo) < best_len:
                best, best_len = (lo, hi), hi - lo
        return best

    cov = coverage
    lo, hi = minimal_span(cov)
    lo, hi = min(lo, hi_idx), max(hi, hi_idx)  # burst must contain the day high
    while (hi - lo + 1) > cap_min and cov > 0.55:
        cov = round(cov - 0.05, 2)
        lo, hi = minimal_span(cov)
        lo, hi = min(lo, hi_idx), max(hi, hi_idx)
    if (hi - lo + 1) > cap_min:
        # fixed-length max-sum span constrained to contain hi_idx (earliest tie-break)
        best_s, best_lo = -1, max(0, hi_idx - hard_cap_min + 1)
        for start in range(max(0, hi_idx - hard_cap_min + 1),
                           min(hi_idx, n_min - hard_cap_min) + 1):
            s = sum(dense[start:start + hard_cap_min])
            if s > best_s:
                best_s, best_lo = s, start
        lo, hi = best_lo, best_lo + hard_cap_min - 1
    if (hi - lo + 1) < min_min:  # symmetric extend, bounded by tape extent
        need = min_min - (hi - lo + 1)
        lo = max(0, lo - need // 2)
        hi = min(n_min - 1, lo + min_min - 1)
        lo = max(0, hi - min_min + 1)
    span_ticks = sum(dense[lo:hi + 1])
    win_start = t0 + timedelta(minutes=lo - pad_pre_min)
    win_end = t0 + timedelta(minutes=hi + 1 + pad_post_min)
    day0 = t0.replace(hour=0, minute=0, second=0, microsecond=0)
    win_start = max(win_start, day0)
    win_end = min(win_end, day0 + timedelta(days=1))
    ohlcv_start = max(win_start - timedelta(minutes=ohlcv_lead_min), day0)
    return DerivedWindow(win_start, win_end, ohlcv_start, {
        "day_ticks": total, "span_ticks": span_ticks, "coverage_used": cov,
        "span_min": hi - lo + 1, "hi_at": hi_at.isoformat(),
        "tape_first_min": t0.isoformat(), "tape_last_min": t1.isoformat(),
        "params": {"coverage": coverage, "cap_min": cap_min, "hard_cap_min": hard_cap_min,
                   "min_min": min_min, "pad_pre_min": pad_pre_min,
                   "pad_post_min": pad_post_min, "ohlcv_lead_min": ohlcv_lead_min},
    })


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--source-database-url",
        required=True,
        help=(
            "explicit PostgreSQL URL containing replay_golden_*; opened in one "
            "REPEATABLE READ, READ ONLY transaction"
        ),
    )
    ap.add_argument("--out-dir", default="D:/CHILI-Docker/chili-data/replay_batch")
    ap.add_argument(
        "--rossxref",
        action="store_true",
        help=(
            "emit ONLY the hand-set Ross-phase cross-reference windows into "
            "window_manifest_rossxref.json (separate manifest — one "
            "(symbol, day) row per manifest)"
        ),
    )
    args = ap.parse_args()
    hand_set = ROSS_PHASE_XREF if args.rossxref else LEGACY_TIEBACK
    build_sha = clean_build_sha()
    generator_sha256 = file_sha256(__file__)
    receipt_helper_sha256 = file_sha256(RECEIPT_HELPER)

    import psycopg2

    source_identity = guarded_database_identity(
        args.source_database_url, sink=False
    )
    source_url, expected_database_name = (
        args.source_database_url,
        source_identity.dbname,
    )
    conn = psycopg2.connect(source_url)
    conn.set_session(
        readonly=True,
        autocommit=False,
        isolation_level="REPEATABLE READ",
    )
    verify_connected_endpoint(conn, source_identity)
    windows = []
    with conn.cursor() as cur:
        cur.execute("SET LOCAL TIME ZONE 'UTC'")
        cur.execute(
            "SELECT current_database(), current_setting('transaction_read_only'), "
            "current_setting('transaction_isolation'), current_setting('TimeZone')"
        )
        source_database_name, read_only, isolation, time_zone = cur.fetchone()
        source_database_name = str(source_database_name)
        if source_database_name != expected_database_name:
            raise RuntimeError("source database identity mismatch")
        if str(read_only).lower() not in {"on", "true"}:
            raise RuntimeError("source transaction is not read-only")
        if str(isolation).lower() != "repeatable read":
            raise RuntimeError("source transaction is not repeatable read")
        if str(time_zone).upper() not in {"UTC", "ETC/UTC"}:
            raise RuntimeError("source session timezone is not UTC")
        cur.execute(CENSUS_SQL)
        census = cur.fetchall()
        for sym, day, ticks, nbbo in census:
            if ticks == 0:
                continue
            if args.rossxref and (sym, day) not in ROSS_PHASE_XREF:
                continue
            cur.execute(HIST_SQL, {"s": sym, "day": day})
            hist = cur.fetchall()
            cur.execute(HI_SQL, {"s": sym, "day": day})
            (hi_at,) = cur.fetchone()
            d = derive_window([(m, int(n)) for m, n in hist], hi_at)
            key = (sym, day)
            entry = {
                "symbol": sym, "day": day,
                "class": "retained_archive",
                "ticks": int(ticks), "nbbo": int(nbbo),
                "win_start": d.win_start.isoformat(),
                "win_end": d.win_end.isoformat(),
                "ohlcv_start": d.ohlcv_start.isoformat(),
                "prepend": False,
                "window_source": "derived",
                "est_runtime_s": estimate_runtime_s(int(ticks)),
                "derivation": d.evidence,
            }
            if key in hand_set:
                c = hand_set[key]
                delta_start = (datetime.fromisoformat(c["win_start"]) - d.win_start)
                delta_end = (datetime.fromisoformat(c["win_end"]) - d.win_end)
                label = "ROSSXREF" if args.rossxref else "TIEBACK"
                print(f"[derive] {label} {sym} {day}: hand-picked {c['win_start']}..{c['win_end']}"
                      f" | derived {d.win_start.isoformat()}..{d.win_end.isoformat()}"
                      f" (delta start {delta_start}, end {delta_end}) — keeping hand-set bounds")
                entry.update(c)
                entry["window_source"] = "legacy_tieback_diagnostic"
                if args.rossxref:
                    entry["phase_note"] = "ross_phase_xref"
            windows.append(entry)

    # Tier selection is diagnostic only: legacy tie-backs plus the largest
    # windows. No Ross/video label can promote a window.
    per_day: dict[str, int] = {}
    for w in windows:
        w["tier"] = "library"
    def promote(w) -> bool:
        if (
            per_day.get(w["day"], 0) >= MAX_PER_DAY
            and w["window_source"] != "legacy_tieback_diagnostic"
        ):
            return False
        w["tier"] = "baseline"
        per_day[w["day"]] = per_day.get(w["day"], 0) + 1
        return True

    n_base = 0
    for w in windows:
        if w["window_source"] == "legacy_tieback_diagnostic":
            if promote(w):
                n_base += 1
    for w in sorted(windows, key=lambda x: -x["ticks"]):
        if n_base >= BASELINE_TARGET:
            break
        if w["tier"] == "baseline":
            continue
        if promote(w):
            n_base += 1

    # Hash exact retained rows only for the bounded baseline tier. Nonempty
    # hashes prove byte stability, not causal completeness or executable coverage.
    for window in windows:
        if window["tier"] != "baseline":
            window["source_content_receipt"] = None
            window["source_content_receipt_sha256"] = None
            window["source_content_status"] = "NOT_HASHED"
            window["coverage_status"] = "COVERAGE_UNAVAILABLE"
            continue
        receipt = golden_window_content_receipt(
            conn,
            symbol=window["symbol"],
            start=window["ohlcv_start"],
            end=window["win_end"],
        )
        window["source_content_receipt"] = receipt
        window["source_content_receipt_sha256"] = content_receipt_sha256(
            receipt
        )
        rows_present = (
            receipt["ticks"]["bytes"] > 0 and receipt["nbbo"]["bytes"] > 0
        )
        window["source_content_status"] = (
            "CONTENT_HASHED" if rows_present else "ROWS_UNAVAILABLE"
        )
        window["coverage_status"] = (
            "DIAGNOSTIC_ONLY" if rows_present else "COVERAGE_UNAVAILABLE"
        )
    conn.rollback()
    conn.close()

    est_total = sum(w["est_runtime_s"] for w in windows if w["tier"] == "baseline")
    doc = {"schema": "chili.replay-window-manifest.v2",
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "evidence_grade": "DIAGNOSTIC_ONLY",
           "causal_use_allowed": False,
           "window_selection": "post_session_hindsight_activity_burst",
           "ross_grade_credit_allowed": False,
           "source_backend_sealed": False,
           "child_source_snapshot_pinned": False,
           "build_sha": build_sha,
           "generator_sha256": generator_sha256,
           "receipt_helper_sha256": receipt_helper_sha256,
           "query_contract_sha256": query_contract_sha256(),
           "source_database_name": source_database_name,
           "source_database_identity": source_identity.public_dict(),
           "source_transaction": "REPEATABLE_READ_READ_ONLY",
           "baseline_count": n_base, "library_count": len(windows) - n_base,
           "baseline_est_runtime_s": est_total,
           "windows": sorted(windows, key=lambda w: (w["tier"] != "baseline", w["day"], w["symbol"]))}
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(
        args.out_dir,
        "window_manifest_rossxref.json" if args.rossxref else "window_manifest.json",
    )
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"[derive] {len(windows)} windows -> {out}")
    print(f"[derive] baseline tier: {n_base} windows, est {est_total / 3600:.1f}h sequential")
    for w in doc["windows"]:
        if w["tier"] == "baseline":
            print(f"  BASELINE {w['day']} {w['symbol']:6s} {w['class']:6s} "
                  f"{w['ticks']:>9,}t {w['win_start'][11:16]}-{w['win_end'][11:16]} "
                  f"({w['window_source']}) est {w['est_runtime_s'] // 60}min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
