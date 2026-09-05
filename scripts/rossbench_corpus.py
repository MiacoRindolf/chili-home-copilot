#!/usr/bin/env python
"""Ross Parity Bench — corpus selection (``chili.rossbench_corpus.v1``).

Turns ``project_ws/AgentOps/ross/ross_master_ledger.json``
(``chili.ross_master_ledger.v1``) into the two artefacts the rest of the bench
consumes:

  * ``corpus.csv``  — ``symbol,date`` FIRST, so ``historical_tick_hydrator.py
    --csv`` / ``hydration_coverage_report.py --csv`` / ``hydration_preflight.py
    --csv`` can eat this file directly. ``read_pairs_csv``
    (historical_tick_hydrator.py:1472) reads only ``symbol``/``date``, ignores
    every other column, and de-duplicates PRESERVING FIRST-SEEN ORDER — which is
    why the row order below is a hydration work queue, not decoration.
  * ``corpus.json`` — the same rows plus the provenance a grader needs: why each
    row is (or is not) scoreable, what confirms its tape, when it expires, and
    what the hydration ledger says about it.

WHY THIS IS NOT A ONE-LINER OVER THE LEDGER
-------------------------------------------
The ledger is a merge of 26 batch extractions from video transcripts. Measured
over all 187 rows before this file was written:

  * 30 of the 187 rows are NOT TRADES. They are no-trade / miss records that a
    ``walk_lists`` heuristic merged in from five different sub-schemas. They are
    separated here by ``_path`` (``trades``/``rows`` are trades; the other three
    are not) and carried as ``record_kind='ross_no_trade'``. Scoring them as
    trades would credit CHILI for entries Ross never took.
  * ``0`` IS A NULL SENTINEL, not a measurement: entry_px has 67 zeros, exit_px
    103, shares 118, pnl_usd 30. A 0 pnl_usd read as a real flat destroys both
    Avoidance (a miss scored as a scratch) and Capture (a divide by a fake
    denominator), so ``sentinel_to_none`` is load-bearing, not tidying.
  * ``entry_time_et`` is a NARRATIVE field. 132/157 trade rows START with a
    clock, 14 more carry one mid-sentence, 11 carry none at all. The parser
    reports WHICH of those three it found (``entry_clock_basis``) rather than
    silently returning None for the mid-sentence ones.
  * ``account`` has three vocabularies (main / small / big). ``big`` collapses to
    ``main`` — the ledger's own ``big_account_pnl_claimed_separate`` handling in
    build_ross_manifest.py:_norm_account (:122) already treats "big" and "main" as
    one account, and this file matches it so the two builders cannot disagree.
  * ``confidence`` is inferred 90 / approx 59 / exact 8, and only 18 rows carry a
    UTC entry instant. Both are carried through verbatim; neither is upgraded.

ORDERING — AND WHY EACH KEY EXISTS
----------------------------------
``order_key`` is a five-part tuple, emitted machine-readably as
``order_key_spec`` so a reviewer can check the applied rule against the intent:

  1. ``not (tape_confirmed and outcome == 'win')`` — a tape-confirmed Ross WIN is
     the only row that can answer "did CHILI capture the move", because it is the
     only row where both the move and the tape are established facts.
  2. ``not lane_alive`` — within a tier, the symbol-days on which CHILI's lane was
     demonstrably ARMED come first. On those the comparison is a decision, not an
     uptime gap; every other row risks measuring that the process was not running
     (project_ross_master_ledger_0903: 71 % of the deficit is uptime).
  3. ``era_rank`` — COARSE (Jun/Jul 2026, then Aug/Sep 2026, then everything
     else). Deliberately coarse: IQFeed lookup retention is a 180-day cliff, so
     June rows expire before August rows and must be hydrated first — but a
     FINE-grained date key would make key 4 dead (dates are near-unique), and a
     sort key that never fires is a comment pretending to be code.
  4. ``-ross_usd`` — Ross dollars descending. Losses therefore sort LAST within
     their tier; they are the negative control and are never dropped.
  5. ``(date, symbol, record_kind, ledger_index)`` — a TOTAL order, so two runs
     over the same ledger emit byte-identical files.

WHAT THIS FILE REFUSES TO DO
----------------------------
An ``unrecoverable`` row (no provider can serve its tape) is REPORTED — it stays
in the corpus with ``provider='unrecoverable'``, it is listed under
``report.unrecoverable``, and it is printed. It is never silently dropped: a
corpus that quietly shrinks is how a bench comes to measure a subset and call it
the whole.

Usage:
  python scripts/rossbench_corpus.py --out-dir project_ws/AgentOps/ross
  python scripts/rossbench_corpus.py --out-dir X --no-db      # skip hydration_jobs
  python scripts/rossbench_corpus.py --out-dir X --check      # exit 1 on drift
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_REPO = _SCRIPTS.parent

# Imported, never re-spelled. hydration_coverage_report.py:74 imports from the
# hydrator for exactly this reason: a hardcoded copy of a retention constant or a
# source tag that drifted from the hydrator would produce a confident, wrong
# answer rather than an error.
from historical_tick_hydrator import (  # noqa: E402
    DEFAULT_HYDRATED_DB,
    IQFEED_RETENTION_DAYS,
    IQFEED_RETENTION_FLOOR_MEASURED,
    IQFEED_RETENTION_MEASURED_ON,
    connect as hydrator_connect,
    iqfeed_retention_floor,
)

SCHEMA = "chili.rossbench_corpus.v1"
LEDGER_SCHEMA = "chili.ross_master_ledger.v1"
DEFAULT_LEDGER = _REPO / "project_ws" / "AgentOps" / "ross" / "ross_master_ledger.json"


# ─────────────────────────────────────────────────────────────────────────────
# LEDGER VOCABULARIES — every one of these was MEASURED on the 187-row ledger
# before it was written down. The counts in the comments are what the ledger
# actually held at ``chili.ross_master_ledger.v1``; if a re-build changes them,
# ``assert_ledger_shape`` fails and names the new value rather than quietly
# reclassifying rows.
# ─────────────────────────────────────────────────────────────────────────────

# ``_path`` records which sub-schema the walk_lists merge pulled a row out of.
# Measured: trades 137 + rows 20 = 157 real trades; no_trade_references 16 +
# misses_and_no_trades 11 + ross_no_trade_context 3 = 30 non-trades.
TRADE_PATHS: tuple[str, ...] = ("trades", "rows")
NO_TRADE_PATHS: tuple[str, ...] = (
    "no_trade_references", "misses_and_no_trades", "ross_no_trade_context",
)

# Fields where the extraction wrote 0 for "absent". Measured zero counts across
# the 157 trade rows: entry_px 67, exit_px 103, shares 118, pnl_usd 30.
# stop_px is only present on 71 rows and is included for the same reason.
NULL_SENTINEL_FIELDS: tuple[str, ...] = (
    "entry_px", "exit_px", "stop_px", "shares", "pnl_usd",
)

# ``tape_coverage`` vocabulary, measured across xref (68) + hydration_worklist
# (55): none_must_hydrate / recorded_nbbo_only / recorded_ticks. The two
# "recorded_*" values mean OUR OWN live recording holds rows for the symbol-day.
RECORDED_TAPE_COVERAGE: tuple[str, ...] = ("recorded_ticks", "recorded_nbbo_only")

# Verdicts that prove CHILI's momentum lane was RUNNING and had the symbol on the
# board that day. ``never_armed`` and ``not_in_universe`` do not (the lane may
# simply never have seen it); ``unknown_no_data`` explicitly does not.
LANE_ALIVE_LEDGER_VERDICTS: tuple[str, ...] = ("armed_no_entry", "entered_wrong_leg")

# The lane-alive head of the corpus, from the operator's Ross-Parity-Bench step-8
# brief. This is an INPUT, not a derivation — but it is CROSS-CHECKED against the
# ledger by ``assert_lane_alive_supported``: every pair here must carry a
# ``LANE_ALIVE_LEDGER_VERDICTS`` verdict in the ledger's xref, or the build fails
# and names the row. Measured at write time: the ledger holds 12 armed_no_entry +
# 1 entered_wrong_leg xref rows, and all 8 below are inside that 13, so the
# operator's list is a deliberate SUBSET (ANY/MIMI/SHPH 06-25..26 and SXTC 08-18
# are armed_no_entry too but are not in the brief's head).
LANE_ALIVE_SYMBOL_DAYS: tuple[tuple[str, str], ...] = (
    ("ILLR", "2026-06-25"),
    ("SDOT", "2026-06-26"),
    ("ZDAI", "2026-06-26"),
    ("UPC", "2026-06-29"),
    ("IPST", "2026-08-17"),
    ("WETO", "2026-08-17"),
    ("PFSA", "2026-08-18"),
    ("SLE", "2026-08-18"),
)

# Coarse eras for order key 3. See the module docstring for why coarse.
# ("YYYY-MM" prefixes, first match wins.)
ERA_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("2026_jun_jul", ("2026-06", "2026-07")),
    ("2026_aug_sep", ("2026-08", "2026-09")),
)
ERA_OTHER = "other"

# Polygon's reach. docs/HISTORICAL_TICK_HYDRATOR.md "Provider comparison" states
# retention "back to ~2003" for Polygon/Massive v3 against IQFeed's hard
# 180-calendar-day cliff. ⚠️ The "~" is the DOC'S approximation — unlike the
# IQFeed floor (measured day-by-day in Phase 1: 2026-03-06 returned data, every
# weekday 2026-02-23..2026-03-05 returned NO_DATA), nobody has probed Polygon's
# actual first served day. It is therefore overridable with --polygon-floor, and
# a row rejected by it is reported, not dropped.
POLYGON_HISTORY_FLOOR = date(2003, 1, 1)

# Leading clock: the 132/157 shape, e.g. "~09:20 (break of 10.00 = VWAP…)".
_CLOCK_LEADING = re.compile(r"^\s*~?\s*(\d{1,2}:\d{2})")
# Anywhere: catches the further 14 rows whose clock sits mid-sentence, e.g.
# "not stated (…he called it at 09:41…)". Reported with a DIFFERENT basis so a
# consumer can decline the weaker 14 without re-parsing.
_CLOCK_ANY = re.compile(r"(\d{1,2}:\d{2})")

CLOCK_BASIS_LEADING = "leading"
CLOCK_BASIS_MID_SENTENCE = "mid_sentence"
CLOCK_BASIS_ABSENT = "absent"

RECORD_KIND_TRADE = "ross_trade"
RECORD_KIND_NO_TRADE = "ross_no_trade"
RECORD_KIND_XREF = "xref_symbol_day"

PROVIDER_IQFEED = "iqfeed"
PROVIDER_POLYGON = "polygon"
PROVIDER_UNRECOVERABLE = "unrecoverable"

HYDRATION_UNKNOWN = "unknown_db_unavailable"
HYDRATION_ABSENT = "absent"


# ─────────────────────────────────────────────────────────────────────────────
# FIELD NORMALISERS
# ─────────────────────────────────────────────────────────────────────────────

def sentinel_to_none(value: Any) -> float | None:
    """``0`` -> ``None``. THE NONE RULE IS LOAD-BEARING.

    The extraction wrote 0 where the transcript said nothing. Treating one of
    those as a measured zero turns a miss into a scratch in Avoidance and a real
    denominator into a fake one in Capture. Non-zero numbers pass through as
    floats; anything non-numeric becomes None (the ledger holds prose in some of
    these slots on the merged sub-schemas).
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never a price
        return None
    if not isinstance(value, (int, float)):
        return None
    f = float(value)
    return None if f == 0.0 else f


