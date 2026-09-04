#!/usr/bin/env python
"""Ross Parity Bench — tape density verification (``chili.rossbench_density.v1``).

Answers ONE question per symbol-day: **does the hydrated tape actually carry
enough rows, over the window we are about to replay, to be the tape we think it
is?** A replay over a sparse tape does not fail — it prints a clean number with a
plus sign in front of it, which is the most expensive failure mode this bench
has (replay_harness_invariants.py:1, "measuring silence").

THE PREDICATE IS READ OUT OF THE DRIVER, NOT RE-TYPED
-----------------------------------------------------
``scripts/replay_v3_fsm_window.py`` mirrors the tape into the sim sink with two
SQL constants, and its own receipt's ``density`` block is computed from the row
counts those two mirrors returned (replay_v3_fsm_window.py:1150-1162). A density
check that used ANY OTHER predicate would measure a different population and
report a number the driver will never see — so this module PARSES those two
constants out of the driver's source at run time (``read_driver_sql``) and
rewrites them into ``SELECT count(*)`` (``derive_count_sql``).

Consequences, all deliberate:

  * If the driver's predicate changes, this check changes with it. There is no
    copy to drift.
  * If the driver's SQL stops matching the flat single-table shape this rewrite
    assumes, ``derive_count_sql`` raises and NAMES the drift rather than
    silently counting the wrong thing.
  * The driver cannot be IMPORTED: ``replay_v3_fsm_window`` raises SystemExit at
    module scope when ``TEST_DATABASE_URL``'s database name does not end in
    ``_test`` (replay_v3_fsm_window.py:147), and it pulls in ``app.config`` and
    pandas. Parsing the source keeps this module DB-free and importable from a
    test, the same way ``replay_harness_invariants._sql_constants`` does.

⚠️ TWO WINDOWS, AND THEY ARE NOT THE SAME WINDOW. The driver's mirrors run over
``[OHLCV_START, WIN_END)`` (replay_v3_fsm_window.py:490 and :562) and its receipt
divides by ``WIN_END - OHLCV_START``. This check measures ``[WIN_START, WIN_END)``
— the window under test. When ``OHLCV_START == WIN_START`` (what every caller in
the tree passes) the two agree; when they differ, the driver's denominator is
LARGER and its rate is correspondingly lower. Both windows are printed in the
receipt so the two numbers can never be compared by accident.

⚠️ CLOCK ASYMMETRY. ``iqfeed_trade_ticks.observed_at`` is TIMESTAMP (naive UTC);
``momentum_nbbo_spread_tape.observed_at`` is TIMESTAMPTZ (confirmed against
information_schema — replay_v3_fsm_window.py:211-217). Binding a naive bound to
the timestamptz column makes PostgreSQL coerce it through the SESSION TimeZone,
which is harmless only while that is UTC. This module binds NAIVE bounds to the
trade table and AWARE UTC bounds to the NBBO table, exactly as the driver does.

THE FLOOR
---------
``ROSSBENCH_DENSITY_MIN_RATIO`` (default 1.0) is hydrated_rows / live_rows. The
1.0 is not a taste: Phase-3 fidelity measured that the hydrated tape does not
contradict our own recording, it CONTAINS it — our recording is 66-100 % complete
depending on the day and duplicates up to 46 % of its rows when two bridge
processes overlap (docs/HISTORICAL_TICK_HYDRATOR.md, "Validating a hydrated day
against our own recording"). So hydrated ⊇ recorded, and anything below 1.0 means
the hydration is short of a tape we already hold — ``density_regression``.

Usage:
  # one symbol-day, window given the way the driver takes it
  python scripts/rossbench_density_check.py --symbol SDOT \\
      --win-start 2026-06-26T13:00:00 --win-end 2026-06-26T15:00:00 --json out.json

  # a whole corpus, each row over its own 04:00-20:00 ET session
  python scripts/rossbench_density_check.py --csv corpus.csv --json out.json

Exit codes: 0 ok · 1 a density verdict failed · 2 usage/connection error.
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from historical_tick_hydrator import (  # noqa: E402
    DEFAULT_HYDRATED_DB,
    NBBO_TABLE,
    TRADES_TABLE,
    resolve_dsn,
)
from hydration_coverage_report import et_session_bounds_utc  # noqa: E402

SCHEMA = "chili.rossbench_density.v1"
DRIVER_PATH = _SCRIPTS / "replay_v3_fsm_window.py"

# The two mirror constants. NOT the ``_TRADE_TAPE_SQL`` / ``_NBBO_TAPE_SQL``
# reads: those are the in-memory grid loads, and the trade one is wrapped in a
# ``row_number()`` subquery that applies TICK_STRIDE. The MIRRORS are what
# actually land rows in the sink the FSM reads, and their counts are what the
# driver's own receipt reports as density.
MIRROR_CONSTANTS: dict[str, str] = {
    "trades": "_TRADE_MIRROR_SQL",
    "nbbo": "_NBBO_MIRROR_SQL",
}

# Which table each dataset lives in, and whether its ``observed_at`` is aware.
# Imported table names so a rename in the hydrator cannot leave a stale literal
# here querying a relation nothing writes to.
#
# ⚠️ The predicates this module extracts from the driver at run time are
# ``price>0`` (trades) and ``bid>0 AND ask>=bid`` (NBBO). The hydrator doc and
# hydration_coverage_report.py:112 quote the NBBO one as
# ``bid > 0 AND ask > 0 AND ask >= bid``. Those are the SAME population —
# ``bid>0 AND ask>=bid`` implies ``ask>0`` — but the driver's spelling is what is
# used here, because the driver's spelling is what runs.
DATASET_TABLE: dict[str, tuple[str, bool]] = {
    "trades": (TRADES_TABLE, False),   # TIMESTAMP, naive UTC
    "nbbo": (NBBO_TABLE, True),        # TIMESTAMPTZ, aware
}

# The driver injects its provenance predicate at this slot
# (replay_v3_fsm_window.py:226 ``source_predicate``). Filled here with the
# psycopg2 paramstyle the mirrors use; ``assert_source_predicate_shape`` proves
# the driver still builds the same clause before this is trusted.
SOURCE_SLOT = "{source}"
SOURCE_PREDICATE_PSYCOPG2 = " AND source = ANY(%s)"
SOURCE_PREDICATE_MARKER = "AND source = ANY("

# hydrated / live. 1.0 = "the hydrated tape must be a superset of our own
# recording", which is the Phase-3 measured relationship (see module docstring).
# No silent default: the env var is read, the CLI overrides it, and the value
# actually applied is echoed in every receipt.
DENSITY_MIN_RATIO_ENV = "ROSSBENCH_DENSITY_MIN_RATIO"
DENSITY_MIN_RATIO_DEFAULT = 1.0

# ⚠️ A VACUUM FULL just ran on ``chili`` and left the 89.6M-row tape table with
# zero stats and ``relallvisible=0`` on 4.07M pages, so a bulk count there can be
# minutes rather than milliseconds. 20 s is the operator's declared budget for
# the live cross-check: past it the answer is not worth the I/O the live lane
# would give up for it, and the check degrades to ``live_density_unavailable``.
LIVE_STATEMENT_TIMEOUT_S = 20

VERDICT_OK = "ok"
VERDICT_NO_TAPE = "no_hydrated_tape"
VERDICT_NBBO_MISSING = "nbbo_missing_not_replayable"
VERDICT_TRADES_MISSING = "trades_missing_not_replayable"
VERDICT_REGRESSION = "density_regression"
LIVE_UNAVAILABLE = "live_density_unavailable"

FAILING_VERDICTS = (VERDICT_NO_TAPE, VERDICT_NBBO_MISSING,
                    VERDICT_TRADES_MISSING, VERDICT_REGRESSION)


# ─────────────────────────────────────────────────────────────────────────────
# READ THE DRIVER'S OWN SQL
# ─────────────────────────────────────────────────────────────────────────────

def read_driver_sql(name: str, driver_src: str) -> str:
    """The module-level string constant ``name`` from the driver's source.

    Implicitly-concatenated adjacent literals are folded by the parser into ONE
    ``ast.Constant``, so the driver's multi-line SQL comes back as a single
    string. Raises rather than returning a default: a density check that silently
    fell back to a hardcoded predicate would be the exact drift this exists to
    prevent.
    """
    tree = ast.parse(driver_src)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return node.value.value
                raise AssertionError(
                    f"read_driver_sql: {name} in {DRIVER_PATH.name} is no longer a plain "
                    f"string constant (got {type(node.value).__name__}); this module can no "
                    "longer read the driver's predicate and must not guess it."
                )
    raise AssertionError(
        f"read_driver_sql: {name} is ABSENT from {DRIVER_PATH.name} — the mirror SQL was "
        "renamed or removed, so the density check no longer measures what the driver reads."
    )


def assert_source_predicate_shape(driver_src: str) -> None:
    """The driver must still build its provenance clause the way we fill the slot.

    We inject ``SOURCE_PREDICATE_PSYCOPG2`` into ``{source}`` ourselves rather
    than importing ``source_predicate`` (importing the driver is impossible — see
    the module docstring). That is an assumption, so it is CHECKED against the
    driver's text: if the clause shape changes, a source-filtered count here would
    quietly stop matching the driver's population.
    """
    if SOURCE_PREDICATE_MARKER not in driver_src:
        raise AssertionError(
            f"assert_source_predicate_shape: {DRIVER_PATH.name} no longer contains "
            f"{SOURCE_PREDICATE_MARKER!r} — its provenance predicate changed shape and "
            f"{SOURCE_PREDICATE_PSYCOPG2!r} is no longer the same filter."
        )


_SELECT_HEAD = re.compile(r"^\s*SELECT\s+.+?\s+FROM\s+", re.IGNORECASE | re.DOTALL)
_ORDER_TAIL = re.compile(r"\s+ORDER\s+BY\s+.*$", re.IGNORECASE | re.DOTALL)
_VALIDITY = re.compile(
    r"observed_at\s*<\s*%s\s+AND\s+(?P<pred>.+?)(?:\{source\}|\s+ORDER\s+BY\b|$)",
    re.IGNORECASE | re.DOTALL,
)


def derive_count_sql(raw_sql: str, *, with_source_filter: bool) -> tuple[str, str, str]:
    """(count_sql, table, validity_predicate) from one of the driver's mirror SQLs.

    The rewrite is deliberately narrow — ``SELECT <cols> FROM <one table> WHERE
    ...`` becomes ``SELECT count(*) FROM <one table> WHERE ...`` with the ORDER BY
    dropped — and every assumption it makes is asserted first. A subquery, a JOIN
    or a second FROM would make the non-greedy head match the wrong span, so those
    are rejected loudly instead of counted wrongly.
    """
    flat = " ".join(str(raw_sql).split())
    if "FROM (" in flat.upper():
        raise AssertionError(
            "derive_count_sql: the mirror SQL now contains a subquery; the flat "
            "SELECT->count(*) rewrite would count the wrong relation. Rewrite this "
            f"function against the new shape:\n  {flat[:200]}"
        )
    if flat.upper().count(" FROM ") != 1:
        raise AssertionError(
            "derive_count_sql: expected exactly one FROM in the mirror SQL, got "
            f"{flat.upper().count(' FROM ')}:\n  {flat[:200]}"
        )
    m = _VALIDITY.search(flat)
    if not m or not m.group("pred").strip():
        raise AssertionError(
            "derive_count_sql: cannot locate the validity predicate after the window "
            "bound in the mirror SQL — the loader's own predicate is what makes this a "
            f"density check rather than a row count:\n  {flat[:200]}"
        )
    predicate = m.group("pred").strip()

    table_m = re.search(r"\sFROM\s+([A-Za-z_][A-Za-z0-9_]*)", flat, re.IGNORECASE)
    if not table_m:
        raise AssertionError(f"derive_count_sql: no table name after FROM:\n  {flat[:200]}")
    table = table_m.group(1)

    body = _SELECT_HEAD.sub("SELECT count(*) FROM ", flat, count=1)
    body = _ORDER_TAIL.sub("", body)
    body = body.replace(SOURCE_SLOT, SOURCE_PREDICATE_PSYCOPG2 if with_source_filter else "")
    return " ".join(body.split()), table, predicate


# ─────────────────────────────────────────────────────────────────────────────
# COUNTING
# ─────────────────────────────────────────────────────────────────────────────

def _bound(ts: datetime, aware: bool) -> datetime:
    """Bind naive-UTC to the trade table and aware-UTC to the NBBO table."""
    if aware:
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def count_dataset(
    conn,
    dataset: str,
    symbol: str,
    win_start: datetime,
    win_end: datetime,
    *,
    driver_src: str,
    sources: Sequence[str] = (),
    statement_timeout_s: int | None = None,
) -> int:
    """Rows the driver's mirror WOULD return for one dataset over one window."""
    _table, aware = DATASET_TABLE[dataset]
    sql, _t, _p = derive_count_sql(
        read_driver_sql(MIRROR_CONSTANTS[dataset], driver_src),
        with_source_filter=bool(sources),
    )
    params: list[Any] = [symbol, _bound(win_start, aware), _bound(win_end, aware)]
    if sources:
        params.append(list(sources))
    with conn.cursor() as cur:
        if statement_timeout_s is not None:
            # SET LOCAL, so the budget dies with this transaction and cannot leak
            # onto a pooled connection someone else is holding.
            cur.execute("SET LOCAL statement_timeout = %s", (f"{int(statement_timeout_s)}s",))
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    return int(row[0] if row else 0)


