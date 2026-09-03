"""Fail LOUDLY before a counterfactual run that would return nothing.

THE TWO WAYS A HYDRATED SYMBOL-DAY SILENTLY PRODUCES AN EMPTY STUDY
------------------------------------------------------------------
Both were found by review AFTER four phases of acceptance, because every earlier
acceptance drove the tape LOADERS -- and the loaders were never the problem.
The loaders return ticks correctly.  Admission is downstream of them.

1. NO USABLE NBBO TAPE.  ``counterfactual_replay._confidence`` returns
   ``("no_tape", ["no_nbbo_tape"])`` the moment the NBBO tape is empty, and
   bar-candidate generation is guarded on the NBBO ticks, not the trade ticks.
   Under the default ``live_admission_mode=True`` the two tick-driven families
   that could have used trade prints are deliberately skipped.  So a symbol-day
   with trades and no book yields ZERO candidates -- and "has trades" is what an
   earlier coverage report called replayable.

2. NO SOURCE EVENT.  ``run_counterfactual_symbol_replay`` gates every entry on
   ``require_source_before_entry`` (default True) and skips with
   ``no_ross_source_before_entry``.  Source events do NOT come from the database:
   ``load_ross_source_events`` reads JSONL files under
   ``D:\\CHILI-Docker\\chili-data\\ross_stream\\``, and those files carry nothing
   after early July 2026.  A corpus of August/September symbol-days therefore
   admits NOTHING, the run exits 0, and it prints a replay with zero trades.

Neither failure announces itself.  Both produce a clean, well-formed, empty
result -- which is the most expensive kind of wrong answer, because it looks
like a finding.  This exits non-zero with the sentence that explains it.

USAGE
-----
    python scripts/hydration_preflight.py --csv corpus.csv
    python scripts/hydration_preflight.py --symbol-day LGCL:2026-08-26 \\
        --allow-pre-source-entries

Pass the SAME admission flag you intend to pass to the replay harness.  The two
bypasses are NOT interchangeable -- see docs/HISTORICAL_TICK_HYDRATOR.md:

  --allow-pre-source-entries   keeps live_admission_mode ON; candidates still
                               come only from live's real ladder, but entries no
                               longer require a catalyst.
  --no-live-admission-mode     re-enables the ``market_certified`` synthetic
                               source window AND two harness-only tick families
                               live never evaluates. A DIFFERENT strategy from
                               live, not a wider view of the same one.

Read-only.  Touches ``chili_hydrated`` only; never ``chili``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from historical_tick_hydrator import (  # noqa: E402
    DEFAULT_HYDRATED_DB,
    NBBO_TABLE,
    TRADES_TABLE,
    connect,
    read_pairs_csv,
    resolve_dsn,
)

ET = ZoneInfo("America/New_York")


def session_bounds_utc(day: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(day, time(4, 0), tzinfo=ET).astimezone(timezone.utc)
    hi = datetime.combine(day, time(20, 0), tzinfo=ET).astimezone(timezone.utc)
    return lo, hi


def _load_source_events(symbol: str, since: datetime, until: datetime) -> int:
    """Count local Ross/catalyst source rows the replay would admit on.

    Imported lazily and behind the hydrated DSN, because importing
    ``app.services.trading`` pulls in ``app.config``, which requires
    ``DATABASE_URL``.  Pointing it at the HYDRATED database here is not a
    convenience: it is the same environment the study itself must run under, so
    a preflight that passes under a different DSN than the run would be lying.
    """
    os.environ.setdefault("DATABASE_URL", resolve_dsn(DEFAULT_HYDRATED_DB))
    from app.services.trading.momentum_neural.counterfactual_replay import (  # noqa: E402
        load_ross_source_events,
    )
    events = load_ross_source_events(since=since, until=until, symbols=[symbol])
    return sum(len(v) for v in events.values())


def check_symbol_day(conn, symbol: str, day: date, *,
                     source_gate_bypassed: bool) -> dict[str, Any]:
    lo, hi = session_bounds_utc(day)
    lo_n, hi_n = lo.replace(tzinfo=None), hi.replace(tzinfo=None)
    rec: dict[str, Any] = {"symbol": symbol, "date": day.isoformat(),
                           "since_utc": lo.isoformat(), "until_utc": hi.isoformat()}
    with conn.cursor() as cur:
        # The loaders' OWN predicates, not paraphrases of them.
        cur.execute(
            f"SELECT count(*) FROM {TRADES_TABLE} WHERE symbol = %s "
            "AND observed_at >= %s AND observed_at < %s AND price > 0",
            (symbol, lo_n, hi_n),
        )
        rec["trade_ticks"] = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT count(*) FROM {NBBO_TABLE} WHERE symbol = %s "
            "AND observed_at >= %s AND observed_at < %s "
            "AND bid > 0 AND ask > 0 AND ask >= bid",
            (symbol, lo, hi),
        )
        rec["nbbo_ticks"] = int(cur.fetchone()[0])

    rec["source_events"] = _load_source_events(symbol, lo, hi)
    rec["source_gate_bypassed"] = source_gate_bypassed

    blockers: list[str] = []
    if rec["trade_ticks"] == 0:
        blockers.append(
            "no_trade_tape: load_trade_tape would return zero ticks")
    if rec["nbbo_ticks"] == 0:
        blockers.append(
            "no_nbbo_tape: load_nbbo_tape would return zero ticks, so "
            "_confidence returns 'no_tape' and bar-candidate generation is "
            "skipped entirely — the run yields ZERO candidates, not few")
    if rec["source_events"] == 0 and not source_gate_bypassed:
        blockers.append(
            "no_source_events: load_ross_source_events returns nothing for this "
            "symbol-day, so every candidate is skipped with "
            "'no_ross_source_before_entry' and the run exits 0 with an empty "
            "study. Pass --allow-pre-source-entries or --no-live-admission-mode "
            "to the harness (and to this preflight) if that is intended")
    rec["blockers"] = blockers
    rec["ok"] = not blockers
    return rec


def build_report(pairs: list[tuple[str, date]], dbname: str,
                 env_path: str | None = None, *,
                 source_gate_bypassed: bool = False) -> dict[str, Any]:
    conn = connect(dbname, env_path)
    try:
        rows = [check_symbol_day(conn, sym, day,
                                 source_gate_bypassed=source_gate_bypassed)
                for sym, day in pairs]
    finally:
        conn.close()
    blocked = [r for r in rows if not r["ok"]]
    reasons: dict[str, int] = {}
    for r in blocked:
        for b in r["blockers"]:
            reasons[b.split(":", 1)[0]] = reasons.get(b.split(":", 1)[0], 0) + 1
    return {
        "database": dbname,
        "symbol_days": len(rows),
        "ok": len(rows) - len(blocked),
        "blocked": len(blocked),
        "source_gate_bypassed": source_gate_bypassed,
        "blocker_counts": reasons,
        "blocked_symbol_days": [f"{r['symbol']} {r['date']}: "
                                f"{', '.join(b.split(':', 1)[0] for b in r['blockers'])}"
                                for r in blocked],
        "per_symbol_day": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="corpus CSV (symbol,date)")
    ap.add_argument("--symbol-day", action="append", default=[],
                    metavar="SYMBOL:YYYY-MM-DD")
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--allow-pre-source-entries", action="store_true",
                    help="the harness flag that drops require_source_before_entry")
    ap.add_argument("--no-live-admission-mode", action="store_true",
                    dest="no_live_admission_mode",
                    help="the harness flag that re-enables market_certified")
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args(argv)

    pairs: list[tuple[str, date]] = []
    for token in args.symbol_day:
        sym, _, d = token.partition(":")
        pairs.append((sym.upper().strip(), date.fromisoformat(d.strip())))
    if args.csv:
        pairs.extend(read_pairs_csv(args.csv))
    if not pairs:
        ap.error("nothing to do: pass --csv and/or --symbol-day")

    report = build_report(
        pairs, args.db_name, args.env_file,
        source_gate_bypassed=bool(args.allow_pre_source_entries
                                  or args.no_live_admission_mode),
    )
    if args.json:
        with open(args.json, "w", newline="", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    print(json.dumps({k: v for k, v in report.items() if k != "per_symbol_day"},
                     indent=2, default=str))
    return 1 if report["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