def parse_entry_clock(text: Any) -> tuple[str | None, str]:
    """``entry_time_et`` -> (``HH:MM`` or None, basis).

    ``entry_time_et`` is NARRATIVE. Returns the basis alongside the clock so a
    consumer can distinguish the 132 rows whose clock leads the field from the 14
    whose clock is buried in prose ("not stated (…around 09:41 he…)"), which are
    weaker evidence for the same nominal value.
    """
    if not isinstance(text, str) or not text.strip():
        return None, CLOCK_BASIS_ABSENT
    m = _CLOCK_LEADING.match(text)
    if m:
        return m.group(1), CLOCK_BASIS_LEADING
    m = _CLOCK_ANY.search(text)
    if m:
        return m.group(1), CLOCK_BASIS_MID_SENTENCE
    return None, CLOCK_BASIS_ABSENT


def normalize_account(text: Any) -> str | None:
    """main / small / None, with ``big`` collapsed to ``main``.

    Same mapping as build_ross_manifest.py:_norm_account (:122) — "big" or "main"
    -> "main" — so the two builders cannot disagree about which account a row
    belongs to. Measured on the 157 trade rows: main 49, small 25, big 16,
    absent 67.
    """
    if not text:
        return None
    t = str(text).strip().lower()
    if "small" in t:
        return "small"
    if "big" in t or "main" in t:
        return "main"
    return None