def measure_side(
    conn,
    symbol: str,
    win_start: datetime,
    win_end: datetime,
    *,
    driver_src: str,
    sources: Sequence[str] = (),
    statement_timeout_s: int | None = None,
) -> dict[str, Any]:
    """{trade_rows, nbbo_rows, ticks_per_second, nbbo_rows_per_second} for one DB."""
    span = max((win_end - win_start).total_seconds(), 1e-9)
    trades = count_dataset(conn, "trades", symbol, win_start, win_end,
                           driver_src=driver_src, sources=sources,
                           statement_timeout_s=statement_timeout_s)
    nbbo = count_dataset(conn, "nbbo", symbol, win_start, win_end,
                         driver_src=driver_src, sources=sources,
                         statement_timeout_s=statement_timeout_s)
    return {
        "trade_rows": trades,
        "nbbo_rows": nbbo,
        "window_seconds": round(span, 3),
        "ticks_per_second": round(trades / span, 6),
        "nbbo_rows_per_second": round(nbbo / span, 6),
    }


def measure_live(
    conn_factory: Callable[[], Any],
    symbol: str,
    win_start: datetime,
    win_end: datetime,
    *,
    driver_src: str,
    sources: Sequence[str] = (),
    statement_timeout_s: int = LIVE_STATEMENT_TIMEOUT_S,
) -> tuple[dict[str, Any] | None, str | None]:
    """(live measurement, unavailable_reason). NEVER raises.

    ⚠️ This is the ONE read against the live ``chili`` database, and it is a
    courtesy cross-check, not a gate on the corpus. It runs READ ONLY under a
    hard statement timeout, and EVERY failure — connection refused, missing
    table, and above all the timeout a post-VACUUM-FULL table with zero stats
    will produce — degrades to ``live_density_unavailable`` with the reason
    recorded. A bench that dies because the live lane is busy is a bench nobody
    runs.
    """
    conn = None
    try:
        conn = conn_factory()
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        out = measure_side(conn, symbol, win_start, win_end, driver_src=driver_src,
                           sources=sources, statement_timeout_s=statement_timeout_s)
        return out, None
    except Exception as exc:  # noqa: BLE001 — see the docstring
        return None, f"{type(exc).__name__}: {str(exc).strip()[:300]}"
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ─────────────────────────────────────────────────────────────────────────────
# VERDICT
# ─────────────────────────────────────────────────────────────────────────────