def outcome_of(pnl_usd: float | None) -> str:
    """win / loss / unknown. ``unknown`` is NOT ``flat``: 30 of the trade rows
    carry the 0 sentinel, and ``sentinel_to_none`` has already turned those into
    None by the time this sees them."""
    if pnl_usd is None:
        return "unknown"
    return "win" if pnl_usd > 0 else "loss"


def era_rank(day: str) -> tuple[int, str]:
    """(rank, label) for order key 3. See the docstring for why coarse."""
    for idx, (label, prefixes) in enumerate(ERA_BUCKETS):
        if any(str(day).startswith(p) for p in prefixes):
            return idx, label
    return len(ERA_BUCKETS), ERA_OTHER


def parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RETENTION / PROVIDER
# ─────────────────────────────────────────────────────────────────────────────

def expires_on(day: date, retention_days: int = IQFEED_RETENTION_DAYS) -> date:
    """The last calendar day IQFeed lookup can still serve ``day`` at tick fidelity.

    ``day + 180``. This is the SAME cliff the hydrator enforces from the other
    side — ``iqfeed_retention_floor(today) = today - 180`` — because
    ``day >= today - 180  <=>  day + 180 >= today``. Stating it as an expiry date
    rather than a floor is what makes the corpus a work queue: a row whose
    ``expires_on`` is nearest is the row to hydrate first.
    """
    return day + timedelta(days=int(retention_days))


def provider_for(
    day: date | None,
    *,
    today: date,
    polygon_floor: date = POLYGON_HISTORY_FLOOR,
    jobs_exhausted: bool = False,
) -> tuple[str, str]:
    """(provider, reason). ``unrecoverable`` is a REPORTED outcome, never a drop.

    IQFeed first: it is the fit source for FSM replay trades (its at-trade quote
    is a native measurement, not a reconstruction — docs/HISTORICAL_TICK_HYDRATOR.md
    "Provider comparison"), and it is what the corpus races the cliff for. Past
    the cliff, Polygon. ``jobs_exhausted`` is the empirical case: the hydration
    ledger already shows every provider we tried returning ``no_data``/``failed``
    for this symbol-day, which outranks any date arithmetic.
    """
    if day is None:
        return PROVIDER_UNRECOVERABLE, "date_unparseable"
    if jobs_exhausted:
        return (PROVIDER_UNRECOVERABLE,
                "hydration_jobs: every attempted provider returned no_data/failed")
    if day > today:
        return PROVIDER_UNRECOVERABLE, f"date {day.isoformat()} is in the future (today {today.isoformat()})"
    if day >= iqfeed_retention_floor(today):
        return (PROVIDER_IQFEED,
                f"within IQFeed lookup retention (floor {iqfeed_retention_floor(today).isoformat()} "
                f"= today - {IQFEED_RETENTION_DAYS}d)")
    if day >= polygon_floor:
        return (PROVIDER_POLYGON,
                f"past the IQFeed {IQFEED_RETENTION_DAYS}d cliff "
                f"(floor {iqfeed_retention_floor(today).isoformat()}); Polygon v3 reaches "
                f"back to ~{polygon_floor.isoformat()}")
    return (PROVIDER_UNRECOVERABLE,
            f"older than both providers (IQFeed floor {iqfeed_retention_floor(today).isoformat()}, "
            f"Polygon floor {polygon_floor.isoformat()})")


# ─────────────────────────────────────────────────────────────────────────────
# HYDRATION LEDGER
# ─────────────────────────────────────────────────────────────────────────────
# ``hydration_jobs`` is keyed (symbol, trading_day, dataset, provider) with
# status in done / no_data / failed / skipped / pending
# (historical_tick_hydrator.py:542 DDL, :1593 status roll-up).
#
# "Replayable" is the hydrator doc's definition, NOT "has trades":
# ``counterfactual_replay._confidence`` returns ``no_tape`` the moment the NBBO
# tape is empty and bar-candidate generation is guarded on NBBO ticks, so a
# trades-only symbol-day yields ZERO candidates — not few
# (docs/HISTORICAL_TICK_HYDRATOR.md, "WHAT 'REPLAYABLE' MEANS").
#
# ⚠️ ``--status`` cannot answer this and neither can a bare status string: a job
# marked ``done`` that loaded zero rows is a HOLE, not coverage. So every
# roll-up below reads ``rows_loaded``, not just ``status``.