def _ratio(hydrated: int, live: int) -> float | None:
    """None when there is nothing to divide by. live == 0 is NOT a regression:
    hydrated ⊇ ∅ holds trivially, and reporting infinity would be a lie about a
    measurement nobody made."""
    if live <= 0:
        return None
    return round(float(hydrated) / float(live), 6)


def verdict_for(
    hydrated: dict[str, Any],
    live: dict[str, Any] | None,
    *,
    min_ratio: float,
) -> dict[str, Any]:
    """The density verdict for one symbol-day, with the reason spelled out.

    Replayability comes FIRST and does not depend on the live side: per
    docs/HISTORICAL_TICK_HYDRATOR.md, ``counterfactual_replay._confidence``
    returns ``no_tape`` the moment the NBBO tape is empty and bar-candidate
    generation is guarded on NBBO ticks — so a trades-only symbol-day yields ZERO
    candidates, not few. A run over one is not a thin replay, it is no replay.
    """
    reasons: list[str] = []
    ratios = {
        "trades": _ratio(hydrated["trade_rows"], live["trade_rows"]) if live else None,
        "nbbo": _ratio(hydrated["nbbo_rows"], live["nbbo_rows"]) if live else None,
    }
    if hydrated["trade_rows"] == 0 and hydrated["nbbo_rows"] == 0:
        verdict = VERDICT_NO_TAPE
        reasons.append("the hydrated tape holds zero rows over this window under the "
                       "driver's own mirror predicate")
    elif hydrated["nbbo_rows"] == 0:
        verdict = VERDICT_NBBO_MISSING
        reasons.append("trades but no usable NBBO: _confidence returns no_tape and "
                       "bar-candidate generation is NBBO-guarded, so this yields zero "
                       "candidates (the hydrator doc's tape_only_not_replayable tier)")
    elif hydrated["trade_rows"] == 0:
        verdict = VERDICT_TRADES_MISSING
        reasons.append("NBBO but no trade prints: the FSM has a book and no tape to "
                       "drive it")
    else:
        verdict = VERDICT_OK
    if verdict == VERDICT_OK and live is not None:
        short = [(k, r) for k, r in ratios.items() if r is not None and r < float(min_ratio)]
        if short:
            verdict = VERDICT_REGRESSION
            for k, r in short:
                reasons.append(
                    f"{k}: hydrated/live = {r} < {min_ratio} — Phase-3 measured the "
                    "hydrated tape as a SUPERSET of our own recording, so short of it "
                    "means the hydration is incomplete, not that live over-recorded")
    return {
        "verdict": verdict,
        "reasons": reasons,
        "ratios": ratios,
        "min_ratio": float(min_ratio),
    }


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

def check_symbol_day(
    symbol: str,
    win_start: datetime,
    win_end: datetime,
    *,
    driver_src: str,
    hydrated_conn_factory: Callable[[], Any],
    live_conn_factory: Callable[[], Any] | None,
    min_ratio: float,
    sources: Sequence[str] = (),
    live_sources: Sequence[str] = (),
    live_statement_timeout_s: int = LIVE_STATEMENT_TIMEOUT_S,
) -> dict[str, Any]:
    """One symbol-day receipt. Injectable connection factories so a test can run
    this with fakes and never touch a database."""
    conn = hydrated_conn_factory()
    try:
        hydrated = measure_side(conn, symbol, win_start, win_end,
                                driver_src=driver_src, sources=sources)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    live, live_error = (None, "skipped (no live DSN)")
    if live_conn_factory is not None:
        live, live_error = measure_live(
            live_conn_factory, symbol, win_start, win_end, driver_src=driver_src,
            sources=live_sources, statement_timeout_s=live_statement_timeout_s)

    out = {
        "symbol": symbol,
        "win_start": win_start.isoformat(),
        "win_end": win_end.isoformat(),
        "hydrated": hydrated,
        "live": live,
        "live_status": LIVE_UNAVAILABLE if live is None else "measured",
        "live_error": live_error,
        # The driver mirrors from OHLCV_START, not WIN_START. Say so on every row
        # so nobody compares this rate against the driver receipt's by accident.
        "window_note": ("measured over [WIN_START, WIN_END); the driver's own mirrors run "
                        "over [OHLCV_START, WIN_END) and its receipt divides by that wider "
                        "span (replay_v3_fsm_window.py:490, :562, :1105)"),
        "sources": list(sources),
        "live_sources": list(live_sources),
    }
    out.update(verdict_for(hydrated, live, min_ratio=min_ratio))
    return out