HYDRATION_JOBS_SQL = (
    "SELECT symbol, trading_day, dataset, provider, status, rows_loaded, last_error "
    "FROM hydration_jobs WHERE (symbol, trading_day) IN %s"
)


def roll_up_hydration(job_rows: Sequence[dict[str, Any]] | None) -> dict[str, Any]:
    """Job rows for ONE symbol-day -> the summary a corpus row carries.

    ``None`` means the ledger could not be read at all (DB down / --no-db) and is
    reported as ``unknown_db_unavailable`` — distinct from ``absent``, which
    means the ledger WAS read and holds nothing for this symbol-day. Conflating
    the two would let a dead database look like an un-hydrated corpus.
    """
    if job_rows is None:
        return {
            "hydration_status": HYDRATION_UNKNOWN,
            "hydration_jobs": None,
            "hydrated_trade_rows": None,
            "hydrated_nbbo_rows": None,
            "replayable": None,
            "jobs_exhausted": False,
        }
    if not job_rows:
        return {
            "hydration_status": HYDRATION_ABSENT,
            "hydration_jobs": [],
            "hydrated_trade_rows": 0,
            "hydrated_nbbo_rows": 0,
            "replayable": False,
            "jobs_exhausted": False,
        }
    per_dataset: dict[str, int] = {"trades": 0, "nbbo": 0}
    statuses: set[str] = set()
    for row in job_rows:
        ds = str(row.get("dataset") or "")
        statuses.add(str(row.get("status") or ""))
        if ds in per_dataset:
            per_dataset[ds] += int(row.get("rows_loaded") or 0)
    trades, nbbo = per_dataset["trades"], per_dataset["nbbo"]
    replayable = trades > 0 and nbbo > 0
    # Exhausted = we ran jobs and NOT ONE of them produced a row. A partially
    # loaded symbol-day is a hole to fill, not a dead end.
    exhausted = (trades == 0 and nbbo == 0
                 and bool(statuses & {"no_data", "failed"})
                 and not (statuses & {"pending", "skipped"}))
    if replayable:
        status = "replayable_trades_and_nbbo"
    elif trades > 0:
        # The hydrator doc's own tier name for trades-without-a-usable-book.
        status = "tape_only_not_replayable"
    elif nbbo > 0:
        status = "nbbo_only_not_replayable"
    elif exhausted:
        status = "exhausted_no_data"
    else:
        status = "jobs_present_zero_rows"
    return {
        "hydration_status": status,
        "hydration_jobs": sorted(
            ({"dataset": str(r.get("dataset") or ""),
              "provider": str(r.get("provider") or ""),
              "status": str(r.get("status") or ""),
              "rows_loaded": int(r.get("rows_loaded") or 0),
              "last_error": r.get("last_error")} for r in job_rows),
            key=lambda r: (r["dataset"], r["provider"]),
        ),
        "hydrated_trade_rows": trades,
        "hydrated_nbbo_rows": nbbo,
        "replayable": replayable,
        "jobs_exhausted": exhausted,
    }


def fetch_hydration_jobs(
    pairs: Sequence[tuple[str, date]],
    *,
    dbname: str = DEFAULT_HYDRATED_DB,
    env_path: str | None = None,
    connect: Callable[..., Any] = hydrator_connect,
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]] | None, str | None]:
    """({(symbol, 'YYYY-MM-DD'): [job rows]}, error). NEVER raises.

    Read-only against ``chili_hydrated``; ``chili`` is not touched here at all.
    On any failure it returns ``(None, reason)`` so every row degrades to
    ``unknown_db_unavailable`` WITH the reason recorded, rather than the build
    dying or — worse — silently reporting an un-hydrated corpus.
    """
    if not pairs:
        return {}, None
    conn = None
    try:
        conn = connect(dbname, env_path)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(HYDRATION_JOBS_SQL, (tuple(pairs),))
            out: dict[tuple[str, str], list[dict[str, Any]]] = {(s, d.isoformat()): [] for s, d in pairs}
            for sym, day, dataset, provider, status, rows_loaded, last_error in cur.fetchall():
                key = (str(sym), day.isoformat() if hasattr(day, "isoformat") else str(day)[:10])
                out.setdefault(key, []).append({
                    "dataset": dataset, "provider": provider, "status": status,
                    "rows_loaded": rows_loaded, "last_error": last_error,
                })
        return out, None
    except Exception as exc:  # noqa: BLE001 — a dead ledger must not kill the build
        logger.warning("[rossbench_corpus] hydration_jobs unreadable (%s): %s",
                       type(exc).__name__, exc)
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ─────────────────────────────────────────────────────────────────────────────
# LEDGER SHAPE GUARDS
# ─────────────────────────────────────────────────────────────────────────────