def resolve_min_ratio(cli_value: str | None,
                      env: dict[str, str] | None = None) -> tuple[float, str]:
    """(value, where it came from). CLI beats env beats the documented default."""
    env = os.environ if env is None else env
    if cli_value is not None:
        return float(cli_value), "--min-ratio"
    raw = (env.get(DENSITY_MIN_RATIO_ENV) or "").strip()
    if raw:
        return float(raw), DENSITY_MIN_RATIO_ENV
    return DENSITY_MIN_RATIO_DEFAULT, "default (Phase-3: hydrated is a superset of recorded)"


def read_corpus_pairs(path: str) -> list[tuple[str, date]]:
    """(symbol, date) from corpus.csv, order preserved, duplicates collapsed.

    Same column contract as ``historical_tick_hydrator.read_pairs_csv``
    (:1472) so one corpus.csv drives the hydrator, the coverage report and this.
    """
    out: list[tuple[str, date]] = []
    seen: set[tuple[str, date]] = set()
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
        sym_key = "symbol" if "symbol" in fields else "ticker"
        day_key = next((k for k in ("date", "trading_day", "session_date", "day")
                        if k in fields), None)
        if day_key is None:
            raise SystemExit(f"{path}: no date column (looked for date/trading_day/session_date/day)")
        for row in reader:
            sym = (row.get(sym_key) or "").strip().upper()
            raw = (row.get(day_key) or "").strip()
            if not sym or not raw:
                continue
            pair = (sym, date.fromisoformat(raw[:10]))
            if pair not in seen:
                seen.add(pair)
                out.append(pair)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default=os.environ.get("SYMBOL"))
    ap.add_argument("--win-start", default=os.environ.get("WIN_START"),
                    help="UTC-naive ISO, same contract as the driver's WIN_START")
    ap.add_argument("--win-end", default=os.environ.get("WIN_END"),
                    help="UTC-naive ISO, same contract as the driver's WIN_END")
    ap.add_argument("--csv", default=None,
                    help="corpus.csv; each (symbol, date) measured over its own "
                         "04:00-20:00 ET session (hydration_coverage_report."
                         "et_session_bounds_utc) unless --win-start/--win-end are given")
    ap.add_argument("--db-name", default=DEFAULT_HYDRATED_DB,
                    help="hydrated database to measure (never chili)")
    ap.add_argument("--env-file", default=os.environ.get("CHILI_ENV_FILE"))
    ap.add_argument("--live-dsn", default=None,
                    help="live chili DSN for the cross-check; default DATABASE_URL")
    ap.add_argument("--no-live", action="store_true",
                    help="skip the live cross-check entirely")
    ap.add_argument("--sources", default=os.environ.get("SOURCE_FILTER") or "",
                    help="comma-separated tape provenance allow-list for the HYDRATED "
                         "side; unset = the driver's default (no source predicate)")
    ap.add_argument("--live-sources", default="",
                    help="source allow-list for the LIVE side. Unset by design: the "
                         "hydrated source tags (iqfeed_lookup_hist, polygon_v3_*) do "
                         "not exist in chili, so reusing --sources there would count "
                         "zero and report a false regression.")
    ap.add_argument("--min-ratio", default=None,
                    help=f"hydrated/live floor; env {DENSITY_MIN_RATIO_ENV}, "
                         f"default {DENSITY_MIN_RATIO_DEFAULT}")
    ap.add_argument("--live-timeout-s", type=int, default=LIVE_STATEMENT_TIMEOUT_S)
    ap.add_argument("--json", default=None, help="write the receipt here")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    import psycopg2  # local: keeps this module importable by a DB-free test

    driver_src = DRIVER_PATH.read_text(encoding="utf-8")
    assert_source_predicate_shape(driver_src)
    min_ratio, min_ratio_origin = resolve_min_ratio(args.min_ratio)
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    live_sources = tuple(s.strip() for s in args.live_sources.split(",") if s.strip())

    if args.csv:
        pairs = read_corpus_pairs(args.csv)
        if not pairs:
            print("[rossbench_density_check] corpus is empty", file=sys.stderr)
            return 2
    elif args.symbol and args.win_start and args.win_end:
        pairs = [(args.symbol.strip().upper(), None)]
    else:
        print("[rossbench_density_check] need --csv, or --symbol with "
              "--win-start/--win-end (env SYMBOL/WIN_START/WIN_END also work)",
              file=sys.stderr)
        return 2

    hydrated_dsn = resolve_dsn(args.db_name, args.env_file)
    live_dsn = None if args.no_live else (args.live_dsn or os.environ.get("DATABASE_URL"))

    def _hydrated():
        return psycopg2.connect(hydrated_dsn)

    live_factory = None
    if live_dsn:
        def _live():
            return psycopg2.connect(live_dsn)
        live_factory = _live

    results: list[dict[str, Any]] = []
    for symbol, day in pairs:
        if args.win_start and args.win_end:
            lo = datetime.fromisoformat(args.win_start)
            hi = datetime.fromisoformat(args.win_end)
        else:
            lo, hi = et_session_bounds_utc(day)
        try:
            results.append(check_symbol_day(
                symbol, lo, hi, driver_src=driver_src,
                hydrated_conn_factory=_hydrated, live_conn_factory=live_factory,
                min_ratio=min_ratio, sources=sources, live_sources=live_sources,
                live_statement_timeout_s=args.live_timeout_s))
        except Exception as exc:  # noqa: BLE001
            # The HYDRATED side failing is not a soft condition — that is the tape
            # under test — but one bad symbol-day must not hide the other 203.
            logger.error("[rossbench_density_check] %s %s: %s: %s",
                         symbol, lo, type(exc).__name__, exc)
            results.append({"symbol": symbol, "win_start": lo.isoformat(),
                            "win_end": hi.isoformat(), "verdict": "hydrated_read_failed",
                            "reasons": [f"{type(exc).__name__}: {exc}"]})

    failed = [r for r in results if r.get("verdict") in FAILING_VERDICTS
              or r.get("verdict") == "hydrated_read_failed"]
    doc = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "driver": {"path": str(DRIVER_PATH),
                   "constants": dict(MIRROR_CONSTANTS),
                   "predicates": {
                       ds: derive_count_sql(read_driver_sql(const, driver_src),
                                            with_source_filter=False)[2]
                       for ds, const in MIRROR_CONSTANTS.items()}},
        "min_ratio": min_ratio,
        "min_ratio_origin": min_ratio_origin,
        "hydrated_db": args.db_name,
        "live_checked": bool(live_dsn),
        "live_statement_timeout_s": args.live_timeout_s,
        "counts": {"symbol_days": len(results), "failed": len(failed)},
        "results": results,
    }
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", newline="", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, default=str)
            fh.write("\n")

    for r in results:
        live = r.get("live")
        live_txt = (f"live t={live['trade_rows']} n={live['nbbo_rows']}"
                    if live else f"live={LIVE_UNAVAILABLE} ({r.get('live_error')})")
        hyd = r.get("hydrated") or {}
        print(f"[rossbench_density_check] {r['symbol']:<8} {r['win_start']} "
              f"hydrated t={hyd.get('trade_rows')} ({hyd.get('ticks_per_second')}/s) "
              f"n={hyd.get('nbbo_rows')} ({hyd.get('nbbo_rows_per_second')}/s)  "
              f"{live_txt}  -> {r['verdict']}")
        for reason in r.get("reasons") or []:
            print(f"[rossbench_density_check]     {reason}")
    print(f"[rossbench_density_check] {len(results)} symbol-day(s), "
          f"{len(failed)} failing (min_ratio={min_ratio} from {min_ratio_origin})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