def assert_ledger_shape(ledger: dict[str, Any]) -> None:
    """Fail closed on a ledger this file was not written against.

    A schema bump that renamed ``_path`` or dropped ``xref`` would otherwise
    produce a corpus that is merely SMALLER — and a bench measuring a subset it
    believes is the whole is the failure mode this whole module exists to avoid.
    """
    got = str(ledger.get("schema") or "")
    if got != LEDGER_SCHEMA:
        raise AssertionError(
            f"assert_ledger_shape: ledger schema is {got!r}, not {LEDGER_SCHEMA!r} — "
            "the _path / null-sentinel / account rules in this file were measured "
            "against that schema and are not known to hold for another."
        )
    for key in ("trades", "xref"):
        if not isinstance(ledger.get(key), list):
            raise AssertionError(f"assert_ledger_shape: ledger has no {key!r} list")
    known = set(TRADE_PATHS) | set(NO_TRADE_PATHS)
    unknown = sorted({str(t.get("_path")) for t in ledger["trades"]} - known)
    if unknown:
        raise AssertionError(
            f"assert_ledger_shape: unknown _path value(s) {unknown} in ledger['trades'] — "
            "this file classifies trade vs no-trade by _path, so an unrecognised "
            "sub-schema would be silently scored as whichever side the default fell on."
        )


def assert_lane_alive_supported(
    ledger: dict[str, Any],
    lane_alive: Iterable[tuple[str, str]] = LANE_ALIVE_SYMBOL_DAYS,
) -> None:
    """Every declared lane-alive symbol-day must be backed by the ledger's verdict.

    The list is an operator INPUT (the step-8 brief), so it cannot be derived —
    but it CAN be falsified. If a named pair is not ``armed_no_entry`` /
    ``entered_wrong_leg`` in the ledger's xref, then either the ledger changed or
    the list is wrong, and ordering the corpus by it would put unproven rows at
    the head of the bench.
    """
    verdicts: dict[tuple[str, str], str] = {}
    for x in ledger.get("xref") or []:
        v = x.get("chili_verdict") or x.get("verdict")
        if v:
            verdicts[(str(x.get("symbol")), str(x.get("date")))] = str(v)
    bad = []
    for pair in lane_alive:
        got = verdicts.get((str(pair[0]), str(pair[1])))
        if got not in LANE_ALIVE_LEDGER_VERDICTS:
            bad.append((pair, got))
    if bad:
        raise AssertionError(
            "assert_lane_alive_supported: declared lane-alive symbol-day(s) are not "
            f"backed by a {list(LANE_ALIVE_LEDGER_VERDICTS)} verdict in the ledger's "
            "xref — the lane cannot be shown to have been armed there:\n"
            + "\n".join(f"  {s} {d}: ledger verdict {got!r}" for (s, d), got in bad)
        )


# ─────────────────────────────────────────────────────────────────────────────
# TAPE CONFIRMATION
# ─────────────────────────────────────────────────────────────────────────────

def tape_confirmation(
    pair: tuple[str, str],
    *,
    coverage: dict[tuple[str, str], str],
    tape_pins: set[tuple[str, str]],
    hydration: dict[str, Any],
) -> tuple[bool, str]:
    """(tape_confirmed, basis) — "a tape for this symbol-day is an established fact".

    ⚠️ This is DELIBERATELY not the same predicate as ``replayable``. Confirmed
    means somebody has actually read a tape for this symbol-day; replayable means
    it carries BOTH trades and a usable book, which is what the FSM needs. A
    ``recorded_nbbo_only`` day is confirmed and NOT replayable, and the corpus
    must be able to say both at once.

    Three independent confirmations, checked strongest-first:
      1. hydration_jobs shows trades AND nbbo loaded (bought history, present now)
      2. the ledger carries a ``tape_pin`` — a per-minute price band that was read
         off the tape row by row, so the tape demonstrably existed
      3. the ledger's ``tape_coverage`` says our own live recording holds rows
    """
    if hydration.get("replayable"):
        return True, "hydration_jobs: trades and nbbo loaded"
    if pair in tape_pins:
        return True, "ledger tape_pin (per-minute band read off the tape)"
    cov = coverage.get(pair)
    if cov in RECORDED_TAPE_COVERAGE:
        return True, f"ledger tape_coverage={cov} (our own live recording)"
    if cov:
        return False, f"ledger tape_coverage={cov}"
    return False, "no tape confirmation in the ledger or the hydration jobs"


# ─────────────────────────────────────────────────────────────────────────────
# ROW BUILD
# ─────────────────────────────────────────────────────────────────────────────

CSV_COLUMNS: tuple[str, ...] = (
    # symbol,date FIRST — read_pairs_csv (historical_tick_hydrator.py:1472) reads
    # only these two and preserves first-seen order, so this file IS the hydration
    # work queue. Every other column is ignored by the hydrator by design.
    "symbol", "date",
    "rank", "record_kind", "video_id", "side", "account",
    "entry_clock_et", "entry_clock_basis", "entry_time_utc",
    "entry_px", "exit_px", "stop_px", "shares", "pnl_usd",
    "outcome", "confidence",
    "lane_alive", "tape_confirmed", "tape_confirmation_basis",
    "scoreable", "scoreable_reason",
    "expires_on", "days_to_expiry", "iqfeed_retention_ok",
    "provider", "provider_reason",
    "hydration_status", "hydrated_trade_rows", "hydrated_nbbo_rows", "replayable",
    "ledger_path", "ledger_src",
)


def _scoreable(row: dict[str, Any]) -> tuple[bool, str]:
    """The honest scoreable core: a real trade, on a clock, with a real entry
    price, inside IQFeed's reach.

    Measured over the ledger at write time this admits 86 rows — 49 wins, 21
    losses and 16 whose pnl is the 0 sentinel (``outcome='unknown'``). The 70
    with a real pnl are the Capture/Avoidance denominator; the 16 are scoreable
    for ENTRY geometry but carry no dollar truth, which is why ``outcome`` is
    carried separately rather than folded into this boolean.
    """
    if row["record_kind"] != RECORD_KIND_TRADE:
        return False, f"record_kind={row['record_kind']} (not a Ross trade)"
    if row["entry_clock_et"] is None:
        return False, "entry_time_et carries no clock (narrative field)"
    if row["entry_px"] is None:
        return False, "entry_px is the 0 null sentinel"
    if not row["iqfeed_retention_ok"]:
        return False, (f"expires_on {row['expires_on']} is past — outside IQFeed's "
                       f"{IQFEED_RETENTION_DAYS}d tick retention")
    return True, "clock + entry_px + inside IQFeed retention"


def order_key(row: dict[str, Any]) -> tuple:
    """The five-part total order. See the module docstring for why each key exists."""
    return (
        0 if (row["tape_confirmed"] and row["outcome"] == "win") else 1,
        0 if row["lane_alive"] else 1,
        int(row["_era_rank"]),
        -float(row["_ross_usd"] or 0.0),
        str(row["date"]),
        str(row["symbol"]),
        str(row["record_kind"]),
        int(row["_ledger_index"]),
    )


ORDER_KEY_SPEC: tuple[dict[str, str], ...] = (
    {"key": "not (tape_confirmed and outcome == 'win')",
     "why": "a tape-confirmed Ross win is the only row where both the move and the tape are facts"},
    {"key": "not lane_alive",
     "why": "on an armed symbol-day the gap is a decision, not uptime (ross_master_ledger: 71% of the deficit is uptime)"},
    {"key": "era_rank (coarse: 2026-06/07, then 2026-08/09, then other)",
     "why": "June expires first under IQFeed's 180d cliff; coarse so the dollar key below is not dead"},
    {"key": "-ross_usd",
     "why": "Ross dollars descending; losses sort last within their tier and are the negative control"},
    {"key": "(date, symbol, record_kind, ledger_index)",
     "why": "total order, so two builds over one ledger are byte-identical"},
)


def build_rows(
    ledger: dict[str, Any],
    *,
    today: date,
    jobs_by_pair: dict[tuple[str, str], list[dict[str, Any]]] | None,
    lane_alive: Iterable[tuple[str, str]] = LANE_ALIVE_SYMBOL_DAYS,
    polygon_floor: date = POLYGON_HISTORY_FLOOR,
) -> list[dict[str, Any]]:
    """Ledger -> unordered corpus rows. Pure: no DB, no clock, no filesystem."""
    assert_ledger_shape(ledger)
    assert_lane_alive_supported(ledger, lane_alive)

    lane_set = {(str(s), str(d)) for s, d in lane_alive}
    coverage: dict[tuple[str, str], str] = {}
    tape_pins: set[tuple[str, str]] = set()
    xref_pnl: dict[tuple[str, str], float | None] = {}
    for x in ledger.get("xref") or []:
        pair = (str(x.get("symbol")), str(x.get("date")))
        if x.get("tape_coverage"):
            coverage[pair] = str(x["tape_coverage"])
        if x.get("tape_pin"):
            tape_pins.add(pair)
        xref_pnl[pair] = sentinel_to_none(x.get("ross_pnl_usd"))
    # The hydration worklist carries tape_coverage for symbol-days the xref does
    # not cover. It is a FALLBACK, never an override: xref rows are the ones with
    # per-symbol-day DB evidence attached.
    for w in ledger.get("hydration_worklist") or []:
        pair = (str(w.get("symbol")), str(w.get("date")))
        if w.get("tape_coverage") and pair not in coverage:
            coverage[pair] = str(w["tape_coverage"])

    rows: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def _emit(idx: int, kind: str, symbol: str, day_s: str, src: dict[str, Any],
              ross_usd: float | None, extra: dict[str, Any]) -> None:
        pair = (symbol, day_s)
        seen_pairs.add(pair)
        day = parse_day(day_s)
        hyd = roll_up_hydration(None if jobs_by_pair is None else jobs_by_pair.get(pair, []))
        confirmed, basis = tape_confirmation(
            pair, coverage=coverage, tape_pins=tape_pins, hydration=hyd)
        provider, provider_reason = provider_for(
            day, today=today, polygon_floor=polygon_floor,
            jobs_exhausted=bool(hyd["jobs_exhausted"]))
        exp = expires_on(day) if day is not None else None
        rank, era_label = era_rank(day_s)
        row: dict[str, Any] = {
            "symbol": symbol,
            "date": day_s,
            "record_kind": kind,
            "video_id": src.get("video_id"),
            "lane_alive": pair in lane_set,
            "tape_confirmed": confirmed,
            "tape_confirmation_basis": basis,
            "expires_on": exp.isoformat() if exp else None,
            "days_to_expiry": (exp - today).days if exp else None,
            "iqfeed_retention_ok": bool(exp is not None and exp >= today),
            "provider": provider,
            "provider_reason": provider_reason,
            "era": era_label,
            "ledger_path": src.get("_path"),
            "ledger_src": src.get("_src"),
            "_era_rank": rank,
            "_ross_usd": ross_usd,
            "_ledger_index": idx,
        }
        row.update(hyd)
        row.update(extra)
        if not _symbol_is_ticker(symbol):
            # Narrative in the ledger's symbol field ("NUWE (09:30 pivot 5.34)",
            # "EDBL/LGHL/BIYA"). Reported, never handed to the hydrator: on 2026-09-04 one of
            # these aborted a 60-row IQFeed pass at row 32 (varchar(32) on hydration_jobs).
            row["hydration_status"] = "symbol_malformed"
            row["provider"] = "unhydratable"
            row["provider_reason"] = "symbol_malformed"
        scoreable, reason = _scoreable(row)
        row["scoreable"] = scoreable
        row["scoreable_reason"] = reason
        rows.append(row)

    for idx, t in enumerate(ledger["trades"]):
        path = str(t.get("_path"))
        kind = RECORD_KIND_TRADE if path in TRADE_PATHS else RECORD_KIND_NO_TRADE
        clock, clock_basis = parse_entry_clock(t.get("entry_time_et"))
        px = {f: sentinel_to_none(t.get(f)) for f in NULL_SENTINEL_FIELDS}
        pnl = px["pnl_usd"]
        _emit(idx, kind, str(t.get("symbol")), str(t.get("date")), t,
              pnl if kind == RECORD_KIND_TRADE else None,
              {
                  "side": t.get("side"),
                  "account": normalize_account(t.get("account")),
                  "entry_clock_et": clock,
                  "entry_clock_basis": clock_basis,
                  "entry_time_utc": t.get("entry_time_utc"),
                  "confidence": t.get("confidence"),
                  "outcome": outcome_of(pnl) if kind == RECORD_KIND_TRADE else "unknown",
                  "setup": t.get("setup"),
                  "notes": t.get("notes"),
                  **px,
              })

    # xref-only symbol-days. FOUR of the eight declared lane-alive cases
    # (UPC 06-29, WETO 08-17, PFSA 08-18, SLE 08-18) have NO trade row at all —
    # they exist only as cross-reference rows — so a corpus built from
    # ledger['trades'] alone would silently omit half the bench's head.
    for idx, x in enumerate(ledger.get("xref") or []):
        pair = (str(x.get("symbol")), str(x.get("date")))
        if pair in seen_pairs:
            continue
        pnl = sentinel_to_none(x.get("ross_pnl_usd"))
        _emit(10_000 + idx, RECORD_KIND_XREF, pair[0], pair[1], x, pnl, {
            "side": None,
            "account": normalize_account(x.get("account")),
            "entry_clock_et": parse_entry_clock(x.get("ross_entry_time_et"))[0],
            "entry_clock_basis": parse_entry_clock(x.get("ross_entry_time_et"))[1],
            "entry_time_utc": x.get("ross_entry_time_utc"),
            "confidence": None,
            "outcome": outcome_of(pnl),
            "setup": None,
            "notes": x.get("notes"),
            "entry_px": None, "exit_px": None, "stop_px": None,
            "shares": None, "pnl_usd": pnl,
            "chili_verdict": x.get("chili_verdict") or x.get("verdict"),
        })
    return rows


def build_corpus(
    ledger: dict[str, Any],
    *,
    today: date,
    jobs_by_pair: dict[tuple[str, str], list[dict[str, Any]]] | None,
    hydration_error: str | None = None,
    lane_alive: Iterable[tuple[str, str]] = LANE_ALIVE_SYMBOL_DAYS,
    polygon_floor: date = POLYGON_HISTORY_FLOOR,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """The full ``chili.rossbench_corpus.v1`` document, rows already ordered."""
    rows = build_rows(ledger, today=today, jobs_by_pair=jobs_by_pair,
                      lane_alive=lane_alive, polygon_floor=polygon_floor)
    rows.sort(key=order_key)
    for n, row in enumerate(rows, start=1):
        row["rank"] = n

    unrecoverable = [
        {"rank": r["rank"], "symbol": r["symbol"], "date": r["date"],
         "record_kind": r["record_kind"], "reason": r["provider_reason"]}
        for r in rows if r["provider"] == PROVIDER_UNRECOVERABLE
    ]
    scoreable = [r for r in rows if r["scoreable"]]
    counts = {
        "rows": len(rows),
        "by_record_kind": _counter(r["record_kind"] for r in rows),
        "by_provider": _counter(r["provider"] for r in rows),
        "by_hydration_status": _counter(str(r["hydration_status"]) for r in rows),
        "by_era": _counter(r["era"] for r in rows),
        "lane_alive": sum(1 for r in rows if r["lane_alive"]),
        "tape_confirmed": sum(1 for r in rows if r["tape_confirmed"]),
        "scoreable": len(scoreable),
        "scoreable_wins": sum(1 for r in scoreable if r["outcome"] == "win"),
        "scoreable_losses": sum(1 for r in scoreable if r["outcome"] == "loss"),
        "scoreable_pnl_unknown": sum(1 for r in scoreable if r["outcome"] == "unknown"),
        "unrecoverable": len(unrecoverable),
    }
    return {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_from": {"schema": LEDGER_SCHEMA,
                       "counts": ledger.get("counts"),
                       "source_files": len(ledger.get("source_files") or [])},
        "as_of_date": today.isoformat(),
        "retention": {
            "iqfeed_retention_days": IQFEED_RETENTION_DAYS,
            "iqfeed_floor_today": iqfeed_retention_floor(today).isoformat(),
            "iqfeed_floor_measured": IQFEED_RETENTION_FLOOR_MEASURED.isoformat(),
            "iqfeed_floor_measured_on": IQFEED_RETENTION_MEASURED_ON.isoformat(),
            "polygon_floor": polygon_floor.isoformat(),
            "polygon_floor_is_approximate": True,
        },
        "hydration_lookup": {
            "read": jobs_by_pair is not None,
            "error": hydration_error,
            "note": ("hydration_jobs was NOT read; every row's hydration_status is "
                     f"{HYDRATION_UNKNOWN!r} and tape_confirmed falls back to the "
                     "ledger's own tape_coverage/tape_pin evidence"
                     if jobs_by_pair is None else None),
        },
        "lane_alive_symbol_days": [list(p) for p in lane_alive],
        "order_key_spec": list(ORDER_KEY_SPEC),
        "counts": counts,
        "unrecoverable": unrecoverable,
        "rows": rows,
    }


def _counter(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")


def _symbol_is_ticker(symbol: Any) -> bool:
    sym = str(symbol or "").strip().upper()
    return bool(sym) and sym != "UNKNOWN" and bool(_TICKER_RE.match(sym))


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_csv(doc: dict[str, Any], kinds: Sequence[str] | None = None) -> str:
    """CSV text. ``newline=''`` + an explicit ``\\n`` terminator everywhere:
    Windows text mode and csv's default ``\\r\\n`` would otherwise both rewrite
    the bytes of an otherwise identical corpus
    (reference_python_write_text_crlf_windows)."""
    import io
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for row in doc["rows"]:
        if kinds and row["record_kind"] not in kinds:
            continue
        if not _symbol_is_ticker(row.get("symbol")):
            continue  # unhydratable; reported in corpus.json, never in the hydrate CSV
        w.writerow([_csv_cell(row.get(c)) for c in CSV_COLUMNS])
    return buf.getvalue()


def render_json(doc: dict[str, Any]) -> str:
    public = dict(doc)
    public["rows"] = [{k: v for k, v in r.items() if not k.startswith("_")}
                      for r in doc["rows"]]
    return json.dumps(public, indent=2, ensure_ascii=False, default=str) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(text)


def _comparable(text: str) -> str:
    """Drift comparison that ignores only ``generated_at`` — a timestamp is not
    content, and re-running the builder must not look like a change."""
    return re.sub(r'"generated_at":\s*"[^"]*"', '"generated_at": null', text)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                    help="ross_master_ledger.json (chili.ross_master_ledger.v1)")
    ap.add_argument("--out-dir", required=True,
                    help="directory to write corpus.csv + corpus.json into")
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB,
                    help="hydrated database holding hydration_jobs (never chili)")
    ap.add_argument("--env-file", default=os.environ.get("CHILI_ENV_FILE"))
    ap.add_argument("--no-db", action="store_true",
                    help="skip the hydration_jobs lookup entirely; every row reports "
                         f"hydration_status={HYDRATION_UNKNOWN!r}")
    ap.add_argument("--today", default=None,
                    help="ISO date to compute the IQFeed retention cliff against "
                         "(default: today). Explicit so a build is reproducible.")
    ap.add_argument("--polygon-floor", default=POLYGON_HISTORY_FLOOR.isoformat(),
                    help="oldest date Polygon v3 is assumed to serve; the doc says "
                         "'~2003' and nobody has probed the real cliff")
    ap.add_argument("--kinds", default=None,
                    help="comma-separated record_kind allow-list for corpus.csv ONLY "
                         "(corpus.json always carries every row). Unset = all kinds.")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if either output would change; writes nothing")
    ap.add_argument("--fail-on-unrecoverable", action="store_true",
                    help="exit 3 when any row has no provider that can serve its tape")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    polygon_floor = date.fromisoformat(args.polygon_floor)
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip()) if args.kinds else None

    with open(args.ledger, encoding="utf-8") as fh:
        ledger = json.load(fh)

    # Pair list for the jobs lookup comes from the ledger, so the query is bounded
    # by the corpus rather than scanning the whole ledger table.
    pairs: list[tuple[str, date]] = []
    seen: set[tuple[str, date]] = set()
    for src in list(ledger.get("trades") or []) + list(ledger.get("xref") or []):
        day = parse_day(src.get("date"))
        sym = str(src.get("symbol") or "").strip().upper()
        if not sym or day is None:
            continue
        if (sym, day) not in seen:
            seen.add((sym, day))
            pairs.append((sym, day))

    if args.no_db:
        jobs, err = None, "skipped (--no-db)"
    else:
        jobs, err = fetch_hydration_jobs(pairs, dbname=args.db_name, env_path=args.env_file)

    doc = build_corpus(ledger, today=today, jobs_by_pair=jobs, hydration_error=err,
                       polygon_floor=polygon_floor)

    out_dir = Path(args.out_dir)
    csv_path, json_path = out_dir / "corpus.csv", out_dir / "corpus.json"
    csv_text, json_text = render_csv(doc, kinds), render_json(doc)

    if args.check:
        drift = []
        for path, text in ((csv_path, csv_text), (json_path, json_text)):
            old = path.read_text(encoding="utf-8") if path.exists() else None
            if old is None or _comparable(old) != _comparable(text):
                drift.append(str(path))
        if drift:
            print("DRIFT: would change " + ", ".join(drift))
            return 1
        print(f"OK: corpus is up to date ({doc['counts']['rows']} rows)")
        return 0

    _write(csv_path, csv_text)
    _write(json_path, json_text)

    c = doc["counts"]
    print(f"[rossbench_corpus] wrote {csv_path} + {json_path}")
    print(f"[rossbench_corpus]   rows={c['rows']} scoreable={c['scoreable']} "
          f"(win {c['scoreable_wins']} / loss {c['scoreable_losses']} / "
          f"pnl-unknown {c['scoreable_pnl_unknown']})")
    print(f"[rossbench_corpus]   lane_alive={c['lane_alive']} "
          f"tape_confirmed={c['tape_confirmed']}")
    print(f"[rossbench_corpus]   by_provider={c['by_provider']}")
    print(f"[rossbench_corpus]   by_hydration_status={c['by_hydration_status']}")
    if err:
        print(f"[rossbench_corpus]   hydration_jobs NOT read: {err}")
    # REPORTED, never silently dropped.
    for u in doc["unrecoverable"]:
        print(f"[rossbench_corpus]   UNRECOVERABLE #{u['rank']} {u['symbol']} "
              f"{u['date']} ({u['record_kind']}): {u['reason']}")
    if doc["unrecoverable"] and args.fail_on_unrecoverable:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
